"""System and meta routes: health, capabilities, config, help, metrics, SDK, circuit-breaker, UI."""

import os
import socket
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from app import state as _state
from app.api_help import build_api_help
from app.auth_middleware import (
    AuthIdentity,
    is_agent_token_valid,
    require_any_auth,
    require_master_key,
)
from app.config import settings
from app.metrics import metrics
from app.models import (
    CapabilitiesResponse,
    HealthResponse,
)
from app.state import _err
from app.version import APP_VERSION, get_version_source

PROJECT_ROOT = Path(__file__).resolve().parents[2]


router = APIRouter()


# ---------------------------------------------------------------------------
# Health & System
# ---------------------------------------------------------------------------


async def _deep_ssh_check(host: str, port: int) -> bool | None:
    """SSH deep check: try to authenticate and run `true`.

    Returns:
        True  — SSH is fully functional.
        False — SSH connection or command failed.
        None  — not configured (no test credentials).
    """
    user = settings.ssh_health_user
    password = settings.ssh_health_password
    if not user:
        return None
    try:
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, port=port, username=user, password=password, timeout=5, allow_agent=False, look_for_keys=False)
        _, stdout, _ = client.exec_command("true", timeout=5)
        exit_code = stdout.channel.recv_exit_status()
        client.close()
        return exit_code == 0
    except Exception:
        return False


@router.get("/health", tags=["system"], response_model=HealthResponse)
async def health_check():
    """Health check endpoint.

    Reports readiness based on all critical subsystems: Redis,
    PostgreSQL, API key configuration, and SSH server reachability.
    """
    from app import build_info
    from app.version import APP_VERSION

    redis_ok = _state.redis_queue is not None and _state.redis_queue._redis is not None
    persistent_sessions_ok = _state.session_store is not None
    meta = build_info.get_build_metadata()

    api_key_ok = bool(settings.api_key)

    ssh_host = os.environ.get("GATEWAY_SSH_HOST", "sshd")
    ssh_port = int(os.environ.get("GATEWAY_SSH_PORT", "22"))

    # TCP-level check (always)
    ssh_tcp_ok = False
    try:
        with socket.create_connection((ssh_host, ssh_port), timeout=2):
            ssh_tcp_ok = True
    except (OSError, TimeoutError):
        pass

    # Deep SSH check (only when test credentials configured)
    ssh_deep_ok = await _deep_ssh_check(ssh_host, ssh_port)
    ssh_ok = ssh_tcp_ok if ssh_deep_ok is None else ssh_deep_ok

    redis_degraded = not redis_ok and settings.redis_url
    sessions_degraded = settings.persistent_sessions_enabled and not persistent_sessions_ok
    auth_degraded = settings.api_auth_enabled and not api_key_ok
    ssh_degraded = not ssh_ok
    status = "degraded" if (redis_degraded or sessions_degraded or auth_degraded or ssh_degraded) else "ok"
    return HealthResponse(
        status=status,
        redis=redis_ok,
        persistent_sessions=persistent_sessions_ok,
        postgres=persistent_sessions_ok,
        ready=status == "ok",
        api_key_configured=api_key_ok,
        ssh_server_reachable=ssh_ok,
        build_sha=meta["build_sha"],
        build_time=meta["build_time"],
        started_at=meta["started_at"],
        version=APP_VERSION,
    )


@router.get("/api/capabilities", tags=["system"], response_model=CapabilitiesResponse)
async def get_capabilities():
    """Return API capabilities and environment information.

    Unauthenticated — used by agents to discover server settings.
    """
    servers = _state.server_manager.list_servers() if _state.server_manager else []
    server_count = len(servers)
    hint = ""
    if server_count == 0:
        hint = "No servers configured. Create one via POST /api/servers or connect directly with POST /api/ssh/connect"
    return CapabilitiesResponse(
        version=APP_VERSION,
        version_source=get_version_source(),
        auth_mode="api_key" if settings.api_auth_enabled else "none",
        session_timeout=settings.session_timeout,
        cleanup_interval=settings.cleanup_interval,
        ssh_default_timeout=settings.ssh_default_timeout,
        max_sessions_per_ip=settings.max_sessions_per_ip,
        rate_limit_requests=settings.rate_limit_requests,
        rate_limit_window=settings.rate_limit_window,
        server_count=server_count,
        agent_token_enabled=bool(
            await is_agent_token_valid(settings, settings.agent_token, _state.agent_token_store)
        ),
        agent_token_ttl=settings.agent_token_ttl,
        hint=hint,
    )


@router.get("/api/config", tags=["system"])
async def get_config(_identity: AuthIdentity = Depends(require_master_key)):
    """Return runtime configuration (secrets masked)."""
    from app.config import settings

    return {
        "session_timeout": settings.session_timeout,
        "cleanup_interval": settings.cleanup_interval,
        "ssh_default_timeout": settings.ssh_default_timeout,
        "max_sessions_per_ip": settings.max_sessions_per_ip,
        "rate_limit_requests": settings.rate_limit_requests,
        "rate_limit_window": settings.rate_limit_window,
        "persistent_sessions_enabled": settings.persistent_sessions_enabled,
        "known_hosts_store": settings.known_hosts_store or "null",
        "api_auth_enabled": settings.api_auth_enabled,
        "agent_token_enabled": bool(settings.agent_token),
        "agent_token_ttl": settings.agent_token_ttl,
        "read_only": getattr(settings, "read_only", False),
    }


@router.get("/api/help", tags=["help"])
async def api_help(request: Request, _identity: AuthIdentity = Depends(require_any_auth)):
    """API reference: auth requirements, quick-start examples, and all endpoints.

    Accessible with any valid API key (master key or agent token).
    """
    return build_api_help(request)


@router.get("/metrics", tags=["system"], response_class=PlainTextResponse)
async def prometheus_metrics(_identity: AuthIdentity = Depends(require_master_key)):
    """Prometheus metrics endpoint."""
    if _state.circuit_breakers is not None:
        counts = await _state.circuit_breakers.count_by_state()
        metrics.set_circuit_breaker_counts(counts)
    if _state.manager is not None and isinstance(_state.manager.pool_stats, dict):
        metrics.update_pool_metrics(**_state.manager.pool_stats)
    return Response(content=metrics.get_metrics(), media_type="text/plain")


@router.get("/api/sdk/download", tags=["system"], response_class=PlainTextResponse)
async def download_sdk(_identity: AuthIdentity = Depends(require_master_key)):
    """Download Python SDK.

    Note: auth is handled by the global middleware.
    """
    sdk_path = PROJECT_ROOT / "sdk" / "ssh_gateway.py"
    if not sdk_path.exists():
        raise HTTPException(status_code=404, detail=_err(404, "SDK not found"))
    content = sdk_path.read_text()
    return Response(
        content=content,
        media_type="text/x-python",
        headers={"Content-Disposition": "attachment; filename=ssh_gateway.py"},
    )


@router.get("/api/circuit-breaker/stats", tags=["system"])
async def circuit_breaker_stats(_identity: AuthIdentity = Depends(require_master_key)):
    """Get circuit breaker statistics."""
    return await _state.circuit_breakers.get_all_stats()


@router.get("/", tags=["system"], response_class=HTMLResponse)
async def root():
    """Serve the web terminal UI.

    Protected by global auth middleware — requires a valid X-API-Key header
    (master key or agent token) when API auth is enabled.

    See GET /api/help for the REST API reference.
    """
    return FileResponse("app/static/index.html")
