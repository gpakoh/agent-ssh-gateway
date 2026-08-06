"""Shared utilities for fleet MCP adapters."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, TypedDict

import httpx
from starlette.requests import Request

# Gitea/GitHub owner and repo names never legitimately contain "/" — this is
# also what stops path-segment injection: gitea_client.py/github_client.py
# build request paths via "/repos/{owner}/{repo}/...".format(owner=owner,
# repo=repo) against an httpx client with a base_url. httpx normalizes ".."
# segments in the resulting path against the FULL URL (base_url's path
# included), so an unvalidated owner like "foo/../../admin" reached
# {api_base}/admin/... instead of {api_base}/repos/foo/.../admin/... — a
# real endpoint escape past ALLOWED_ENDPOINTS, using the fleet adapter's own
# GITEA_TOKEN/GITHUB_TOKEN credential. Confirmed empirically via httpx's own
# request-building.
_REPO_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def validate_repo_owner_or_name(value: str, *, label: str = "value") -> str:
    """Validate a Gitea/GitHub owner or repo name — no "/", no traversal.

    Raises ValueError if value is empty, contains a path separator, or
    doesn't match the conservative safe-charset every real owner/repo name
    satisfies.
    """
    if not _REPO_OWNER_RE.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def validate_repo_path(path: str) -> str:
    """Validate a repository-relative file path used in a contents API call.

    Unlike owner/repo, a real path legitimately contains "/" for
    subdirectories — but must never contain a ".." segment, a leading "/"
    (absolute), or be empty, all of which let it escape the intended
    /repos/{owner}/{repo}/contents/{path} URL structure the same way an
    unvalidated owner/repo does.
    """
    if not path or path.startswith("/"):
        raise ValueError(f"Invalid path: {path!r}")
    parts = path.split("/")
    if any(p in ("", "..", ".") for p in parts):
        raise ValueError(f"Invalid path: {path!r}")
    return path


class FleetEnv(TypedDict):
    token: str
    host: str
    port: int


def tool_success(tool: str, result: Any, *, source: str) -> dict[str, Any]:
    """Return the canonical Contract v1 success envelope used by fleet tools."""
    return {
        "ok": True,
        "tool": tool,
        "result": result,
        "error": None,
        "meta": {
            "contract_version": "1",
            "source": source,
        },
    }


def tool_error(
    tool: str,
    code: str,
    message: str,
    *,
    source: str,
    retryable: bool = False,
) -> dict[str, Any]:
    """Return the canonical Contract v1 error envelope used by fleet tools."""
    return {
        "ok": False,
        "tool": tool,
        "result": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
        "meta": {
            "contract_version": "1",
            "source": source,
        },
    }


def json_safe(value: Any) -> Any:
    """Convert driver-native values to JSON-safe primitives without stringifying the payload."""
    import json

    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def minimize_repo_payload(data: dict[str, Any], *, provider: str) -> dict[str, Any]:
    """Return compact repository metadata without embedded owner PII or API URL noise."""
    owner = data.get("owner") or {}
    if provider == "gitea":
        visibility = (
            "private"
            if data.get("private")
            else "internal"
            if data.get("internal")
            else "public"
        )
        stars = data.get("stars_count")
    else:
        visibility = data.get("visibility") or (
            "private" if data.get("private") else "public"
        )
        stars = data.get("stargazers_count")

    return {
        "owner": {"login": owner.get("login")},
        "name": data.get("name"),
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "visibility": visibility,
        "language": data.get("language"),
        "default_branch": data.get("default_branch"),
        "permissions": data.get("permissions"),
        "counters": {
            "stars": stars,
            "forks": data.get("forks_count"),
            "watchers": data.get("watchers_count"),
            "open_issues": data.get("open_issues_count"),
        },
        "topics": data.get("topics", []),
        "archived": data.get("archived"),
        "html_url": data.get("html_url"),
    }


def remote_api_error(tool: str, source: str, exc: Exception) -> dict[str, Any]:
    """Map a fleet adapter's client exception to a Contract v1 error.

    Mirrors examples/mcp_server/server.py's _remote_api_error for the same
    GiteaClient/GitHubClient exception vocabulary (ValueError for bad
    input, PermissionError for 401/403, httpx.HTTPStatusError for any
    other non-2xx e.g. a 404 on a typo'd repo/issue number -- none of
    which are retryable, unlike a real transport failure). RuntimeError
    additionally covers this module's own _get_client() raising when a
    required token env var is missing. Not used for DockerClient, whose
    RuntimeError means a command actually failed, not a missing dependency.
    """
    if isinstance(exc, ValueError):
        return tool_error(tool, "INVALID_INPUT", str(exc), source=source)
    if isinstance(exc, PermissionError):
        return tool_error(tool, "AUTH_ERROR", str(exc), source=source)
    if isinstance(exc, httpx.HTTPStatusError):
        return tool_error(tool, "REMOTE_API_ERROR", str(exc), source=source)
    if isinstance(exc, RuntimeError):
        return tool_error(tool, "DEPENDENCY_MISSING", str(exc), source=source)
    return tool_error(tool, "INTERNAL_ERROR", str(exc), source=source)


def extract_auth_token(request: Request, valid_tokens: set[str]) -> str | None:
    """Extract and validate auth token from Bearer header or mcp_token query param.
    Returns the token string if valid, None otherwise.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ")
        if token in valid_tokens:
            return token
        return None
    token = request.query_params.get("mcp_token", "")
    if token and token in valid_tokens:
        return token
    return None


def normalize_list_response(
    value: Any,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a bare list in a stable dict for MCP tool output.
    MCP protocol expects tool results to be JSON objects (dicts), not bare arrays.
    This helper normalises list data to {"items": [...], "count": N}.
    """
    if isinstance(value, dict):
        if "items" in value:
            if "count" not in value:
                value["count"] = len(value["items"])
            if meta:
                value.update(meta)
            return value
        if meta:
            value.update(meta)
        return value
    if isinstance(value, list):
        result: dict[str, Any] = {"items": value, "count": len(value)}
        if meta:
            result.update(meta)
        return result
    return {"items": [], "count": 0, "error": "unexpected response type"}


def resolve_docker_host(hostname: str, network: str = "internal_net") -> str:
    """Resolve a Docker container name to its current IP on a given network.

    A static /etc/hosts entry for a container name drifts the moment that
    container is ever recreated (new container = new IP on the same
    network) — nothing re-writes it automatically. Resolving live via
    `docker inspect` avoids depending on that file staying in sync. Falls
    back to the hostname as-is when resolution fails (off-host, no Docker,
    different network, already an IP, etc.) so this is safe to call
    unconditionally.
    """
    try:
        fmt = f"{{{{.NetworkSettings.Networks.{network}.IPAddress}}}}"
        result = subprocess.run(
            ["docker", "inspect", "-f", fmt, hostname],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            ip = result.stdout.strip()
            if ip:
                return ip
    except Exception:
        pass
    return hostname


def get_fleet_env() -> FleetEnv:
    """Read standard fleet env vars, raise if missing."""
    token = os.environ.get("MCP_PUBLIC_TOKEN", "")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = os.environ.get("MCP_PORT", "")
    if not token:
        raise RuntimeError("MCP_PUBLIC_TOKEN is required")
    if not port:
        raise RuntimeError("MCP_PORT is required")
    return {"token": token, "host": host, "port": int(port)}
