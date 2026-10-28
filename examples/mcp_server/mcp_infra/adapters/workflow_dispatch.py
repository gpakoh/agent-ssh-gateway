"""Typed Gitea workflow dispatch in the explicit write/admin MCP mode."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import httpx
from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr
from tool_results import tool_error, tool_success

from app.gitea_workflow_dispatch import DispatchRefused, dispatch_status, dispatch_workflow
from examples.mcp_server.mcp_infra.tool_registry import register_tool


def _context(owner: str, repo: str) -> tuple[Any, str, Path]:
    from examples.mcp_server.mcp_infra.adapters import coordination_adapter, remote

    if owner != coordination_adapter._owner():
        raise DispatchRefused(
            "AUTH_ERROR", "Workflow owner is outside the registered project owner"
        )
    try:
        coordination_adapter._registered(repo)
    except ValueError as exc:
        raise DispatchRefused(
            "AUTH_ERROR", "Target repository is not a registered source project"
        ) from exc
    fingerprint = remote._caller_fingerprint()
    if not fingerprint:
        raise DispatchRefused("AUTH_ERROR", "Authenticated caller identity is required")
    token = os.environ.get("GITEA_TOKEN")
    if not token:
        raise DispatchRefused("DEPENDENCY_MISSING", "GITEA_TOKEN is not configured")
    return (
        remote._server_gitea_client()(token),
        hashlib.sha256(fingerprint.encode()).hexdigest(),
        Path(os.environ.get("MCP_AUDIT_LOG_PATH", "logs/mcp_audit.jsonl")).parent
        / "workflow-dispatch",
    )


def _audit(metadata: dict[str, Any]) -> None:
    from examples.mcp_server.mcp_audit import McpAuditEvent
    from examples.mcp_server.mcp_infra.adapters import remote

    try:
        remote._get_gitea_audit_logger().append_required(
            McpAuditEvent(
                event_type="mcp.gitea_dispatch_intent",
                tool="gitea_dispatch_workflow",
                action="workflow_dispatch",
                decision="allow",
                reason="Explicit operator workflow dispatch; values retained only as a fingerprint",
                metadata=metadata,
            )
        )
    except Exception as exc:
        raise DispatchRefused(
            "AUDIT_UNAVAILABLE", "Required dispatch audit could not be persisted"
        ) from exc


def _fail(tool: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DispatchRefused):
        return tool_error(tool=tool, code=exc.code, message=str(exc), source="gitea")
    if isinstance(exc, PermissionError):
        return tool_error(
            tool=tool, code="AUTH_ERROR", message="Gitea repository access denied", source="gitea"
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return tool_error(
            tool=tool,
            code="CAPABILITY_UNAVAILABLE",
            message="Gitea workflow preflight endpoint is unavailable",
            source="gitea",
        )
    return tool_error(
        tool=tool,
        code="DISPATCH_UNAVAILABLE",
        message="Dispatch operation unavailable; read status with the same operation key before retrying",
        retryable=False,
        source="gitea",
    )


async def gitea_dispatch_workflow(
    owner: str,
    repo: str,
    workflow_path: str,
    ref: str,
    expected_ref_sha: str,
    inputs: dict[str, StrictStr | StrictBool | StrictInt | StrictFloat],
    operation_key: str,
    reason: str,
    dry_run: StrictBool = False,
) -> dict[str, Any]:
    """Explicitly dispatch a reviewed workflow in a registered repository.

    Requires repo+admin scope, canonical repository administrator permission,
    expected branch SHA, schema-typed non-secret inputs and a stable operation
    key. dry_run validates without dispatching. A durable receipt precedes POST.
    Reusing a key never sends POST again, including after a lost response/crash.
    Read gitea_workflow_dispatch_status to reconcile. accepted_unbound or unknown
    is not success; only a directly returned and checked run ID is verified.
    verified attests run identity, not its conclusion. Branch pre/post checks
    detect drift, but Gitea has no atomic expected-SHA dispatch primitive.
    """
    tool = "gitea_dispatch_workflow"
    try:
        client, actor, journal = _context(owner, repo)
        async with client:
            result = await dispatch_workflow(
                client,
                owner=owner,
                repo=repo,
                workflow=workflow_path,
                ref=ref,
                expected_ref_sha=expected_ref_sha,
                inputs=inputs,
                operation_key=operation_key,
                reason=reason,
                actor=actor,
                journal_dir=journal,
                audit=_audit,
                dry_run=dry_run,
            )
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(tool, result=result, source="gitea")


async def gitea_workflow_dispatch_status(
    owner: str, repo: str, operation_key: str
) -> dict[str, Any]:
    """Read one caller-bound dispatch receipt and reconcile only by GET.

    Never dispatches or chooses an unproven candidate run after a lost POST
    response. not_found applies to this configured persistent journal only.
    """
    tool = "gitea_workflow_dispatch_status"
    try:
        client, actor, journal = _context(owner, repo)
        async with client:
            result = await dispatch_status(
                client,
                owner=owner,
                repo=repo,
                operation_key=operation_key,
                actor=actor,
                journal_dir=journal,
            )
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(tool, result=result, source="gitea")


def register_all() -> None:
    register_tool("gitea_dispatch_workflow")(gitea_dispatch_workflow)
    register_tool("gitea_workflow_dispatch_status")(gitea_workflow_dispatch_status)
