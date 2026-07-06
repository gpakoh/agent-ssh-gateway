"""HTTP client for the experimental MCP server example."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from command_policy import validate_readonly_command


def _project_root() -> str:
    root = os.environ.get("MCP_GATEWAY_PROJECT_ROOT", "").strip().rstrip("/")
    if not root:
        raise GatewayClientError(
            "MCP_GATEWAY_PROJECT_ROOT is required for project tools"
        )
    return root


def _safe_project(project: str) -> str:
    if not project:
        raise GatewayClientError("project argument is required")
    parts = project.strip("/").split("/")
    for p in parts:
        if p in ("..", ".", "~", ""):
            raise GatewayClientError(f"Invalid project name: {project!r}")
    return "/".join(parts)


def resolve_file_path(path: str) -> str:
    """Resolve a file path for gateway file operations.

    Relative paths are resolved under MCP_GATEWAY_PROJECT_ROOT.
    Absolute paths are allowed only if under the project root.
    Path traversal (..) is blocked.

    Returns the resolved absolute path.
    """
    if not path:
        raise GatewayClientError("path is required")

    if ".." in path.split("/"):
        raise GatewayClientError(f"path traversal blocked: {path!r}")

    root = os.environ.get("MCP_GATEWAY_PROJECT_ROOT", "").strip().rstrip("/")

    if path.startswith("/"):
        if not root:
            return path
        if not path.startswith(root):
            allowed = root or "(not set)"
            raise GatewayClientError(
                f"absolute path {path!r} is outside allowed root {allowed}"
            )
        return path

    if root:
        resolved = root + "/" + path.lstrip("/")
        return resolved

    return path


class GatewayClientError(RuntimeError):
    """Raised when the gateway returns an error."""


class GatewayClient:
    """Small HTTP wrapper around agent-ssh-gateway."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("GATEWAY_BASE_URL", "http://localhost:8085")
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("GATEWAY_API_KEY", "")
        self.session_id = session_id or os.environ.get("GATEWAY_SESSION_ID", "")
        self.command_timeout = int(os.environ.get("MCP_GATEWAY_COMMAND_TIMEOUT", "120"))
        self.job_timeout = int(os.environ.get("MCP_GATEWAY_JOB_TIMEOUT", "180"))

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise GatewayClientError("GATEWAY_API_KEY is required")
        return {"X-API-Key": self.api_key}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = httpx.get(
            f"{self.base_url}{path}",
            params=params,
            headers=self._headers(),
            timeout=30,
        )
        if response.status_code >= 400:
            raise GatewayClientError(
                f"GET {path} failed: {response.status_code} {response.text}"
            )
        return response.json()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        if response.status_code >= 400:
            raise GatewayClientError(
                f"POST {path} failed: {response.status_code} {response.text}"
            )
        return response.json()

    def _require_session_id(self) -> str:
        if not self.session_id:
            raise GatewayClientError("GATEWAY_SESSION_ID is required")
        return self.session_id

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def list_sessions(self) -> dict[str, Any]:
        return self._get("/api/ssh/sessions")

    def session_health(self, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or self._require_session_id()
        return self._get(f"/api/ssh/session/{sid}/health")

    def execute_restricted(
        self, command: str, session_id: str | None = None
    ) -> dict[str, Any]:
        sid = session_id or self._require_session_id()
        safe_command = validate_readonly_command(command)
        return self._post(
            "/api/ssh/execute",
            {
                "session_id": sid,
                "command": safe_command,
                "async_mode": True,
                "redact_output": True,
                "timeout": self.command_timeout,
            },
        )

    def execute_project_command(
        self, project: str, command: str
    ) -> dict[str, Any]:
        sid = self._require_session_id()
        root = _project_root()
        proj = _safe_project(project)
        full_command = f"cd {root}/{proj} && {command}"
        return self._post(
            "/api/ssh/execute",
            {
                "session_id": sid,
                "command": full_command,
                "async_mode": True,
                "redact_output": True,
                "timeout": self.command_timeout,
            },
        )

    def job_status(self, job_id: str) -> dict[str, Any]:
        return self._get(f"/api/jobs/{job_id}/status")

    def job_result(
        self, job_id: str, redact_output: bool = True
    ) -> dict[str, Any]:
        return self._get(
            f"/api/jobs/{job_id}/result",
            {"redact_output": str(redact_output).lower()},
        )

    def wait_job(
        self, job_id: str, timeout_sec: int | None = None
    ) -> dict[str, Any]:
        deadline = time.time() + (timeout_sec or self.job_timeout)
        while time.time() < deadline:
            status = self.job_status(job_id)
            if status.get("status") in {"completed", "failed", "cancelled"}:
                return self.job_result(job_id)
            time.sleep(1)
        raise GatewayClientError(
            f"Job {job_id} did not finish before timeout"
        )

    def read_file(
        self, path: str, session_id: str | None = None
    ) -> dict[str, Any]:
        sid = session_id or self._require_session_id()
        return self._post("/api/file/read", {"session_id": sid, "path": path})

    def write_file(
        self,
        path: str,
        content: str,
        session_id: str | None = None,
        mode: str = "overwrite",
    ) -> dict[str, Any]:
        sid = session_id or self._require_session_id()
        return self._post(
            "/api/file/write",
            {
                "session_id": sid,
                "path": path,
                "content": content,
                "mode": mode,
            },
        )

    def repo_status(
        self, session_id: str | None = None
    ) -> dict[str, Any]:
        commands = {
            "pwd": "pwd",
            "status": "git status --short",
            "recent_commits": "git log --oneline -10",
            "tags": "git tag --list --sort=-creatordate | head -10",
        }
        output: dict[str, Any] = {}
        for name, command in commands.items():
            job = self.execute_restricted(command, session_id=session_id)
            output[name] = self.wait_job(job["job_id"])
        return output
