"""Experimental MCP server for agent-ssh-gateway.

This server is intentionally kept outside the gateway core.
"""

from __future__ import annotations

from typing import Any

from gateway_client import GatewayClient
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agent-ssh-gateway")
client = GatewayClient()


@mcp.tool()
def gateway_health() -> dict[str, Any]:
    """Check gateway health."""
    return client.health()


@mcp.tool()
def gateway_list_sessions() -> dict[str, Any]:
    """List current SSH sessions visible to the configured API key."""
    return client.list_sessions()


@mcp.tool()
def gateway_session_health(session_id: str | None = None) -> dict[str, Any]:
    """Check an SSH session health."""
    return client.session_health(session_id=session_id)


@mcp.tool()
def gateway_execute_restricted(
    command: str, session_id: str | None = None
) -> dict[str, Any]:
    """Execute an allowlisted read-only command as a redacted async job."""
    return client.execute_restricted(command, session_id=session_id)


@mcp.tool()
def gateway_job_status(job_id: str) -> dict[str, Any]:
    """Get background job status."""
    return client.job_status(job_id)


@mcp.tool()
def gateway_job_result(
    job_id: str, redact_output: bool = True
) -> dict[str, Any]:
    """Get background job result."""
    return client.job_result(job_id, redact_output=redact_output)


@mcp.tool()
def gateway_wait_job(
    job_id: str, timeout_sec: int | None = None
) -> dict[str, Any]:
    """Wait for a background job and return its result."""
    return client.wait_job(job_id, timeout_sec=timeout_sec)


@mcp.tool()
def gateway_read_file(
    path: str, session_id: str | None = None
) -> dict[str, Any]:
    """Read a file through the gateway file API."""
    return client.read_file(path, session_id=session_id)


@mcp.tool()
def gateway_repo_status(
    session_id: str | None = None
) -> dict[str, Any]:
    """Collect basic repository status using read-only commands."""
    return client.repo_status(session_id=session_id)


if __name__ == "__main__":
    mcp.run()
