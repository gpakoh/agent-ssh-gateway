"""Project-scoped Py Coordination MCP tools using Gitea issues as durable state."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from tool_results import tool_error, tool_success

from examples.mcp_server.coordination import (
    CoordinationOutcomeUnknown,
    board_from_threads,
    create_thread,
    list_threads,
    open_thread,
    validate_project,
    write_event,
)
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.adapters import remote
from examples.mcp_server.mcp_infra.tool_registry import register_tool


def _owner() -> str:
    owner = os.environ.get("MCP_COORD_GITEA_OWNER", "gpakoh").strip()
    return validate_project(owner)


def _registered(project: str) -> str:
    name = validate_project(project)
    try:
        metadata = server_attr("_get_workspace_registry")().project_info(name)
    except Exception as exc:
        raise ValueError("project is not registered in SSH Gateway") from exc
    if not isinstance(metadata, dict) or metadata.get("type") == "candidate-clone":
        raise ValueError("coordination requires a registered source project")
    return name


def _principal() -> str | None:
    # Gitea sees the shared service account; this adds an opaque, verified
    # Gateway caller pseudonym without leaking the bearer token itself.
    fp = remote._caller_fingerprint()
    return hashlib.sha256(fp.encode()).hexdigest()[:16] if fp else None


def _fail(tool: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, RuntimeError) and str(exc) == "GITEA_TOKEN is not configured":
        return tool_error(tool=tool, code="DEPENDENCY_MISSING", message="GITEA_TOKEN is not configured", source="gitea")
    if isinstance(exc, CoordinationOutcomeUnknown):
        return tool_error(
            tool=tool, code="MUTATION_OUTCOME_UNKNOWN", message=str(exc),
            hint="Reopen the target thread or inbox using the original request_key before retrying.",
            retryable=False, source="gitea",
        )
    return remote._remote_api_error(tool, "gitea", exc)


async def _connection(tool: str, project: str, *, write: bool) -> tuple[str, str]:
    # Pure validation occurs before any network request or write.
    repo = _registered(project)
    owner = _owner()
    if write and _principal() is None:
        raise PermissionError("authenticated Gateway caller identity required for coordination writes")
    if not os.environ.get("GITEA_TOKEN"):
        raise RuntimeError("GITEA_TOKEN is not configured")
    return owner, repo


async def coord_inbox(project: str, limit: int = 25) -> dict[str, Any]:
    """Show open/closed project threads and the latest verified coordination state."""
    tool = "coord_inbox"
    try:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("limit must be an integer from 1 to 30")
        owner, repo = await _connection(tool, project, write=False)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            all_threads = await list_threads(client, owner, repo)
            threads = []
            for item in all_threads[:limit]:
                full = await open_thread(client, owner, repo, item["number"])
                threads.append({**item, "coord_state": full["coord_state"]})
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(
        tool,
        result={"project": repo, "threads": threads, "count": len(threads),
                "truncated": len(all_threads) > limit},
        source="gitea",
    )


async def coord_board(project: str, limit: int = 25) -> dict[str, Any]:
    """Read the project's Gitea-backed Kanban columns without changing issues."""
    tool = "coord_board"
    try:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("limit must be an integer from 1 to 30")
        owner, repo = await _connection(tool, project, write=False)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            all_threads = await list_threads(client, owner, repo)
            enriched = []
            for item in all_threads[:limit]:
                thread = await open_thread(client, owner, repo, item["number"])
                enriched.append({**item, "coord_state": thread["coord_state"]})
            columns = board_from_threads(enriched)
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(
        tool, result={
            "project": repo, "columns": columns, "count": len(enriched),
            "truncated": len(all_threads) > limit,
            "note": "Gitea Issues is authoritative; resolved does not automatically close an Issue",
        }, source="gitea",
    )


async def coord_open(project: str, issue_number: int) -> dict[str, Any]:
    """Read the full coordination thread and its durable Gitea comments."""
    tool = "coord_open"
    try:
        owner, repo = await _connection(tool, project, write=False)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            thread = await open_thread(client, owner, repo, issue_number)
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(tool, result={"project": repo, "thread": thread}, source="gitea")


async def coord_create(
    source_project: str, target_project: str, title: str, message: str, request_key: str
) -> dict[str, Any]:
    """Address an issue to a registered project's inbox; request_key is reusable."""
    tool = "coord_create"
    try:
        source = _registered(source_project)
        owner, target = await _connection(tool, target_project, write=True)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            repo = await client.get_repo(owner, target)
            if not (repo.get("permissions") or {}).get("push"):
                raise PermissionError("Gitea identity lacks target repository write access")
            outcome = await create_thread(
                client, owner, target,
                source_project=source, title=title, body=message, request_key=request_key,
                actor=_principal() or "",
            )
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(tool, result={"target_project": target, **outcome}, source="gitea")


async def _mutate(
    tool: str, project: str, issue_number: int, message: str,
    request_key: str, state: str
) -> dict[str, Any]:
    try:
        owner, repo = await _connection(tool, project, write=True)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            meta = await client.get_repo(owner, repo)
            if not (meta.get("permissions") or {}).get("push"):
                raise PermissionError("Gitea identity lacks target repository write access")
            outcome = await write_event(
                client, owner, repo, issue_number,
                text=message, request_key=request_key, state=state,
                actor=_principal() or "",
            )
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(tool, result={"project": repo, **outcome}, source="gitea")


async def coord_reply(project: str, issue_number: int, message: str, request_key: str) -> dict[str, Any]:
    """Append a response without changing the coordination state."""
    return await _mutate("coord_reply", project, issue_number, message, request_key, "message")


async def coord_accept(project: str, issue_number: int, message: str, request_key: str) -> dict[str, Any]:
    """Acknowledge an incoming request in its original thread."""
    return await _mutate("coord_accept", project, issue_number, message, request_key, "accepted")


async def coord_update(
    project: str, issue_number: int, state: str, message: str, request_key: str
) -> dict[str, Any]:
    """Record accepted/waiting_review/resolved with verification evidence."""
    if state not in {"accepted", "waiting_review", "resolved"}:
        return tool_error(tool="coord_update", code="INVALID_INPUT", message="invalid coordination transition", source="gitea")
    return await _mutate("coord_update", project, issue_number, message, request_key, state)


def register_all() -> None:
    register_tool("coord_inbox")(coord_inbox)
    register_tool("coord_board")(coord_board)
    register_tool("coord_open")(coord_open)
    register_tool("coord_create")(coord_create)
    register_tool("coord_reply")(coord_reply)
    register_tool("coord_accept")(coord_accept)
    register_tool("coord_update")(coord_update)
