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
import json
import os
import re
import stat
import subprocess
import time as _time
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
) -> dict[str, Any]:
    """Execute one confirmed deploy plan and return verified evidence."""
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
    _resolve_compose_project_dir(project_dir, dc)
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

    if isinstance(result, RunResult):
        payload = {
            "stdout": result.stdout,
            "stderr": result.stderr,
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
        "docker_deploy_site_audit",
        "confirm_operation",
        "docker_pending_actions",
    ):
        register_tool(_tool)(globals()[_tool])
