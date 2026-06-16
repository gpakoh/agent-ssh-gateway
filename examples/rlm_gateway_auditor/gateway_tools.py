"""Safe gateway tools for the experimental RLM auditor."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

BASE_URL = os.environ.get("GATEWAY_BASE_URL", "http://localhost:8085").rstrip("/")
API_KEY = os.environ.get("GATEWAY_API_KEY", "")


class GatewayError(RuntimeError):
    """Raised when the gateway API returns an error."""


def _headers() -> dict[str, str]:
    if not API_KEY:
        raise GatewayError("GATEWAY_API_KEY is required")
    return {"X-API-Key": API_KEY}


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(f"{BASE_URL}{path}", json=payload, headers=_headers(), timeout=30)
    if response.status_code >= 400:
        raise GatewayError(f"{path} failed: {response.status_code} {response.text}")
    return response.json()


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = httpx.get(f"{BASE_URL}{path}", params=params, headers=_headers(), timeout=30)
    if response.status_code >= 400:
        raise GatewayError(f"{path} failed: {response.status_code} {response.text}")
    return response.json()


def gateway_health() -> bool:
    """Check gateway is alive via the public health endpoint."""
    try:
        response = httpx.get(f"{BASE_URL}/health", timeout=10)
        return response.status_code < 400
    except httpx.RequestError:
        return False


def gateway_check_auth() -> bool:
    """Verify API key is accepted by calling a protected endpoint."""
    try:
        _get("/api/ssh/sessions")
        return True
    except GatewayError:
        return False


def gateway_check_session(session_id: str) -> bool:
    """Verify session exists and is healthy."""
    try:
        resp = _get(f"/api/ssh/session/{session_id}/health")
        return resp.get("healthy", False)
    except GatewayError:
        return False


# ---------------------------------------------------------------------------
# Command allowlist — restricts what commands the RLM profile may run
# ---------------------------------------------------------------------------

ALLOWED_COMMAND_PREFIXES = (
    "git status",
    "git log",
    "git diff",
    "git show",
    "git tag",
    "pytest -q",
    "pytest -x",
    "ruff check",
    "mypy",
    "find ",
    "grep ",
    "sed -n",
    "cat ",
    "head ",
    "tail ",
    "wc ",
    "ls ",
    "python -m compileall",
)

DENIED_COMMAND_PARTS = (
    " rm ",
    "mv ",
    "chmod ",
    "chown ",
    "> ",
    "| ",
    "git push",
    "git reset",
    "git commit",
    "git branch -D",
    "docker ",
    "pip install",
    "apt ",
    "curl ",
    "wget ",
)


def validate_readonly_command(command: str) -> str:
    """Check command against allowlist/denylist. Returns stripped command."""
    stripped = command.strip()
    if any(part in stripped for part in DENIED_COMMAND_PARTS):
        raise GatewayError(f"Command denied by RLM auditor profile: {command}")
    if not stripped.startswith(ALLOWED_COMMAND_PREFIXES):
        raise GatewayError(f"Command not allowed by RLM auditor profile: {command}")
    return stripped


# ---------------------------------------------------------------------------
# Gateway API functions
# ---------------------------------------------------------------------------


def gateway_execute(session_id: str, command: str) -> dict[str, Any]:
    """Run a command through agent-ssh-gateway as an async, redacted job."""
    return _post(
        "/api/ssh/execute",
        {
            "session_id": session_id,
            "command": command,
            "async_mode": True,
            "redact_output": True,
        },
    )


def gateway_execute_restricted(session_id: str, command: str) -> dict[str, Any]:
    """Execute only if the command passes the allowlist."""
    return gateway_execute(session_id, validate_readonly_command(command))


def gateway_job_status(job_id: str) -> dict[str, Any]:
    """Return job status."""
    return _get(f"/api/jobs/{job_id}/status")


def gateway_job_result(job_id: str, redact_output: bool = True) -> dict[str, Any]:
    """Return job result."""
    return _get(f"/api/jobs/{job_id}/result", {"redact_output": str(redact_output).lower()})


def gateway_wait_job(job_id: str, timeout_sec: int = 120) -> dict[str, Any]:
    """Wait for a job to finish and return its result."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        status = gateway_job_status(job_id)
        if status.get("status") in {"completed", "failed", "cancelled"}:
            return gateway_job_result(job_id)
        time.sleep(1)
    raise GatewayError(f"Job {job_id} did not finish within {timeout_sec}s")


def gateway_read_file(session_id: str, path: str) -> dict[str, Any]:
    """Read a file through the gateway file API."""
    return _post("/api/file/read", {"session_id": session_id, "path": path})


def gateway_repo_status(session_id: str) -> dict[str, Any]:
    """Collect basic repository status using safe read-only commands."""
    commands = {
        "status": "git status --short",
        "recent_commits": "git log --oneline -10",
        "tags": "git tag --list --sort=-creatordate | head -10",
    }
    results: dict[str, Any] = {}
    for name, command in commands.items():
        job = gateway_execute(session_id, command)
        results[name] = gateway_wait_job(job["job_id"])
    return results


# ---------------------------------------------------------------------------
# Subagent tools — read‑only subset for controlled subcalls
# ---------------------------------------------------------------------------

READ_ONLY_SUB_TOOLS: dict[str, Any] = {
    "gateway_job_status": gateway_job_status,
    "gateway_job_result": gateway_job_result,
    "gateway_read_file": gateway_read_file,
}
