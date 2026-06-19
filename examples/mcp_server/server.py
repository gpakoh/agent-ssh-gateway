"""Experimental MCP server for agent-ssh-gateway.

This server is intentionally kept outside the gateway core.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from chatgpt_tools import (
    git_diff_stat,
    git_status,
    project_git_diff_stat,
    project_git_status,
    project_recent_commits,
    project_run_compileall,
    project_run_lint,
    project_run_tests,
    project_show_changes,
    project_working_directory,
    recent_commits,
    run_compileall,
    run_lint,
    run_tests,
    show_changes,
    working_directory,
)
from command_policy import CommandPolicyError
from gateway_client import GatewayClient, GatewayClientError
from handoff import read_handoff, show_handoff_status, write_handoff_plan
from mcp.server.fastmcp import FastMCP
from self_test import run_self_test
from tool_modes import should_register_tool
from tool_results import error_result, text_result
from write_modes import WriteModeError, WritePermissionError

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
    except (GatewayClientError, CommandPolicyError, WritePermissionError, WriteModeError) as exc:
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


@register_tool("gateway_working_directory")
def gateway_working_directory(session_id: str | None = None) -> dict[str, Any]:
    """Print working directory on the SSH target."""
    return run_tool(
        tool="gateway_working_directory",
        title="Working directory",
        fn=lambda: working_directory(client, session_id=session_id),
        success_text="Collected current working directory.",
    )


@register_tool("gateway_git_status")
def gateway_git_status(session_id: str | None = None) -> dict[str, Any]:
    """Show git working tree status (short format)."""
    return run_tool(
        tool="gateway_git_status",
        title="Git status",
        fn=lambda: git_status(client, session_id=session_id),
        success_text="Collected git status.",
    )


@register_tool("gateway_recent_commits")
def gateway_recent_commits(session_id: str | None = None) -> dict[str, Any]:
    """List recent commits (git log --oneline -10)."""
    return run_tool(
        tool="gateway_recent_commits",
        title="Recent commits",
        fn=lambda: recent_commits(client, session_id=session_id),
        success_text="Collected recent commits.",
    )


@register_tool("gateway_git_diff_stat")
def gateway_git_diff_stat(session_id: str | None = None) -> dict[str, Any]:
    """Show uncommitted diff stat (git diff --stat)."""
    return run_tool(
        tool="gateway_git_diff_stat",
        title="Git diff stat",
        fn=lambda: git_diff_stat(client, session_id=session_id),
        success_text="Collected git diff stat.",
    )


@register_tool("gateway_show_changes")
def gateway_show_changes(session_id: str | None = None) -> dict[str, Any]:
    """Show combined git status and diff stat."""
    return run_tool(
        tool="gateway_show_changes",
        title="Show changes",
        fn=lambda: show_changes(client, session_id=session_id),
        success_text="Collected repository change summary.",
    )


@register_tool("gateway_run_tests")
def gateway_run_tests(session_id: str | None = None) -> dict[str, Any]:
    """Run test suite (pytest -q)."""
    return run_tool(
        tool="gateway_run_tests",
        title="Run tests",
        fn=lambda: run_tests(client, session_id=session_id),
        success_text="Ran test suite.",
    )


@register_tool("gateway_run_lint")
def gateway_run_lint(session_id: str | None = None) -> dict[str, Any]:
    """Run ruff linter on the project."""
    return run_tool(
        tool="gateway_run_lint",
        title="Run lint",
        fn=lambda: run_lint(client, session_id=session_id),
        success_text="Ran lint checks.",
    )


@register_tool("gateway_run_compileall")
def gateway_run_compileall(session_id: str | None = None) -> dict[str, Any]:
    """Run Python compileall on the project."""
    return run_tool(
        tool="gateway_run_compileall",
        title="Run compileall",
        fn=lambda: run_compileall(client, session_id=session_id),
        success_text="Ran Python compileall.",
    )


@register_tool("gateway_project_working_directory")
def gateway_project_working_directory(project: str) -> dict[str, Any]:
    """Print working directory within MCP_GATEWAY_PROJECT_ROOT/{project}."""
    return run_tool(
        tool="gateway_project_working_directory",
        title="Project working directory",
        fn=lambda: project_working_directory(client, project),
        success_text="Collected project working directory.",
    )


@register_tool("gateway_project_git_status")
def gateway_project_git_status(project: str) -> dict[str, Any]:
    """Show git working tree status within a project directory."""
    return run_tool(
        tool="gateway_project_git_status",
        title="Project git status",
        fn=lambda: project_git_status(client, project),
        success_text="Collected project git status.",
    )


@register_tool("gateway_project_recent_commits")
def gateway_project_recent_commits(project: str) -> dict[str, Any]:
    """List recent commits within a project (git log --oneline -10)."""
    return run_tool(
        tool="gateway_project_recent_commits",
        title="Project recent commits",
        fn=lambda: project_recent_commits(client, project),
        success_text="Collected project recent commits.",
    )


@register_tool("gateway_project_git_diff_stat")
def gateway_project_git_diff_stat(project: str) -> dict[str, Any]:
    """Show uncommitted diff stat within a project."""
    return run_tool(
        tool="gateway_project_git_diff_stat",
        title="Project git diff stat",
        fn=lambda: project_git_diff_stat(client, project),
        success_text="Collected project git diff stat.",
    )


@register_tool("gateway_project_show_changes")
def gateway_project_show_changes(project: str) -> dict[str, Any]:
    """Show combined git status and diff stat within a project."""
    return run_tool(
        tool="gateway_project_show_changes",
        title="Project show changes",
        fn=lambda: project_show_changes(client, project),
        success_text="Collected project change summary.",
    )


@register_tool("gateway_project_run_tests")
def gateway_project_run_tests(project: str) -> dict[str, Any]:
    """Run test suite within a project (pytest -q)."""
    return run_tool(
        tool="gateway_project_run_tests",
        title="Project run tests",
        fn=lambda: project_run_tests(client, project),
        success_text="Ran project test suite.",
    )


@register_tool("gateway_project_run_lint")
def gateway_project_run_lint(project: str) -> dict[str, Any]:
    """Run ruff linter within a project."""
    return run_tool(
        tool="gateway_project_run_lint",
        title="Project run lint",
        fn=lambda: project_run_lint(client, project),
        success_text="Ran project lint checks.",
    )


@register_tool("gateway_project_run_compileall")
def gateway_project_run_compileall(project: str) -> dict[str, Any]:
    """Run Python compileall within a project."""
    return run_tool(
        tool="gateway_project_run_compileall",
        title="Project run compileall",
        fn=lambda: project_run_compileall(client, project),
        success_text="Ran project Python compileall.",
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


@register_tool("gateway_read_handoff")
def gateway_read_handoff(session_id: str | None = None) -> dict[str, Any]:
    """Read .ai-bridge handoff files."""
    return run_tool(
        tool="gateway_read_handoff",
        title="Read handoff",
        fn=lambda: read_handoff(client, session_id=session_id),
        success_text="Read .ai-bridge handoff files.",
    )


@register_tool("gateway_show_handoff_status")
def gateway_show_handoff_status(session_id: str | None = None) -> dict[str, Any]:
    """Show compact handoff file availability."""
    return run_tool(
        tool="gateway_show_handoff_status",
        title="Handoff status",
        fn=lambda: show_handoff_status(client, session_id=session_id),
        success_text="Collected .ai-bridge handoff status.",
    )


@register_tool("gateway_write_handoff_plan")
def gateway_write_handoff_plan(
    task: str,
    agent: str = "opencode",
    notes: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Write .ai-bridge/current-plan.md given a task description."""
    return run_tool(
        tool="gateway_write_handoff_plan",
        title="Write handoff plan",
        fn=lambda: write_handoff_plan(
            client,
            task=task,
            agent=agent,
            notes=notes,
            session_id=session_id,
        ),
        success_text="Wrote .ai-bridge/current-plan.md.",
    )


if __name__ == "__main__":
    mcp.run()
