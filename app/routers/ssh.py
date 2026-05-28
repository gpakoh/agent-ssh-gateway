"""SSH-related routes — connect, execute, disconnect, session management, and WebSocket PTY."""

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Request, UploadFile, File, Query


from app.config import settings
from app.state import _err
from app import state as _state
from app.auth_middleware import ws_auth_check, is_agent_token_valid
from app.security import sanitize_command, rate_limit_mutation
from app.ssh_manager import SSHManagerError, ConnectionError as SSHConnError, AuthenticationError, SessionNotFoundError, TimeoutError, ExecutionError
from app.models import (
    ConnectRequest,
    ConnectResponse,
    ExecuteRequest,
    ExecuteResponse,
    DisconnectRequest,
    DisconnectResponse,
    SessionsResponse,
    SessionInfo,
    AgentTokenResponse,
    AgentTokenRefreshResponse,
    SessionTimeoutRequest,
    SessionTimeoutResponse,
    SessionConfigResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def time_to_iso(timestamp: float) -> str:
    """Convert Unix timestamp to ISO format."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Agent Token
# ---------------------------------------------------------------------------

@router.post("/api/agent/token", response_model=AgentTokenResponse)
async def agent_token_generate():
    """Generate a short-lived agent token (separate from API_KEY).

    Requires API_KEY auth. The generated token can be rotated
    without affecting the main API_KEY.
    """
    import secrets as _secrets
    from datetime import timedelta

    token = _secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.agent_token_ttl)
    settings.agent_token = token
    settings.agent_token_expires_at = expires_at
    logger.info("Agent token generated (ttl=%ds)", settings.agent_token_ttl)
    return AgentTokenResponse(
        token=token,
        ttl=settings.agent_token_ttl,
        expires_at=expires_at.isoformat(),
    )


@router.post("/api/agent/token/refresh", response_model=AgentTokenRefreshResponse)
async def agent_token_refresh():
    """Refresh (rotate) the agent token.

    Invalidates the previous agent token and issues a new one.
    """
    import secrets as _secrets
    from datetime import timedelta

    token = _secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.agent_token_ttl)
    settings.agent_token = token
    settings.agent_token_expires_at = expires_at
    logger.info("Agent token refreshed (ttl=%ds)", settings.agent_token_ttl)
    return AgentTokenRefreshResponse(
        token=token,
        ttl=settings.agent_token_ttl,
        expires_at=expires_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Session Config
# ---------------------------------------------------------------------------

@router.get("/api/config/session", response_model=SessionConfigResponse)
async def get_session_config():
    """Get current session configuration."""
    active = await _state.manager.list_sessions()
    return SessionConfigResponse(
        session_timeout=_state.manager._session_timeout,
        cleanup_interval=_state.manager._cleanup_interval,
        max_sessions_per_ip=settings.max_sessions_per_ip,
        active_sessions=len(active),
    )


@router.patch("/api/config/session/timeout", response_model=SessionTimeoutResponse)
async def update_session_timeout(req: SessionTimeoutRequest):
    """Update session timeout dynamically."""
    previous = _state.manager._session_timeout
    _state.manager._session_timeout = req.timeout
    return SessionTimeoutResponse(
        timeout=req.timeout,
        previous_timeout=previous,
    )


# ---------------------------------------------------------------------------
# SSH Connection
# ---------------------------------------------------------------------------

@router.post("/api/ssh/connect", response_model=ConnectResponse)
@rate_limit_mutation(10, "minute")
async def ssh_connect(req: ConnectRequest, request: Request):
    """Create a new SSH session."""
    session_id = await _state.manager.create_session(
        host=req.host,
        port=req.port,
        username=req.username,
        password=req.password,
        private_key=req.private_key,
        key_passphrase=req.key_passphrase,
    )

    # Persist session if store is available
    if _state.session_store:
        try:
            await _state.session_store.save_session(
                session_id=session_id,
                host=req.host,
                port=req.port,
                username=req.username,
                password=req.password,
                private_key=req.private_key,
                key_passphrase=req.key_passphrase,
                ttl=settings.session_timeout,
            )
        except Exception as exc:
            logger.warning("Failed to persist session %s: %s", session_id, exc)

    return ConnectResponse(session_id=session_id)


@router.post("/api/ssh/execute", response_model=ExecuteResponse)
@rate_limit_mutation(60, "minute")
async def ssh_execute(req: ExecuteRequest, request: Request):
    """Execute a command on an existing SSH session."""
    # Sanitize Command
    try:
        sanitized = sanitize_command(req.command)
    except ValueError as exc:
        _state.audit_logger.log_security_event(
            "BLOCKED_COMMAND", str(exc), request.client.host
        )
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))

    # Audit Log
    _state.audit_logger.log_command(req.session_id, sanitized, request.client.host)

    result = await _state.manager.execute(
        session_id=req.session_id,
        command=sanitized,
        timeout=req.timeout,
    )
    return ExecuteResponse(**result)


@router.post("/api/ssh/disconnect", response_model=DisconnectResponse)
async def ssh_disconnect(req: DisconnectRequest):
    """Close an SSH session."""
    await _state.manager.disconnect(req.session_id)

    if _state.session_store:
        try:
            await _state.session_store.deactivate_session(req.session_id)
        except Exception as exc:
            logger.warning("Failed to deactivate session %s in store: %s", req.session_id, exc)

    return DisconnectResponse()


@router.get("/api/ssh/sessions", response_model=SessionsResponse)
async def ssh_sessions():
    """List all active SSH sessions with details."""
    records = await _state.manager.list_sessions()
    now = time.time()
    sessions = [
        SessionInfo(
            session_id=r.session_id,
            host=r.host,
            port=r.port,
            username=r.username,
            connected_at=time_to_iso(r.connected_at),
            last_command_at=time_to_iso(r.last_activity) if r.last_activity else None,
            idle_seconds=round(now - r.last_activity, 1),
        )
        for r in records
    ]
    return SessionsResponse(sessions=sessions, count=len(sessions))


# ---------------------------------------------------------------------------
# Heartbeat / Keepalive
# ---------------------------------------------------------------------------

@router.post("/api/ssh/heartbeat")
async def ssh_heartbeat(req: DisconnectRequest):
    """Refresh session timeout by touching it."""
    record = await _state.manager.get_session(req.session_id)
    if not record:
        raise SessionNotFoundError(f"Session {req.session_id} not found")
    record.touch()
    return {"status": "ok", "session_id": req.session_id, "idle_time": record.idle_time}


@router.get("/api/ssh/session/{session_id}/health")
async def session_health(session_id: str):
    """Check session health and auto-reconnect if needed."""
    record = await _state.manager.get_session(session_id)
    if not record:
        raise SessionNotFoundError(f"Session {session_id} not found")

    is_connected = record.is_connected()

    if not is_connected:
        logger.info("Session %s disconnected, attempting auto-reconnect", session_id)
        reconnected = await _state.manager.reconnect(session_id)
        return {
            "session_id": session_id,
            "connected": reconnected,
            "reconnected": True,
            "reconnect_count": record.reconnect_count,
            "reconnect_reason": record.last_reconnect_reason or "timeout",
            "idle_time": record.idle_time,
        }

    return {
        "session_id": session_id,
        "connected": True,
        "reconnected": False,
        "reconnect_count": record.reconnect_count,
        "reconnect_reason": None,
        "idle_time": record.idle_time,
    }


# ---------------------------------------------------------------------------
# Websocket Streaming
# ---------------------------------------------------------------------------

@router.websocket("/api/ssh/execute/stream")
async def ssh_execute_stream(websocket: WebSocket):
    """Execute a command and stream output via WebSocket."""
    ws_err = await ws_auth_check(websocket, settings)
    if ws_err is not None:
        await websocket.close(code=ws_err[0], reason=ws_err[1])
        return
    await websocket.accept()
    try:
        # First Message Must Contain Session_id And Command
        data = await websocket.receive_json()
        session_id = data.get("session_id", "")
        command = data.get("command", "")

        if not session_id or not command:
            await websocket.send_json({"type": "error", "data": "session_id and command are required"})
            await websocket.close()
            return

        try:
            command = sanitize_command(command)
        except ValueError as exc:
            _state.audit_logger.log_security_event(
                "BLOCKED_COMMAND", str(exc), websocket.client.host
            )
            await websocket.send_json({"type": "error", "data": str(exc)})
            await websocket.close()
            return

        async for msg_type, msg_data in _state.manager.execute_stream(session_id, command):
            await websocket.send_json({"type": msg_type, "data": msg_data})

    except WebSocketDisconnect:
        logger.info("Websocket Client Disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
        try:
            await websocket.send_json({"type": "error", "data": str(exc)})
        except Exception:
            pass
        await websocket.close()


@router.websocket("/api/ssh/pty/{session_id}/stream")
async def pty_stream(websocket: WebSocket, session_id: str):
    """Interactive PTY via WebSocket."""
    ws_err = await ws_auth_check(websocket, settings)
    if ws_err is not None:
        await websocket.close(code=ws_err[0], reason=ws_err[1])
        return
    await websocket.accept()

    channel = None
    try:
        init_data = await websocket.receive_json()
        term = init_data.get("term", "xterm-256color")
        rows = init_data.get("rows", 24)
        cols = init_data.get("cols", 80)

        channel = await _state.manager.create_pty_channel(session_id, term, rows, cols)

        async def read_from_channel():
            try:
                while True:
                    if channel.recv_ready():
                        data = channel.recv(4096).decode("utf-8", errors="replace")
                        await websocket.send_json({"type": "output", "data": data})
                    elif channel.closed:
                        break
                    await asyncio.sleep(0.01)
            except Exception:
                pass

        async def write_to_channel():
            try:
                while True:
                    msg = await websocket.receive_json()
                    msg_type = msg.get("type")
                    if msg_type == "input":
                        channel.send(msg.get("data", ""))
                    elif msg_type == "resize":
                        channel.resize_pty(
                            width=msg.get("cols", 80),
                            height=msg.get("rows", 24),
                        )
                    elif msg_type == "close":
                        break
            except WebSocketDisconnect:
                pass

        await asyncio.gather(read_from_channel(), write_to_channel())
    except Exception as exc:
        logger.error("PTY WebSocket error: %s", exc)
    finally:
        if channel:
            try:
                channel.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Port Checker
# ---------------------------------------------------------------------------


@router.get("/api/ssh/check-port")
async def check_port(
    host: str = Query(..., description="Target hostname or IP"),
    port: int = Query(22, ge=1, le=65535, description="Target port"),
    timeout: float = Query(5.0, ge=0.5, le=30.0, description="Connection timeout in seconds"),
):
    """Check if a remote TCP port is reachable. No auth required."""
    start = time.monotonic()
    try:
        _reader, _writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        _writer.close()
        await _writer.wait_closed()
        elapsed = int((time.monotonic() - start) * 1000)
        return {"host": host, "port": port, "reachable": True, "duration_ms": elapsed}
    except (OSError, asyncio.TimeoutError, ConnectionError):
        elapsed = int((time.monotonic() - start) * 1000)
        return {"host": host, "port": port, "reachable": False, "duration_ms": elapsed}


# ---------------------------------------------------------------------------
# Environment Inspect
# ---------------------------------------------------------------------------


@router.get("/api/ssh/session/{session_id}/env")
async def session_env(
    session_id: str,
    prefix: str = Query(None, description="Filter env vars by prefix (e.g. PATH)"),
):
    """Read environment variables from an active SSH session."""
    result = await _state.manager.execute(session_id=session_id, command="printenv", timeout=10)
    if result["exit_code"] != 0:
        raise HTTPException(status_code=502, detail=_err(502, f"Failed to read env: {result['stderr']}"))

    env = {}
    for line in result["stdout"].splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        if prefix and not key.startswith(prefix):
            continue
        env[key] = val
    return env


# ---------------------------------------------------------------------------
# SSH Key Upload
# ---------------------------------------------------------------------------


@router.post("/api/ssh/keys")
async def upload_ssh_key(
    file: UploadFile = File(...),
):
    """Upload an SSH private key. Stored in /app/ssh_keys/."""
    content = await file.read()
    if len(content) > 64 * 1024:
        raise HTTPException(status_code=400, detail=_err(400, "Key file too large (max 64KB)"))
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail=_err(400, "Key must be valid UTF-8 text"))

    if not text.startswith("-----BEGIN"):
        raise HTTPException(status_code=400, detail=_err(400, "Not a valid private key format"))

    keys_dir = "/app/ssh_keys"
    os.makedirs(keys_dir, exist_ok=True)
    name = file.filename or f"key-{uuid.uuid4().hex[:8]}.pem"
    fpath = os.path.join(keys_dir, name)

    with open(fpath, "w") as f:
        f.write(text)
    os.chmod(fpath, 0o600)

    return {"name": name, "path": fpath, "size": len(content)}
