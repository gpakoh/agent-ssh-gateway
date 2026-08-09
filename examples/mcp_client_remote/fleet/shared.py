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


def tool_success(
    tool: str,
    result: Any,
    *,
    source: str,
    truncated: bool = False,
    redacted: bool = False,
) -> dict[str, Any]:
    """Return the canonical Contract v1 success envelope used by fleet tools."""
    return {
        "ok": True,
        "tool": tool,
        "result": result,
        "error": None,
        "meta": {
            "contract_version": "1",
            "source": source,
            "truncated": bool(truncated),
            "redacted": bool(redacted),
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


def minimize_user_payload(user: Any) -> dict[str, Any] | None:
    """Return compact user metadata with no account PII.

    Raw Gitea issue payloads embed the full user object (email, is_admin,
    last_login, created, restricted, active, location, website, ...); GitHub
    embeds extensive profile/URL fields. Only login (+ full_name when
    present) is needed to triage an issue or PR.
    """
    if not isinstance(user, dict):
        return None
    result: dict[str, Any] = {"login": user.get("login") or user.get("username")}
    full_name = user.get("full_name")
    if full_name:
        result["full_name"] = full_name
    return result


MAX_ISSUE_BODY_CHARS = 4000


def _truncate_body(body: str) -> tuple[str, bool]:
    """Cap an issue/PR body to MAX_ISSUE_BODY_CHARS, returning (text, truncated).

    Dependabot PR bodies can reach tens of kilobytes; dumping them whole
    into a tool result floods the agent context (audit finding: unlimited
    GitHub/Gitea responses). The head of the body is kept so the actual
    change description survives; a marker notes the truncation.
    """
    if len(body) <= MAX_ISSUE_BODY_CHARS:
        return body, False
    head = body[:MAX_ISSUE_BODY_CHARS]
    marker = f"\n\n[... truncated: {len(body)} chars > {MAX_ISSUE_BODY_CHARS} limit]"
    return head + marker, True


def minimize_issue_payload(data: dict[str, Any], *, provider: str) -> dict[str, Any]:
    """Return compact issue/PR metadata without embedded user PII or noise.

    Raw upstream issue/PR objects embed full user objects (email,
    is_admin, last_login for Gitea), nested repo/milestone/label
    structures and API URLs — a context-flooding and PII leak. This
    allowlist keeps only what an agent needs to triage, and caps oversized
    bodies. ``provider`` is accepted for parity with
    ``minimize_repo_payload``; both providers share the same output shape.
    """
    result: dict[str, Any] = {
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "user": minimize_user_payload(data.get("user")),
        "labels": [
            {"name": label.get("name"), "color": label.get("color")}
            for label in (data.get("labels") or [])
            if isinstance(label, dict)
        ],
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }
    body = data.get("body")
    if body:
        result["body"], result["body_truncated"] = _truncate_body(body)
    else:
        result["body_truncated"] = False
    closed_at = data.get("closed_at")
    if closed_at:
        result["closed_at"] = closed_at
    if data.get("comments") is not None:
        result["comments"] = data["comments"]
    milestone = data.get("milestone")
    if isinstance(milestone, dict):
        m: dict[str, Any] = {"title": milestone.get("title")}
        if milestone.get("state"):
            m["state"] = milestone["state"]
        if milestone.get("due_on"):
            m["due_on"] = milestone["due_on"]
        result["milestone"] = m
    assignees = data.get("assignees")
    if isinstance(assignees, list):
        result["assignees"] = [minimize_user_payload(a) for a in assignees]
    elif isinstance(data.get("assignee"), dict):
        result["assignees"] = [minimize_user_payload(data.get("assignee"))]
    if data.get("html_url"):
        result["html_url"] = data["html_url"]
    if isinstance(data.get("pull_request"), dict):
        result["pull_request"] = True
    if data.get("draft") is not None:
        result["draft"] = bool(data["draft"])
    for ref_key in ("head", "base"):
        ref = data.get(ref_key)
        if isinstance(ref, dict):
            result[ref_key] = {
                "label": ref.get("label"),
                "ref": ref.get("ref"),
                "sha": ref.get("sha"),
            }
    return result


def remote_api_error(tool: str, source: str, exc: Exception) -> dict[str, Any]:
    """Mirrors examples/mcp_server/server.py's _remote_api_error for the same
    GiteaClient/GitHubClient exception vocabulary (ValueError for bad
    input, PermissionError for 401/403, httpx.HTTPStatusError for any
    other non-2xx e.g. a 404 on a typo'd repo/issue number -- none of
    which are retryable, unlike a real transport failure, now handled
    below instead of merely called out in this docstring). RuntimeError
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
    if isinstance(exc, httpx.TransportError):
        return tool_error(tool, "REMOTE_UNAVAILABLE", str(exc), retryable=True, source=source)
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


def minimize_action_run_payload(run: dict[str, Any]) -> dict[str, Any]:
    """Return compact Gitea/GitHub Actions run metadata without PII.

    Raw action-run payloads embed full user objects (email, is_admin,
    last_login) under actor/trigger_actor and a ~50-field repository
    object (clone URLs, counters, topics, license) — a context flood.
    Keep only what an agent needs to triage a run.
    """
    return {
        "id": run.get("id"),
        "run_number": run.get("run_number"),
        "run_attempt": run.get("run_attempt"),
        "name": run.get("display_title") or run.get("name"),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "head_branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "actor": minimize_user_payload(run.get("actor")),
        "trigger_actor": minimize_user_payload(run.get("trigger_actor")),
        "repository": {
            "name": (run.get("repository") or {}).get("name"),
            "full_name": (run.get("repository") or {}).get("full_name"),
        }
        if isinstance(run.get("repository"), dict)
        else None,
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "html_url": run.get("html_url"),
    }


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


def list_pagination_meta(count: int, per_page: int) -> dict[str, Any]:
    """Pagination metadata for a single-page list result.

    ``truncated`` is computed from what was actually fetched: a page that
    came back with fewer than ``per_page`` items is reliably the last one,
    while a full page may have a successor. Always includes ``page`` so a
    client that later adds page-based navigation can rely on the shape.
    """
    return {
        "page": 1,
        "per_page": per_page,
        "truncated": count >= per_page,
    }


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
