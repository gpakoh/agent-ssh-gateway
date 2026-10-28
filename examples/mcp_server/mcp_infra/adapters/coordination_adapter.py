"""Project-scoped Py Coordination MCP tools using Gitea issues as durable state."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from pydantic import StrictInt
from tool_results import tool_error, tool_success

from app.coordination_close import CloseRefused, close_thread
from examples.mcp_server.coordination import (
    CoordinationOutcomeUnknown,
    board_from_threads,
    create_thread,
    list_threads_preview,
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
    if metadata.get("type") == "coordination-only":
        # No filesystem checkout is implied. Bind only the exact trusted
        # Gitea owner/repo configured in the source-controlled registry.
        if (
            "root" in metadata
            or metadata.get("gitea_repo") != f"{_owner()}/{name}"
        ):
            raise ValueError("Gitea-only coordination repo mapping is not verified")
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
            all_threads, scan_truncated = await list_threads_preview(client, owner, repo)
            threads = []
            for item in all_threads[:limit]:
                full = await open_thread(client, owner, repo, item["number"])
                threads.append({**item, "coord_state": full["coord_state"]})
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(
        tool,
        result={"project": repo, "threads": threads, "count": len(threads),
                "truncated": scan_truncated or len(all_threads) > limit},
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
            all_threads, scan_truncated = await list_threads_preview(client, owner, repo)
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
            "truncated": scan_truncated or len(all_threads) > limit,
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
            if (
                repo.get("full_name") != f"{owner}/{target}"
                or (repo.get("permissions") or {}).get("push") is not True
            ):
                raise PermissionError("Gitea target repository identity or write access is unverified")
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
            if (
                meta.get("full_name") != f"{owner}/{repo}"
                or (meta.get("permissions") or {}).get("push") is not True
            ):
                raise PermissionError("Gitea target repository identity or write access is unverified")
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


async def coord_close(
    project: str, issue_number: StrictInt, resolution_event_id: StrictInt,
    expected_state_sha256: str, request_key: str, reason: str,
) -> dict[str, Any]:
    """Explicitly close one reviewed, resolved coordination Issue.

    Requires repo+admin scope and an existing trusted resolved report. Read
    coord_open.close_state_sha256, review the report/discussion, then supply
    that exact lease and resolution_event_id. reason records the operator's
    explicit close decision. No automatic close follows merge or resolved.

    Existing comments are retained without duplication. Attempts are journaled
    under the configured audit directory; retries after dispatch are read-only.
    Reopened/changed Issues are never automatically closed again. Gitea lacks
    Issue CAS here: pre/post reads detect drift but cannot prevent remote edits
    in the HTTP window. Unknown outcomes require read-only reconciliation.
    """
    tool = "coord_close"

    def audit(phase: str, why: str, metadata: dict[str, Any]) -> None:
        from examples.mcp_server.mcp_audit import McpAuditEvent

        logger = remote._get_gitea_audit_logger()
        event = McpAuditEvent(
            event_type="mcp.gitea_destructive_" + phase, tool=tool,
            action="close_coordination_issue", decision="allow", reason=why,
            metadata=metadata,
        )
        if phase == "intent":
            try:
                logger.append_required(event)
            except Exception as exc:
                raise CloseRefused("AUDIT_UNAVAILABLE", "Audit unavailable; Issue close refused") from exc
        else:
            logger.append(event)

    try:
        owner, repo = await _connection(tool, project, write=True)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            meta = await client.get_repo(owner, repo)
            if meta.get("full_name") != f"{owner}/{repo}" or (meta.get("permissions") or {}).get("push") is not True:
                raise PermissionError("Repository write permission or identity not verified")
            outcome = await close_thread(
                client, owner, repo, issue_number, resolution_event_id=resolution_event_id,
                expected_state_sha256=expected_state_sha256, request_key=request_key,
                reason=reason, actor=_principal() or "",
                journal_dir=Path(os.environ.get("MCP_AUDIT_LOG_PATH", "logs/mcp_audit.jsonl")).parent / "coordination-close",
                audit=audit,
            )
    except CloseRefused as exc:
        return tool_error(tool=tool, code=exc.code, message=str(exc), source="gitea")
    except CoordinationOutcomeUnknown as exc:
        return _fail(tool, exc)
    except PermissionError:
        return tool_error(tool=tool, code="AUTH_ERROR", message="Caller or repository write permission denied", source="gitea")
    except ValueError:
        return tool_error(tool=tool, code="INVALID_INPUT", message="Invalid close request or coordination thread", source="gitea")
    except Exception:
        return tool_error(tool=tool, code="CLOSE_UNAVAILABLE", message="Issue close is unavailable; inspect the thread before retrying", source="gitea")
    return tool_success(tool, result={"project": repo, **outcome}, source="gitea")


def register_all() -> None:
    register_tool("coord_inbox")(coord_inbox)
    register_tool("coord_board")(coord_board)
    register_tool("coord_open")(coord_open)
    register_tool("coord_create")(coord_create)
    register_tool("coord_reply")(coord_reply)
    register_tool("coord_accept")(coord_accept)
    register_tool("coord_update")(coord_update)
    register_tool("coord_close")(coord_close)
