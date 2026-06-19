"""ChatGPT-safe high-level MCP tools.

These tools avoid exposing a generic SSH execute surface to ChatGPT
by wrapping fixed allowlisted commands in semantic functions.
"""

from __future__ import annotations

from typing import Any

from gateway_client import GatewayClient


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
