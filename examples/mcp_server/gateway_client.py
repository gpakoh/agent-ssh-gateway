"""HTTP client for the experimental MCP server example."""

from __future__ import annotations

import functools
import os
import threading
import time
from typing import Any

import httpx
from command_policy import validate_readonly_command


def _project_root() -> str:
    root = os.environ.get("MCP_GATEWAY_PROJECT_ROOT", "").strip().rstrip("/")
    if not root:
        raise GatewayClientError("MCP_GATEWAY_PROJECT_ROOT is required for project tools")
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
            raise GatewayClientError(f"absolute path {path!r} is outside allowed root {allowed}")
        return path

    if root:
        resolved = root + "/" + path.lstrip("/")
        return resolved

    return path


class GatewayClientError(RuntimeError):
    """Raised when the gateway returns an error.

    Attributes:
        status_code: HTTP status code from the gateway, or None for client-side errors.
        body: Parsed JSON body from the gateway response, or None.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class GatewayClient:
    """Small HTTP wrapper around agent-ssh-gateway."""

    _SESSION_NOT_FOUND = "SESSION_NOT_FOUND"

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

        self._reconnect_lock = threading.Lock()
        self._ssh_host = os.environ.get("GATEWAY_SSH_HOST", "")
        self._ssh_port = int(os.environ.get("GATEWAY_SSH_PORT", "22"))
        self._ssh_user = os.environ.get("GATEWAY_SSH_USER", "") or os.environ.get(
            "GATEWAY_SSH_USERNAME", ""
        )
        self._ssh_password = os.environ.get("GATEWAY_SSH_PASSWORD", "")
        self._ssh_private_key = os.environ.get("GATEWAY_SSH_PRIVATE_KEY", "")
        if not self._ssh_private_key:
            key_path = os.environ.get("GATEWAY_SSH_KEY_PATH", "")
            if key_path:
                try:
                    with open(key_path) as f:
                        self._ssh_private_key = f.read()
                except OSError:
                    pass

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise GatewayClientError("GATEWAY_API_KEY is required")
        return {"X-API-Key": self.api_key}

    def _reconnect_session(self) -> None:
        if not self._ssh_host or not self._ssh_user:
            raise GatewayClientError(
                "GATEWAY_SSH_HOST and GATEWAY_SSH_USER are required for auto-reconnect"
            )
        payload: dict[str, Any] = {
            "host": self._ssh_host,
            "port": self._ssh_port,
            "username": self._ssh_user,
        }
        if self._ssh_password:
            payload["password"] = self._ssh_password
        if self._ssh_private_key:
            payload["private_key"] = self._ssh_private_key

        response = httpx.post(
            f"{self.base_url}/api/ssh/connect",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        if response.status_code >= 400:
            raise GatewayClientError(f"auto-reconnect failed: {response.status_code}")
        data = response.json()
        self.session_id = data["session_id"]

    def connect(self) -> str:
        """Establish SSH session and return session_id."""
        self._reconnect_session()
        return self.session_id

    def disconnect(self, session_id: str | None = None) -> None:
        """Close SSH session. Best-effort — never raises."""
        sid = session_id or self.session_id
        if not sid:
            return
        try:
            self._post("/api/ssh/disconnect", {"session_id": sid})
        except Exception:
            pass
        if sid == self.session_id:
            self.session_id = ""

    @staticmethod
    def _retry_on_session_not_found(
        func: Any,
    ) -> Any:
        @functools.wraps(func)
        def wrapper(self: GatewayClient, *args: Any, **kwargs: Any) -> Any:
            for attempt in range(2):
                try:
                    return func(self, *args, **kwargs)
                except GatewayClientError as e:
                    if attempt == 0 and GatewayClient._SESSION_NOT_FOUND in str(e):
                        old_sid = self.session_id
                        with self._reconnect_lock:
                            if self.session_id == old_sid:
                                self._reconnect_session()
                        continue
                    raise
            return None  # unreachable

        return wrapper

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        response = httpx.get(
            f"{self.base_url}{path}",
            params=params,
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code >= 400:
            body: dict[str, Any] | None = None
            try:
                body = response.json()
            except Exception:
                pass
            raise GatewayClientError(
                f"GET {path} failed: {response.status_code} {response.text}",
                status_code=response.status_code,
                body=body,
            )
        data = response.json()
        if isinstance(data, dict) and data.get("error") == "NOT_SUPPORTED":
            raise GatewayClientError(
                "NOT_SUPPORTED",
                status_code=response.status_code,
                body=data,
            )
        return data

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        if response.status_code >= 400:
            body: dict[str, Any] | None = None
            try:
                body = response.json()
            except Exception:
                pass
            raise GatewayClientError(
                f"POST {path} failed: {response.status_code} {response.text}",
                status_code=response.status_code,
                body=body,
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

    @_retry_on_session_not_found
    def session_health(self, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or self._require_session_id()
        return self._get(f"/api/ssh/session/{sid}/health")

    @_retry_on_session_not_found
    def execute_restricted(self, command: str, session_id: str | None = None) -> dict[str, Any]:
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

    @_retry_on_session_not_found
    def execute_project_command(self, project: str, command: str) -> dict[str, Any]:
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

    def job_result(self, job_id: str, redact_output: bool = True) -> dict[str, Any]:
        return self._get(
            f"/api/jobs/{job_id}/result",
            {"redact_output": str(redact_output).lower()},
        )

    def wait_job(self, job_id: str, timeout_sec: int | None = None) -> dict[str, Any]:
        """Wait for job completion using long-poll, falling back to polling.

        Falls back to polling on NOT_SUPPORTED (multi-worker) or 404 (old gateway).
        No fallback on PERMISSION_DENIED, JOB_NOT_FOUND, or other real errors.
        """
        effective_timeout = timeout_sec or self.job_timeout
        http_timeout = effective_timeout + 5

        try:
            result = self._get(
                f"/api/jobs/{job_id}/wait",
                params={"timeout": effective_timeout},
                timeout=http_timeout,
            )
            return result
        except GatewayClientError as exc:
            should_fallback = False
            if exc.status_code == 404:
                should_fallback = True
            elif exc.body and exc.body.get("error") == "NOT_SUPPORTED":
                should_fallback = True
            elif exc.status_code == 200 and exc.body and exc.body.get("error") == "NOT_SUPPORTED":
                should_fallback = True

            if not should_fallback:
                raise

        # Polling fallback
        deadline = time.time() + effective_timeout
        while time.time() < deadline:
            status = self.job_status(job_id)
            if status.get("status") in {"completed", "failed", "cancelled"}:
                result = self.job_result(job_id)
                if "execution_duration_ms" not in result and result.get("duration") is not None:
                    result["execution_duration_ms"] = int(result["duration"] * 1000)
                return result
            time.sleep(1)
        raise GatewayClientError(f"Job {job_id} did not finish before timeout")

    @_retry_on_session_not_found
    def read_file(self, path: str, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or self._require_session_id()
        return self._post("/api/file/read", {"session_id": sid, "path": path})

    @_retry_on_session_not_found
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
        self, session_id: str | None = None, project: str | None = None
    ) -> dict[str, Any]:
        commands = {
            "status": "git status --short",
            "recent_commits": "git log --oneline -10",
            "tags": "git tag --list --sort=-creatordate",
        }
        output: dict[str, Any] = {}
        for name, command in commands.items():
            if project:
                job = self.execute_project_command(project, command)
            else:
                job = self.execute_restricted(command, session_id=session_id)
            result = self.wait_job(job["job_id"])
            if name == "tags" and isinstance(result, dict):
                stdout = result.get("stdout") or result.get("output") or ""
                lines = stdout.strip().split("\n")[:10]
                result["stdout"] = "\n".join(lines)
            output[name] = result
        return output
