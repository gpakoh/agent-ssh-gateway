"""System and meta routes: health, capabilities, config, help, metrics, SDK, circuit-breaker, UI."""

import asyncio
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
    HealthComponentStatus,
    HealthResponse,
)
from app.state import _err
from app.version import APP_VERSION, get_version_source

PROJECT_ROOT = Path(__file__).resolve().parents[2]


router = APIRouter()


# ---------------------------------------------------------------------------
# Health & System
# ---------------------------------------------------------------------------


HEALTH_REDIS_TIMEOUT_SECONDS = 1.0
HEALTH_POSTGRES_TIMEOUT_SECONDS = 1.0
HEALTH_SSH_OPERATION_TIMEOUT_SECONDS = 1.0
HEALTH_SSH_PROBE_TIMEOUT_SECONDS = 2.25
# Docker's HTTP caller times out at 4s. Keep a full second of scheduling/serialization margin.
HEALTH_AGGREGATE_TIMEOUT_SECONDS = 3.0


def _component_status(
    *,
    ok: bool,
    required: bool,
    failure_class: str | None = None,
) -> HealthComponentStatus:
    return HealthComponentStatus(
        status="ok" if (ok or not required) else "degraded",
        required=required,
        failure_class=None if ok else failure_class,
    )


def _classify_health_failure(exc: BaseException) -> str:
    """Map dependency failures to a bounded, non-sensitive public class."""
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, RedisTimeoutError, SqlAlchemyTimeoutError)):
            return "timeout"
        if isinstance(current, (OSError, RedisConnectionError, OperationalError)):
            return "connect_error"
        cause = current.__cause__ or current.__context__
        current = cause if isinstance(cause, BaseException) else None
    return "unavailable"


def _deep_ssh_check_blocking(
    host: str,
    port: int,
    user: str,
    password: str | None,
) -> bool:
    """Blocking Paramiko probe. Caller must run this outside the event loop."""
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host,
            port=port,
            username=user,
            password=password,
            timeout=HEALTH_SSH_OPERATION_TIMEOUT_SECONDS,
            allow_agent=False,
            look_for_keys=False,
        )
        _, stdout, _ = client.exec_command(
            "true", timeout=HEALTH_SSH_OPERATION_TIMEOUT_SECONDS
        )
        return stdout.channel.recv_exit_status() == 0
    finally:
        client.close()


async def _probe_deep_ssh(
    host: str,
    port: int,
    user: str,
    password: str | None,
) -> tuple[bool, str | None]:
    try:
        ok = await asyncio.wait_for(
            asyncio.to_thread(_deep_ssh_check_blocking, host, port, user, password),
            timeout=HEALTH_SSH_PROBE_TIMEOUT_SECONDS,
        )
        return ok, None if ok else "unavailable"
    except Exception as exc:
        return False, _classify_health_failure(exc)


async def _deep_ssh_check(host: str, port: int) -> bool | None:
    """Compatibility helper for the optional deep SSH health probe."""
    user = settings.ssh_health_user
    if not user:
        return None
    ok, _failure = await _probe_deep_ssh(
        host, port, user, settings.ssh_health_password
    )
    return ok


def _tcp_ssh_check_blocking(host: str, port: int) -> None:
    """Blocking TCP connect used by the shallow SSH health probe."""
    with socket.create_connection(
        (host, port), timeout=HEALTH_SSH_OPERATION_TIMEOUT_SECONDS
    ):
        return None


async def _probe_ssh(host: str, port: int) -> tuple[bool, str | None]:
    """Probe SSH without blocking the FastAPI event loop."""
    user = settings.ssh_health_user
    if user:
        return await _probe_deep_ssh(
            host, port, user, settings.ssh_health_password
        )
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_tcp_ssh_check_blocking, host, port),
            timeout=HEALTH_SSH_PROBE_TIMEOUT_SECONDS,
        )
        return True, None
    except Exception as exc:
        return False, _classify_health_failure(exc)


async def _probe_redis() -> tuple[bool, str | None]:
    """Actively verify Redis when configured; never leak backend exception text."""
    queue = _state.redis_queue
    client = queue._redis if queue is not None else None
    if client is None:
        return False, "unavailable"
    try:
        await asyncio.wait_for(client.ping(), timeout=HEALTH_REDIS_TIMEOUT_SECONDS)
        return True, None
    except Exception as exc:
        return False, _classify_health_failure(exc)


async def _probe_postgres() -> tuple[bool, str | None]:
    """Actively verify the persistent-session PostgreSQL engine."""
    from sqlalchemy import text

    store = _state.session_store
    engine = store._engine if store is not None else None
    if engine is None:
        return False, "unavailable"

    async def _select_one() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_select_one(), timeout=HEALTH_POSTGRES_TIMEOUT_SECONDS)
        return True, None
    except Exception as exc:
        return False, _classify_health_failure(exc)


async def _run_health_probes(
    *,
    redis_required: bool,
    postgres_required: bool,
    ssh_host: str,
    ssh_port: int,
) -> dict[str, tuple[bool, str | None]]:
    """Run independent dependency probes concurrently under one aggregate budget."""
    tasks: dict[str, asyncio.Task[tuple[bool, str | None]]] = {
        "ssh": asyncio.create_task(_probe_ssh(ssh_host, ssh_port)),
    }
    if redis_required:
        tasks["redis"] = asyncio.create_task(_probe_redis())
    if postgres_required:
        tasks["postgres"] = asyncio.create_task(_probe_postgres())

    done, pending = await asyncio.wait(
        set(tasks.values()), timeout=HEALTH_AGGREGATE_TIMEOUT_SECONDS
    )
    results: dict[str, tuple[bool, str | None]] = {}
    for name, task in tasks.items():
        if task in pending:
            task.cancel()
            results[name] = (False, "timeout")
            continue
        try:
            results[name] = task.result()
        except Exception as exc:
            results[name] = (False, _classify_health_failure(exc))

    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return results


@router.get("/health", tags=["system"], response_model=HealthResponse)
async def health_check():
    """Return bounded aggregate health while preserving per-component truth."""
    from app import build_info

    meta = build_info.get_build_metadata()

    redis_required = bool(settings.redis_url)
    postgres_required = bool(settings.persistent_sessions_enabled)

    ssh_host = os.environ.get("GATEWAY_SSH_HOST", "sshd")
    ssh_port = int(os.environ.get("GATEWAY_SSH_PORT", "22"))

    probe_results = await _run_health_probes(
        redis_required=redis_required,
        postgres_required=postgres_required,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
    )

    if redis_required:
        redis_ok, redis_failure = probe_results["redis"]
    else:
        redis_ok = _state.redis_queue is not None and _state.redis_queue._redis is not None
        redis_failure = None if redis_ok else "not_configured"

    if postgres_required:
        persistent_sessions_ok, postgres_failure = probe_results["postgres"]
    else:
        persistent_sessions_ok = _state.session_store is not None
        postgres_failure = None if persistent_sessions_ok else "not_configured"

    ssh_ok, ssh_failure = probe_results["ssh"]

    api_key_ok = bool(settings.api_key)
    auth_required = bool(settings.api_auth_enabled)
    auth_ok = api_key_ok or not auth_required
    auth_failure = None if auth_ok else "unavailable"

    components: dict[str, HealthComponentStatus] = {
        "redis": _component_status(
            ok=redis_ok, required=redis_required, failure_class=redis_failure
        ),
        "postgres": _component_status(
            ok=persistent_sessions_ok,
            required=postgres_required,
            failure_class=postgres_failure,
        ),
        # Backward-compatible structured alias for the existing flat field.
        "persistent_sessions": _component_status(
            ok=persistent_sessions_ok,
            required=postgres_required,
            failure_class=postgres_failure,
        ),
        "auth": _component_status(
            ok=auth_ok, required=auth_required, failure_class=auth_failure
        ),
        "ssh": _component_status(ok=ssh_ok, required=True, failure_class=ssh_failure),
    }

    degraded = any(component.status == "degraded" for component in components.values())
    status = "degraded" if degraded else "ok"

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
        components=components,
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
