"""Experimental MCP server for agent-ssh-gateway.

This server is intentionally kept outside the gateway core.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from command_policy import CommandPolicyError
from gateway_client import GatewayClient, GatewayClientError
from mcp.server.fastmcp import FastMCP
from self_test import run_self_test
from tool_modes import should_register_tool
from tool_results import error_result, text_result

mcp = FastMCP("agent-ssh-gateway")
client = GatewayClient()


def register_tool(name: str):
    """Decorator: register MCP tool only if visible in the active mode."""
    def decorator(func):
        if should_register_tool(name):
            return mcp.tool(name=name)(func)
        return func
    return decorator


def run_tool(
    *,
    tool: str,
    title: str,
    fn: Callable[[], dict[str, Any]],
    success_text: str,
) -> dict[str, Any]:
    """Execute a tool call with structured error handling."""
    try:
        data = fn()
    except (GatewayClientError, CommandPolicyError) as exc:
        return error_result(tool=tool, title=title, error=str(exc))
    return text_result(tool=tool, title=title, text=success_text, data=data)


@register_tool("gateway_health")
def gateway_health() -> dict[str, Any]:
    """Check gateway health."""
    return run_tool(
        tool="gateway_health",
        title="Gateway health",
        fn=client.health,
        success_text="Gateway is reachable.",
    )


@register_tool("gateway_list_sessions")
def gateway_list_sessions() -> dict[str, Any]:
    """List current SSH sessions visible to the configured API key."""
    def _list() -> dict[str, Any]:
        data = client.list_sessions()
        return data

    return run_tool(
        tool="gateway_list_sessions",
        title="List sessions",
        fn=_list,
        success_text="Retrieved session list.",
    )


@register_tool("gateway_session_health")
def gateway_session_health(session_id: str | None = None) -> dict[str, Any]:
    """Check an SSH session health."""
    def _health() -> dict[str, Any]:
        return client.session_health(session_id=session_id)

    return run_tool(
        tool="gateway_session_health",
        title="Session health",
        fn=_health,
        success_text="Session health retrieved.",
    )


@register_tool("gateway_execute_restricted")
def gateway_execute_restricted(
    command: str, session_id: str | None = None
) -> dict[str, Any]:
    """Execute an allowlisted read-only command as a redacted async job."""
    def _exec() -> dict[str, Any]:
        return client.execute_restricted(command, session_id=session_id)

    return run_tool(
        tool="gateway_execute_restricted",
        title="Restricted execute",
        fn=_exec,
        success_text="Command submitted as a background job.",
    )


@register_tool("gateway_job_status")
def gateway_job_status(job_id: str) -> dict[str, Any]:
    """Get background job status."""
    def _status() -> dict[str, Any]:
        data = client.job_status(job_id)
        return data

    return run_tool(
        tool="gateway_job_status",
        title="Job status",
        fn=_status,
        success_text=f"Job {job_id} status retrieved.",
    )


@register_tool("gateway_job_result")
def gateway_job_result(
    job_id: str, redact_output: bool = True
) -> dict[str, Any]:
    """Get background job result."""
    def _result() -> dict[str, Any]:
        data = client.job_result(job_id, redact_output=redact_output)
        return data

    return run_tool(
        tool="gateway_job_result",
        title="Job result",
        fn=_result,
        success_text=f"Job {job_id} result retrieved.",
    )


@register_tool("gateway_wait_job")
def gateway_wait_job(
    job_id: str, timeout_sec: int | None = None
) -> dict[str, Any]:
    """Wait for a background job and return its result."""
    def _wait() -> dict[str, Any]:
        return client.wait_job(job_id, timeout_sec=timeout_sec)

    return run_tool(
        tool="gateway_wait_job",
        title="Wait job",
        fn=_wait,
        success_text=f"Job {job_id} completed.",
    )


@register_tool("gateway_read_file")
def gateway_read_file(
    path: str, session_id: str | None = None
) -> dict[str, Any]:
    """Read a file through the gateway file API."""
    def _read() -> dict[str, Any]:
        return client.read_file(path, session_id=session_id)

    return run_tool(
        tool="gateway_read_file",
        title="Read file",
        fn=_read,
        success_text=f"File {path} read successfully.",
    )


@register_tool("gateway_repo_status")
def gateway_repo_status(
    session_id: str | None = None
) -> dict[str, Any]:
    """Collect basic repository status using read-only commands."""
    def _status() -> dict[str, Any]:
        return client.repo_status(session_id=session_id)

    return run_tool(
        tool="gateway_repo_status",
        title="Repository status",
        fn=_status,
        success_text="Collected repository status.",
    )


@register_tool("gateway_self_test")
def gateway_self_test() -> dict[str, Any]:
    """Run read-only diagnostics for the MCP gateway example."""
    data = run_self_test(client)
    status = data.get("status", "unknown")
    return text_result(
        tool="gateway_self_test",
        title="Gateway self-test",
        text=f"Gateway MCP self-test status: {status}",
        data=data,
    )


if __name__ == "__main__":
    mcp.run()
