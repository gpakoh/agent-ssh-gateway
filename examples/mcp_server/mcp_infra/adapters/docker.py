"""Docker adapter: container/compose inspection and dangerous operations.

DockerClient and _confirm_store are resolved through the server module at
call time: tests patch examples.mcp_server.server.DockerClient and
examples.mcp_server.server._confirm_store and expect the patched objects
here (test_mcp_compose_confirm, test_mcp_contract_v1_docker_postgres).

Tools are registered explicitly via register_all() (called by server.py
after runtime.set_mcp) instead of import-time decorator side effects:
server.py may be importlib.reloaded, and the adapters are cached in
sys.modules, so import-time registration would miss the new FastMCP
instance.
"""

from __future__ import annotations

import os
import time as _time
from collections.abc import Callable
from typing import Any

from tool_results import tool_error, tool_success, validate_pagination

from examples.mcp_client_remote.fleet.docker_client import (
    RunResult,  # noqa: F401  (used in impl return annotations)
)
from examples.mcp_server.docker_confirm import ConfirmAction, ConfirmStatus
from examples.mcp_server.mcp_audit import McpAuditEvent
from examples.mcp_server.mcp_infra.tool_registry import register_tool


def _docker_client():
    from examples.mcp_server import server as _server

    return _server.DockerClient()


def _confirm_store():
    from examples.mcp_server import server as _server

    return _server._confirm_store


def _get_audit_logger():
    from examples.mcp_server import server as _server

    return _server.get_audit_logger()


async def docker_ps(all: bool = False, limit: int = 50) -> dict[str, Any]:
    """List running containers as structured rows. Use all=True to include
    stopped containers. limit: max rows (default 50)."""
    client = _docker_client()
    try:
        validate_pagination(limit, "limit")
        rows = await client.ps(all=all, limit=limit)
    except ValueError as exc:
        return tool_error(tool="docker_ps", code="INVALID_INPUT", message=str(exc), source="docker")
    except RuntimeError as exc:
        return tool_error(tool="docker_ps", code="DOCKER_COMMAND_FAILED", message=str(exc), source="docker")
    return tool_success(
        "docker_ps",
        result={"containers": rows, "count": len(rows)},
        truncated=client.last_truncated,
        redacted=client.last_redacted,
        source="docker",
    )

async def docker_images(limit: int = 50) -> dict[str, Any]:
    """List Docker images on the host as structured rows. limit: max rows (default 50)."""
    client = _docker_client()
    try:
        validate_pagination(limit, "limit")
        rows = await client.images(limit=limit)
    except ValueError as exc:
        return tool_error(tool="docker_images", code="INVALID_INPUT", message=str(exc), source="docker")
    except RuntimeError as exc:
        return tool_error(tool="docker_images", code="DOCKER_COMMAND_FAILED", message=str(exc), source="docker")
    return tool_success(
        "docker_images",
        result={"images": rows, "count": len(rows)},
        truncated=client.last_truncated,
        source="docker",
    )

async def docker_inspect(name: str) -> dict[str, Any]:
    """Inspect a container by name or ID. Returns structured metadata
    reduced to a strict allowlist (host paths, PID, IPs, network/endpoint
    IDs and compose working dirs are dropped)."""
    client = _docker_client()
    try:
        data = await client.inspect(name, max_lines=500)
    except (ValueError, RuntimeError) as exc:
        return tool_error(tool="docker_inspect", code="DOCKER_COMMAND_FAILED", message=str(exc), source="docker")
    return tool_success(
        "docker_inspect",
        result=data,
        redacted=True,
        truncated=client.last_truncated,
        source="docker",
    )

async def docker_logs(container: str, tail: int = 200) -> dict[str, Any]:
    """Fetch logs from a running container. tail: number of recent lines (1-1000, default 200)."""
    try:
        validate_pagination(tail, "tail", max_value=1000)
        result = await _docker_client().logs(container, tail=tail)
    except ValueError as exc:
        return tool_error(tool="docker_logs", code="INVALID_INPUT", message=str(exc), source="docker")
    except RuntimeError as exc:
        return tool_error(tool="docker_logs", code="DOCKER_COMMAND_FAILED", message=str(exc), source="docker")
    return tool_success("docker_logs", result=result, source="docker")

async def docker_stats(limit: int = 50) -> dict[str, Any]:
    """Show live resource usage statistics for all running containers as
    structured rows. limit: max rows (default 50)."""
    client = _docker_client()
    try:
        validate_pagination(limit, "limit")
        rows = await client.stats(limit=limit)
    except ValueError as exc:
        return tool_error(tool="docker_stats", code="INVALID_INPUT", message=str(exc), source="docker")
    except RuntimeError as exc:
        return tool_error(tool="docker_stats", code="DOCKER_COMMAND_FAILED", message=str(exc), source="docker")
    return tool_success(
        "docker_stats",
        result={"stats": rows, "count": len(rows)},
        truncated=client.last_truncated,
        source="docker",
    )

async def docker_compose_ps(
    project_dir: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List containers in a Docker Compose project as structured rows. limit: max rows (default 50)."""
    client = _docker_client()
    try:
        validate_pagination(limit, "limit")
        rows = await client.compose_ps(project_dir=project_dir, limit=limit)
    except ValueError as exc:
        return tool_error(tool="docker_compose_ps", code="INVALID_INPUT", message=str(exc), source="docker")
    except RuntimeError as exc:
        return tool_error(tool="docker_compose_ps", code="DOCKER_COMMAND_FAILED", message=str(exc), source="docker")
    if isinstance(rows, str):
        return tool_success("docker_compose_ps", result=rows, source="docker")
    return tool_success(
        "docker_compose_ps",
        result={"containers": rows, "count": len(rows)},
        truncated=client.last_truncated,
        redacted=client.last_redacted,
        source="docker",
    )

async def docker_compose_services(
    project_dir: str | None = None,
) -> dict[str, Any]:
    """List service names defined in a Docker Compose project."""
    try:
        result = await _docker_client().compose_services(project_dir=project_dir)
    except ValueError as exc:
        return tool_error(
            tool="docker_compose_services", code="INVALID_INPUT", message=str(exc), source="docker"
        )
    except RuntimeError as exc:
        return tool_error(
            tool="docker_compose_services", code="DOCKER_COMMAND_FAILED", message=str(exc), source="docker"
        )
    return tool_success("docker_compose_services", result=result, source="docker")

async def docker_compose_logs(
    project_dir: str | None = None,
    services: list[str] | None = None,
    tail: int = 100,
    follow: bool = False,
    timestamps: bool = False,
) -> dict[str, Any]:
    """Fetch logs from services in a Docker Compose project. tail: 1-1000 lines."""
    try:
        result = await _docker_client().compose_logs(
            project_dir=project_dir,
            services=services,
            tail=tail,
            follow=follow,
            timestamps=timestamps,
        )
    except ValueError as exc:
        return tool_error(
            tool="docker_compose_logs", code="INVALID_INPUT", message=str(exc), source="docker"
        )
    except RuntimeError as exc:
        return tool_error(
            tool="docker_compose_logs", code="DOCKER_COMMAND_FAILED", message=str(exc), source="docker"
        )
    return tool_success("docker_compose_logs", result=result, source="docker")

async def docker_stop(container: str, timeout: int = 10) -> dict[str, Any]:
    """Stop a running container. DANGEROUS: requires confirmation via confirm_operation(token).
    timeout: seconds before force kill (1-120, default 10)."""
    _docker_client()._validate_container_name(container)
    summary = f"Stop container {container}"
    action = _confirm_store().create_action(
        "docker_stop", {"container": container, "timeout": timeout}, summary, risk="medium"
    )
    return _confirmation_response(action)

async def docker_restart(container: str, timeout: int = 10) -> dict[str, Any]:
    """Restart a container. DANGEROUS: requires confirmation via confirm_operation(token).
    timeout: seconds before force kill (1-120, default 10)."""
    _docker_client()._validate_container_name(container)
    summary = f"Restart container {container}"
    action = _confirm_store().create_action(
        "docker_restart", {"container": container, "timeout": timeout}, summary, risk="medium"
    )
    return _confirmation_response(action)

async def docker_compose_up(
    project_dir: str | None = None,
    services: list[str] | None = None,
    detach: bool = True,
    build: bool = False,
    timeout: int = 120,
) -> dict[str, Any]:
    """Start services in a Docker Compose project. DANGEROUS: requires confirmation via confirm_operation(token)."""
    svc_list = ", ".join(services) if services else "all services"
    summary = f"Compose up ({svc_list}) in {project_dir or 'default dir'}"
    action = _confirm_store().create_action(
        "docker_compose_up",
        {"project_dir": project_dir, "services": services, "detach": detach, "build": build, "timeout": timeout},
        summary,
        risk="medium",
    )
    return _confirmation_response(action)

async def docker_compose_restart(
    project_dir: str | None = None,
    services: list[str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Restart services in a Docker Compose project. DANGEROUS: requires confirmation via confirm_operation(token)."""
    svc_list = ", ".join(services) if services else "all services"
    summary = f"Compose restart ({svc_list}) in {project_dir or 'default dir'}"
    action = _confirm_store().create_action(
        "docker_compose_restart",
        {"project_dir": project_dir, "services": services, "timeout": timeout},
        summary,
        risk="medium",
    )
    return _confirmation_response(action)

async def docker_compose_build(
    project_dir: str | None = None,
    services: list[str] | None = None,
    no_cache: bool = False,
    timeout: int = 300,
) -> dict[str, Any]:
    """Build (or rebuild) services in a Docker Compose project. DANGEROUS: requires confirmation via confirm_operation(token)."""
    svc_list = ", ".join(services) if services else "all services"
    summary = f"Compose build ({svc_list}) in {project_dir or 'default dir'}"
    action = _confirm_store().create_action(
        "docker_compose_build",
        {"project_dir": project_dir, "services": services, "no_cache": no_cache, "timeout": timeout},
        summary,
        risk="medium",
    )
    return _confirmation_response(action)

async def _docker_start_impl(container: str, timeout: int | None = None) -> str:
    return await _docker_client().start(container, timeout=timeout)

async def _docker_stop_impl(container: str, timeout: int = 10) -> str:
    return await _docker_client().stop(container, timeout=timeout)

async def _docker_restart_impl(container: str, timeout: int = 10) -> str:
    return await _docker_client().restart(container, timeout=timeout)

async def _docker_rm_impl(container: str, force: bool = False) -> RunResult:
    return await _docker_client().rm(container, force=force)

async def _docker_compose_down_impl(
    project_dir: str | None = None,
    remove_orphans: bool = False,
    timeout: int = 30,
    volumes: bool = False,
) -> RunResult:
    return await _docker_client().compose_down(
        project_dir=project_dir,
        remove_orphans=remove_orphans,
        timeout=timeout,
        volumes=volumes,
    )

async def _docker_prune_impl(type: str = "container") -> RunResult:
    return await _docker_client().prune(type)

async def _docker_exec_impl(container: str, command: list[str], timeout: int = 30) -> RunResult:
    return await _docker_client().exec(container, command, timeout=timeout)

async def _docker_run_impl(
    image: str,
    command: list[str],
    container_name: str | None = None,
    timeout: int = 60,
) -> RunResult:
    return await _docker_client().run(
        image, command, container_name=container_name, timeout=timeout
    )

async def _docker_compose_up_impl(
    project_dir: str | None = None,
    services: list[str] | None = None,
    detach: bool = True,
    build: bool = False,
    timeout: int = 120,
) -> str:
    return await _docker_client().compose_up(
        project_dir=project_dir,
        services=services,
        detach=detach,
        build=build,
        timeout=timeout,
    )

async def _docker_compose_restart_impl(
    project_dir: str | None = None,
    services: list[str] | None = None,
    timeout: int = 30,
) -> str:
    return await _docker_client().compose_restart(
        project_dir=project_dir,
        services=services,
        timeout=timeout,
    )

async def _docker_compose_build_impl(
    project_dir: str | None = None,
    services: list[str] | None = None,
    no_cache: bool = False,
    timeout: int = 300,
) -> str:
    return await _docker_client().compose_build(
        project_dir=project_dir,
        services=services,
        no_cache=no_cache,
        timeout=timeout,
    )

async def _docker_rmi_impl(images: list[str]) -> RunResult:
    return await _docker_client().rmi(images)

async def _docker_volume_rm_impl(volumes: list[str]) -> RunResult:
    return await _docker_client().volume_rm(volumes)

_CONFIRM_HANDLERS: dict[str, Callable[..., Any]] = {
    "docker_start": _docker_start_impl,
    "docker_stop": _docker_stop_impl,
    "docker_restart": _docker_restart_impl,
    "docker_rm": _docker_rm_impl,
    "docker_compose_down": _docker_compose_down_impl,
    "docker_compose_up": _docker_compose_up_impl,
    "docker_compose_restart": _docker_compose_restart_impl,
    "docker_compose_build": _docker_compose_build_impl,
    "docker_prune": _docker_prune_impl,
    "docker_exec": _docker_exec_impl,
    "docker_run": _docker_run_impl,
    "docker_rmi": _docker_rmi_impl,
    "docker_volume_rm": _docker_volume_rm_impl,
}

def _confirmation_response(action: ConfirmAction) -> dict[str, Any]:
    remaining = max(0, int(60 - (_time.monotonic() - action.created_at)))
    return tool_success(
        tool=action.tool,
        result={
            "status": "confirmation_required",
            "action_id": action.action_id,
            "confirm_token": action.confirm_token,
            "expires_in_sec": remaining,
            "summary": action.summary,
            "risk": action.risk,
        },
        source="docker",
        dangerous=True,
    )

async def docker_rm(container: str, force: bool = False) -> dict[str, Any]:
    """Remove a container. DANGEROUS: requires confirmation via confirm_operation(token)."""
    _docker_client()._validate_container_name(container)
    summary = f"Remove container {container}"
    action = _confirm_store().create_action(
        "docker_rm", {"container": container, "force": force}, summary
    )
    return _confirmation_response(action)

def _get_token_scopes() -> list[str]:
    """Return the current request's granted scopes.

    Reads the authenticated access token from FastMCP's per-request
    contextvar (set by AuthContextMiddleware whenever auth is enabled).
    Falls back to MCP_TOKEN_SCOPES for contexts with no request (unit
    tests, manual scripts) since that env var is never set by the
    running service itself.
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        access_token = get_access_token()
        if access_token is not None:
            return list(access_token.scopes)
    except Exception:
        pass

    raw = os.environ.get("MCP_TOKEN_SCOPES", "")
    return [s.strip() for s in raw.split(",") if s.strip()]

async def docker_compose_down(
    project_dir: str | None = None,
    remove_orphans: bool = False,
    timeout: int = 30,
    volumes: bool = False,
) -> dict[str, Any]:
    """Stop and remove a Compose stack. DANGEROUS: requires confirmation.
    With mcp:docker:admin scope: use volumes=True to also remove named volumes."""
    if volumes:
        scopes = _get_token_scopes()
        if "mcp:docker:admin" not in scopes:
            # Emit structured audit event
            try:
                audit_logger = _get_audit_logger()
                audit_logger.append(McpAuditEvent(
                    event_type="mcp.tool_denied",
                    tool="docker_compose_down",
                    action="validate_scope",
                    decision="deny",
                    reason="volumes=true requires mcp:docker:admin scope.",
                    error_code="DOCKER_ADMIN_SCOPE_REQUIRED",
                ))
            except Exception:
                pass  # audit failure must not change tool behavior
            return tool_error(
                tool="docker_compose_down",
                code="DOCKER_ADMIN_SCOPE_REQUIRED",
                message="volumes=true requires mcp:docker:admin scope.",
                source="docker",
            )
    dc = _docker_client()
    dc._validate_project_dir(project_dir)
    parts = []
    if project_dir:
        parts.append(f"project={project_dir}")
    if volumes:
        parts.append("--volumes")
    summary = f"Compose down {' '.join(parts)}"
    action = _confirm_store().create_action(
        "docker_compose_down",
        {
            "project_dir": project_dir,
            "remove_orphans": remove_orphans,
            "timeout": timeout,
            "volumes": volumes,
        },
        summary,
    )
    return _confirmation_response(action)

async def docker_prune(type: str = "container") -> dict[str, Any]:
    """Prune Docker resources. DANGEROUS: requires confirmation. Allowed types: container, image, network.
    With mcp:docker:admin scope: also volume, system."""
    scopes = _get_token_scopes()
    has_admin = "mcp:docker:admin" in scopes
    if type in ("volume", "system") and not has_admin:
        return tool_error(
            tool="docker_prune",
            code="DOCKER_ADMIN_SCOPE_REQUIRED",
            message=f"Prune type '{type}' requires mcp:docker:admin scope.",
            hint="Request admin scope or use one of: container, image, network.",
            source="docker",
        )
    try:
        _docker_client()._validate_prune_type(type, admin_scope=has_admin)
    except ValueError as e:
        # Emit structured audit event
        try:
            audit_logger = _get_audit_logger()
            audit_logger.append(McpAuditEvent(
                event_type="mcp.tool_denied",
                tool="docker_prune",
                action="validate_prune_type",
                decision="deny",
                reason=str(e),
                error_code="INVALID_INPUT",
                metadata={"command_root": type},
            ))
        except Exception:
            pass  # audit failure must not change tool behavior
        return tool_error(
            tool="docker_prune",
            code="INVALID_INPUT",
            message=str(e),
            source="docker",
        )
    summary = f"Prune {type}s"
    action = _confirm_store().create_action("docker_prune", {"type": type}, summary)
    return _confirmation_response(action)

async def docker_exec(
    container: str,
    command: list[str],
    timeout: int = 30,
) -> dict[str, Any]:
    """Execute a command inside an existing container. ADMIN: requires mcp:docker:admin scope + confirmation.

    DANGEROUS: argv is checked against a safety denylist (env, shadow, shell launchers, etc.).
    This denylist is a safety guardrail, not a security boundary. docker_exec remains
    an admin-only dangerous operation and requires both mcp:docker:admin and confirmation.
    The system does not guarantee prevention of all data exfiltration through docker_exec.
    """
    dc = _docker_client()
    try:
        dc._validate_container_name(container)
    except ValueError as e:
        return tool_error(
            tool="docker_exec",
            code="INVALID_INPUT",
            message=str(e),
            source="docker",
        )
    try:
        dc._validate_exec_argv(command)
    except ValueError as e:
        # Emit structured audit event
        try:
            audit_logger = _get_audit_logger()
            audit_logger.append(McpAuditEvent(
                event_type="mcp.tool_denied",
                tool="docker_exec",
                action="validate_exec_command",
                decision="deny",
                reason=str(e),
                error_code="DOCKER_EXEC_COMMAND_BLOCKED",
                metadata={"command_root": command[0] if command else ""},
            ))
        except Exception:
            pass  # audit failure must not change tool behavior
        return tool_error(
            tool="docker_exec",
            code="DOCKER_EXEC_COMMAND_BLOCKED",
            message=str(e),
            hint="Use a narrower diagnostic command that does not dump environment variables, SSH keys, or shadow files.",
            source="docker",
        )
    timeout = max(1, min(timeout, 300))
    summary = f"Exec in {container}: {' '.join(command)}"
    action = _confirm_store().create_action(
        "docker_exec",
        {"container": container, "command": command, "timeout": timeout},
        summary,
        required_scope="mcp:docker:admin",
    )
    return _confirmation_response(action)

async def docker_run(
    image: str,
    command: list[str],
    container_name: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Create and start a container from an image. ADMIN: requires mcp:docker:admin scope + confirmation.

    Image must be in the MCP_DOCKER_RUN_ALLOWED_IMAGES allowlist.
    Container runs with --rm and is removed after completion.
    """
    allowed_raw = os.environ.get("MCP_DOCKER_RUN_ALLOWED_IMAGES", "").strip()
    if not allowed_raw:
        return tool_error(
            tool="docker_run",
            code="DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED",
            message="docker_run requires MCP_DOCKER_RUN_ALLOWED_IMAGES environment variable.",
            hint="Set MCP_DOCKER_RUN_ALLOWED_IMAGES with comma-separated image:tag entries.",
            source="docker",
        )
    allowed_images = {ref.strip() for ref in allowed_raw.split(",") if ref.strip()}

    dc = _docker_client()
    try:
        dc._validate_image_tag(image)
    except ValueError as e:
        return tool_error(
            tool="docker_run",
            code="DOCKER_RUN_IMAGE_INVALID",
            message=str(e),
            source="docker",
        )
    if image not in allowed_images:
        # Emit structured audit event
        try:
            audit_logger = _get_audit_logger()
            audit_logger.append(McpAuditEvent(
                event_type="mcp.tool_denied",
                tool="docker_run",
                action="validate_image",
                decision="deny",
                reason=f"Image '{image}' is not in the configured allowlist.",
                error_code="DOCKER_RUN_IMAGE_NOT_ALLOWED",
                metadata={"command_root": image},
            ))
        except Exception:
            pass  # audit failure must not change tool behavior
        return tool_error(
            tool="docker_run",
            code="DOCKER_RUN_IMAGE_NOT_ALLOWED",
            message=f"Image '{image}' is not in the configured allowlist.",
            hint="Only images listed in MCP_DOCKER_RUN_ALLOWED_IMAGES are permitted.",
            source="docker",
        )
    if container_name:
        try:
            dc._validate_container_name(container_name)
        except ValueError as e:
            return tool_error(
                tool="docker_run",
                code="INVALID_INPUT",
                message=str(e),
                source="docker",
            )
    try:
        dc._validate_exec_argv(command)
    except ValueError as e:
        # Emit structured audit event
        try:
            audit_logger = _get_audit_logger()
            audit_logger.append(McpAuditEvent(
                event_type="mcp.tool_denied",
                tool="docker_run",
                action="validate_exec_command",
                decision="deny",
                reason=str(e),
                error_code="DOCKER_EXEC_COMMAND_BLOCKED",
                metadata={"command_root": command[0] if command else ""},
            ))
        except Exception:
            pass  # audit failure must not change tool behavior
        return tool_error(
            tool="docker_run",
            code="DOCKER_EXEC_COMMAND_BLOCKED",
            message=str(e),
            source="docker",
        )
    timeout = max(1, min(timeout, 600))

    summary = f"Run {image}: {' '.join(command)}"
    if container_name:
        summary += f" (name={container_name})"
    action = _confirm_store().create_action(
        "docker_run",
        {
            "image": image,
            "command": command,
            "container_name": container_name,
            "timeout": timeout,
        },
        summary,
        required_scope="mcp:docker:admin",
    )
    return _confirmation_response(action)

async def docker_rmi(images: list[str]) -> dict[str, Any]:
    """Remove one or more Docker images (1-5). ADMIN: requires mcp:docker:admin scope + confirmation."""
    dc = _docker_client()
    if not images or len(images) > 5:
        return tool_error(
            tool="docker_rmi",
            code="DOCKER_RMI_INVALID_REFERENCE",
            message="docker_rmi accepts 1-5 images.",
            source="docker",
        )
    for img in images:
        try:
            dc._validate_image_ref(img)
        except ValueError as e:
            # Emit structured audit event
            try:
                audit_logger = _get_audit_logger()
                audit_logger.append(McpAuditEvent(
                    event_type="mcp.tool_denied",
                    tool="docker_rmi",
                    action="validate_image_ref",
                    decision="deny",
                    reason=str(e),
                    error_code="DOCKER_RMI_INVALID_REFERENCE",
                    metadata={"command_root": img},
                ))
            except Exception:
                pass  # audit failure must not change tool behavior
            return tool_error(
                tool="docker_rmi",
                code="DOCKER_RMI_INVALID_REFERENCE",
                message=str(e),
                source="docker",
            )
    summary = f"Remove image(s): {', '.join(images)}"
    action = _confirm_store().create_action(
        "docker_rmi",
        {"images": images},
        summary,
        required_scope="mcp:docker:admin",
    )
    return _confirmation_response(action)

async def docker_volume_rm(volumes: list[str]) -> dict[str, Any]:
    """Remove one or more Docker volumes (1-5). ADMIN: requires mcp:docker:admin scope + confirmation."""
    dc = _docker_client()
    if not volumes or len(volumes) > 5:
        return tool_error(
            tool="docker_volume_rm",
            code="DOCKER_VOLUME_RM_INVALID_NAME",
            message="docker_volume_rm accepts 1-5 volumes.",
            source="docker",
        )
    for vol in volumes:
        try:
            dc._validate_volume_name(vol)
        except ValueError as e:
            # Emit structured audit event
            try:
                audit_logger = _get_audit_logger()
                audit_logger.append(McpAuditEvent(
                    event_type="mcp.tool_denied",
                    tool="docker_volume_rm",
                    action="validate_volume_name",
                    decision="deny",
                    reason=str(e),
                    error_code="DOCKER_VOLUME_RM_INVALID_NAME",
                    metadata={"command_root": vol},
                ))
            except Exception:
                pass  # audit failure must not change tool behavior
            return tool_error(
                tool="docker_volume_rm",
                code="DOCKER_VOLUME_RM_INVALID_NAME",
                message=str(e),
                source="docker",
            )
    summary = f"Remove volume(s): {', '.join(volumes)}"
    action = _confirm_store().create_action(
        "docker_volume_rm",
        {"volumes": volumes},
        summary,
        required_scope="mcp:docker:admin",
    )
    return _confirmation_response(action)

async def confirm_operation(token: str) -> dict[str, Any]:
    """Confirm a pending dangerous Docker operation using the one-time token from the confirmation response."""
    action, status = _confirm_store().peek_action(token)
    if action is None:
        code = {
            ConfirmStatus.INVALID: "CONFIRM_TOKEN_INVALID",
            ConfirmStatus.EXPIRED: "CONFIRM_TOKEN_EXPIRED",
            ConfirmStatus.CONSUMED: "CONFIRM_TOKEN_CONSUMED",
        }.get(status, "INTERNAL_ERROR")
        msg = {
            ConfirmStatus.INVALID: "Invalid confirmation token",
            ConfirmStatus.EXPIRED: "Confirmation token expired (TTL 60s)",
            ConfirmStatus.CONSUMED: "Confirmation token already used",
        }.get(status, "Unknown error")

        # Emit structured audit event
        try:
            audit_logger = _get_audit_logger()
            audit_logger.append(McpAuditEvent(
                event_type="mcp.tool_blocked",
                tool="confirm_operation",
                action="confirm_docker_operation",
                decision="deny",
                reason=msg,
                error_code=code,
            ))
        except Exception:
            pass  # audit failure must not change tool behavior

        return tool_error(
            tool="confirm_operation",
            code=code,
            message=msg,
            hint="Call the dangerous tool again to get a new token.",
            retryable=False,
            source="docker",
        )

    handler = _CONFIRM_HANDLERS.get(action.tool)
    if not handler:
        return tool_error(
            tool="confirm_operation",
            code="INTERNAL_ERROR",
            message=f"No handler for {action.tool}",
            source="docker",
        )

    # Double Barrier: confirming an admin-only operation (docker_exec,
    # docker_run, docker_rmi, docker_volume_rm) re-checks that the caller
    # holds mcp:docker:admin. Possession of a confirm token alone must not
    # complete an admin action for a caller granted only mcp:docker.
    # The token is only consumed after every check passes, so a failed
    # scope check does not burn it.
    if action.required_scope != "mcp:docker":
        scopes = _get_token_scopes()
        if action.required_scope not in scopes:
            # Emit structured audit event
            try:
                audit_logger = _get_audit_logger()
                audit_logger.append(McpAuditEvent(
                    event_type="mcp.tool_blocked",
                    tool="confirm_operation",
                    action=f"confirm_{action.tool}",
                    decision="deny",
                    reason=(
                        f"{action.required_scope} required to confirm "
                        f"{action.tool}"
                    ),
                    error_code="CONFIRM_SCOPE_DENIED",
                ))
            except Exception:
                pass  # audit failure must not change tool behavior
            return tool_error(
                tool="confirm_operation",
                code="CONFIRM_SCOPE_DENIED",
                message=(
                    f"{action.required_scope} scope required to confirm "
                    f"{action.tool}"
                ),
                hint="Request the admin Docker scope to confirm this operation.",
                retryable=False,
                source="docker",
            )

    if not _confirm_store().consume_action(action.action_id):
        return tool_error(
            tool="confirm_operation",
            code="CONFIRM_TOKEN_CONSUMED",
            message="Confirmation token already used",
            hint="Call the dangerous tool again to get a new token.",
            retryable=False,
            source="docker",
        )

    try:
        result = await handler(**action.kwargs)
    except Exception as exc:
        return tool_error(
            tool=action.tool,
            code="DOCKER_COMMAND_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )

    if isinstance(result, dict) and "ok" in result:
        return result

    if isinstance(result, str):
        return tool_success(
            tool=action.tool,
            result={"output": result},
            source="docker",
        )

    return tool_success(
        tool=action.tool,
        result=result,
        source="docker",
    )

async def docker_pending_actions() -> dict[str, Any]:
    """List all pending dangerous Docker operations awaiting confirmation."""
    _confirm_store().cleanup_expired()
    pending = _confirm_store().list_pending()
    count = len(pending)
    return tool_success(
        tool="docker_pending_actions",
        result={"count": count, "items": pending},
        source="docker",
    )

def register_all() -> None:
    for _tool in (
        "docker_ps",
        "docker_images",
        "docker_inspect",
        "docker_logs",
        "docker_stats",
        "docker_compose_ps",
        "docker_compose_services",
        "docker_compose_logs",
        "docker_stop",
        "docker_restart",
        "docker_compose_up",
        "docker_compose_restart",
        "docker_compose_build",
        "docker_rm",
        "docker_compose_down",
        "docker_prune",
        "docker_exec",
        "docker_run",
        "docker_rmi",
        "docker_volume_rm",
        "confirm_operation",
        "docker_pending_actions",
    ):
        register_tool(_tool)(globals()[_tool])
