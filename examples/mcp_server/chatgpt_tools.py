"""ChatGPT-safe high-level MCP tools.

These tools avoid exposing a generic SSH execute surface to ChatGPT
by wrapping fixed allowlisted commands in semantic functions.
"""

from __future__ import annotations

from typing import Any

from gateway_client import GatewayClient, GatewayClientError


def run_readonly_command(
    client: GatewayClient, command: str, session_id: str | None = None
) -> dict[str, Any]:
    job = client.execute_restricted(command, session_id=session_id)
    return client.wait_job(job["job_id"])


def working_directory(
    client: GatewayClient, session_id: str | None = None
) -> dict[str, Any]:
    return run_readonly_command(client, "pwd", session_id=session_id)


def git_status(
    client: GatewayClient, session_id: str | None = None
) -> dict[str, Any]:
    return run_readonly_command(
        client, "git status --short", session_id=session_id
    )


def recent_commits(
    client: GatewayClient, session_id: str | None = None
) -> dict[str, Any]:
    return run_readonly_command(
        client, "git log --oneline -10", session_id=session_id
    )


def git_diff_stat(
    client: GatewayClient, session_id: str | None = None
) -> dict[str, Any]:
    return run_readonly_command(
        client, "git diff --stat", session_id=session_id
    )


def show_changes(
    client: GatewayClient, session_id: str | None = None
) -> dict[str, Any]:
    return {
        "git_status": git_status(client, session_id=session_id),
        "git_diff_stat": git_diff_stat(client, session_id=session_id),
    }


def run_tests(
    client: GatewayClient, session_id: str | None = None
) -> dict[str, Any]:
    return run_readonly_command(client, "pytest -q", session_id=session_id)


def run_lint(
    client: GatewayClient, session_id: str | None = None
) -> dict[str, Any]:
    return run_readonly_command(
        client, "ruff check app tests examples", session_id=session_id
    )


def run_compileall(
    client: GatewayClient, session_id: str | None = None
) -> dict[str, Any]:
    return run_readonly_command(
        client, "python -m compileall app tests examples",
        session_id=session_id,
    )


# ── Project-aware tools (cd into MCP_GATEWAY_PROJECT_ROOT/{project}) ──

def run_project_command(
    client: GatewayClient,
    project: str,
    command: str,
) -> dict[str, Any]:
    job = client.execute_project_command(project, command)
    return client.wait_job(job["job_id"])


def project_working_directory(
    client: GatewayClient, project: str
) -> dict[str, Any]:
    return run_project_command(client, project, "pwd")


def project_git_status(
    client: GatewayClient, project: str
) -> dict[str, Any]:
    return run_project_command(client, project, "git status --short")


def project_recent_commits(
    client: GatewayClient, project: str
) -> dict[str, Any]:
    return run_project_command(
        client, project, "git log --oneline -10"
    )


def project_git_diff_stat(
    client: GatewayClient, project: str
) -> dict[str, Any]:
    return run_project_command(client, project, "git diff --stat")


def project_show_changes(
    client: GatewayClient, project: str
) -> dict[str, Any]:
    return {
        "git_status": project_git_status(client, project),
        "git_diff_stat": project_git_diff_stat(client, project),
    }


def project_run_tests(
    client: GatewayClient, project: str
) -> dict[str, Any]:
    return run_project_command(client, project, "pytest -q")


def project_run_lint(
    client: GatewayClient, project: str
) -> dict[str, Any]:
    return run_project_command(
        client, project, "ruff check app tests examples"
    )


def project_run_compileall(
    client: GatewayClient, project: str
) -> dict[str, Any]:
    return run_project_command(
        client, project, "python -m compileall app tests examples"
    )
