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

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import subprocess
import time as _time
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from tool_results import tool_error, tool_success, validate_pagination

from examples.mcp_client_remote.fleet.docker_client import (
    RunResult,  # noqa: F401  (used in impl return annotations)
)
from examples.mcp_server.docker_confirm import ConfirmAction, ConfirmStatus
from examples.mcp_server.mcp_audit import McpAuditEvent
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.tool_registry import register_tool


def _docker_client():
    return server_attr("DockerClient")()


def _confirm_store():
    return server_attr("_confirm_store")


def _get_audit_logger():
    return server_attr("get_audit_logger")()


def _resolve_compose_project_dir(project_dir: str | None, client: Any) -> str | None:
    """Resolve a registered project id without exposing its host path.

    Existing callers may still pass an allowed filesystem path. A value that
    names a workspace-registry project is resolved internally to its root and
    revalidated by DockerClient's existing allowed-root policy. Failures for a
    registered project deliberately report only the logical id.
    """
    if project_dir is None:
        return None

    from app.workspace.policy import WorkspacePolicyError

    registry = server_attr("_get_workspace_registry")()
    try:
        project = registry.project_info(project_dir)
    except WorkspacePolicyError:
        return project_dir

    resolved = str(project.get("root") or "")
    if not resolved:
        raise ValueError(f"Registered project {project_dir!r} has no workspace root")
    try:
        client._validate_project_dir(resolved)
    except ValueError:
        raise ValueError(
            f"Registered project {project_dir!r} is unavailable to Docker Compose"
        ) from None
    return resolved


def _resolve_compose_confirmation_target(
    project_dir: str | None,
    client: Any,
) -> tuple[str | None, str | None]:
    """Resolve a Compose target once before creating a dangerous action.

    Registered project ids are converted to their exact allowed root now so a
    later registry remap cannot retarget an already-issued confirmation.  The
    logical id is retained only as a safe display label for summaries/errors.
    """
    resolved = _resolve_compose_project_dir(project_dir, client)
    label = project_dir if project_dir is not None and resolved != project_dir else None
    return resolved, label


def _redact_bound_compose_path(
    message: str | None,
    resolved_dir: str | None,
    label: str | None,
) -> str | None:
    if not message or not resolved_dir or not label:
        return message
    return message.replace(resolved_dir, label)


_DEPLOY_STABILITY_SECONDS = 90
_DEPLOY_HELPER_CONTAINER = "mcp-oauth"
_DEPLOY_CONTRACTS: dict[str, dict[str, str]] = {
    "gpt-browser-bridge": {
        "source_project": "gpt-browser-bridge",
        "infra_project": "infra-quart",
        "script_path": "deploy/deploy-gpt-browser-bridge.sh",
        "state_path": ".gpt-browser-bridge-state/deploy.json",
        "image_env": "GPT_BRIDGE_TARGET_IMAGE",
        "image_repo_env": "GPT_BRIDGE_IMAGE_REPO",
        "compose_project": "infra-quart",
        "profile_volume": "infra-quart_gpt-browser-bridge-profile",
        "logical_volume": "gpt-browser-bridge-profile",
        "verify_container": "infra-quart-gpt-browser-bridge-1",
        "generation_label": "io.xloud.gpt-browser-bridge.deploy-generation",
    }
}

_SITE_AUDIT_SOURCE_PROJECT = "site-audit-platform"
_SITE_AUDIT_SCRIPT_PATH = "deploy-site-audit.sh"
_SITE_AUDIT_STATE_VOLUME = "ssh-gateway-site-audit-deploy-state"
_SITE_AUDIT_VERIFY_STABILITY_SECONDS = 5
_SITE_AUDIT_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("audit-api", "site-audit-api", "site-audit-api"),
    ("audit-crawler-worker", "site-audit-crawler-worker", "site-audit-crawler"),
    ("audit-browser-worker", "site-audit-browser-worker", "site-audit-browser"),
    ("audit-security-worker", "site-audit-security-worker", "site-audit-security"),
    ("audit-performance-worker", "site-audit-performance-worker", "site-audit-performance"),
)
_SITE_AUDIT_PROTECTED_CONTAINERS: tuple[str, ...] = (
    "site-audit-db",
    "site-audit-egress-proxy",
    "site-audit-pdf",
    "site-audit-crm-db",
    "site-audit-crm-web",
    "site-audit-crm-ingress",
    "site-audit-crm-daemon",
)

_XLOUD_SOURCE_PROJECT = "xloud"
_XLOUD_OPERATOR_PROJECT = "site-audit-platform"
_XLOUD_SCRIPT_PATH = "deploy-xloud.sh"
_XLOUD_STATE_VOLUME = "ssh-gateway-xloud-deploy-state"
_XLOUD_VERIFY_STABILITY_SECONDS = 5
_XLOUD_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("site", "xloud-site", "xloud-site"),
    ("intake", "xloud-intake", "xloud-intake"),
)
_XLOUD_REQUIRED_NETWORKS = ("proxmox_macvlan", "site-audit-control")
_XLOUD_AUDIT_API_CONTAINER = "site-audit-api"

_TELEGRAM_STORAGE_SOURCE_PROJECT = "infra-quart"
_TELEGRAM_STORAGE_SCRIPT_PATH = "scripts/deploy-telegram-storage-service.sh"
_TELEGRAM_STORAGE_COMPOSE_PATH = "docker-compose.yml"
_TELEGRAM_STORAGE_CONTAINER = "telegram-storage-service"
_TELEGRAM_STORAGE_STATE_VOLUME = "ssh-gateway-telegram-storage-deploy-state"
_TELEGRAM_STORAGE_VERIFY_STABILITY_SECONDS = 5

_CONTENT_HARVESTER_SOURCE_PROJECT = "content-harvester-service"
_CONTENT_HARVESTER_SCRIPT_PATH = "deploy-content-harvester.sh"
_CONTENT_HARVESTER_STATE_VOLUME = "ssh-gateway-content-harvester-deploy-state"
_CONTENT_HARVESTER_VERIFY_STABILITY_SECONDS = 5
_CONTENT_HARVESTER_TARGETS: tuple[tuple[str, str], ...] = (
    ("api", "content-harvester-api"),
    ("telegram-worker", "content-harvester-telegram-worker"),
    ("telegram-scheduler", "content-harvester-telegram-scheduler"),
    ("backup-worker", "content-harvester-backup-worker"),
)


def _trusted_image_repo(spec: dict[str, str]) -> str:
    """Resolve the trusted image repository for a deploy contract.

    There is deliberately no built-in default: the trusted registry is
    deployment configuration, so a missing or blank value must fail closed
    rather than silently widening the accepted image prefix.
    """
    env_name = spec["image_repo_env"]
    repo = os.environ.get(env_name, "").strip()
    if not repo:
        raise ValueError(
            f"{env_name} must be set to the trusted image repository for "
            f"contract {spec['source_project']!r}"
        )
    return repo


def _git_capture(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("candidate git identity check failed") from exc
    if result.returncode != 0:
        raise RuntimeError("candidate git identity check failed")
    return result.stdout.strip()


def _safe_registered_root(info: dict[str, Any], *, expected_type: str | None = None) -> Path:
    if expected_type is not None and info.get("type") != expected_type:
        raise ValueError(f"registered project must have type {expected_type!r}")
    raw = info.get("root")
    if not isinstance(raw, str) or not raw:
        raise ValueError("registered project root is unavailable")
    try:
        root = Path(raw).resolve(strict=True)
    except OSError as exc:
        raise ValueError("registered project root is unavailable") from exc
    if not root.is_dir():
        raise ValueError("registered project root is unavailable")
    return root


def _read_candidate_metadata(root: Path) -> dict[str, Any]:
    path = root / ".git" / "mcp-candidate-clone.json"
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("candidate metadata is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("candidate metadata is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate metadata is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("candidate metadata is invalid")
    return payload


def _sanitize_deploy_tail(client: Any, text: str) -> str:
    lines = [client._sanitize_string(line) for line in text.splitlines()]
    return "\n".join(lines)[-8192:]


async def _prepare_deploy_contract(
    *,
    contract: str,
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    target_image: str,
) -> dict[str, Any]:
    spec = _DEPLOY_CONTRACTS.get(contract)
    if spec is None:
        raise ValueError("unknown deploy contract")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
        raise ValueError("expected_head_sha must be a 40-character lowercase SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_script_blob_sha):
        raise ValueError("expected_script_blob_sha must be a 40-character lowercase SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_script_sha256):
        raise ValueError("expected_script_sha256 must be a 64-character lowercase SHA-256")
    expected_image_prefix = re.escape(_trusted_image_repo(spec))
    if not re.fullmatch(expected_image_prefix + r"@sha256:[0-9a-f]{64}", target_image):
        raise ValueError("target_image must be an immutable digest for the contract repository")

    registry = server_attr("_get_workspace_registry")()
    candidate_info = registry.project_info(candidate_project)
    candidate_root = _safe_registered_root(candidate_info, expected_type="candidate-clone")
    metadata = _read_candidate_metadata(candidate_root)
    if metadata.get("project_id") != candidate_project:
        raise ValueError("candidate metadata project identity mismatch")
    if metadata.get("source_project") != spec["source_project"]:
        raise ValueError("candidate source project is not authorized for this deploy contract")
    if metadata.get("head") != expected_head_sha or metadata.get("base_sha") != expected_head_sha:
        raise ValueError("candidate metadata is not pinned to the expected deploy head")

    head = _git_capture(candidate_root, "rev-parse", "HEAD").lower()
    if head != expected_head_sha:
        raise ValueError("candidate HEAD changed")
    if _git_capture(candidate_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("candidate workspace must be clean")

    script = candidate_root / spec["script_path"]
    try:
        script_info = script.lstat()
        resolved_script = script.resolve(strict=True)
        resolved_script.relative_to(candidate_root)
    except (OSError, ValueError) as exc:
        raise ValueError("deploy script is unavailable or escapes candidate root") from exc
    if stat.S_ISLNK(script_info.st_mode) or not stat.S_ISREG(script_info.st_mode):
        raise ValueError("deploy script is not a safe regular file")
    blob_sha = _git_capture(candidate_root, "rev-parse", f"{expected_head_sha}:{spec['script_path']}")
    if blob_sha != expected_script_blob_sha:
        raise ValueError("deploy script Git blob identity mismatch")
    raw_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    if raw_sha != expected_script_sha256:
        raise ValueError("deploy script SHA-256 identity mismatch")

    infra_info = registry.project_info(spec["infra_project"])
    infra_root = _safe_registered_root(infra_info)
    state_file = infra_root / spec["state_path"]
    try:
        state_file.resolve(strict=False).relative_to(infra_root)
    except ValueError as exc:
        raise ValueError("deploy state path escapes infra root") from exc

    client = _docker_client()
    target_image_id = await client.image_id(target_image)
    helper_container = os.environ.get("MCP_DEPLOY_HELPER_CONTAINER", _DEPLOY_HELPER_CONTAINER).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", helper_container):
        raise ValueError("MCP_DEPLOY_HELPER_CONTAINER is invalid")
    helper_image_id = await client.container_image_id(helper_container)
    volume = await client.volume_metadata(spec["profile_volume"])
    if volume.get("Name") != spec["profile_volume"] or volume.get("Driver") not in (None, "", "local"):
        raise RuntimeError("deploy profile volume identity mismatch")
    if volume.get("Options") not in (None, {}, []):
        raise RuntimeError("deploy profile volume has unsupported driver options")
    labels = volume.get("Labels")
    if not isinstance(labels, dict):
        raise RuntimeError("deploy profile volume labels are unavailable")
    if labels.get("com.docker.compose.project") != spec["compose_project"]:
        raise RuntimeError("deploy profile volume Compose project mismatch")
    if labels.get("com.docker.compose.volume") != spec["logical_volume"]:
        raise RuntimeError("deploy profile logical volume mismatch")
    mountpoint = volume.get("Mountpoint")
    if not isinstance(mountpoint, str) or not mountpoint.startswith("/"):
        raise RuntimeError("deploy profile volume mountpoint is unavailable")
    if any(ch in mountpoint for ch in ("\x00", "\n", "\r", ",")):
        raise RuntimeError("deploy profile volume mountpoint is unsafe")

    return {
        "spec": spec,
        "candidate_root": str(candidate_root),
        "infra_root": str(infra_root),
        "script_path": str(resolved_script),
        "state_file": str(state_file),
        "target_image": target_image,
        "target_image_id": target_image_id,
        "helper_image_id": helper_image_id,
        "profile_mountpoint": mountpoint,
    }


def _single_inspect(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise RuntimeError("deployment verification returned invalid container metadata")


def _verify_deploy_evidence(
    *,
    spec: dict[str, str],
    inspected: dict[str, Any],
    target_image: str,
    target_image_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    if inspected.get("Image") != target_image_id:
        raise RuntimeError("deployed container image ID does not match target")
    config = inspected.get("Config")
    runtime_state = inspected.get("State")
    if not isinstance(config, dict) or not isinstance(runtime_state, dict):
        raise RuntimeError("deployed container metadata is incomplete")
    if config.get("Image") != target_image:
        raise RuntimeError("deployed container image reference does not match target")
    health = runtime_state.get("Health")
    if not isinstance(health, dict) or health.get("Status") != "healthy":
        raise RuntimeError("deployed container is not Docker-healthy")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise RuntimeError("deployed container labels are unavailable")
    generation = labels.get(spec["generation_label"])
    if not isinstance(generation, str) or not generation:
        raise RuntimeError("deployed container generation label is missing")
    container_id = inspected.get("Id")
    if state.get("gpt_browser_bridge_image") != target_image:
        raise RuntimeError("deployment state image does not match target")
    if state.get("gpt_browser_bridge_image_id") != target_image_id:
        raise RuntimeError("deployment state image ID does not match target")
    if state.get("gpt_browser_bridge_container_id") != container_id:
        raise RuntimeError("deployment state container identity does not match runtime")
    if state.get("gpt_browser_bridge_deploy_generation") != generation:
        raise RuntimeError("deployment state generation does not match runtime")
    fingerprint = state.get("gpt_browser_bridge_compose_config_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise RuntimeError("deployment state Compose fingerprint is invalid")
    return {
        "container_id": container_id,
        "image_id": target_image_id,
        "image": target_image,
        "health": "healthy",
        "restart_count": inspected.get("RestartCount"),
        "started_at": runtime_state.get("StartedAt"),
        "deploy_generation": generation,
        "compose_config_fingerprint": fingerprint,
        "deployed_at": state.get("deployed_at"),
    }


def _docker_inventory_scope(*, truncated: bool) -> dict[str, Any]:
    """Describe the single Docker daemon view without exposing endpoint secrets/topology."""
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    docker_context = os.environ.get("DOCKER_CONTEXT", "").strip()
    if docker_host:
        endpoint_kind = docker_host.partition(":")[0].lower() or "configured"
        endpoint_material = f"host:{docker_host}"
        endpoint_source = "DOCKER_HOST"
    elif docker_context:
        endpoint_kind = "context"
        endpoint_material = f"context:{docker_context}"
        endpoint_source = "DOCKER_CONTEXT"
    else:
        endpoint_kind = "unix"
        endpoint_material = "default:unix:///var/run/docker.sock"
        endpoint_source = "docker_default"

    return {
        "scope": "configured_docker_daemon",
        "endpoint": {
            "kind": endpoint_kind,
            "identity": f"sha256:{hashlib.sha256(endpoint_material.encode('utf-8')).hexdigest()}",
            "source": endpoint_source,
        },
        "daemon_completeness": "truncated" if truncated else "complete_for_query",
        "host_completeness": "unknown",
        "count_semantics": "number_of_rows_returned_from_the_configured_daemon_after_limit",
        "multi_endpoint_selection": {
            "supported": False,
            "reason": "docker_ps is bound to the process-configured Docker endpoint",
        },
    }


async def docker_ps(all: bool = False, limit: int = 50) -> dict[str, Any]:
    """List containers from the configured Docker daemon only.

    Use all=True to include stopped containers. ``count`` is the number of rows
    returned after ``limit``; inventory metadata states daemon/host completeness.
    """
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
        result={
            "containers": rows,
            "count": len(rows),
            "inventory": _docker_inventory_scope(truncated=client.last_truncated),
        },
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
        resolved_project_dir = _resolve_compose_project_dir(project_dir, client)
        rows = await client.compose_ps(project_dir=resolved_project_dir, limit=limit)
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
    client = _docker_client()
    try:
        resolved_project_dir = _resolve_compose_project_dir(project_dir, client)
        result = await client.compose_services(project_dir=resolved_project_dir)
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
    client = _docker_client()
    try:
        resolved_project_dir = _resolve_compose_project_dir(project_dir, client)
        result = await client.compose_logs(
            project_dir=resolved_project_dir,
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
    client = _docker_client()
    try:
        resolved_dir, label = _resolve_compose_confirmation_target(project_dir, client)
    except ValueError as exc:
        return tool_error(
            tool="docker_compose_up",
            code="INVALID_INPUT",
            message=str(exc),
            source="docker",
        )
    svc_list = ", ".join(services) if services else "all services"
    summary = f"Compose up ({svc_list}) in {label or resolved_dir or 'default dir'}"
    kwargs = {
        "project_dir": project_dir,
        "services": services,
        "detach": detach,
        "build": build,
        "timeout": timeout,
    }
    if label:
        kwargs["_resolved_project_dir"] = resolved_dir
        kwargs["_project_label"] = label
    action = _confirm_store().create_action(
        "docker_compose_up",
        kwargs,
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
    client = _docker_client()
    try:
        resolved_dir, label = _resolve_compose_confirmation_target(project_dir, client)
    except ValueError as exc:
        return tool_error(
            tool="docker_compose_restart",
            code="INVALID_INPUT",
            message=str(exc),
            source="docker",
        )
    svc_list = ", ".join(services) if services else "all services"
    summary = f"Compose restart ({svc_list}) in {label or resolved_dir or 'default dir'}"
    kwargs = {"project_dir": project_dir, "services": services, "timeout": timeout}
    if label:
        kwargs["_resolved_project_dir"] = resolved_dir
        kwargs["_project_label"] = label
    action = _confirm_store().create_action(
        "docker_compose_restart",
        kwargs,
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
    client = _docker_client()
    try:
        resolved_dir, label = _resolve_compose_confirmation_target(project_dir, client)
    except ValueError as exc:
        return tool_error(
            tool="docker_compose_build",
            code="INVALID_INPUT",
            message=str(exc),
            source="docker",
        )
    svc_list = ", ".join(services) if services else "all services"
    summary = f"Compose build ({svc_list}) in {label or resolved_dir or 'default dir'}"
    kwargs = {
        "project_dir": project_dir,
        "services": services,
        "no_cache": no_cache,
        "timeout": timeout,
    }
    if label:
        kwargs["_resolved_project_dir"] = resolved_dir
        kwargs["_project_label"] = label
    action = _confirm_store().create_action(
        "docker_compose_build",
        kwargs,
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
    client = _docker_client()
    resolved_project_dir = _resolve_compose_project_dir(project_dir, client)
    return await client.compose_down(
        project_dir=resolved_project_dir,
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
    client = _docker_client()
    resolved_project_dir = _resolve_compose_project_dir(project_dir, client)
    return await client.compose_up(
        project_dir=resolved_project_dir,
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
    client = _docker_client()
    resolved_project_dir = _resolve_compose_project_dir(project_dir, client)
    return await client.compose_restart(
        project_dir=resolved_project_dir,
        services=services,
        timeout=timeout,
    )

async def _docker_compose_build_impl(
    project_dir: str | None = None,
    services: list[str] | None = None,
    no_cache: bool = False,
    timeout: int = 300,
) -> str:
    client = _docker_client()
    resolved_project_dir = _resolve_compose_project_dir(project_dir, client)
    return await client.compose_build(
        project_dir=resolved_project_dir,
        services=services,
        no_cache=no_cache,
        timeout=timeout,
    )

async def _docker_rmi_impl(images: list[str]) -> RunResult:
    return await _docker_client().rmi(images)

async def _docker_volume_rm_impl(volumes: list[str]) -> RunResult:
    return await _docker_client().volume_rm(volumes)


def _site_audit_target_images(
    image_namespace: str,
    source_revision: str,
) -> dict[str, dict[str, str]]:
    namespace = image_namespace.strip().rstrip("/") if isinstance(image_namespace, str) else ""
    if (
        not namespace
        or "@" in namespace
        or "//" in namespace
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", namespace)
    ):
        raise ValueError("image_namespace must be a safe registry/repository namespace")
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("source_revision must be an exact lowercase 40-hex SHA")
    return {
        service: {
            "container": container,
            "image": f"{namespace}/{image_name}:{source_revision}",
        }
        for service, container, image_name in _SITE_AUDIT_TARGETS
    }


def _site_audit_lkg_target_images(
    state: dict[str, Any],
) -> tuple[str, str, dict[str, dict[str, str]]]:
    """Derive the guarded image namespace from an LKG without caller input."""
    source_revision = state.get("source_revision")
    if not isinstance(source_revision, str) or not re.fullmatch(
        r"[0-9a-f]{40}", source_revision
    ):
        raise RuntimeError("Site Audit LKG source revision is missing or malformed")
    deployed = state.get("deployed")
    if not isinstance(deployed, list) or len(deployed) != len(_SITE_AUDIT_TARGETS):
        raise RuntimeError("Site Audit LKG must contain exactly five deployed targets")

    by_service: dict[str, dict[str, Any]] = {}
    for item in deployed:
        if not isinstance(item, dict):
            raise RuntimeError("Site Audit deployed target evidence is malformed")
        service = item.get("service")
        if not isinstance(service, str) or service in by_service:
            raise RuntimeError("Site Audit deployed target service set is malformed")
        by_service[service] = item

    namespaces: set[str] = set()
    for service, _container, image_name in _SITE_AUDIT_TARGETS:
        item = by_service.get(service)
        if item is None:
            raise RuntimeError("Site Audit LKG service set differs from guarded contract")
        image = item.get("image")
        suffix = f"/{image_name}:{source_revision}"
        if not isinstance(image, str) or not image.endswith(suffix):
            raise RuntimeError("Site Audit LKG image reference differs from guarded contract")
        namespace = image[: -len(suffix)]
        if not namespace:
            raise RuntimeError("Site Audit LKG image namespace is missing")
        namespaces.add(namespace)
    if len(namespaces) != 1:
        raise RuntimeError("Site Audit LKG targets do not share one image namespace")

    image_namespace = next(iter(namespaces))
    target_images = _site_audit_target_images(image_namespace, source_revision)
    return source_revision, image_namespace, target_images


def _safe_regular_under(root: Path, relative_path: str, label: str) -> Path:
    path = root / relative_path
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is unavailable or escapes its registered root") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} is not a safe regular file")
    return resolved


def _xloud_target_images(
    image_namespace: str,
    source_revision: str,
) -> dict[str, dict[str, str]]:
    namespace = image_namespace.strip().rstrip("/") if isinstance(image_namespace, str) else ""
    if (
        not namespace
        or "@" in namespace
        or "//" in namespace
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", namespace)
    ):
        raise ValueError("image_namespace must be a safe registry/repository namespace")
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("source_revision must be an exact lowercase 40-hex SHA")
    return {
        role: {
            "container": container,
            "image": f"{namespace}/{image_name}:{source_revision}",
        }
        for role, container, image_name in _XLOUD_TARGETS
    }


def _xloud_secret_fingerprint(env_file: Path) -> str:
    pattern = re.compile(r"^\s*(?:export\s+)?AUDIT_CONTROL_API_KEY\s*=")
    try:
        text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Site Audit operator env file is unreadable") from exc
    assignments = [line for line in text.splitlines() if pattern.match(line)]
    if len(assignments) != 1:
        raise ValueError("Site Audit operator env must define AUDIT_CONTROL_API_KEY exactly once")
    try:
        parsed = dotenv_values(stream=StringIO(assignments[0]), interpolate=False)
    except Exception as exc:
        raise ValueError("Site Audit operator env has an invalid AUDIT_CONTROL_API_KEY assignment") from exc
    value = parsed.get("AUDIT_CONTROL_API_KEY")
    if (
        not isinstance(value, str)
        or not value
        or "${" in value
        or any(ch in value for ch in ("\x00", "\n", "\r"))
    ):
        raise ValueError("Site Audit operator env has an invalid AUDIT_CONTROL_API_KEY assignment")
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _xloud_resource_fingerprint(metadata: dict[str, Any]) -> str:
    stable = {
        key: metadata.get(key)
        for key in (
            "Name",
            "Id",
            "Driver",
            "Scope",
            "Internal",
            "Attachable",
            "Ingress",
            "Options",
            "Labels",
        )
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


async def _xloud_topology_fingerprint(client: Any) -> tuple[str, str]:
    existing: list[str] = []
    for _role, container, _image_name in _XLOUD_TARGETS:
        if await client.container_exists(container):
            existing.append(container)
    if not existing:
        return "absent", "sha256:" + hashlib.sha256(b"absent").hexdigest()
    if len(existing) != len(_XLOUD_TARGETS):
        raise RuntimeError("partial prior XLOUD topology is ambiguous; both guarded roles or none are required")

    evidence: list[dict[str, Any]] = []
    for _role, container, _image_name in _XLOUD_TARGETS:
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        runtime = inspected.get("State")
        config = inspected.get("Config")
        if not isinstance(runtime, dict) or not isinstance(config, dict):
            raise RuntimeError("prior XLOUD target metadata is incomplete")
        if runtime.get("Running") is not True:
            raise RuntimeError("prior XLOUD target is not running")
        image_id = inspected.get("Image")
        config_image = config.get("Image")
        if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise RuntimeError("prior XLOUD image ID is malformed")
        if not isinstance(config_image, str) or not config_image:
            raise RuntimeError("prior XLOUD image reference is unavailable")
        evidence.append(
            {
                "container": container,
                "container_id": inspected.get("Id"),
                "image_id": image_id,
                "image": config_image,
                "restart_count": inspected.get("RestartCount"),
                "started_at": runtime.get("StartedAt"),
            }
        )
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return "present", "sha256:" + hashlib.sha256(raw).hexdigest()


def _xloud_helper_payload(client: Any, run: RunResult) -> tuple[dict[str, Any] | None, str]:
    output_tail = _sanitize_deploy_tail(client, (run.stdout or "") + "\n" + (run.stderr or ""))
    try:
        parsed = json.loads((run.stdout or "").strip())
    except json.JSONDecodeError:
        return None, output_tail
    if not isinstance(parsed, dict) or parsed.get("version") != 1:
        return None, output_tail
    safe_tail = parsed.get("stderr_tail") or parsed.get("stdout_tail") or []
    if isinstance(safe_tail, list):
        output_tail = _sanitize_deploy_tail(client, "\n".join(str(item) for item in safe_tail))
    return parsed, output_tail


def _xloud_validate_lkg(
    state: dict[str, Any],
    *,
    source_revision: str,
    target_images: dict[str, dict[str, str]],
) -> dict[str, Any]:
    if state.get("STATE_VERSION") != "1":
        raise RuntimeError("XLOUD LKG state version is invalid")
    if state.get("SOURCE_REVISION") != source_revision:
        raise RuntimeError("XLOUD LKG source revision mismatch")
    if state.get("SITE_IMAGE") != target_images["site"]["image"]:
        raise RuntimeError("XLOUD LKG site image reference mismatch")
    if state.get("INTAKE_IMAGE") != target_images["intake"]["image"]:
        raise RuntimeError("XLOUD LKG intake image reference mismatch")
    for role, prefix in (("site", "SITE"), ("intake", "INTAKE")):
        image_id = state.get(f"{prefix}_IMAGE_ID")
        container_id = state.get(f"{prefix}_CONTAINER_ID")
        if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise RuntimeError(f"XLOUD LKG {role} image ID is malformed")
        if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise RuntimeError(f"XLOUD LKG {role} container ID is malformed")
    site_ipv4 = state.get("SITE_IPV4")
    try:
        ipaddress.IPv4Address(site_ipv4 or "")
    except ipaddress.AddressValueError as exc:
        raise RuntimeError("XLOUD LKG site IPv4 is invalid") from exc
    try:
        stability = int(str(state.get("STABILITY_SECONDS") or ""))
    except ValueError as exc:
        raise RuntimeError("XLOUD LKG stability evidence is malformed") from exc
    if stability < 90:
        raise RuntimeError("XLOUD LKG stability evidence is below 90 seconds")
    return state


async def _verify_xloud_live(
    client: Any,
    *,
    state: dict[str, Any],
    source_revision: str,
    target_images: dict[str, dict[str, str]],
    image_ids: dict[str, str],
) -> dict[str, Any]:
    state = _xloud_validate_lkg(
        state,
        source_revision=source_revision,
        target_images=target_images,
    )
    if state.get("SITE_IMAGE_ID") != image_ids.get("site"):
        raise RuntimeError("XLOUD LKG site image ID differs from confirmed image")
    if state.get("INTAKE_IMAGE_ID") != image_ids.get("intake"):
        raise RuntimeError("XLOUD LKG intake image ID differs from confirmed image")
    live: dict[str, dict[str, Any]] = {}
    for role, container, _image_name in _XLOUD_TARGETS:
        prefix = role.upper()
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        runtime = inspected.get("State")
        config = inspected.get("Config")
        if not isinstance(runtime, dict) or not isinstance(config, dict):
            raise RuntimeError("XLOUD target metadata is incomplete")
        networks = await client.container_network_snapshot(container)
        health = runtime.get("Health")
        labels = config.get("Labels")
        if inspected.get("Id") != state[f"{prefix}_CONTAINER_ID"]:
            raise RuntimeError("XLOUD target container identity differs from LKG")
        if inspected.get("Image") != state[f"{prefix}_IMAGE_ID"]:
            raise RuntimeError("XLOUD target image ID differs from LKG")
        if config.get("Image") != target_images[role]["image"]:
            raise RuntimeError("XLOUD target image reference differs from guarded set")
        if inspected.get("RestartCount") != 0:
            raise RuntimeError("XLOUD target restarted after rollout")
        if not isinstance(health, dict) or health.get("Status") != "healthy":
            raise RuntimeError("XLOUD target is not Docker-healthy")
        if not isinstance(labels, dict):
            raise RuntimeError("XLOUD target labels are unavailable")
        if labels.get("org.opencontainers.image.revision") != source_revision:
            raise RuntimeError("XLOUD target OCI revision differs from source revision")
        if labels.get("io.xloud.role") != role:
            raise RuntimeError("XLOUD target role label mismatch")
        if "xloud_default" not in networks:
            raise RuntimeError("XLOUD target is not attached to the default network")
        if role == "site":
            if networks.get("proxmox_macvlan") != state.get("SITE_IPV4"):
                raise RuntimeError("XLOUD site macvlan identity differs from pinned LKG")
        elif "site-audit-control" not in networks:
            raise RuntimeError("XLOUD intake is not attached to the Site Audit control network")
        live[container] = {
            "container_id": inspected.get("Id"),
            "image_id": inspected.get("Image"),
            "image": config.get("Image"),
            "restart_count": inspected.get("RestartCount"),
            "started_at": runtime.get("StartedAt"),
            "health": health.get("Status"),
            "revision": labels.get("org.opencontainers.image.revision"),
            "role": labels.get("io.xloud.role"),
        }
    return live


async def _prepare_xloud_deploy(
    *,
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    source_revision: str,
    image_namespace: str,
    require_stable_topology: bool = True,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
        raise ValueError("expected_head_sha must be a 40-character lowercase SHA")
    if source_revision != expected_head_sha:
        raise ValueError("source_revision must exactly match expected_head_sha")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_script_blob_sha):
        raise ValueError("expected_script_blob_sha must be a 40-character lowercase SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_script_sha256):
        raise ValueError("expected_script_sha256 must be a 64-character lowercase SHA-256")
    target_images = _xloud_target_images(image_namespace, source_revision)

    registry = server_attr("_get_workspace_registry")()
    candidate_info = registry.project_info(candidate_project)
    candidate_root = _safe_registered_root(candidate_info, expected_type="candidate-clone")
    metadata = _read_candidate_metadata(candidate_root)
    if metadata.get("project_id") != candidate_project:
        raise ValueError("candidate metadata project identity mismatch")
    if metadata.get("source_project") != _XLOUD_SOURCE_PROJECT:
        raise ValueError("candidate source project is not authorized for XLOUD deployment")
    if metadata.get("head") != expected_head_sha or metadata.get("base_sha") != expected_head_sha:
        raise ValueError("candidate metadata is not pinned to the expected XLOUD head")
    if _git_capture(candidate_root, "rev-parse", "HEAD").lower() != expected_head_sha:
        raise ValueError("candidate HEAD changed")
    if _git_capture(candidate_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("candidate workspace must be clean")

    script = _safe_regular_under(candidate_root, _XLOUD_SCRIPT_PATH, "XLOUD deploy script")
    blob_sha = _git_capture(candidate_root, "rev-parse", f"{expected_head_sha}:{_XLOUD_SCRIPT_PATH}")
    if blob_sha != expected_script_blob_sha:
        raise ValueError("XLOUD deploy script Git blob identity mismatch")
    if hashlib.sha256(script.read_bytes()).hexdigest() != expected_script_sha256:
        raise ValueError("XLOUD deploy script SHA-256 identity mismatch")

    operator_info = registry.project_info(_XLOUD_OPERATOR_PROJECT)
    operator_root = _safe_registered_root(operator_info)
    client = _docker_client()
    client._validate_project_dir(str(operator_root))
    env_file = _safe_regular_under(operator_root, ".env", "Site Audit operator env file")
    secret_fingerprint = _xloud_secret_fingerprint(env_file)

    state_volume = await client.volume_metadata(_XLOUD_STATE_VOLUME)
    if state_volume.get("Name") != _XLOUD_STATE_VOLUME:
        raise RuntimeError("XLOUD deploy-state volume identity mismatch")
    if state_volume.get("Driver") not in (None, "", "local") or state_volume.get("Options") not in (
        None,
        {},
        [],
    ):
        raise RuntimeError("XLOUD deploy-state volume is not a plain local volume")
    state_volume_fingerprint = _xloud_resource_fingerprint(state_volume)

    network_fingerprints: dict[str, str] = {}
    for network_name in _XLOUD_REQUIRED_NETWORKS:
        network = await client.network_metadata(network_name)
        if network.get("Name") != network_name:
            raise RuntimeError(f"required XLOUD network {network_name} identity mismatch")
        network_fingerprints[network_name] = _xloud_resource_fingerprint(network)

    audit = _single_inspect(await client.inspect(_XLOUD_AUDIT_API_CONTAINER, max_lines=10))
    audit_runtime = audit.get("State")
    if not isinstance(audit_runtime, dict):
        raise RuntimeError("Site Audit API runtime metadata is incomplete")
    audit_health = audit_runtime.get("Health")
    if not isinstance(audit_health, dict) or audit_health.get("Status") != "healthy":
        raise RuntimeError("Site Audit API is not Docker-healthy")
    audit_snapshot = {
        "container_id": audit.get("Id"),
        "started_at": audit_runtime.get("StartedAt"),
        "image_id": audit.get("Image"),
    }
    if not all(isinstance(value, str) and value for value in audit_snapshot.values()):
        raise RuntimeError("Site Audit API identity evidence is incomplete")

    image_ids: dict[str, str] = {}
    for role in ("site", "intake"):
        image = target_images[role]["image"]
        image_id = await client.image_id(image)
        if await client.image_revision(image_id) != source_revision:
            raise RuntimeError(f"XLOUD {role} image OCI revision differs from source revision")
        image_ids[role] = image_id
    if image_ids["site"] == image_ids["intake"]:
        raise RuntimeError("XLOUD site and intake image IDs must differ")

    helper_container = os.environ.get("MCP_DEPLOY_HELPER_CONTAINER", _DEPLOY_HELPER_CONTAINER).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", helper_container):
        raise ValueError("MCP_DEPLOY_HELPER_CONTAINER is invalid")
    helper_image_id = await client.container_image_id(helper_container)
    if require_stable_topology:
        prior_mode, topology_fingerprint = await _xloud_topology_fingerprint(client)
    else:
        prior_mode, topology_fingerprint = "recovery", ""

    plan = {
        "candidate_root": str(candidate_root),
        "operator_root": str(operator_root),
        "script_path": str(script),
        "env_file": str(env_file),
        "state_volume": _XLOUD_STATE_VOLUME,
        "state_volume_fingerprint": state_volume_fingerprint,
        "network_fingerprints": network_fingerprints,
        "audit_snapshot": audit_snapshot,
        "secret_fingerprint": secret_fingerprint,
        "helper_image_id": helper_image_id,
        "source_revision": source_revision,
        "image_namespace": image_namespace.strip().rstrip("/"),
        "target_images": target_images,
        "image_ids": image_ids,
        "prior_mode": prior_mode,
        "topology_fingerprint": topology_fingerprint,
    }
    if require_stable_topology:
        preflight = await client.run_xloud_deploy_contract_helper(
            helper_image_id=helper_image_id,
            candidate_root=str(candidate_root),
            operator_root=str(operator_root),
            script_path=str(script),
            env_file=str(env_file),
            state_volume=_XLOUD_STATE_VOLUME,
            site_image=target_images["site"]["image"],
            intake_image=target_images["intake"]["image"],
            site_image_id=image_ids["site"],
            intake_image_id=image_ids["intake"],
            source_revision=source_revision,
            secret_fingerprint=secret_fingerprint,
            mode="preflight",
            stability_seconds=90,
            timeout=180,
        )
        payload, output_tail = _xloud_helper_payload(client, preflight)
        if (
            preflight.exit_code != 0
            or payload is None
            or payload.get("exit_code") != 0
            or payload.get("pending_exists") is not False
        ):
            raise RuntimeError(f"XLOUD repo-owned preflight failed: {output_tail}")
    return plan


async def _verify_xloud_guard_resources(client: Any, plan: dict[str, Any]) -> None:
    state_volume = await client.volume_metadata(plan["state_volume"])
    if _xloud_resource_fingerprint(state_volume) != plan["state_volume_fingerprint"]:
        raise RuntimeError("XLOUD deploy-state volume changed during rollout")
    for network_name, fingerprint in plan["network_fingerprints"].items():
        network = await client.network_metadata(network_name)
        if _xloud_resource_fingerprint(network) != fingerprint:
            raise RuntimeError(f"XLOUD required network {network_name} changed during rollout")
    audit = _single_inspect(await client.inspect(_XLOUD_AUDIT_API_CONTAINER, max_lines=10))
    runtime = audit.get("State")
    if not isinstance(runtime, dict):
        raise RuntimeError("Site Audit API runtime metadata is incomplete")
    health = runtime.get("Health")
    if not isinstance(health, dict) or health.get("Status") != "healthy":
        raise RuntimeError("Site Audit API is not Docker-healthy")
    current = {
        "container_id": audit.get("Id"),
        "started_at": runtime.get("StartedAt"),
        "image_id": audit.get("Image"),
    }
    if current != plan["audit_snapshot"]:
        raise RuntimeError("Site Audit API identity changed during XLOUD rollout")


async def _prepare_site_audit_deploy(
    *,
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    source_revision: str,
    image_namespace: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
        raise ValueError("expected_head_sha must be a 40-character lowercase SHA")
    if source_revision != expected_head_sha:
        raise ValueError("source_revision must exactly match expected_head_sha")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_script_blob_sha):
        raise ValueError("expected_script_blob_sha must be a 40-character lowercase SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_script_sha256):
        raise ValueError("expected_script_sha256 must be a 64-character lowercase SHA-256")
    target_images = _site_audit_target_images(image_namespace, source_revision)

    registry = server_attr("_get_workspace_registry")()
    candidate_info = registry.project_info(candidate_project)
    candidate_root = _safe_registered_root(candidate_info, expected_type="candidate-clone")
    metadata = _read_candidate_metadata(candidate_root)
    if metadata.get("project_id") != candidate_project:
        raise ValueError("candidate metadata project identity mismatch")
    if metadata.get("source_project") != _SITE_AUDIT_SOURCE_PROJECT:
        raise ValueError("candidate source project is not authorized for Site Audit deployment")
    if metadata.get("head") != expected_head_sha or metadata.get("base_sha") != expected_head_sha:
        raise ValueError("candidate metadata is not pinned to the expected Site Audit head")
    if _git_capture(candidate_root, "rev-parse", "HEAD").lower() != expected_head_sha:
        raise ValueError("candidate HEAD changed")
    if _git_capture(candidate_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("candidate workspace must be clean")

    script = _safe_regular_under(candidate_root, _SITE_AUDIT_SCRIPT_PATH, "Site Audit deploy script")
    blob_sha = _git_capture(candidate_root, "rev-parse", f"{expected_head_sha}:{_SITE_AUDIT_SCRIPT_PATH}")
    if blob_sha != expected_script_blob_sha:
        raise ValueError("Site Audit deploy script Git blob identity mismatch")
    if hashlib.sha256(script.read_bytes()).hexdigest() != expected_script_sha256:
        raise ValueError("Site Audit deploy script SHA-256 identity mismatch")

    operator_info = registry.project_info(_SITE_AUDIT_SOURCE_PROJECT)
    operator_root = _safe_registered_root(operator_info)
    client = _docker_client()
    client._validate_project_dir(str(operator_root))
    env_file = _safe_regular_under(operator_root, ".env", "Site Audit operator env file")

    state_volume = await client.volume_metadata(_SITE_AUDIT_STATE_VOLUME)
    if state_volume.get("Name") != _SITE_AUDIT_STATE_VOLUME:
        raise RuntimeError("Site Audit deploy-state volume identity mismatch")
    if state_volume.get("Driver") not in (None, "", "local"):
        raise RuntimeError("Site Audit deploy-state volume driver is unsupported")
    if state_volume.get("Options") not in (None, {}, []):
        raise RuntimeError("Site Audit deploy-state volume has unsupported driver options")

    helper_container = os.environ.get("MCP_DEPLOY_HELPER_CONTAINER", _DEPLOY_HELPER_CONTAINER).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", helper_container):
        raise ValueError("MCP_DEPLOY_HELPER_CONTAINER is invalid")
    helper_image_id = await client.container_image_id(helper_container)

    return {
        "candidate_root": str(candidate_root),
        "operator_root": str(operator_root),
        "script_path": str(script),
        "env_file": str(env_file),
        "state_volume": _SITE_AUDIT_STATE_VOLUME,
        "helper_image_id": helper_image_id,
        "source_revision": source_revision,
        "image_namespace": image_namespace.strip().rstrip("/"),
        "target_images": target_images,
    }


def _site_audit_state_maps(
    state: dict[str, Any],
    *,
    source_revision: str,
    target_images: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if state.get("schema_version") != 1:
        raise RuntimeError("Site Audit deployment state schema_version is invalid")
    if state.get("source_revision") != source_revision:
        raise RuntimeError("Site Audit deployment state source revision mismatch")
    generation = state.get("deploy_generation")
    if not isinstance(generation, str) or not re.fullmatch(
        r"[0-9a-f]{12}-[0-9]{8}T[0-9]{6}Z", generation
    ):
        raise RuntimeError("Site Audit deployment generation is malformed")
    stability = state.get("stability_seconds")
    if not isinstance(stability, int) or isinstance(stability, bool) or not 1 <= stability <= 600:
        raise RuntimeError("Site Audit deployment stability evidence is malformed")
    if state.get("crm_route_surface_verified") is not True:
        raise RuntimeError("Site Audit CRM route-surface evidence is missing")

    deployed = state.get("deployed")
    previous = state.get("previous")
    protected = state.get("protected_containers_unchanged")
    if not isinstance(deployed, list) or len(deployed) != len(_SITE_AUDIT_TARGETS):
        raise RuntimeError("Site Audit LKG must contain exactly five deployed targets")
    if not isinstance(previous, list) or len(previous) != len(_SITE_AUDIT_TARGETS):
        raise RuntimeError("Site Audit LKG must contain exactly five rollback targets")
    if not isinstance(protected, list) or len(protected) != len(_SITE_AUDIT_PROTECTED_CONTAINERS):
        raise RuntimeError("Site Audit protected-container evidence is incomplete")

    deployed_by_service: dict[str, dict[str, Any]] = {}
    for item in deployed:
        if not isinstance(item, dict):
            raise RuntimeError("Site Audit deployed target evidence is malformed")
        raw_service = item.get("service")
        if not isinstance(raw_service, str) or raw_service in deployed_by_service:
            raise RuntimeError("Site Audit deployed target service set is malformed")
        deployed_by_service[raw_service] = item

    previous_by_service: dict[str, dict[str, Any]] = {}
    for item in previous:
        if not isinstance(item, dict):
            raise RuntimeError("Site Audit rollback target evidence is malformed")
        raw_service = item.get("service")
        if not isinstance(raw_service, str) or raw_service in previous_by_service:
            raise RuntimeError("Site Audit rollback target service set is malformed")
        previous_by_service[raw_service] = item

    expected_services = {service for service, _container, _image in _SITE_AUDIT_TARGETS}
    if set(deployed_by_service) != expected_services or set(previous_by_service) != expected_services:
        raise RuntimeError("Site Audit target service set does not match the guarded contract")

    for service, container, _image_name in _SITE_AUDIT_TARGETS:
        item = deployed_by_service[service]
        expected = target_images[service]
        if item.get("container") != container or item.get("image") != expected["image"]:
            raise RuntimeError("Site Audit deployed image/container set changed")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("image_id") or "")):
            raise RuntimeError("Site Audit deployed image ID is malformed")
        for field in ("container_id", "started_at"):
            value = item.get(field)
            if not isinstance(value, str) or not value:
                raise RuntimeError(f"Site Audit deployed {field} evidence is malformed")
        restart_count = item.get("restart_count")
        if not isinstance(restart_count, int) or isinstance(restart_count, bool) or restart_count != 0:
            raise RuntimeError("Site Audit deployed target restart evidence is not zero")

        old = previous_by_service[service]
        if old.get("container") != container:
            raise RuntimeError("Site Audit rollback container mapping changed")
        for field in ("image_id", "rollback_ref", "config_image", "container_id"):
            value = old.get(field)
            if not isinstance(value, str) or not value or "\t" in value or "\n" in value:
                raise RuntimeError(f"Site Audit rollback {field} evidence is malformed")
        old_restart = old.get("restart_count")
        if not isinstance(old_restart, int) or isinstance(old_restart, bool) or old_restart < 0:
            raise RuntimeError("Site Audit rollback restart evidence is malformed")

    protected_by_name: dict[str, dict[str, Any]] = {}
    for item in protected:
        if not isinstance(item, dict):
            raise RuntimeError("Site Audit protected-container evidence is malformed")
        raw_container = item.get("container")
        if not isinstance(raw_container, str) or raw_container in protected_by_name:
            raise RuntimeError("Site Audit protected-container set is malformed")
        for field in ("container_id", "started_at"):
            value = item.get(field)
            if not isinstance(value, str) or not value:
                raise RuntimeError(f"Site Audit protected {field} evidence is malformed")
        protected_by_name[raw_container] = item
    if set(protected_by_name) != set(_SITE_AUDIT_PROTECTED_CONTAINERS):
        raise RuntimeError("Site Audit protected-container set changed")
    return deployed_by_service, protected_by_name


async def _verify_site_audit_live(
    client: Any,
    *,
    state: dict[str, Any],
    source_revision: str,
    target_images: dict[str, dict[str, str]],
) -> dict[str, Any]:
    deployed, protected = _site_audit_state_maps(
        state,
        source_revision=source_revision,
        target_images=target_images,
    )
    target_snapshot: dict[str, dict[str, Any]] = {}
    for service, container, _image_name in _SITE_AUDIT_TARGETS:
        item = deployed[service]
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        config = inspected.get("Config")
        runtime = inspected.get("State")
        if not isinstance(config, dict) or not isinstance(runtime, dict):
            raise RuntimeError("Site Audit target container metadata is incomplete")
        health = runtime.get("Health")
        labels = config.get("Labels")
        if inspected.get("Id") != item["container_id"]:
            raise RuntimeError("Site Audit target container identity changed")
        if inspected.get("Image") != item["image_id"]:
            raise RuntimeError("Site Audit target live image ID differs from LKG")
        if config.get("Image") != target_images[service]["image"]:
            raise RuntimeError("Site Audit target live image reference differs from guarded set")
        if inspected.get("RestartCount") != 0:
            raise RuntimeError("Site Audit target restarted after rollout")
        if runtime.get("StartedAt") != item["started_at"]:
            raise RuntimeError("Site Audit target StartedAt differs from LKG")
        if not isinstance(health, dict) or health.get("Status") != "healthy":
            raise RuntimeError("Site Audit target is not Docker-healthy")
        if not isinstance(labels, dict) or labels.get("org.opencontainers.image.revision") != source_revision:
            raise RuntimeError("Site Audit target OCI revision differs from exact source revision")
        target_snapshot[container] = {
            "container_id": inspected.get("Id"),
            "image_id": inspected.get("Image"),
            "image": config.get("Image"),
            "restart_count": inspected.get("RestartCount"),
            "started_at": runtime.get("StartedAt"),
            "health": health.get("Status"),
            "revision": labels.get("org.opencontainers.image.revision"),
        }

    protected_snapshot: dict[str, dict[str, Any]] = {}
    for container in _SITE_AUDIT_PROTECTED_CONTAINERS:
        item = protected[container]
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        runtime = inspected.get("State")
        if not isinstance(runtime, dict):
            raise RuntimeError("Site Audit protected container metadata is incomplete")
        if inspected.get("Id") != item["container_id"] or runtime.get("StartedAt") != item["started_at"]:
            raise RuntimeError("Site Audit protected dependency/CRM identity changed")
        protected_snapshot[container] = {
            "container_id": inspected.get("Id"),
            "started_at": runtime.get("StartedAt"),
        }
    return {"targets": target_snapshot, "protected": protected_snapshot}


def _site_audit_pending_maps(
    pending: dict[str, Any],
    *,
    source_revision: str,
    target_images: dict[str, dict[str, str]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    if pending.get("schema_version") != 1 or pending.get("status") != "pending":
        raise RuntimeError("Site Audit pending evidence schema/status is invalid")
    if pending.get("source_revision") != source_revision:
        raise RuntimeError("Site Audit pending evidence source revision mismatch")
    generation = pending.get("deploy_generation")
    if not isinstance(generation, str) or not re.fullmatch(
        r"[0-9a-f]{12}-[0-9]{8}T[0-9]{6}Z", generation
    ):
        raise RuntimeError("Site Audit pending deploy generation is malformed")

    previous = pending.get("previous")
    target = pending.get("target")
    protected = pending.get("protected_before")
    if not isinstance(previous, list) or len(previous) != len(_SITE_AUDIT_TARGETS):
        raise RuntimeError("Site Audit pending rollback evidence must contain exactly five targets")
    if not isinstance(target, list) or len(target) != len(_SITE_AUDIT_TARGETS):
        raise RuntimeError("Site Audit pending target evidence must contain exactly five targets")
    if not isinstance(protected, list) or len(protected) != len(_SITE_AUDIT_PROTECTED_CONTAINERS):
        raise RuntimeError("Site Audit pending protected-container evidence is incomplete")

    previous_by_service: dict[str, dict[str, Any]] = {}
    target_by_service: dict[str, dict[str, Any]] = {}
    protected_by_name: dict[str, dict[str, Any]] = {}
    for item in previous:
        if not isinstance(item, dict):
            raise RuntimeError("Site Audit pending rollback target evidence is malformed")
        service = item.get("service")
        if not isinstance(service, str) or service in previous_by_service:
            raise RuntimeError("Site Audit pending rollback service set is malformed")
        previous_by_service[service] = item
    for item in target:
        if not isinstance(item, dict):
            raise RuntimeError("Site Audit pending target evidence is malformed")
        service = item.get("service")
        if not isinstance(service, str) or service in target_by_service:
            raise RuntimeError("Site Audit pending target service set is malformed")
        target_by_service[service] = item
    for item in protected:
        if not isinstance(item, dict):
            raise RuntimeError("Site Audit pending protected evidence is malformed")
        container = item.get("container")
        if not isinstance(container, str) or container in protected_by_name:
            raise RuntimeError("Site Audit pending protected-container set is malformed")
        protected_by_name[container] = item

    expected_services = {service for service, _container, _image in _SITE_AUDIT_TARGETS}
    if set(previous_by_service) != expected_services or set(target_by_service) != expected_services:
        raise RuntimeError("Site Audit pending service set differs from guarded contract")
    if set(protected_by_name) != set(_SITE_AUDIT_PROTECTED_CONTAINERS):
        raise RuntimeError("Site Audit pending protected-container set changed")

    for service, container, _image_name in _SITE_AUDIT_TARGETS:
        old = previous_by_service[service]
        tgt = target_by_service[service]
        if old.get("container") != container or tgt.get("container") != container:
            raise RuntimeError("Site Audit pending container mapping changed")
        if tgt.get("image") != target_images[service]["image"]:
            raise RuntimeError("Site Audit pending target image set changed")
        for item, fields, label in (
            (old, ("image_id", "rollback_ref", "config_image", "container_id"), "rollback"),
            (tgt, ("image_id",), "target"),
        ):
            for field in fields:
                value = item.get(field)
                if not isinstance(value, str) or not value or "\t" in value or "\n" in value:
                    raise RuntimeError(f"Site Audit pending {label} {field} evidence is malformed")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", old["image_id"]):
            raise RuntimeError("Site Audit pending rollback image ID is malformed")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", tgt["image_id"]):
            raise RuntimeError("Site Audit pending target image ID is malformed")
        restart_count = old.get("restart_count")
        if not isinstance(restart_count, int) or isinstance(restart_count, bool) or restart_count < 0:
            raise RuntimeError("Site Audit pending rollback restart evidence is malformed")

    for container in _SITE_AUDIT_PROTECTED_CONTAINERS:
        item = protected_by_name[container]
        for field in ("container_id", "started_at"):
            value = item.get(field)
            if not isinstance(value, str) or not value or "\t" in value or "\n" in value:
                raise RuntimeError(f"Site Audit pending protected {field} evidence is malformed")
    return previous_by_service, target_by_service, protected_by_name


async def _verify_site_audit_recovery(
    client: Any,
    *,
    pending: dict[str, Any],
    source_revision: str,
    target_images: dict[str, dict[str, str]],
) -> dict[str, Any]:
    previous, _target, protected = _site_audit_pending_maps(
        pending,
        source_revision=source_revision,
        target_images=target_images,
    )
    pre_mutation = True
    rolled_back = True
    live_targets: dict[str, dict[str, Any]] = {}
    for service, container, _image_name in _SITE_AUDIT_TARGETS:
        old = previous[service]
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        config = inspected.get("Config")
        runtime = inspected.get("State")
        if not isinstance(config, dict) or not isinstance(runtime, dict):
            raise RuntimeError("Site Audit recovery target metadata is incomplete")
        health = runtime.get("Health")
        if not isinstance(health, dict) or health.get("Status") != "healthy":
            raise RuntimeError("Site Audit recovery target is not Docker-healthy")
        if inspected.get("Image") != old["image_id"]:
            pre_mutation = False
            rolled_back = False
        if (
            inspected.get("Id") != old["container_id"]
            or config.get("Image") != old["config_image"]
            or inspected.get("RestartCount") != old["restart_count"]
        ):
            pre_mutation = False
        if config.get("Image") != old["rollback_ref"] or inspected.get("RestartCount") != 0:
            rolled_back = False
        live_targets[container] = {
            "container_id": inspected.get("Id"),
            "image_id": inspected.get("Image"),
            "image": config.get("Image"),
            "restart_count": inspected.get("RestartCount"),
            "health": health.get("Status"),
        }

    live_protected: dict[str, dict[str, Any]] = {}
    for container in _SITE_AUDIT_PROTECTED_CONTAINERS:
        expected = protected[container]
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        runtime = inspected.get("State")
        if not isinstance(runtime, dict):
            raise RuntimeError("Site Audit recovery protected metadata is incomplete")
        if inspected.get("Id") != expected["container_id"] or runtime.get("StartedAt") != expected["started_at"]:
            raise RuntimeError("Site Audit protected dependency/CRM identity changed during recovery")
        live_protected[container] = {
            "container_id": inspected.get("Id"),
            "started_at": runtime.get("StartedAt"),
        }

    if pre_mutation:
        outcome = "pre_mutation_restored"
    elif rolled_back:
        outcome = "rolled_back_to_previous_images"
    else:
        raise RuntimeError("Site Audit recovery result is neither pre-mutation nor proven rollback")
    return {
        "outcome": outcome,
        "targets": live_targets,
        "protected": live_protected,
        "deploy_generation": pending["deploy_generation"],
    }


def _site_audit_helper_payload(client: Any, run: RunResult) -> tuple[dict[str, Any] | None, str]:
    output_tail = _sanitize_deploy_tail(client, (run.stdout or "") + "\n" + (run.stderr or ""))
    try:
        parsed = json.loads((run.stdout or "").strip())
    except json.JSONDecodeError:
        return None, output_tail
    return (parsed if isinstance(parsed, dict) else None), output_tail


async def _accept_site_audit_payload(
    client: Any,
    *,
    payload: dict[str, Any],
    source_revision: str,
    target_images: dict[str, dict[str, str]],
) -> dict[str, Any]:
    if payload.get("pending_exists") is not False:
        raise RuntimeError("Site Audit pending transaction evidence remains after successful helper exit")
    state = payload.get("state")
    if not isinstance(state, dict):
        raise RuntimeError("Site Audit helper returned malformed LKG state")
    first = await _verify_site_audit_live(
        client,
        state=state,
        source_revision=source_revision,
        target_images=target_images,
    )
    await asyncio.sleep(_SITE_AUDIT_VERIFY_STABILITY_SECONDS)
    second = await _verify_site_audit_live(
        client,
        state=state,
        source_revision=source_revision,
        target_images=target_images,
    )
    if first != second:
        raise RuntimeError("Site Audit deployment changed during Gateway stability verification")
    return {"state": state, "live": second}


def _telegram_storage_resource_fingerprint(metadata: dict[str, Any]) -> str:
    stable = {
        key: metadata.get(key)
        for key in ("Name", "Driver", "Scope", "Options", "Labels")
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _telegram_storage_snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _telegram_storage_validate_operator_env(env_file: Path) -> None:
    required = (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_STORAGE_API_TOKEN",
        "TELEGRAM_STORAGE_CHAT_ID",
    )
    required_pattern = re.compile(
        r"^\s*(?:export\s+)?(TELEGRAM_BOT_TOKEN|TELEGRAM_STORAGE_API_TOKEN|TELEGRAM_STORAGE_CHAT_ID)\s*="
    )
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("Telegram Storage operator env file is unreadable") from exc

    assignments: dict[str, list[str]] = {key: [] for key in required}
    for line in lines:
        match = required_pattern.match(line)
        if match is not None:
            assignments[match.group(1)].append(line)

    for key in required:
        matches = assignments[key]
        if len(matches) != 1:
            raise ValueError(
                f"Telegram Storage operator env must define {key} exactly once"
            )
        try:
            parsed = dotenv_values(stream=StringIO(matches[0]), interpolate=False)
        except Exception as exc:
            raise ValueError(
                f"Telegram Storage operator env has an invalid {key} assignment"
            ) from exc
        value = parsed.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Telegram Storage operator env has an invalid {key} assignment"
            )
        if key == "TELEGRAM_STORAGE_CHAT_ID" and value.strip() == "0":
            raise ValueError(
                "Telegram Storage operator env has an invalid TELEGRAM_STORAGE_CHAT_ID assignment"
            )


async def _telegram_storage_read_state(
    client: Any,
    *,
    helper_image_id: str,
    state_volume: str,
) -> tuple[dict[str, Any], str]:
    run = await client.read_telegram_storage_deploy_state(
        helper_image_id=helper_image_id,
        state_volume=state_volume,
    )
    if run.exit_code != 0:
        raise RuntimeError("Telegram Storage deploy state could not be read safely")
    try:
        payload = json.loads((run.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("Telegram Storage deploy-state reader returned invalid evidence") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("Telegram Storage deploy-state reader schema is invalid")
    state_exists = payload.get("state_exists")
    state = payload.get("state")
    if not isinstance(state_exists, bool) or not isinstance(state, dict):
        raise RuntimeError("Telegram Storage deploy-state reader evidence is malformed")
    normalized: dict[str, Any] = {"state_exists": state_exists, "state": {}}
    if state_exists:
        image = state.get("telegram_storage_service_image")
        deployed_at = state.get("deployed_at")
        if not isinstance(image, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", image
        ):
            raise RuntimeError("Telegram Storage LKG image reference is malformed")
        if not isinstance(deployed_at, str) or not deployed_at.strip():
            raise RuntimeError("Telegram Storage LKG deployed_at evidence is malformed")
        normalized["state"] = {
            "telegram_storage_service_image": image,
            "deployed_at": deployed_at,
        }
    elif state:
        raise RuntimeError("Telegram Storage absent deploy state contains unexpected evidence")
    return normalized, _telegram_storage_snapshot_fingerprint(normalized)


async def _telegram_storage_runtime_snapshot(client: Any) -> tuple[str, dict[str, Any], str]:
    if not await client.container_exists(_TELEGRAM_STORAGE_CONTAINER):
        snapshot: dict[str, Any] = {"mode": "absent"}
        return "absent", snapshot, _telegram_storage_snapshot_fingerprint(snapshot)
    inspected = _single_inspect(await client.inspect(_TELEGRAM_STORAGE_CONTAINER, max_lines=10))
    runtime = inspected.get("State")
    config = inspected.get("Config")
    if not isinstance(runtime, dict) or not isinstance(config, dict):
        raise RuntimeError("Telegram Storage runtime metadata is incomplete")
    if runtime.get("Running") is not True:
        raise RuntimeError("Telegram Storage prior container is not running")
    image_id = inspected.get("Image")
    config_image = config.get("Image")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("Telegram Storage prior image ID is malformed")
    if not isinstance(config_image, str) or not config_image:
        raise RuntimeError("Telegram Storage prior image reference is unavailable")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise RuntimeError("Telegram Storage Compose labels are unavailable")
    if labels.get("com.docker.compose.project") != "infra-quart":
        raise RuntimeError("Telegram Storage Compose project identity mismatch")
    if labels.get("com.docker.compose.service") != "telegram-storage-service":
        raise RuntimeError("Telegram Storage Compose service identity mismatch")
    config_hash = labels.get("com.docker.compose.config-hash")
    if not isinstance(config_hash, str) or not config_hash:
        raise RuntimeError("Telegram Storage Compose config hash is unavailable")
    health = runtime.get("Health")
    health_status = health.get("Status") if isinstance(health, dict) else None
    snapshot = {
        "mode": "present",
        "container_id": inspected.get("Id"),
        "image_id": image_id,
        "image": config_image,
        "restart_count": inspected.get("RestartCount"),
        "started_at": runtime.get("StartedAt"),
        "health": health_status,
        "compose_config_hash": config_hash,
    }
    return "present", snapshot, _telegram_storage_snapshot_fingerprint(snapshot)


def _telegram_storage_target_repo(script_text: str) -> str:
    match = re.search(
        r'^REPO="\$\{TELEGRAM_STORAGE_SERVICE_REPO:-([^}]+)\}"$',
        script_text,
        re.MULTILINE,
    )
    if match is None:
        raise ValueError("Telegram Storage deploy script repository contract is unavailable")
    repo = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", repo):
        raise ValueError("Telegram Storage deploy script repository contract is invalid")
    return repo


def _telegram_storage_bootstrap_state(
    *,
    repo: str,
    state: dict[str, Any],
    prior_mode: str,
    prior_runtime: dict[str, Any],
) -> dict[str, str] | None:
    if prior_mode != "present":
        raise RuntimeError(
            "Telegram Storage guarded rollout requires one existing live generation to preserve rollback evidence"
        )
    prior_image = prior_runtime.get("image")
    prior_started_at = prior_runtime.get("started_at")
    if not isinstance(prior_image, str) or not re.fullmatch(
        re.escape(repo) + r"@sha256:[0-9a-f]{64}", prior_image
    ):
        raise RuntimeError("Telegram Storage prior live image is not an immutable repository digest")
    if prior_runtime.get("health") != "healthy":
        raise RuntimeError("Telegram Storage prior live generation is not Docker-healthy")
    if not isinstance(prior_started_at, str) or not prior_started_at.strip():
        raise RuntimeError("Telegram Storage prior live StartedAt evidence is unavailable")
    if state["state_exists"]:
        if state["state"].get("telegram_storage_service_image") != prior_image:
            raise RuntimeError("Telegram Storage durable LKG differs from the live rollback generation")
        return None
    return {
        "telegram_storage_service_image": prior_image,
        "deployed_at": prior_started_at,
    }


async def _prepare_telegram_storage_deploy(
    *,
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    target_image: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
        raise ValueError("expected_head_sha must be a 40-character lowercase SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_script_blob_sha):
        raise ValueError("expected_script_blob_sha must be a 40-character lowercase SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_script_sha256):
        raise ValueError("expected_script_sha256 must be a 64-character lowercase SHA-256")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", target_image):
        raise ValueError("target_image must be an immutable digest reference")

    registry = server_attr("_get_workspace_registry")()
    candidate_info = registry.project_info(candidate_project)
    candidate_root = _safe_registered_root(candidate_info, expected_type="candidate-clone")
    metadata = _read_candidate_metadata(candidate_root)
    if metadata.get("project_id") != candidate_project:
        raise ValueError("candidate metadata project identity mismatch")
    if metadata.get("source_project") != _TELEGRAM_STORAGE_SOURCE_PROJECT:
        raise ValueError("candidate source project is not authorized for Telegram Storage deployment")
    if metadata.get("head") != expected_head_sha or metadata.get("base_sha") != expected_head_sha:
        raise ValueError("candidate metadata is not pinned to the expected Telegram Storage head")
    if _git_capture(candidate_root, "rev-parse", "HEAD").lower() != expected_head_sha:
        raise ValueError("candidate HEAD changed")
    if _git_capture(candidate_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("candidate workspace must be clean")

    script = _safe_regular_under(
        candidate_root,
        _TELEGRAM_STORAGE_SCRIPT_PATH,
        "Telegram Storage deploy script",
    )
    blob_sha = _git_capture(
        candidate_root,
        "rev-parse",
        f"{expected_head_sha}:{_TELEGRAM_STORAGE_SCRIPT_PATH}",
    )
    if blob_sha != expected_script_blob_sha:
        raise ValueError("Telegram Storage deploy script Git blob identity mismatch")
    if hashlib.sha256(script.read_bytes()).hexdigest() != expected_script_sha256:
        raise ValueError("Telegram Storage deploy script SHA-256 identity mismatch")
    try:
        script_text = script.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Telegram Storage deploy script is unreadable") from exc
    for marker in (
        "TELEGRAM_STORAGE_SERVICE_TARGET_IMAGE",
        "TELEGRAM_STORAGE_OPERATOR_ENV_FILE",
        "TELEGRAM_STORAGE_DEPLOY_STATE_DIR",
        "preflight_backup_readiness_config",
    ):
        if marker not in script_text:
            raise ValueError("Telegram Storage guarded deploy seam is missing")
    repo = _telegram_storage_target_repo(script_text)
    if not re.fullmatch(re.escape(repo) + r"@sha256:[0-9a-f]{64}", target_image):
        raise ValueError("target_image must pin the Telegram Storage deploy repository")

    compose_file = _safe_regular_under(
        candidate_root,
        _TELEGRAM_STORAGE_COMPOSE_PATH,
        "Telegram Storage Compose desired state",
    )
    try:
        compose_text = compose_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Telegram Storage Compose desired state is unreadable") from exc
    for marker in (
        "telegram-storage-service:",
        "TELEGRAM_STORAGE_API_TOKEN",
        "http://localhost:8060/ready",
    ):
        if marker not in compose_text:
            raise ValueError("Telegram Storage Compose readiness contract is incomplete")

    operator_info = registry.project_info(_TELEGRAM_STORAGE_SOURCE_PROJECT)
    operator_root = _safe_registered_root(operator_info)
    client = _docker_client()
    client._validate_project_dir(str(operator_root))
    env_file = _safe_regular_under(operator_root, ".env", "Telegram Storage operator env file")
    _telegram_storage_validate_operator_env(env_file)

    state_volume = _TELEGRAM_STORAGE_STATE_VOLUME
    client._validate_volume_name(state_volume)
    volume = await client.volume_metadata(state_volume)
    if volume.get("Name") != state_volume:
        raise RuntimeError("Telegram Storage deploy-state volume identity mismatch")
    if volume.get("Driver") not in (None, "", "local") or volume.get("Options") not in (
        None,
        {},
        [],
    ):
        raise RuntimeError("Telegram Storage deploy-state volume is not a plain local volume")
    volume_fingerprint = _telegram_storage_resource_fingerprint(volume)

    helper_container = os.environ.get("MCP_DEPLOY_HELPER_CONTAINER", _DEPLOY_HELPER_CONTAINER).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", helper_container):
        raise ValueError("MCP_DEPLOY_HELPER_CONTAINER is invalid")
    helper_image_id = await client.container_image_id(helper_container)
    state, state_fingerprint = await _telegram_storage_read_state(
        client,
        helper_image_id=helper_image_id,
        state_volume=state_volume,
    )
    prior_mode, prior_runtime, runtime_fingerprint = await _telegram_storage_runtime_snapshot(client)
    bootstrap_state = _telegram_storage_bootstrap_state(
        repo=repo,
        state=state,
        prior_mode=prior_mode,
        prior_runtime=prior_runtime,
    )

    return {
        "candidate_root": str(candidate_root),
        "operator_root": str(operator_root),
        "script_path": str(script),
        "env_file": str(env_file),
        "state_volume": state_volume,
        "state_volume_fingerprint": volume_fingerprint,
        "state_fingerprint": state_fingerprint,
        "state": state,
        "helper_image_id": helper_image_id,
        "target_image": target_image,
        "prior_mode": prior_mode,
        "prior_runtime": prior_runtime,
        "runtime_fingerprint": runtime_fingerprint,
        "bootstrap_state": bootstrap_state,
    }


async def _verify_telegram_storage_live(
    client: Any,
    *,
    target_image: str,
) -> dict[str, Any]:
    target_image_id = await client.image_id(target_image)
    inspected = _single_inspect(await client.inspect(_TELEGRAM_STORAGE_CONTAINER, max_lines=10))
    runtime = inspected.get("State")
    config = inspected.get("Config")
    if not isinstance(runtime, dict) or not isinstance(config, dict):
        raise RuntimeError("Telegram Storage deployed container metadata is incomplete")
    if runtime.get("Running") is not True:
        raise RuntimeError("Telegram Storage deployed container is not running")
    if inspected.get("Image") != target_image_id:
        raise RuntimeError("Telegram Storage deployed image ID differs from target")
    if config.get("Image") != target_image:
        raise RuntimeError("Telegram Storage deployed image reference differs from target")
    if inspected.get("RestartCount") != 0:
        raise RuntimeError("Telegram Storage deployed container restarted after rollout")
    health = runtime.get("Health")
    if not isinstance(health, dict) or health.get("Status") != "healthy":
        raise RuntimeError("Telegram Storage deployed container is not Docker-healthy")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise RuntimeError("Telegram Storage deployed Compose labels are unavailable")
    if labels.get("com.docker.compose.project") != "infra-quart":
        raise RuntimeError("Telegram Storage deployed Compose project identity mismatch")
    if labels.get("com.docker.compose.service") != "telegram-storage-service":
        raise RuntimeError("Telegram Storage deployed Compose service identity mismatch")
    config_hash = labels.get("com.docker.compose.config-hash")
    if not isinstance(config_hash, str) or not config_hash:
        raise RuntimeError("Telegram Storage deployed Compose config hash is unavailable")
    return {
        "container_id": inspected.get("Id"),
        "image_id": target_image_id,
        "image": target_image,
        "restart_count": inspected.get("RestartCount"),
        "started_at": runtime.get("StartedAt"),
        "health": "healthy",
        "compose_config_hash": config_hash,
    }


def _telegram_storage_helper_payload(
    client: Any,
    run: RunResult,
) -> tuple[dict[str, Any] | None, str]:
    try:
        parsed = json.loads((run.stdout or "").strip())
    except json.JSONDecodeError:
        return None, _sanitize_deploy_tail(
            client, (run.stdout or "") + "\n" + (run.stderr or "")
        )
    payload = parsed if isinstance(parsed, dict) else None
    if payload is None:
        return None, _sanitize_deploy_tail(
            client, (run.stdout or "") + "\n" + (run.stderr or "")
        )
    return payload, _sanitize_deploy_tail(client, str(payload.get("output_tail") or ""))


async def _docker_deploy_telegram_storage_contract_impl(
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    target_image: str,
    state_volume: str,
    state_volume_fingerprint: str,
    state_fingerprint: str,
    prior_mode: str,
    runtime_fingerprint: str,
    timeout: int = 300,
) -> dict[str, Any]:
    try:
        plan = await _prepare_telegram_storage_deploy(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            target_image=target_image,
        )
        confirmed = {
            "state_volume": state_volume,
            "state_volume_fingerprint": state_volume_fingerprint,
            "state_fingerprint": state_fingerprint,
            "prior_mode": prior_mode,
            "runtime_fingerprint": runtime_fingerprint,
        }
        if any(plan[key] != value for key, value in confirmed.items()):
            raise RuntimeError("Telegram Storage confirmation identities changed before execution")
    except (ValueError, RuntimeError) as exc:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )

    client = _docker_client()
    run = await client.run_telegram_storage_deploy_contract_helper(
        helper_image_id=plan["helper_image_id"],
        candidate_root=plan["candidate_root"],
        operator_root=plan["operator_root"],
        script_path=plan["script_path"],
        env_file=plan["env_file"],
        state_volume=state_volume,
        target_image=target_image,
        bootstrap_image=(plan["bootstrap_state"] or {}).get("telegram_storage_service_image"),
        bootstrap_deployed_at=(plan["bootstrap_state"] or {}).get("deployed_at"),
        timeout=timeout,
    )
    payload, output_tail = _telegram_storage_helper_payload(client, run)
    helper_exit = payload.get("exit_code") if isinstance(payload, dict) else None
    if run.exit_code == -1 or helper_exit == 124:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS",
            message="Telegram Storage deploy outcome is unknown after an execution timeout",
            result={
                "target_image": target_image,
                "state_volume": state_volume,
                "output_tail": output_tail,
            },
            source="docker",
            retryable=False,
            redacted=True,
            hint="Do not retry deploy. Reconcile the exact live container and deploy-state volume against the original confirmation first.",
        )
    if run.exit_code != 0 or payload is None or helper_exit != 0:
        return tool_error(
            tool="docker_deploy_contract",
            code=("DEPLOY_CONTRACT_EVIDENCE_INVALID" if payload is None else "DEPLOY_CONTRACT_FAILED"),
            message=(
                "Telegram Storage deploy helper returned invalid evidence"
                if payload is None
                else str(payload.get("error") or "Telegram Storage deploy contract failed")
            ),
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
            redacted=True,
        )

    state = payload.get("state")
    if not isinstance(state, dict) or state.get("telegram_storage_service_image") != target_image:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_EVIDENCE_INVALID",
            message="Telegram Storage deploy helper returned mismatched LKG image evidence",
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
            redacted=True,
        )
    deployed_at = state.get("deployed_at")
    if not isinstance(deployed_at, str) or not deployed_at.strip():
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_EVIDENCE_INVALID",
            message="Telegram Storage deploy helper returned invalid deployment timestamp evidence",
            source="docker",
            retryable=False,
        )
    try:
        volume = await client.volume_metadata(state_volume)
        if _telegram_storage_resource_fingerprint(volume) != state_volume_fingerprint:
            raise RuntimeError("Telegram Storage deploy-state volume changed during rollout")
        durable_state, _durable_state_fingerprint = await _telegram_storage_read_state(
            client,
            helper_image_id=plan["helper_image_id"],
            state_volume=state_volume,
        )
        if (
            durable_state.get("state_exists") is not True
            or durable_state.get("state", {}).get("telegram_storage_service_image") != target_image
            or durable_state.get("state", {}).get("deployed_at") != deployed_at
        ):
            raise RuntimeError("Telegram Storage durable deploy state differs from helper evidence")
        first = await _verify_telegram_storage_live(client, target_image=target_image)
        await asyncio.sleep(_TELEGRAM_STORAGE_VERIFY_STABILITY_SECONDS)
        second = await _verify_telegram_storage_live(client, target_image=target_image)
        if first != second:
            raise RuntimeError("Telegram Storage deployment changed during Gateway stability verification")
    except RuntimeError as exc:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_VERIFICATION_FAILED",
            message=str(exc),
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
            redacted=True,
        )
    return tool_success(
        "docker_deploy_contract",
        result={
            "source_head_sha": expected_head_sha,
            "script_blob_sha": expected_script_blob_sha,
            "script_sha256": expected_script_sha256,
            "target_image": target_image,
            "target_image_id": second["image_id"],
            "state_volume": state_volume,
            "stability_seconds": _TELEGRAM_STORAGE_VERIFY_STABILITY_SECONDS,
            "deployment": second,
            "deployed_at": deployed_at,
            "output_tail": output_tail,
        },
        source="docker",
        dangerous=True,
        redacted=True,
    )


def _content_harvester_target_image(image_namespace: str, source_revision: str) -> str:
    namespace = image_namespace.strip().rstrip("/") if isinstance(image_namespace, str) else ""
    if (
        not namespace
        or "@" in namespace
        or "//" in namespace
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", namespace)
    ):
        raise ValueError("image_namespace must be a safe registry/repository namespace")
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("source_revision must be an exact lowercase 40-hex SHA")
    return f"{namespace}/content-harvester-service:{source_revision}"


def _content_harvester_resource_fingerprint(kind: str, metadata: dict[str, Any]) -> str:
    if kind == "volume":
        stable = {
            key: metadata.get(key)
            for key in ("Name", "Driver", "Scope", "Options", "Labels")
        }
    elif kind == "network":
        stable = {
            key: metadata.get(key)
            for key in (
                "Name",
                "Id",
                "Driver",
                "Scope",
                "Internal",
                "Attachable",
                "Ingress",
                "Options",
                "Labels",
            )
        }
    else:
        raise ValueError("unsupported deploy resource fingerprint kind")
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


async def _content_harvester_topology_fingerprint(client: Any) -> tuple[str, str]:
    existing: list[str] = []
    for _service, container in _CONTENT_HARVESTER_TARGETS:
        if await client.container_exists(container):
            existing.append(container)
    if not existing:
        return "absent", "sha256:" + hashlib.sha256(b"absent").hexdigest()
    if len(existing) != len(_CONTENT_HARVESTER_TARGETS):
        raise RuntimeError(
            "partial prior Content Harvester topology is ambiguous; all guarded targets or none are required"
        )

    evidence: list[dict[str, Any]] = []
    prior_image_id: str | None = None
    prior_config_image: str | None = None
    for _service, container in _CONTENT_HARVESTER_TARGETS:
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        runtime = inspected.get("State")
        config = inspected.get("Config")
        if not isinstance(runtime, dict) or not isinstance(config, dict):
            raise RuntimeError("prior Content Harvester target metadata is incomplete")
        if runtime.get("Running") is not True:
            raise RuntimeError("prior Content Harvester target is not running")
        image_id = inspected.get("Image")
        config_image = config.get("Image")
        if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise RuntimeError("prior Content Harvester image ID is malformed")
        if not isinstance(config_image, str) or not config_image:
            raise RuntimeError("prior Content Harvester image reference is unavailable")
        if prior_image_id is None:
            prior_image_id = image_id
            prior_config_image = config_image
        elif image_id != prior_image_id or config_image != prior_config_image:
            raise RuntimeError("prior Content Harvester roles do not share one proven image generation")
        if container == "content-harvester-api":
            health = runtime.get("Health")
            if not isinstance(health, dict) or health.get("Status") != "healthy":
                raise RuntimeError("prior Content Harvester API is not Docker-healthy")
        evidence.append(
            {
                "container": container,
                "container_id": inspected.get("Id"),
                "image_id": image_id,
                "config_image": config_image,
                "started_at": runtime.get("StartedAt"),
            }
        )
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return "present", "sha256:" + hashlib.sha256(raw).hexdigest()


async def _prepare_content_harvester_deploy(
    *,
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    source_revision: str,
    image_namespace: str,
    artifact_volume: str,
    runtime_network: str,
    require_stable_topology: bool = True,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
        raise ValueError("expected_head_sha must be a 40-character lowercase SHA")
    if source_revision != expected_head_sha:
        raise ValueError("source_revision must exactly match expected_head_sha")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_script_blob_sha):
        raise ValueError("expected_script_blob_sha must be a 40-character lowercase SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_script_sha256):
        raise ValueError("expected_script_sha256 must be a 64-character lowercase SHA-256")
    target_image = _content_harvester_target_image(image_namespace, source_revision)

    registry = server_attr("_get_workspace_registry")()
    candidate_info = registry.project_info(candidate_project)
    candidate_root = _safe_registered_root(candidate_info, expected_type="candidate-clone")
    metadata = _read_candidate_metadata(candidate_root)
    if metadata.get("project_id") != candidate_project:
        raise ValueError("candidate metadata project identity mismatch")
    if metadata.get("source_project") != _CONTENT_HARVESTER_SOURCE_PROJECT:
        raise ValueError("candidate source project is not authorized for Content Harvester deployment")
    if metadata.get("head") != expected_head_sha or metadata.get("base_sha") != expected_head_sha:
        raise ValueError("candidate metadata is not pinned to the expected Content Harvester head")
    if _git_capture(candidate_root, "rev-parse", "HEAD").lower() != expected_head_sha:
        raise ValueError("candidate HEAD changed")
    if _git_capture(candidate_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("candidate workspace must be clean")

    script = _safe_regular_under(
        candidate_root,
        _CONTENT_HARVESTER_SCRIPT_PATH,
        "Content Harvester deploy script",
    )
    blob_sha = _git_capture(
        candidate_root,
        "rev-parse",
        f"{expected_head_sha}:{_CONTENT_HARVESTER_SCRIPT_PATH}",
    )
    if blob_sha != expected_script_blob_sha:
        raise ValueError("Content Harvester deploy script Git blob identity mismatch")
    if hashlib.sha256(script.read_bytes()).hexdigest() != expected_script_sha256:
        raise ValueError("Content Harvester deploy script SHA-256 identity mismatch")

    operator_info = registry.project_info(_CONTENT_HARVESTER_SOURCE_PROJECT)
    operator_root = _safe_registered_root(operator_info)
    client = _docker_client()
    client._validate_project_dir(str(operator_root))
    env_file = _safe_regular_under(operator_root, ".env", "Content Harvester operator env file")

    state_volume = await client.volume_metadata(_CONTENT_HARVESTER_STATE_VOLUME)
    if state_volume.get("Name") != _CONTENT_HARVESTER_STATE_VOLUME:
        raise RuntimeError("Content Harvester deploy-state volume identity mismatch")
    if state_volume.get("Driver") not in (None, "", "local") or state_volume.get("Options") not in (
        None,
        {},
        [],
    ):
        raise RuntimeError("Content Harvester deploy-state volume is not a plain local volume")

    artifact = await client.volume_metadata(artifact_volume)
    if artifact.get("Name") != artifact_volume:
        raise RuntimeError("Content Harvester artifact volume identity mismatch")
    if artifact.get("Driver") not in (None, "", "local") or artifact.get("Options") not in (
        None,
        {},
        [],
    ):
        raise RuntimeError("Content Harvester artifact volume is not a plain local volume")

    network = await client.network_metadata(runtime_network)
    if network.get("Name") != runtime_network:
        raise RuntimeError("Content Harvester runtime network identity mismatch")
    if network.get("Scope") not in (None, "", "local"):
        raise RuntimeError("Content Harvester runtime network must be local to this Docker daemon")
    if not isinstance(network.get("Driver"), str) or not network.get("Driver"):
        raise RuntimeError("Content Harvester runtime network driver is unavailable")

    if require_stable_topology:
        prior_mode, topology_fingerprint = await _content_harvester_topology_fingerprint(client)
    else:
        prior_mode = "recovery"
        topology_fingerprint = ""

    helper_container = os.environ.get("MCP_DEPLOY_HELPER_CONTAINER", _DEPLOY_HELPER_CONTAINER).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", helper_container):
        raise ValueError("MCP_DEPLOY_HELPER_CONTAINER is invalid")
    helper_image_id = await client.container_image_id(helper_container)

    return {
        "candidate_root": str(candidate_root),
        "operator_root": str(operator_root),
        "script_path": str(script),
        "env_file": str(env_file),
        "state_volume": _CONTENT_HARVESTER_STATE_VOLUME,
        "state_volume_fingerprint": _content_harvester_resource_fingerprint("volume", state_volume),
        "artifact_volume": artifact_volume,
        "artifact_volume_fingerprint": _content_harvester_resource_fingerprint("volume", artifact),
        "runtime_network": runtime_network,
        "runtime_network_fingerprint": _content_harvester_resource_fingerprint("network", network),
        "helper_image_id": helper_image_id,
        "source_revision": source_revision,
        "image_namespace": image_namespace.strip().rstrip("/"),
        "target_image": target_image,
        "prior_mode": prior_mode,
        "topology_fingerprint": topology_fingerprint,
    }


def _content_harvester_state(
    state: dict[str, Any],
    *,
    source_revision: str,
    target_image: str,
    artifact_volume: str,
    runtime_network: str,
) -> dict[str, Any]:
    if state.get("schema_version") != 1:
        raise RuntimeError("Content Harvester deployment state schema_version is invalid")
    if state.get("source_revision") != source_revision:
        raise RuntimeError("Content Harvester deployment state source revision mismatch")
    generation = state.get("deploy_generation")
    if not isinstance(generation, str) or not re.fullmatch(
        r"[0-9a-f]{12}-[0-9]{8}T[0-9]{6}Z", generation
    ):
        raise RuntimeError("Content Harvester deployment generation is malformed")
    if state.get("image") != target_image:
        raise RuntimeError("Content Harvester deployment state image reference changed")
    image_id = state.get("image_id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("Content Harvester deployment state image ID is malformed")
    if state.get("artifact_volume") != artifact_volume:
        raise RuntimeError("Content Harvester deployment state artifact volume changed")
    if state.get("runtime_network") != runtime_network:
        raise RuntimeError("Content Harvester deployment state runtime network changed")
    stability = state.get("stability_seconds")
    if not isinstance(stability, int) or isinstance(stability, bool) or not 1 <= stability <= 600:
        raise RuntimeError("Content Harvester deployment stability evidence is malformed")
    expected_services = [service for service, _container in _CONTENT_HARVESTER_TARGETS]
    if state.get("services") != expected_services:
        raise RuntimeError("Content Harvester deployment service set changed")
    return state


async def _verify_content_harvester_resources(client: Any, plan: dict[str, Any]) -> None:
    state_volume = await client.volume_metadata(plan["state_volume"])
    artifact = await client.volume_metadata(plan["artifact_volume"])
    network = await client.network_metadata(plan["runtime_network"])
    if _content_harvester_resource_fingerprint("volume", state_volume) != plan[
        "state_volume_fingerprint"
    ]:
        raise RuntimeError("Content Harvester deploy-state volume changed during rollout")
    if _content_harvester_resource_fingerprint("volume", artifact) != plan[
        "artifact_volume_fingerprint"
    ]:
        raise RuntimeError("Content Harvester artifact volume changed during rollout")
    if _content_harvester_resource_fingerprint("network", network) != plan[
        "runtime_network_fingerprint"
    ]:
        raise RuntimeError("Content Harvester runtime network changed during rollout")


async def _verify_content_harvester_live(
    client: Any,
    *,
    state: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    validated_state = _content_harvester_state(
        state,
        source_revision=plan["source_revision"],
        target_image=plan["target_image"],
        artifact_volume=plan["artifact_volume"],
        runtime_network=plan["runtime_network"],
    )
    target_image_id = str(validated_state["image_id"])
    if await client.image_id(plan["target_image"]) != target_image_id:
        raise RuntimeError("Content Harvester built image reference no longer resolves to LKG image ID")
    if await client.image_revision(target_image_id) != plan["source_revision"]:
        raise RuntimeError("Content Harvester built image OCI revision differs from source revision")
    await _verify_content_harvester_resources(client, plan)
    snapshot: dict[str, dict[str, Any]] = {}
    for service, container in _CONTENT_HARVESTER_TARGETS:
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        runtime = inspected.get("State")
        config = inspected.get("Config")
        if not isinstance(runtime, dict) or not isinstance(config, dict):
            raise RuntimeError("Content Harvester target container metadata is incomplete")
        if runtime.get("Running") is not True:
            raise RuntimeError("Content Harvester target is not running")
        if inspected.get("Image") != target_image_id:
            raise RuntimeError("Content Harvester target image ID differs from LKG image")
        if config.get("Image") != plan["target_image"]:
            raise RuntimeError("Content Harvester target image reference differs from confirmed image")
        if inspected.get("RestartCount") != 0:
            raise RuntimeError("Content Harvester target restarted after rollout")
        labels = config.get("Labels")
        if not isinstance(labels, dict) or labels.get("org.opencontainers.image.revision") != plan[
            "source_revision"
        ]:
            raise RuntimeError("Content Harvester target OCI revision differs from source revision")
        health_value = "running"
        if service == "api":
            health = runtime.get("Health")
            if not isinstance(health, dict) or health.get("Status") != "healthy":
                raise RuntimeError("Content Harvester API is not Docker-healthy")
            health_value = "healthy"
        snapshot[container] = {
            "container_id": inspected.get("Id"),
            "image_id": inspected.get("Image"),
            "image": config.get("Image"),
            "restart_count": inspected.get("RestartCount"),
            "started_at": runtime.get("StartedAt"),
            "health": health_value,
            "revision": plan["source_revision"],
        }
    return snapshot


def _content_harvester_pending(
    pending: dict[str, Any],
    *,
    plan: dict[str, Any],
) -> dict[str, Any]:
    if pending.get("schema_version") != 1 or pending.get("status") != "pending":
        raise RuntimeError("Content Harvester pending evidence schema/status is invalid")
    if pending.get("source_revision") != plan["source_revision"]:
        raise RuntimeError("Content Harvester pending source revision mismatch")
    generation = pending.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(
        r"[0-9a-f]{12}-[0-9]{8}T[0-9]{6}Z", generation
    ):
        raise RuntimeError("Content Harvester pending generation is malformed")
    if pending.get("target_image") != plan["target_image"]:
        raise RuntimeError("Content Harvester pending target image changed")
    target_image_id = pending.get("target_image_id")
    if not isinstance(target_image_id, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", target_image_id
    ):
        raise RuntimeError("Content Harvester pending target image ID is malformed")
    if pending.get("artifact_volume") != plan["artifact_volume"]:
        raise RuntimeError("Content Harvester pending artifact volume changed")
    if pending.get("runtime_network") != plan["runtime_network"]:
        raise RuntimeError("Content Harvester pending runtime network changed")
    previous_mode = pending.get("previous_mode")
    if previous_mode not in {"absent", "present"}:
        raise RuntimeError("Content Harvester pending previous mode is invalid")
    if previous_mode == "absent":
        if any(
            pending.get(key) is not None
            for key in ("previous_image_id", "previous_config_image", "rollback_ref")
        ):
            raise RuntimeError("Content Harvester absent pending state contains rollback identity")
    else:
        previous_image_id = pending.get("previous_image_id")
        if not isinstance(previous_image_id, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", previous_image_id
        ):
            raise RuntimeError("Content Harvester pending previous image ID is malformed")
        for key in ("previous_config_image", "rollback_ref"):
            value = pending.get(key)
            if not isinstance(value, str) or not value or "\n" in value or "\t" in value:
                raise RuntimeError(f"Content Harvester pending {key} is malformed")
    return pending


async def _verify_content_harvester_recovery(
    client: Any,
    *,
    pending: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    pending = _content_harvester_pending(pending, plan=plan)
    await _verify_content_harvester_resources(client, plan)
    if pending["previous_mode"] == "absent":
        for _service, container in _CONTENT_HARVESTER_TARGETS:
            if await client.container_exists(container):
                raise RuntimeError("Content Harvester bootstrap recovery left a target container")
        return {"outcome": "bootstrap_absent_restored", "deploy_generation": pending["generation"]}

    previous_image_id = pending["previous_image_id"]
    previous_revision = await client.image_revision(previous_image_id)
    refs: set[str] = set()
    live: dict[str, dict[str, Any]] = {}
    for service, container in _CONTENT_HARVESTER_TARGETS:
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        runtime = inspected.get("State")
        config = inspected.get("Config")
        if not isinstance(runtime, dict) or not isinstance(config, dict):
            raise RuntimeError("Content Harvester recovery target metadata is incomplete")
        if runtime.get("Running") is not True or inspected.get("Image") != previous_image_id:
            raise RuntimeError("Content Harvester recovery did not restore the previous image ID")
        config_image = config.get("Image")
        if config_image not in {pending["previous_config_image"], pending["rollback_ref"]}:
            raise RuntimeError("Content Harvester recovery image reference is not a proven previous alias")
        refs.add(str(config_image))
        if service == "api":
            health = runtime.get("Health")
            if not isinstance(health, dict) or health.get("Status") != "healthy":
                raise RuntimeError("Content Harvester recovered API is not Docker-healthy")
        live[container] = {
            "image_id": inspected.get("Image"),
            "image": config_image,
            "restart_count": inspected.get("RestartCount"),
            "started_at": runtime.get("StartedAt"),
        }
    if len(refs) != 1:
        raise RuntimeError("Content Harvester recovery roles do not share one proven image reference")
    only_ref = next(iter(refs))
    outcome = (
        "previous_generation_unchanged"
        if only_ref == pending["previous_config_image"]
        else "rolled_back_to_previous_image_id"
    )
    if outcome == "rolled_back_to_previous_image_id" and any(
        item["restart_count"] != 0 for item in live.values()
    ):
        raise RuntimeError("Content Harvester rollback generation has restart evidence")
    return {
        "outcome": outcome,
        "previous_image_id": previous_image_id,
        "previous_revision": previous_revision,
        "targets": live,
        "deploy_generation": pending["generation"],
    }


def _content_harvester_helper_payload(client: Any, run: RunResult) -> tuple[dict[str, Any] | None, str]:
    output_tail = _sanitize_deploy_tail(client, (run.stdout or "") + "\n" + (run.stderr or ""))
    try:
        parsed = json.loads((run.stdout or "").strip())
    except json.JSONDecodeError:
        return None, output_tail
    return (parsed if isinstance(parsed, dict) else None), output_tail


async def _accept_content_harvester_payload(
    client: Any,
    *,
    payload: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    if payload.get("pending_exists") is not False:
        raise RuntimeError("Content Harvester pending transaction evidence remains")
    state = payload.get("state")
    if not isinstance(state, dict) or not state:
        raise RuntimeError("Content Harvester helper returned malformed LKG state")
    first = await _verify_content_harvester_live(client, state=state, plan=plan)
    await asyncio.sleep(_CONTENT_HARVESTER_VERIFY_STABILITY_SECONDS)
    second = await _verify_content_harvester_live(client, state=state, plan=plan)
    if first != second:
        raise RuntimeError("Content Harvester deployment changed during Gateway stability verification")
    return {"state": state, "live": second}


def _content_harvester_database_url(env_file: Path) -> str:
    try:
        text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Content Harvester operator env file is unreadable") from exc
    assignments = [
        line
        for line in text.splitlines()
        if re.match(r"^\s*(?:export\s+)?HARVESTER_DATABASE_URL\s*=", line)
    ]
    if len(assignments) != 1:
        raise ValueError(
            "Content Harvester operator env must define HARVESTER_DATABASE_URL exactly once"
        )
    try:
        values = dotenv_values(dotenv_path=env_file, interpolate=False)
    except Exception as exc:
        raise ValueError("Content Harvester operator env file could not be parsed") from exc
    database_url = values.get("HARVESTER_DATABASE_URL")
    if (
        not isinstance(database_url, str)
        or not database_url.strip()
        or any(ch in database_url for ch in ("\x00", "\n", "\r"))
    ):
        raise ValueError("HARVESTER_DATABASE_URL must be one non-empty literal value")
    if "${" in database_url:
        raise ValueError("HARVESTER_DATABASE_URL interpolation is not allowed for guarded migration")
    return database_url.strip()


def _content_harvester_migration_contract_fingerprint(
    candidate_root: Path,
    expected_head_sha: str,
) -> str:
    runner_blob = _git_capture(
        candidate_root,
        "rev-parse",
        f"{expected_head_sha}:app/schema_migrations.py",
    )
    migrations_tree = _git_capture(
        candidate_root,
        "rev-parse",
        f"{expected_head_sha}:migrations",
    )
    pyproject_blob = _git_capture(
        candidate_root,
        "rev-parse",
        f"{expected_head_sha}:pyproject.toml",
    )
    for value in (runner_blob, migrations_tree, pyproject_blob):
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError("Content Harvester migration source identity is malformed")
    raw = f"{runner_blob}\n{migrations_tree}\n{pyproject_blob}\n".encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


async def _prepare_content_harvester_migration(
    *,
    candidate_project: str,
    expected_head_sha: str,
    source_revision: str,
    image_namespace: str,
    runtime_network: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
        raise ValueError("expected_head_sha must be a 40-character lowercase SHA")
    if source_revision != expected_head_sha:
        raise ValueError("source_revision must exactly match expected_head_sha")
    target_image = _content_harvester_target_image(image_namespace, source_revision)

    registry = server_attr("_get_workspace_registry")()
    candidate_info = registry.project_info(candidate_project)
    candidate_root = _safe_registered_root(candidate_info, expected_type="candidate-clone")
    metadata = _read_candidate_metadata(candidate_root)
    if metadata.get("project_id") != candidate_project:
        raise ValueError("candidate metadata project identity mismatch")
    if metadata.get("source_project") != _CONTENT_HARVESTER_SOURCE_PROJECT:
        raise ValueError("candidate source project is not authorized for Content Harvester migration")
    if metadata.get("head") != expected_head_sha or metadata.get("base_sha") != expected_head_sha:
        raise ValueError("candidate metadata is not pinned to the expected Content Harvester head")
    if _git_capture(candidate_root, "rev-parse", "HEAD").lower() != expected_head_sha:
        raise ValueError("candidate HEAD changed")
    if _git_capture(candidate_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("candidate workspace must be clean")

    operator_info = registry.project_info(_CONTENT_HARVESTER_SOURCE_PROJECT)
    operator_root = _safe_registered_root(operator_info)
    client = _docker_client()
    client._validate_project_dir(str(operator_root))
    env_file = _safe_regular_under(operator_root, ".env", "Content Harvester operator env file")
    database_url = _content_harvester_database_url(env_file)

    target_image_id = await client.image_id(target_image)
    if await client.image_revision(target_image_id) != source_revision:
        raise RuntimeError("Content Harvester migration image OCI revision differs from source revision")
    network = await client.network_metadata(runtime_network)
    if network.get("Name") != runtime_network:
        raise RuntimeError("Content Harvester migration runtime network identity mismatch")
    if network.get("Scope") not in (None, "", "local"):
        raise RuntimeError("Content Harvester migration runtime network must be local")
    if not isinstance(network.get("Driver"), str) or not network.get("Driver"):
        raise RuntimeError("Content Harvester migration runtime network driver is unavailable")

    return {
        "candidate_root": str(candidate_root),
        "env_file": str(env_file),
        "source_revision": source_revision,
        "image_namespace": image_namespace.strip().rstrip("/"),
        "target_image": target_image,
        "target_image_id": target_image_id,
        "runtime_network": runtime_network,
        "runtime_network_fingerprint": _content_harvester_resource_fingerprint("network", network),
        "database_url": database_url,
        "database_url_fingerprint": "sha256:" + hashlib.sha256(database_url.encode()).hexdigest(),
        "migration_contract_fingerprint": _content_harvester_migration_contract_fingerprint(
            candidate_root,
            expected_head_sha,
        ),
    }


def _content_harvester_migration_report(run: RunResult) -> dict[str, Any] | None:
    if run.exit_code not in {0, 3}:
        return None
    try:
        payload = json.loads((run.stdout or "").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("status") not in {"current", "pending"}:
        return None
    applied = payload.get("applied_versions")
    pending = payload.get("pending_versions")
    total = payload.get("total_migrations")
    if (
        not isinstance(applied, list)
        or not all(isinstance(item, str) for item in applied)
        or not isinstance(pending, list)
        or not all(isinstance(item, str) for item in pending)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total < 1
    ):
        return None
    if payload["status"] == "current" and (run.exit_code != 0 or pending):
        return None
    if payload["status"] == "pending" and (run.exit_code != 3 or not pending):
        return None
    return {
        "status": payload["status"],
        "applied_versions": applied,
        "pending_versions": pending,
        "total_migrations": total,
    }


async def _check_content_harvester_migrations(
    client: Any,
    *,
    plan: dict[str, Any],
    timeout: int,
) -> tuple[RunResult, dict[str, Any] | None]:
    run = await client.run_content_harvester_migration(
        image=plan["target_image"],
        runtime_network=plan["runtime_network"],
        database_url=plan["database_url"],
        mode="check",
        timeout=timeout,
    )
    return run, _content_harvester_migration_report(run)


async def docker_migrate_content_harvester(
    candidate_project: str,
    expected_head_sha: str,
    source_revision: str,
    image_namespace: str,
    runtime_network: str = "infra-quart_infra-net",
    timeout: int = 300,
) -> dict[str, Any]:
    """Prepare a guarded Content Harvester schema migration on the exact release image."""
    timeout = max(60, min(timeout, 600))
    try:
        plan = await _prepare_content_harvester_migration(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            source_revision=source_revision,
            image_namespace=image_namespace,
            runtime_network=runtime_network,
        )
        client = _docker_client()
        check_run, report = await _check_content_harvester_migrations(
            client,
            plan=plan,
            timeout=min(timeout, 120),
        )
    except ValueError as exc:
        return tool_error(
            tool="docker_migrate_content_harvester",
            code="INVALID_INPUT",
            message=str(exc),
            source="docker",
            retryable=False,
        )
    except RuntimeError as exc:
        return tool_error(
            tool="docker_migrate_content_harvester",
            code="MIGRATION_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )
    if report is None:
        return tool_error(
            tool="docker_migrate_content_harvester",
            code="MIGRATION_CHECK_FAILED",
            message="Content Harvester migration check did not return trusted ledger evidence",
            result={"output_tail": _sanitize_deploy_tail(client, check_run.stderr or check_run.stdout)},
            source="docker",
            retryable=False,
            redacted=True,
        )
    if report["status"] == "current":
        return tool_success(
            "docker_migrate_content_harvester",
            result={
                "status": "current",
                "mutation_required": False,
                "source_revision": source_revision,
                "target_image": plan["target_image"],
                "target_image_id": plan["target_image_id"],
                "migration_contract_fingerprint": plan["migration_contract_fingerprint"],
                "report": report,
            },
            source="docker",
            redacted=True,
        )

    action = _confirm_store().create_action(
        "docker_migrate_content_harvester",
        {
            "candidate_project": candidate_project,
            "expected_head_sha": expected_head_sha,
            "source_revision": source_revision,
            "image_namespace": plan["image_namespace"],
            "runtime_network": runtime_network,
            "target_image": plan["target_image"],
            "target_image_id": plan["target_image_id"],
            "runtime_network_fingerprint": plan["runtime_network_fingerprint"],
            "database_url_fingerprint": plan["database_url_fingerprint"],
            "migration_contract_fingerprint": plan["migration_contract_fingerprint"],
            "pending_versions": report["pending_versions"],
            "timeout": timeout,
        },
        (
            f"Apply Content Harvester migrations from {candidate_project}@"
            f"{expected_head_sha[:12]} using exact release image"
        ),
        risk="high",
        required_scope="mcp:docker:admin",
    )
    return _confirmation_response(action)


async def _docker_migrate_content_harvester_impl(
    candidate_project: str,
    expected_head_sha: str,
    source_revision: str,
    image_namespace: str,
    runtime_network: str,
    target_image: str,
    target_image_id: str,
    runtime_network_fingerprint: str,
    database_url_fingerprint: str,
    migration_contract_fingerprint: str,
    pending_versions: list[str],
    timeout: int = 300,
) -> dict[str, Any]:
    try:
        plan = await _prepare_content_harvester_migration(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            source_revision=source_revision,
            image_namespace=image_namespace,
            runtime_network=runtime_network,
        )
        confirmed = {
            "target_image": target_image,
            "target_image_id": target_image_id,
            "runtime_network_fingerprint": runtime_network_fingerprint,
            "database_url_fingerprint": database_url_fingerprint,
            "migration_contract_fingerprint": migration_contract_fingerprint,
        }
        if any(plan[key] != value for key, value in confirmed.items()):
            raise RuntimeError("Content Harvester migration confirmation identities changed")
        client = _docker_client()
        _precheck_run, precheck = await _check_content_harvester_migrations(
            client,
            plan=plan,
            timeout=min(timeout, 120),
        )
        if precheck is None:
            raise RuntimeError("Content Harvester migration pre-check evidence is invalid")
        if precheck["status"] == "current":
            return tool_success(
                "docker_migrate_content_harvester",
                result={
                    "status": "current",
                    "mutation_required": False,
                    "source_revision": source_revision,
                    "target_image": target_image,
                    "target_image_id": target_image_id,
                    "reconciled_before_apply": True,
                    "report": precheck,
                },
                source="docker",
                dangerous=True,
                redacted=True,
            )
        if precheck["pending_versions"] != pending_versions:
            raise RuntimeError("Content Harvester pending migration set changed before execution")
    except (ValueError, RuntimeError) as exc:
        return tool_error(
            tool="docker_migrate_content_harvester",
            code="MIGRATION_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )

    apply_run = await client.run_content_harvester_migration(
        image=plan["target_image"],
        runtime_network=plan["runtime_network"],
        database_url=plan["database_url"],
        mode="apply",
        timeout=timeout,
    )
    check_run, report = await _check_content_harvester_migrations(
        client,
        plan=plan,
        timeout=min(timeout, 120),
    )
    apply_tail = _sanitize_deploy_tail(client, (apply_run.stdout or "") + "\n" + (apply_run.stderr or ""))
    check_tail = _sanitize_deploy_tail(client, (check_run.stdout or "") + "\n" + (check_run.stderr or ""))

    if report is not None and report["status"] == "current":
        return tool_success(
            "docker_migrate_content_harvester",
            result={
                "status": "current",
                "mutation_required": True,
                "source_revision": source_revision,
                "target_image": target_image,
                "target_image_id": target_image_id,
                "reconciled_after_unknown": apply_run.exit_code == -1,
                "apply_exit_code": apply_run.exit_code,
                "report": report,
                "apply_output_tail": apply_tail,
            },
            source="docker",
            dangerous=True,
            redacted=True,
        )
    if report is not None and report["status"] == "pending":
        return tool_error(
            tool="docker_migrate_content_harvester",
            code="MIGRATION_INCOMPLETE_RECONCILED",
            message="Content Harvester migration did not reach current state; a fresh check proves pending work remains",
            result={
                "source_revision": source_revision,
                "target_image": target_image,
                "apply_exit_code": apply_run.exit_code,
                "report": report,
                "apply_output_tail": apply_tail,
                "check_output_tail": check_tail,
            },
            source="docker",
            retryable=True,
            redacted=True,
            hint="Run a fresh migration preflight; do not reuse the consumed confirmation.",
        )
    return tool_error(
        tool="docker_migrate_content_harvester",
        code=(
            "MIGRATION_OUTCOME_AMBIGUOUS"
            if apply_run.exit_code == -1
            else "MIGRATION_VERIFICATION_FAILED"
        ),
        message="Content Harvester migration outcome could not be proven by the ledger check",
        result={
            "source_revision": source_revision,
            "target_image": target_image,
            "apply_exit_code": apply_run.exit_code,
            "apply_output_tail": apply_tail,
            "check_output_tail": check_tail,
        },
        source="docker",
        retryable=False,
        redacted=True,
        hint="Do not retry apply until a later read-only migration check can prove current or pending state.",
    )


async def docker_deploy_content_harvester(
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    source_revision: str,
    image_namespace: str,
    artifact_volume: str,
    runtime_network: str,
    timeout: int = 720,
) -> dict[str, Any]:
    """Prepare a guarded Content Harvester Telegram-canary four-role rollout."""
    timeout = max(180, min(timeout, 900))
    try:
        plan = await _prepare_content_harvester_deploy(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            source_revision=source_revision,
            image_namespace=image_namespace,
            artifact_volume=artifact_volume,
            runtime_network=runtime_network,
        )
    except ValueError as exc:
        return tool_error(
            tool="docker_deploy_content_harvester",
            code="INVALID_INPUT",
            message=str(exc),
            source="docker",
            retryable=False,
        )
    except RuntimeError as exc:
        return tool_error(
            tool="docker_deploy_content_harvester",
            code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )
    action = _confirm_store().create_action(
        "docker_deploy_content_harvester",
        {
            "candidate_project": candidate_project,
            "expected_head_sha": expected_head_sha,
            "expected_script_blob_sha": expected_script_blob_sha,
            "expected_script_sha256": expected_script_sha256,
            "source_revision": source_revision,
            "image_namespace": plan["image_namespace"],
            "artifact_volume": artifact_volume,
            "runtime_network": runtime_network,
            "target_image": plan["target_image"],
            "state_volume_fingerprint": plan["state_volume_fingerprint"],
            "artifact_volume_fingerprint": plan["artifact_volume_fingerprint"],
            "runtime_network_fingerprint": plan["runtime_network_fingerprint"],
            "prior_mode": plan["prior_mode"],
            "topology_fingerprint": plan["topology_fingerprint"],
            "timeout": timeout,
        },
        (
            f"Deploy Content Harvester Telegram canary from {candidate_project}@"
            f"{expected_head_sha[:12]} as four exact roles"
        ),
        risk="high",
        required_scope="mcp:docker:admin",
    )
    return _confirmation_response(action)


async def _docker_deploy_content_harvester_impl(
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    source_revision: str,
    image_namespace: str,
    artifact_volume: str,
    runtime_network: str,
    target_image: str,
    state_volume_fingerprint: str,
    artifact_volume_fingerprint: str,
    runtime_network_fingerprint: str,
    prior_mode: str,
    topology_fingerprint: str,
    timeout: int = 720,
) -> dict[str, Any]:
    try:
        plan = await _prepare_content_harvester_deploy(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            source_revision=source_revision,
            image_namespace=image_namespace,
            artifact_volume=artifact_volume,
            runtime_network=runtime_network,
        )
        confirmed = {
            "target_image": target_image,
            "state_volume_fingerprint": state_volume_fingerprint,
            "artifact_volume_fingerprint": artifact_volume_fingerprint,
            "runtime_network_fingerprint": runtime_network_fingerprint,
            "prior_mode": prior_mode,
            "topology_fingerprint": topology_fingerprint,
        }
        if any(plan[key] != value for key, value in confirmed.items()):
            raise RuntimeError("Content Harvester confirmation identities changed before execution")
    except (ValueError, RuntimeError) as exc:
        return tool_error(
            tool="docker_deploy_content_harvester",
            code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )

    client = _docker_client()
    helper_kwargs = {
        "helper_image_id": plan["helper_image_id"],
        "candidate_root": plan["candidate_root"],
        "operator_root": plan["operator_root"],
        "script_path": plan["script_path"],
        "env_file": plan["env_file"],
        "state_volume": plan["state_volume"],
        "image_namespace": plan["image_namespace"],
        "source_revision": source_revision,
        "artifact_volume": artifact_volume,
        "runtime_network": runtime_network,
        "timeout": timeout,
    }
    run = await client.run_content_harvester_deploy_contract_helper(
        **helper_kwargs, recover=False
    )
    payload, output_tail = _content_harvester_helper_payload(client, run)
    ambiguous = run.exit_code == -1 or (
        isinstance(payload, dict) and payload.get("exit_code") == 124
    )

    if ambiguous:
        try:
            recovery_plan = await _prepare_content_harvester_deploy(
                candidate_project=candidate_project,
                expected_head_sha=expected_head_sha,
                expected_script_blob_sha=expected_script_blob_sha,
                expected_script_sha256=expected_script_sha256,
                source_revision=source_revision,
                image_namespace=image_namespace,
                artifact_volume=artifact_volume,
                runtime_network=runtime_network,
                require_stable_topology=False,
            )
            for key in (
                "target_image",
                "state_volume_fingerprint",
                "artifact_volume_fingerprint",
                "runtime_network_fingerprint",
            ):
                if recovery_plan[key] != plan[key]:
                    raise RuntimeError(f"Content Harvester {key} changed before recovery")
        except (ValueError, RuntimeError) as exc:
            return tool_error(
                tool="docker_deploy_content_harvester",
                code="DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS",
                message=(
                    "Content Harvester deploy outcome is unknown and recovery preflight no longer "
                    f"matches the confirmed source/resources: {exc}"
                ),
                result={"source_revision": source_revision, "output_tail": output_tail},
                source="docker",
                retryable=False,
                hint="Do not retry deploy. Reconcile durable pending/LKG evidence with the original confirmation.",
            )
        recovery_kwargs = {**helper_kwargs}
        for key in (
            "helper_image_id",
            "candidate_root",
            "operator_root",
            "script_path",
            "env_file",
            "state_volume",
            "image_namespace",
        ):
            if key in recovery_plan:
                recovery_kwargs[key] = recovery_plan[key]
        recovery = await client.run_content_harvester_deploy_contract_helper(
            **recovery_kwargs, recover=True
        )
        recovery_payload, recovery_tail = _content_harvester_helper_payload(client, recovery)
        if (
            recovery.exit_code == 0
            and isinstance(recovery_payload, dict)
            and recovery_payload.get("exit_code") == 0
            and recovery_payload.get("mode") == "recover"
            and recovery_payload.get("pending_exists") is False
        ):
            state = recovery_payload.get("state")
            if isinstance(state, dict) and state.get("source_revision") == source_revision:
                try:
                    accepted = await _accept_content_harvester_payload(
                        client, payload=recovery_payload, plan=plan
                    )
                except RuntimeError:
                    accepted = None
                if accepted is not None:
                    return tool_success(
                        "docker_deploy_content_harvester",
                        result={
                            "source_revision": source_revision,
                            "target_image": plan["target_image"],
                            "target_image_id": accepted["state"]["image_id"],
                            "reconciled_after_unknown": True,
                            "recovery_result": "target_accepted",
                            "deployment": accepted,
                            "output_tail": recovery_tail,
                        },
                        source="docker",
                        dangerous=True,
                        redacted=True,
                    )
            pending_before = recovery_payload.get("pending_before")
            if isinstance(pending_before, dict) and pending_before:
                try:
                    recovered = await _verify_content_harvester_recovery(
                        client, pending=pending_before, plan=plan
                    )
                except RuntimeError:
                    recovered = None
                if recovered is not None:
                    return tool_error(
                        tool="docker_deploy_content_harvester",
                        code="DEPLOY_CONTRACT_RECOVERED_NOT_DEPLOYED",
                        message=(
                            "Content Harvester unknown outcome was safely reconciled without "
                            "accepting the requested target generation"
                        ),
                        result={
                            "source_revision": source_revision,
                            "reconciled_after_unknown": True,
                            "recovery": recovered,
                            "output_tail": recovery_tail,
                        },
                        source="docker",
                        retryable=True,
                        hint="Create a fresh preflight/confirmation before any later deploy attempt.",
                    )
        return tool_error(
            tool="docker_deploy_content_harvester",
            code="DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS",
            message=(
                "Content Harvester deploy outcome could not be proven after the repo-owned "
                "--recover reconciliation path"
            ),
            result={"source_revision": source_revision, "output_tail": recovery_tail},
            source="docker",
            retryable=False,
            hint="Reconcile durable pending/LKG state before creating a new deploy confirmation.",
        )

    if run.exit_code != 0 or not isinstance(payload, dict) or payload.get("exit_code") != 0:
        return tool_error(
            tool="docker_deploy_content_harvester",
            code=("DEPLOY_CONTRACT_EVIDENCE_INVALID" if payload is None else "DEPLOY_CONTRACT_FAILED"),
            message=(
                "Content Harvester deploy helper returned invalid evidence"
                if payload is None
                else str(payload.get("error") or "Content Harvester deploy contract failed")
            ),
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
        )
    try:
        accepted = await _accept_content_harvester_payload(client, payload=payload, plan=plan)
    except RuntimeError as exc:
        return tool_error(
            tool="docker_deploy_content_harvester",
            code="DEPLOY_CONTRACT_VERIFICATION_FAILED",
            message=str(exc),
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
        )
    return tool_success(
        "docker_deploy_content_harvester",
        result={
            "source_revision": source_revision,
            "script_blob_sha": expected_script_blob_sha,
            "script_sha256": expected_script_sha256,
            "target_image": plan["target_image"],
            "target_image_id": accepted["state"]["image_id"],
            "artifact_volume": artifact_volume,
            "runtime_network": runtime_network,
            "reconciled_after_unknown": False,
            "deployment": accepted,
            "output_tail": output_tail,
        },
        source="docker",
        dangerous=True,
        redacted=True,
    )


async def _accept_xloud_payload(
    client: Any,
    *,
    payload: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    if payload.get("pending_exists") is not False or payload.get("lkg_exists") is not True:
        raise RuntimeError("XLOUD durable deployment evidence is incomplete")
    state = payload.get("lkg")
    if not isinstance(state, dict) or not state:
        raise RuntimeError("XLOUD helper returned malformed LKG state")
    await _verify_xloud_guard_resources(client, plan)
    first = await _verify_xloud_live(
        client,
        state=state,
        source_revision=plan["source_revision"],
        target_images=plan["target_images"],
        image_ids=plan["image_ids"],
    )
    await asyncio.sleep(_XLOUD_VERIFY_STABILITY_SECONDS)
    await _verify_xloud_guard_resources(client, plan)
    second = await _verify_xloud_live(
        client,
        state=state,
        source_revision=plan["source_revision"],
        target_images=plan["target_images"],
        image_ids=plan["image_ids"],
    )
    if first != second:
        raise RuntimeError("XLOUD deployment changed during Gateway stability verification")
    return {"state": state, "live": second}


async def _verify_xloud_recovery(
    client: Any,
    *,
    failed: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    if failed.get("STATE_VERSION") != "1" or failed.get("SOURCE_REVISION") != plan["source_revision"]:
        raise RuntimeError("XLOUD recovery evidence does not match the confirmed rollout")
    previous_cids = {
        "site": failed.get("PREVIOUS_SITE_CID"),
        "intake": failed.get("PREVIOUS_INTAKE_CID"),
    }
    if previous_cids == {"site": "absent", "intake": "absent"}:
        for _role, container, _image_name in _XLOUD_TARGETS:
            if await client.container_exists(container):
                raise RuntimeError("XLOUD bootstrap recovery left a target container")
        await _verify_xloud_guard_resources(client, plan)
        return {"outcome": "bootstrap_absent_restored"}
    if "absent" in previous_cids.values() or not all(
        isinstance(value, str) and value for value in previous_cids.values()
    ):
        raise RuntimeError("XLOUD recovery evidence describes a partial prior topology")

    live: dict[str, dict[str, Any]] = {}
    for role, container, _image_name in _XLOUD_TARGETS:
        prefix = role.upper()
        expected_id = failed.get(f"PREVIOUS_{prefix}_IMAGE_ID")
        expected_ref = failed.get(f"PREVIOUS_{prefix}_REF")
        if not isinstance(expected_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_id):
            raise RuntimeError("XLOUD recovery previous image ID is malformed")
        if not isinstance(expected_ref, str) or not expected_ref or any(
            ch in expected_ref for ch in ("\x00", "\n", "\r", "\t")
        ):
            raise RuntimeError("XLOUD recovery previous image reference is malformed")
        inspected = _single_inspect(await client.inspect(container, max_lines=10))
        runtime = inspected.get("State")
        config = inspected.get("Config")
        if not isinstance(runtime, dict) or not isinstance(config, dict):
            raise RuntimeError("XLOUD recovered target metadata is incomplete")
        health = runtime.get("Health")
        if inspected.get("Image") != expected_id or config.get("Image") != expected_ref:
            raise RuntimeError("XLOUD recovery did not restore the previous image generation")
        if inspected.get("RestartCount") != 0:
            raise RuntimeError("XLOUD recovered target has non-zero restart evidence")
        if not isinstance(health, dict) or health.get("Status") != "healthy":
            raise RuntimeError("XLOUD recovered target is not Docker-healthy")
        live[container] = {
            "container_id": inspected.get("Id"),
            "image_id": inspected.get("Image"),
            "image": config.get("Image"),
            "restart_count": inspected.get("RestartCount"),
            "started_at": runtime.get("StartedAt"),
            "health": health.get("Status"),
        }
    await _verify_xloud_guard_resources(client, plan)
    return {"outcome": "previous_generation_restored", "targets": live}


async def docker_deploy_xloud(
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    source_revision: str,
    image_namespace: str,
    timeout: int = 720,
) -> dict[str, Any]:
    """Prepare a guarded two-image XLOUD rollout using deploy-xloud.sh."""
    timeout = max(180, min(timeout, 900))
    try:
        plan = await _prepare_xloud_deploy(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            source_revision=source_revision,
            image_namespace=image_namespace,
        )
    except ValueError as exc:
        return tool_error(
            tool="docker_deploy_xloud",
            code="INVALID_INPUT",
            message=str(exc),
            source="docker",
            retryable=False,
        )
    except RuntimeError as exc:
        return tool_error(
            tool="docker_deploy_xloud",
            code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )
    action = _confirm_store().create_action(
        "docker_deploy_xloud",
        {
            "candidate_project": candidate_project,
            "expected_head_sha": expected_head_sha,
            "expected_script_blob_sha": expected_script_blob_sha,
            "expected_script_sha256": expected_script_sha256,
            "source_revision": source_revision,
            "image_namespace": plan["image_namespace"],
            "target_images": plan["target_images"],
            "image_ids": plan["image_ids"],
            "state_volume_fingerprint": plan["state_volume_fingerprint"],
            "network_fingerprints": plan["network_fingerprints"],
            "audit_snapshot": plan["audit_snapshot"],
            "secret_fingerprint": plan["secret_fingerprint"],
            "prior_mode": plan["prior_mode"],
            "topology_fingerprint": plan["topology_fingerprint"],
            "timeout": timeout,
        },
        f"Deploy XLOUD two-image contract from {candidate_project}@{expected_head_sha[:12]}",
        risk="high",
        required_scope="mcp:docker:admin",
    )
    return _confirmation_response(action)


async def _docker_deploy_xloud_impl(
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    source_revision: str,
    image_namespace: str,
    target_images: dict[str, dict[str, str]],
    image_ids: dict[str, str],
    state_volume_fingerprint: str,
    network_fingerprints: dict[str, str],
    audit_snapshot: dict[str, str],
    secret_fingerprint: str,
    prior_mode: str,
    topology_fingerprint: str,
    timeout: int = 720,
) -> dict[str, Any]:
    try:
        plan = await _prepare_xloud_deploy(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            source_revision=source_revision,
            image_namespace=image_namespace,
        )
        confirmed = {
            "target_images": target_images,
            "image_ids": image_ids,
            "state_volume_fingerprint": state_volume_fingerprint,
            "network_fingerprints": network_fingerprints,
            "audit_snapshot": audit_snapshot,
            "secret_fingerprint": secret_fingerprint,
            "prior_mode": prior_mode,
            "topology_fingerprint": topology_fingerprint,
        }
        if any(plan[key] != value for key, value in confirmed.items()):
            raise RuntimeError("XLOUD confirmation identities changed before execution")
    except (ValueError, RuntimeError) as exc:
        return tool_error(
            tool="docker_deploy_xloud",
            code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )

    client = _docker_client()
    helper_kwargs = {
        "helper_image_id": plan["helper_image_id"],
        "candidate_root": plan["candidate_root"],
        "operator_root": plan["operator_root"],
        "script_path": plan["script_path"],
        "env_file": plan["env_file"],
        "state_volume": plan["state_volume"],
        "site_image": plan["target_images"]["site"]["image"],
        "intake_image": plan["target_images"]["intake"]["image"],
        "site_image_id": plan["image_ids"]["site"],
        "intake_image_id": plan["image_ids"]["intake"],
        "source_revision": source_revision,
        "secret_fingerprint": plan["secret_fingerprint"],
        "stability_seconds": 90,
        "timeout": timeout,
    }
    run = await client.run_xloud_deploy_contract_helper(**helper_kwargs, mode="deploy")
    payload, output_tail = _xloud_helper_payload(client, run)
    ambiguous = run.exit_code == -1 or (
        isinstance(payload, dict) and (payload.get("timed_out") is True or payload.get("exit_code") == 124)
    )

    if ambiguous:
        try:
            recovery_plan = await _prepare_xloud_deploy(
                candidate_project=candidate_project,
                expected_head_sha=expected_head_sha,
                expected_script_blob_sha=expected_script_blob_sha,
                expected_script_sha256=expected_script_sha256,
                source_revision=source_revision,
                image_namespace=image_namespace,
                require_stable_topology=False,
            )
            for key in (
                "target_images",
                "image_ids",
                "state_volume_fingerprint",
                "network_fingerprints",
                "audit_snapshot",
                "secret_fingerprint",
            ):
                if recovery_plan[key] != plan[key]:
                    raise RuntimeError(f"XLOUD {key} changed before recovery")
        except (ValueError, RuntimeError) as exc:
            return tool_error(
                tool="docker_deploy_xloud",
                code="DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS",
                message=f"XLOUD deploy outcome is unknown and recovery preflight changed: {exc}",
                result={"source_revision": source_revision, "output_tail": output_tail},
                source="docker",
                retryable=False,
                redacted=True,
                hint="Do not retry deploy. Reconcile the durable pending/LKG evidence first.",
            )
        recovery_kwargs = {**helper_kwargs}
        for key in ("helper_image_id", "candidate_root", "operator_root", "script_path", "env_file", "state_volume"):
            recovery_kwargs[key] = recovery_plan[key]
        recovery = await client.run_xloud_deploy_contract_helper(**recovery_kwargs, mode="recover")
        recovery_payload, recovery_tail = _xloud_helper_payload(client, recovery)
        if (
            recovery.exit_code == 0
            and isinstance(recovery_payload, dict)
            and recovery_payload.get("exit_code") == 0
            and recovery_payload.get("pending_exists") is False
        ):
            lkg = recovery_payload.get("lkg")
            if isinstance(lkg, dict) and lkg.get("SOURCE_REVISION") == source_revision:
                try:
                    accepted = await _accept_xloud_payload(
                        client,
                        payload=recovery_payload,
                        plan=plan,
                    )
                except RuntimeError:
                    accepted = None
                if accepted is not None:
                    return tool_success(
                        "docker_deploy_xloud",
                        result={
                            "source_revision": source_revision,
                            "target_images": plan["target_images"],
                            "image_ids": plan["image_ids"],
                            "reconciled_after_unknown": True,
                            "recovery_result": "target_accepted",
                            "deployment": accepted,
                            "output_tail": recovery_tail,
                        },
                        source="docker",
                        dangerous=True,
                        redacted=True,
                    )
            failed = recovery_payload.get("failed")
            if recovery_payload.get("failed_exists") is True and isinstance(failed, dict):
                try:
                    recovered = await _verify_xloud_recovery(client, failed=failed, plan=plan)
                except RuntimeError:
                    recovered = None
                if recovered is not None:
                    return tool_error(
                        tool="docker_deploy_xloud",
                        code="DEPLOY_CONTRACT_RECOVERED_NOT_DEPLOYED",
                        message="XLOUD unknown outcome was safely reconciled without accepting the target generation",
                        result={
                            "source_revision": source_revision,
                            "reconciled_after_unknown": True,
                            "recovery": recovered,
                            "output_tail": recovery_tail,
                        },
                        source="docker",
                        retryable=True,
                        redacted=True,
                        hint="Create a fresh preflight/confirmation before any later deploy attempt.",
                    )
        return tool_error(
            tool="docker_deploy_xloud",
            code="DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS",
            message="XLOUD deploy outcome could not be proven after repo-owned recovery",
            result={"source_revision": source_revision, "output_tail": recovery_tail},
            source="docker",
            retryable=False,
            redacted=True,
            hint="Reconcile durable pending/LKG state before creating a new deploy confirmation.",
        )

    if run.exit_code != 0 or not isinstance(payload, dict) or payload.get("exit_code") != 0:
        return tool_error(
            tool="docker_deploy_xloud",
            code=("DEPLOY_CONTRACT_EVIDENCE_INVALID" if payload is None else "DEPLOY_CONTRACT_FAILED"),
            message=(
                "XLOUD deploy helper returned invalid evidence"
                if payload is None
                else "XLOUD deploy contract failed"
            ),
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
            redacted=True,
        )
    try:
        accepted = await _accept_xloud_payload(client, payload=payload, plan=plan)
    except RuntimeError as exc:
        return tool_error(
            tool="docker_deploy_xloud",
            code="DEPLOY_CONTRACT_VERIFICATION_FAILED",
            message=str(exc),
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
            redacted=True,
        )
    return tool_success(
        "docker_deploy_xloud",
        result={
            "source_revision": source_revision,
            "script_blob_sha": expected_script_blob_sha,
            "script_sha256": expected_script_sha256,
            "target_images": plan["target_images"],
            "image_ids": plan["image_ids"],
            "stability_seconds": int(accepted["state"]["STABILITY_SECONDS"]),
            "reconciled_after_unknown": False,
            "deployment": accepted,
            "output_tail": output_tail,
        },
        source="docker",
        dangerous=True,
        redacted=True,
    )


async def docker_site_audit_deploy_status() -> dict[str, Any]:
    """Read and verify the durable Site Audit LKG against the live stack.

    This surface is intentionally parameterless: it reads only the fixed
    deploy-state volume used by the guarded Site Audit rollout contract.  The
    volume is mounted read-only into an isolated helper with no Docker socket
    and no network.  Raw state is never returned; only schema-validated,
    live-verified acceptance evidence is exposed.
    """
    client = _docker_client()
    try:
        volume = await client.volume_metadata(_SITE_AUDIT_STATE_VOLUME)
        if volume.get("Name") != _SITE_AUDIT_STATE_VOLUME:
            raise RuntimeError("Site Audit deploy-state volume identity mismatch")
        if volume.get("Driver") not in (None, "", "local"):
            raise RuntimeError("Site Audit deploy-state volume driver is unsupported")
        if volume.get("Options") not in (None, {}, []):
            raise RuntimeError("Site Audit deploy-state volume has unsupported driver options")

        helper_container = os.environ.get(
            "MCP_DEPLOY_HELPER_CONTAINER", _DEPLOY_HELPER_CONTAINER
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", helper_container):
            raise RuntimeError("MCP_DEPLOY_HELPER_CONTAINER is invalid")
        helper_image_id = await client.container_image_id(helper_container)
        run = await client.read_site_audit_deploy_state(
            helper_image_id=helper_image_id,
            state_volume=_SITE_AUDIT_STATE_VOLUME,
        )
        if run.exit_code != 0:
            raise RuntimeError("Site Audit durable deploy state could not be read safely")
        try:
            payload = json.loads((run.stdout or "").strip())
        except json.JSONDecodeError as exc:
            raise RuntimeError("Site Audit durable deploy state reader returned invalid evidence") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise RuntimeError("Site Audit durable deploy state reader schema is invalid")
        if payload.get("pending_exists") is not False:
            raise RuntimeError("Site Audit has unresolved pending deploy evidence")
        if payload.get("state_exists") is not True:
            raise RuntimeError("Site Audit durable LKG state is missing or unsafe")
        state = payload.get("state")
        if not isinstance(state, dict):
            raise RuntimeError("Site Audit durable LKG state is malformed")
        source_revision, image_namespace, target_images = _site_audit_lkg_target_images(state)
        accepted = await _accept_site_audit_payload(
            client,
            payload={"pending_exists": False, "state": state},
            source_revision=source_revision,
            target_images=target_images,
        )
    except (ValueError, RuntimeError) as exc:
        return tool_error(
            tool="docker_site_audit_deploy_status",
            code="DEPLOY_CONTRACT_VERIFICATION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
            redacted=True,
        )

    return tool_success(
        "docker_site_audit_deploy_status",
        result={
            "status": "accepted_lkg",
            "source_revision": source_revision,
            "deploy_generation": state["deploy_generation"],
            "stability_seconds": state["stability_seconds"],
            "crm_route_surface_verified": True,
            "pending_exists": False,
            "image_namespace": image_namespace,
            "targets": accepted["live"]["targets"],
            "protected": accepted["live"]["protected"],
        },
        source="docker",
        redacted=True,
    )


async def docker_deploy_site_audit(
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    source_revision: str,
    image_namespace: str,
    timeout: int = 720,
) -> dict[str, Any]:
    """Prepare a guarded five-image Site Audit rollout using deploy-site-audit.sh.

    ADMIN + DANGEROUS: this read-only preflight returns one confirmation that
    pins the exact candidate/script/source revision and complete five-image set.
    """
    timeout = max(180, min(timeout, 900))
    try:
        plan = await _prepare_site_audit_deploy(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            source_revision=source_revision,
            image_namespace=image_namespace,
        )
    except ValueError as exc:
        return tool_error(
            tool="docker_deploy_site_audit",
            code="INVALID_INPUT",
            message=str(exc),
            source="docker",
            retryable=False,
        )
    except RuntimeError as exc:
        return tool_error(
            tool="docker_deploy_site_audit",
            code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )
    action = _confirm_store().create_action(
        "docker_deploy_site_audit",
        {
            "candidate_project": candidate_project,
            "expected_head_sha": expected_head_sha,
            "expected_script_blob_sha": expected_script_blob_sha,
            "expected_script_sha256": expected_script_sha256,
            "source_revision": source_revision,
            "image_namespace": plan["image_namespace"],
            "target_images": plan["target_images"],
            "timeout": timeout,
        },
        (
            f"Deploy Site Audit five-image contract from {candidate_project}@"
            f"{expected_head_sha[:12]}"
        ),
        risk="high",
        required_scope="mcp:docker:admin",
    )
    return _confirmation_response(action)


async def _docker_deploy_site_audit_impl(
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    source_revision: str,
    image_namespace: str,
    target_images: dict[str, dict[str, str]],
    timeout: int = 720,
) -> dict[str, Any]:
    try:
        plan = await _prepare_site_audit_deploy(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            source_revision=source_revision,
            image_namespace=image_namespace,
        )
        if target_images != plan["target_images"]:
            raise RuntimeError("Site Audit five-image confirmation set changed before execution")
    except (ValueError, RuntimeError) as exc:
        return tool_error(
            tool="docker_deploy_site_audit",
            code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )

    client = _docker_client()
    helper_kwargs = {
        "helper_image_id": plan["helper_image_id"],
        "candidate_root": plan["candidate_root"],
        "operator_root": plan["operator_root"],
        "script_path": plan["script_path"],
        "env_file": plan["env_file"],
        "state_volume": plan["state_volume"],
        "image_namespace": plan["image_namespace"],
        "source_revision": source_revision,
        "timeout": timeout,
    }
    run = await client.run_site_audit_deploy_contract_helper(**helper_kwargs, recover=False)
    payload, output_tail = _site_audit_helper_payload(client, run)
    ambiguous = run.exit_code == -1 or (
        isinstance(payload, dict) and payload.get("exit_code") == 124
    )

    if ambiguous:
        try:
            recovery_plan = await _prepare_site_audit_deploy(
                candidate_project=candidate_project,
                expected_head_sha=expected_head_sha,
                expected_script_blob_sha=expected_script_blob_sha,
                expected_script_sha256=expected_script_sha256,
                source_revision=source_revision,
                image_namespace=image_namespace,
            )
            if recovery_plan["target_images"] != plan["target_images"]:
                raise RuntimeError("Site Audit five-image set changed before recovery")
        except (ValueError, RuntimeError) as exc:
            return tool_error(
                tool="docker_deploy_site_audit",
                code="DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS",
                message=(
                    "Site Audit deploy outcome is unknown and recovery preflight no longer "
                    f"matches the confirmed candidate: {exc}"
                ),
                result={
                    "source_revision": source_revision,
                    "target_images": plan["target_images"],
                    "output_tail": output_tail,
                },
                source="docker",
                retryable=False,
                hint="Do not retry deploy. Reconcile the durable pending/LKG state with the original candidate identity.",
            )

        recovery_kwargs = {
            **helper_kwargs,
            "helper_image_id": recovery_plan["helper_image_id"],
            "candidate_root": recovery_plan["candidate_root"],
            "operator_root": recovery_plan["operator_root"],
            "script_path": recovery_plan["script_path"],
            "env_file": recovery_plan["env_file"],
            "state_volume": recovery_plan["state_volume"],
            "image_namespace": recovery_plan["image_namespace"],
        }
        recovery = await client.run_site_audit_deploy_contract_helper(
            **recovery_kwargs,
            recover=True,
        )
        recovery_payload, recovery_tail = _site_audit_helper_payload(client, recovery)
        if (
            recovery.exit_code == 0
            and isinstance(recovery_payload, dict)
            and recovery_payload.get("exit_code") == 0
            and recovery_payload.get("mode") == "recover"
            and recovery_payload.get("pending_exists") is False
        ):
            state = recovery_payload.get("state")
            if isinstance(state, dict) and state.get("source_revision") == source_revision:
                try:
                    accepted = await _accept_site_audit_payload(
                        client,
                        payload=recovery_payload,
                        source_revision=source_revision,
                        target_images=plan["target_images"],
                    )
                except RuntimeError:
                    accepted = None
                if accepted is not None:
                    return tool_success(
                        "docker_deploy_site_audit",
                        result={
                            "source_revision": source_revision,
                            "script_blob_sha": expected_script_blob_sha,
                            "script_sha256": expected_script_sha256,
                            "target_images": plan["target_images"],
                            "reconciled_after_unknown": True,
                            "recovery_result": "target_accepted",
                            "deployment": accepted,
                            "output_tail": recovery_tail,
                        },
                        source="docker",
                        dangerous=True,
                        redacted=True,
                    )

            pending_before = recovery_payload.get("pending_before")
            if isinstance(pending_before, dict):
                try:
                    recovered = await _verify_site_audit_recovery(
                        client,
                        pending=pending_before,
                        source_revision=source_revision,
                        target_images=plan["target_images"],
                    )
                except RuntimeError:
                    recovered = None
                if recovered is not None:
                    return tool_error(
                        tool="docker_deploy_site_audit",
                        code="DEPLOY_CONTRACT_RECOVERED_NOT_DEPLOYED",
                        message=(
                            "Site Audit unknown outcome was safely reconciled without accepting "
                            "the requested target generation"
                        ),
                        result={
                            "source_revision": source_revision,
                            "target_images": plan["target_images"],
                            "reconciled_after_unknown": True,
                            "recovery": recovered,
                            "output_tail": recovery_tail,
                        },
                        source="docker",
                        retryable=True,
                        hint="Run a fresh preflight and create a new confirmation before any later deploy attempt.",
                    )
        return tool_error(
            tool="docker_deploy_site_audit",
            code="DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS",
            message=(
                "Site Audit deploy outcome could not be proven after the repo-owned "
                "--recover reconciliation path"
            ),
            result={
                "source_revision": source_revision,
                "target_images": plan["target_images"],
                "output_tail": recovery_tail,
            },
            source="docker",
            retryable=False,
            hint="Reconcile the durable pending/LKG state before creating a new deploy confirmation.",
        )

    if run.exit_code != 0 or not isinstance(payload, dict) or payload.get("exit_code") != 0:
        return tool_error(
            tool="docker_deploy_site_audit",
            code=(
                "DEPLOY_CONTRACT_EVIDENCE_INVALID"
                if payload is None
                else "DEPLOY_CONTRACT_FAILED"
            ),
            message=(
                "Site Audit deploy helper returned invalid evidence"
                if payload is None
                else str(payload.get("error") or "Site Audit deploy contract failed")
            ),
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
        )
    try:
        accepted = await _accept_site_audit_payload(
            client,
            payload=payload,
            source_revision=source_revision,
            target_images=plan["target_images"],
        )
    except RuntimeError as exc:
        return tool_error(
            tool="docker_deploy_site_audit",
            code="DEPLOY_CONTRACT_VERIFICATION_FAILED",
            message=str(exc),
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
        )
    return tool_success(
        "docker_deploy_site_audit",
        result={
            "source_revision": source_revision,
            "script_blob_sha": expected_script_blob_sha,
            "script_sha256": expected_script_sha256,
            "target_images": plan["target_images"],
            "reconciled_after_unknown": False,
            "deployment": accepted,
            "output_tail": output_tail,
        },
        source="docker",
        dangerous=True,
        redacted=True,
    )


async def docker_deploy_contract(
    contract: str,
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    target_image: str,
    timeout: int = 480,
) -> dict[str, Any]:
    """Prepare one allowlisted repository deploy contract.

    ADMIN + DANGEROUS: this call is read-only preflight and returns a
    confirmation action. Confirmation revalidates the exact candidate HEAD,
    Git blob, script SHA-256, immutable local image and operator volume.
    """
    timeout = max(120, min(timeout, 600))
    if contract == "telegram-storage":
        try:
            plan = await _prepare_telegram_storage_deploy(
                candidate_project=candidate_project,
                expected_head_sha=expected_head_sha,
                expected_script_blob_sha=expected_script_blob_sha,
                expected_script_sha256=expected_script_sha256,
                target_image=target_image,
            )
        except ValueError as exc:
            return tool_error(
                tool="docker_deploy_contract",
                code="INVALID_INPUT",
                message=str(exc),
                source="docker",
                retryable=False,
            )
        except RuntimeError as exc:
            return tool_error(
                tool="docker_deploy_contract",
                code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
                message=str(exc),
                source="docker",
                retryable=False,
            )
        action = _confirm_store().create_action(
            "docker_deploy_contract",
            {
                "contract": contract,
                "candidate_project": candidate_project,
                "expected_head_sha": expected_head_sha,
                "expected_script_blob_sha": expected_script_blob_sha,
                "expected_script_sha256": expected_script_sha256,
                "target_image": target_image,
                "telegram_storage_confirmation": {
                    "state_volume": plan["state_volume"],
                    "state_volume_fingerprint": plan["state_volume_fingerprint"],
                    "state_fingerprint": plan["state_fingerprint"],
                    "prior_mode": plan["prior_mode"],
                    "runtime_fingerprint": plan["runtime_fingerprint"],
                },
                "timeout": timeout,
            },
            (
                f"Deploy contract {contract} from {candidate_project}@"
                f"{expected_head_sha[:12]} to an exact image digest"
            ),
            risk="high",
            required_scope="mcp:docker:admin",
        )
        return _confirmation_response(action)

    try:
        await _prepare_deploy_contract(
            contract=contract,
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            target_image=target_image,
        )
    except ValueError as exc:
        return tool_error(
            tool="docker_deploy_contract",
            code="INVALID_INPUT",
            message=str(exc),
            source="docker",
            retryable=False,
        )
    except RuntimeError as exc:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )

    summary = (
        f"Deploy contract {contract} from {candidate_project}@{expected_head_sha[:12]} "
        f"to {target_image}"
    )
    action = _confirm_store().create_action(
        "docker_deploy_contract",
        {
            "contract": contract,
            "candidate_project": candidate_project,
            "expected_head_sha": expected_head_sha,
            "expected_script_blob_sha": expected_script_blob_sha,
            "expected_script_sha256": expected_script_sha256,
            "target_image": target_image,
            "timeout": timeout,
        },
        summary,
        risk="high",
        required_scope="mcp:docker:admin",
    )
    return _confirmation_response(action)


async def _docker_deploy_contract_impl(
    contract: str,
    candidate_project: str,
    expected_head_sha: str,
    expected_script_blob_sha: str,
    expected_script_sha256: str,
    target_image: str,
    timeout: int = 480,
    telegram_storage_confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one confirmed deploy plan and return verified evidence."""
    if contract == "telegram-storage":
        if not isinstance(telegram_storage_confirmation, dict):
            return tool_error(
                tool="docker_deploy_contract",
                code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
                message="Telegram Storage confirmation evidence is missing",
                source="docker",
                retryable=False,
            )
        required = {
            "state_volume",
            "state_volume_fingerprint",
            "state_fingerprint",
            "prior_mode",
            "runtime_fingerprint",
        }
        if set(telegram_storage_confirmation) != required or not all(
            isinstance(telegram_storage_confirmation[key], str)
            and telegram_storage_confirmation[key]
            for key in required
        ):
            return tool_error(
                tool="docker_deploy_contract",
                code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
                message="Telegram Storage confirmation evidence is malformed",
                source="docker",
                retryable=False,
            )
        return await _docker_deploy_telegram_storage_contract_impl(
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            target_image=target_image,
            state_volume=telegram_storage_confirmation["state_volume"],
            state_volume_fingerprint=telegram_storage_confirmation[
                "state_volume_fingerprint"
            ],
            state_fingerprint=telegram_storage_confirmation["state_fingerprint"],
            prior_mode=telegram_storage_confirmation["prior_mode"],
            runtime_fingerprint=telegram_storage_confirmation["runtime_fingerprint"],
            timeout=timeout,
        )

    try:
        plan = await _prepare_deploy_contract(
            contract=contract,
            candidate_project=candidate_project,
            expected_head_sha=expected_head_sha,
            expected_script_blob_sha=expected_script_blob_sha,
            expected_script_sha256=expected_script_sha256,
            target_image=target_image,
        )
    except (ValueError, RuntimeError) as exc:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_PRECONDITION_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )

    client = _docker_client()
    spec = plan["spec"]
    run = await client.run_deploy_contract_helper(
        helper_image_id=plan["helper_image_id"],
        candidate_root=plan["candidate_root"],
        infra_root=plan["infra_root"],
        script_path=plan["script_path"],
        state_file=plan["state_file"],
        image_env=spec["image_env"],
        image_ref=target_image,
        profile_volume=spec["profile_volume"],
        profile_mountpoint=plan["profile_mountpoint"],
        timeout=timeout,
    )

    if run.exit_code == -1:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS",
            message=(
                "deploy helper exceeded the outer Docker execution budget; "
                "the deploy transaction may still have reached the host daemon"
            ),
            result={
                "output_tail": _sanitize_deploy_tail(
                    client, (run.stdout or "") + "\n" + (run.stderr or "")
                ),
                "target_image": target_image,
                "verify_container": spec["verify_container"],
            },
            source="docker",
            retryable=True,
            hint=(
                "Reconcile the target container identity/health and deployment state "
                "before retrying the exact same contract."
            ),
        )

    try:
        parsed = json.loads((run.stdout or "").strip())
    except json.JSONDecodeError:
        parsed = None
    helper_payload = parsed if isinstance(parsed, dict) else None
    if helper_payload is None:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_EVIDENCE_INVALID",
            message="deploy helper returned invalid evidence",
            result={
                "exit_code": run.exit_code,
                "output_tail": _sanitize_deploy_tail(
                    client, (run.stdout or "") + "\n" + (run.stderr or "")
                ),
            },
            source="docker",
            retryable=False,
        )

    output_tail = _sanitize_deploy_tail(
        client, str(helper_payload.get("output_tail") or "")
    )
    helper_exit = helper_payload.get("exit_code")
    if run.exit_code != 0 or helper_exit != 0:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_FAILED",
            message=str(helper_payload.get("error") or "deploy contract failed"),
            result={
                "exit_code": helper_exit if isinstance(helper_exit, int) else run.exit_code,
                "output_tail": output_tail,
            },
            source="docker",
            retryable=False,
        )

    state = helper_payload.get("state")
    if not isinstance(state, dict):
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_EVIDENCE_INVALID",
            message="deploy helper returned invalid deployment state evidence",
            source="docker",
            retryable=False,
        )

    try:
        first = _verify_deploy_evidence(
            spec=spec,
            inspected=_single_inspect(
                await client.inspect(spec["verify_container"], max_lines=10)
            ),
            target_image=target_image,
            target_image_id=plan["target_image_id"],
            state=state,
        )
        await asyncio.sleep(_DEPLOY_STABILITY_SECONDS)
        second = _verify_deploy_evidence(
            spec=spec,
            inspected=_single_inspect(
                await client.inspect(spec["verify_container"], max_lines=10)
            ),
            target_image=target_image,
            target_image_id=plan["target_image_id"],
            state=state,
        )
    except RuntimeError as exc:
        return tool_error(
            tool="docker_deploy_contract",
            code="DEPLOY_CONTRACT_VERIFICATION_FAILED",
            message=str(exc),
            result={"output_tail": output_tail},
            source="docker",
            retryable=False,
        )

    for field in ("container_id", "restart_count", "started_at", "deploy_generation"):
        if first.get(field) != second.get(field):
            return tool_error(
                tool="docker_deploy_contract",
                code="DEPLOY_CONTRACT_UNSTABLE",
                message=f"deployment changed during {_DEPLOY_STABILITY_SECONDS}s stability window",
                result={
                    "field": field,
                    "before": first.get(field),
                    "after": second.get(field),
                    "output_tail": output_tail,
                },
                source="docker",
                retryable=False,
            )

    return tool_success(
        "docker_deploy_contract",
        result={
            "contract": contract,
            "source_head_sha": expected_head_sha,
            "script_blob_sha": expected_script_blob_sha,
            "script_sha256": expected_script_sha256,
            "stability_seconds": _DEPLOY_STABILITY_SECONDS,
            "deployment": second,
            "output_tail": output_tail,
        },
        source="docker",
        dangerous=True,
        redacted=True,
    )


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
    "docker_deploy_contract": _docker_deploy_contract_impl,
    "docker_deploy_site_audit": _docker_deploy_site_audit_impl,
    "docker_deploy_xloud": _docker_deploy_xloud_impl,
    "docker_deploy_content_harvester": _docker_deploy_content_harvester_impl,
    "docker_migrate_content_harvester": _docker_migrate_content_harvester_impl,
}


def _get_confirmation_owner_fingerprint() -> str | None:
    """Return a non-secret fingerprint for the authenticated MCP caller.

    The raw bearer token is used only as input to SHA-256 and is never stored,
    returned, logged, or included in errors. A 60-second confirmation is thus
    fenced to the exact authenticated bearer/client pair that created it.
    Contexts without an authenticated MCP request fail closed for action-id
    confirmation; the legacy secret-token path remains available for tests and
    backwards compatibility.
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        access_token = get_access_token()
    except Exception:
        return None
    if access_token is None:
        return None

    token = str(getattr(access_token, "token", "") or "")
    client_id = str(getattr(access_token, "client_id", "") or "")
    if not token or not client_id:
        return None
    material = client_id.encode("utf-8") + b"\0" + token.encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _confirmation_response(action: ConfirmAction) -> dict[str, Any]:
    # Bind ownership before action_id becomes observable to the caller. Never
    # replace an explicitly pre-bound owner on an internally-created action.
    if action.owner_fingerprint is None:
        action.owner_fingerprint = _get_confirmation_owner_fingerprint()
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
    try:
        resolved_dir, label = _resolve_compose_confirmation_target(project_dir, dc)
    except ValueError as exc:
        return tool_error(
            tool="docker_compose_down",
            code="INVALID_INPUT",
            message=str(exc),
            source="docker",
        )
    parts = []
    if project_dir:
        parts.append(f"project={label or resolved_dir}")
    if volumes:
        parts.append("--volumes")
    summary = f"Compose down {' '.join(parts)}"
    kwargs = {
        "project_dir": project_dir,
        "remove_orphans": remove_orphans,
        "timeout": timeout,
        "volumes": volumes,
    }
    if label:
        kwargs["_resolved_project_dir"] = resolved_dir
        kwargs["_project_label"] = label
    action = _confirm_store().create_action(
        "docker_compose_down",
        kwargs,
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

async def confirm_operation(
    token: str | None = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    """Confirm one exact pending Docker action.

    Legacy callers may present the one-time secret ``token``. ChatGPT-facing
    callers may instead present the public ``action_id`` when the platform
    cannot safely relay opaque confirmation secrets; that path is additionally
    fenced to the authenticated bearer/client pair that created the action.
    Exactly one selector is required.
    """
    if bool(token) == bool(action_id):
        return tool_error(
            tool="confirm_operation",
            code="INVALID_INPUT",
            message="Provide exactly one of token or action_id",
            retryable=False,
            source="docker",
        )

    by_action_id = action_id is not None
    if by_action_id:
        action, status = _confirm_store().peek_action_id(action_id or "")
        code_map = {
            ConfirmStatus.INVALID: "INVALID_INPUT",
            ConfirmStatus.EXPIRED: "CONFIRM_TOKEN_EXPIRED",
            ConfirmStatus.CONSUMED: "CONFIRM_TOKEN_CONSUMED",
        }
        msg_map = {
            ConfirmStatus.INVALID: "Invalid confirmation action",
            ConfirmStatus.EXPIRED: "Confirmation action expired (TTL 60s)",
            ConfirmStatus.CONSUMED: "Confirmation action already used",
        }
    else:
        action, status = _confirm_store().peek_action(token or "")
        code_map = {
            ConfirmStatus.INVALID: "CONFIRM_TOKEN_INVALID",
            ConfirmStatus.EXPIRED: "CONFIRM_TOKEN_EXPIRED",
            ConfirmStatus.CONSUMED: "CONFIRM_TOKEN_CONSUMED",
        }
        msg_map = {
            ConfirmStatus.INVALID: "Invalid confirmation token",
            ConfirmStatus.EXPIRED: "Confirmation token expired (TTL 60s)",
            ConfirmStatus.CONSUMED: "Confirmation token already used",
        }

    if action is None:
        code = code_map.get(status, "INTERNAL_ERROR")
        msg = msg_map.get(status, "Unknown error")
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
            hint=(
                "Call the dangerous tool again to create a fresh pending action."
                if by_action_id
                else "Call the dangerous tool again to get a new token."
            ),
            retryable=False,
            source="docker",
        )

    if by_action_id:
        current_owner = _get_confirmation_owner_fingerprint()
        expected_owner = action.owner_fingerprint
        if current_owner is None or expected_owner is None:
            return tool_error(
                tool="confirm_operation",
                code="AUTH_ERROR",
                message="Authenticated confirmation owner is unavailable",
                retryable=False,
                source="docker",
            )
        if not hmac.compare_digest(current_owner, expected_owner):
            return tool_error(
                tool="confirm_operation",
                code="PERMISSION_DENIED",
                message="Pending action belongs to a different authenticated caller",
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
            message=(
                "Confirmation action already used"
                if by_action_id
                else "Confirmation token already used"
            ),
            hint=(
                "Call the dangerous tool again to create a fresh pending action."
                if by_action_id
                else "Call the dangerous tool again to get a new token."
            ),
            retryable=False,
            source="docker",
        )

    label = action.kwargs.get("_project_label")
    resolved_dir = action.kwargs.get("_resolved_project_dir")
    dispatch_kwargs = {
        key: value
        for key, value in action.kwargs.items()
        if key not in {"_project_label", "_resolved_project_dir"}
    }
    if "_resolved_project_dir" in action.kwargs:
        dispatch_kwargs["project_dir"] = resolved_dir

    try:
        result = await handler(**dispatch_kwargs)
    except Exception as exc:
        return tool_error(
            tool=action.tool,
            code="DOCKER_COMMAND_FAILED",
            message=_redact_bound_compose_path(str(exc), resolved_dir, label),
            source="docker",
            retryable=False,
        )

    if isinstance(result, dict) and "ok" in result:
        return result

    if isinstance(result, str):
        return tool_success(
            tool=action.tool,
            result={"output": _redact_bound_compose_path(result, resolved_dir, label)},
            source="docker",
        )

    if isinstance(result, RunResult):
        payload = {
            "stdout": _redact_bound_compose_path(result.stdout, resolved_dir, label),
            "stderr": _redact_bound_compose_path(result.stderr, resolved_dir, label),
            "exit_code": result.exit_code,
        }
        if result.exit_code != 0:
            return tool_error(
                tool=action.tool,
                code="DOCKER_COMMAND_FAILED",
                message="Docker command failed",
                result=payload,
                source="docker",
                retryable=False,
            )
        return tool_success(
            tool=action.tool,
            result=payload,
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
        "docker_deploy_contract",
        "docker_site_audit_deploy_status",
        "docker_deploy_site_audit",
        "docker_deploy_xloud",
        "docker_deploy_content_harvester",
        "docker_migrate_content_harvester",
        "confirm_operation",
        "docker_pending_actions",
    ):
        register_tool(_tool)(globals()[_tool])
