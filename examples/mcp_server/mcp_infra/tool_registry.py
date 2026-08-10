"""Tool registration and execution infrastructure for the MCP server package.

Moved verbatim from server.py (refactor #8). The global FastMCP
instance is reached through runtime.get_mcp() instead of a module-level
`mcp` name.
"""

from __future__ import annotations

import hashlib as _hashlib
import json
import time as _time
from collections.abc import Callable
from typing import Any

from command_policy import CommandPolicyError
from gateway_client import GatewayClientError
from mcp.server.fastmcp import FastMCP
from tool_modes import should_register_tool
from tool_results import tool_error, tool_success
from write_modes import WriteModeError, WritePermissionError

from examples.mcp_server.latency_metrics import get_tracker
from examples.mcp_server.mcp_audit import McpAuditEvent
from examples.mcp_server.mcp_infra import runtime
from examples.mcp_server.mcp_infra._server_ref import server_module as _server_module

from .gateway_errors import (
    _gateway_error_hint,
    _gateway_error_message,
)


def _envelope_to_call_tool_result(envelope: dict[str, Any]):
    """Convert a canonical {"ok": bool, ...} envelope into an explicit
    mcp.types.CallToolResult with isError set from "ok".

    FastMCP's own dict/tuple return-value handling (see
    mcp.server.lowlevel.server.Server.call_tool()'s generic conversion,
    and FunctionMetadata.convert_result()) ALWAYS reports isError=False
    for a plain dict or (content, structured) tuple return, regardless of
    what "ok" says inside it -- the framework has no notion of our own
    envelope convention. Returning an actual CallToolResult is the one
    shape both layers pass through completely unchanged (verified: only
    `isinstance(result, CallToolResult)` bypasses the automatic
    isError=False conversion), so this is the single place that needs to
    intervene to make {"ok": false} tool results actually surface as
    isError=True to the MCP client.
    """
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(envelope, indent=2))],
        structuredContent=envelope,
        isError=not envelope.get("ok", True),
    )


def register_tool(name: str):
    """Decorator: register MCP tool only if visible in the active mode.

    Registers an isError-converting wrapper (see
    _envelope_to_call_tool_result) as the callable FastMCP actually
    dispatches to for real MCP protocol calls, but returns the original,
    unwrapped function to the caller -- so direct Python calls (other
    module code, and the many tests that call gateway_* functions
    directly and assert on the plain {"ok": ...} dict) keep seeing the
    original return value, unaffected by this MCP-protocol-only fix.
    """

    def decorator(func):
        if not should_register_tool(name):
            return func

        import asyncio
        import functools

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_result_wrapper(*args, **kwargs):
                result = await func(*args, **kwargs)
                if isinstance(result, dict) and "ok" in result:
                    return _envelope_to_call_tool_result(result)
                return result

            runtime.get_mcp().tool(name=name)(async_result_wrapper)
            return func

        @functools.wraps(func)
        def sync_result_wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            if isinstance(result, dict) and "ok" in result:
                return _envelope_to_call_tool_result(result)
            return result

        runtime.get_mcp().tool(name=name)(sync_result_wrapper)
        return func

    return decorator


def instrumented(tool_name: str):
    """Decorator that wraps a tool function with latency tracking."""

    def decorator(func):
        import asyncio

        if asyncio.iscoroutinefunction(func):

            async def async_wrapper(*args, **kwargs):
                tracker = get_tracker()
                with tracker.measure(tool_name):
                    result = await func(*args, **kwargs)
                if isinstance(result, dict) and "meta" in result:
                    recs = tracker.records.get(tool_name, [])
                    if recs:
                        result["meta"]["duration_ms"] = int(recs[-1])
                return result

            from functools import wraps
            wraps(func)(async_wrapper)
            return async_wrapper
        else:

            def sync_wrapper(*args, **kwargs):
                tracker = get_tracker()
                with tracker.measure(tool_name):
                    result = func(*args, **kwargs)
                if isinstance(result, dict) and "meta" in result:
                    recs = tracker.records.get(tool_name, [])
                    if recs:
                        result["meta"]["duration_ms"] = int(recs[-1])
                return result

            from functools import wraps
            wraps(func)(sync_wrapper)
            return sync_wrapper

    return decorator


def _validate_project(project: str) -> str:
    """Validate and return project name. Raises ValueError on invalid input."""
    if not project:
        raise ValueError("project argument is required")
    parts = project.strip("/").split("/")
    for p in parts:
        if p in ("..", ".", "~", ""):
            raise ValueError(f"Invalid project name: {project!r}")
    return "/".join(parts)


def compute_toolset_hash(mcp_instance: FastMCP) -> str:
    """Compute SHA-256 hash of the canonical tool manifest.

    Canonical form: sorted list of {name, inputSchema} objects as compact JSON.
    Uses items.sort(key=lambda item: item["name"]) — NOT sorted(dicts).
    """
    tools_dict = {}
    if hasattr(mcp_instance, "_tool_manager"):
        tm = mcp_instance._tool_manager
        if hasattr(tm, "_tools"):
            tools_dict = tm._tools

    items = []
    for name, tool_obj in tools_dict.items():
        schema = getattr(tool_obj, "parameters", None) or {}
        items.append({"name": name, "inputSchema": schema})

    items.sort(key=lambda item: item["name"])  # type: ignore[arg-type,return-value]
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return "sha256:" + _hashlib.sha256(canonical.encode()).hexdigest()


def run_tool(
    *,
    tool: str,
    title: str,
    fn: Callable[[], dict[str, Any]],
    success_text: str,
) -> dict[str, Any]:
    """Execute a tool call with structured error handling."""
    _server = _server_module()

    _start = _time.monotonic()

    def _elapsed() -> float:
        return (_time.monotonic() - _start) * 1000

    try:
        data = fn()
    except Exception as exc:
        if isinstance(exc, CommandPolicyError | WritePermissionError | WriteModeError):
            # Classify the error code
            if isinstance(exc, CommandPolicyError):
                msg = str(exc).lower()
                if "blocked" in msg and "agent backend" in msg:
                    error_code = "AGENT_BACKEND_BLOCKED"
                elif "blocked" in msg and "opencode" in msg:
                    error_code = "OPENCODE_BLOCKED"
                elif "readonly" in msg or "allowlist" in msg or "denied" in msg:
                    error_code = "READONLY_COMMAND"
                else:
                    error_code = "POLICY_VIOLATION"
            elif isinstance(exc, WritePermissionError):
                error_code = "WRITE_PERMISSION_DENIED"
            else:
                error_code = "WRITE_MODE_ERROR"

            # Emit structured audit event
            try:
                audit_logger = _server.get_audit_logger()
                audit_logger.append(McpAuditEvent(
                    event_type="mcp.tool_blocked",
                    tool=tool,
                    action=title,
                    decision="block",
                    reason=str(exc),
                    error_code=error_code,
                ))
            except Exception:
                pass  # audit failure must not change tool behavior

            return tool_error(
                tool=tool,
                code=error_code,
                message=str(exc),
                duration_ms=_elapsed(),
            )
        if isinstance(exc, GatewayClientError):
            code, retryable = _server._classify_gateway_error(exc)
            details = (
                {"job_id": exc.body["job_id"]}
                if isinstance(exc.body, dict) and exc.body.get("job_id")
                else None
            )
            return tool_error(
                tool=tool,
                code=code,
                message=_gateway_error_message(exc),
                retryable=retryable,
                hint=_gateway_error_hint(exc, code),
                details=details,
                duration_ms=_elapsed(),
                source="gateway",
            )
        if isinstance(exc, ValueError):
            msg = str(exc).lower()
            if "traversal" in msg or "blocked" in msg or "denied" in msg:
                err_code = "POLICY_DENIED"
            else:
                err_code = "INVALID_INPUT"
            return tool_error(
                tool=tool,
                code=err_code,
                message=str(exc),
                duration_ms=_elapsed(),
            )
        raise
    if isinstance(data, dict) and data.get("ok") is False:
        error_info = data.get("error") or {}
        meta = data.get("meta") or {}
        return tool_error(
            tool=tool,
            code=error_info.get("code", "INTERNAL_ERROR"),
            message=error_info.get("message", "Tool returned error"),
            result=data.get("result"),
            retryable=bool(error_info.get("retryable", False)),
            hint=error_info.get("hint"),
            details=error_info.get("details"),
            duration_ms=_elapsed(),
            redacted=bool(meta.get("redacted", False)),
            truncated=bool(meta.get("truncated", False)),
            source=meta.get("source", "unknown"),
        )
    if isinstance(data, dict) and "ok" in data:
        if "duration_ms" not in data.get("meta", {}) or data["meta"].get("duration_ms", 0) == 0:
            meta = data.get("meta", {})
            meta["duration_ms"] = round(_elapsed(), 1)
        return data
    # fn() returned a raw (non-canonical) dict -- e.g. build_command_result()'s
    # {"outcome", "exit_code", "stdout", "stderr", ...} with no "ok" key.
    # Preserve it as the actual result instead of discarding it in favor of
    # the human-readable success_text (which was silently replacing real
    # stdout/stderr/exit_code with a static message like "Collected project
    # git status."). A non-zero exit_code is still surfaced as an error,
    # not silently reported as success.
    # A tool function (e.g. remotes()) can signal that it already redacted
    # sensitive content out of its own raw dict via a "redacted" key --
    # otherwise meta.redacted below would default to False regardless of
    # what actually happened, misreporting real redaction as none.
    was_redacted = bool(data.pop("redacted", False)) if isinstance(data, dict) else False
    if isinstance(data, dict) and isinstance(data.get("exit_code"), int) and data["exit_code"] != 0:
        return tool_error(
            tool=tool,
            code="TOOL_EXECUTION_FAILED",
            message=f"Command exited with code {data['exit_code']}",
            result=data,
            duration_ms=_elapsed(),
            redacted=was_redacted,
        )
    return tool_success(
        tool=tool,
        result=data,
        duration_ms=_elapsed(),
        redacted=was_redacted,
        success_text=success_text,
    )
