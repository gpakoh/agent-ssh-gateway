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


_SCRIPT_BODY_DELIM = "__AGENT_SCRIPT_BODY__"


def _script_stdin_wrapper(script: str) -> str:
    """Wrap a script so the SSH target materializes it to a temp file and
    runs ``sh`` on the file, instead of ``sh`` consuming the script from
    the channel stdin.

    With stdin piping, a long-running child of the script (opencode spawns
    a background snapshot ``git add`` after its run) inherits the channel
    stdin. The shell's un-parsed script tail is then gone when the shell
    reaches it, and ``sh`` aborts at EOF with 'unexpected end of file
    (expecting \"fi\")' (exit 2) right after the child -- the agent's diff
    is already saved, so the job looks failed despite complete work
    (confirmed live in the E2E smoke, runs 1-4). Running from a file makes
    the shell's input the file: heredocs are read from the file, not stdin,
    and the inner script's children inherit a drained pipe (EOF), so
    nothing can consume the script source.

    The temp file lives in the SSH target's /tmp (writable there); the
    project root itself is never touched, preserving the read-only-root
    guarantee for the MCP container.
    """
    return (
        'tmpf=$(mktemp /tmp/agent-script-XXXXXX)\n'
        f'cat > "$tmpf" <<\'{_SCRIPT_BODY_DELIM}\'\n'
        + script.rstrip("\n")
        + f"\n{_SCRIPT_BODY_DELIM}\n"
        'sh "$tmpf"\n'
        'rc=$?\n'
        'rm -f "$tmpf"\n'
        'exit $rc\n'
    )


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
        ssh_host: str | None = None,
        ssh_port: int | None = None,
        ssh_user: str | None = None,
        ssh_password: str | None = None,
        ssh_private_key: str | None = None,
        ssh_key_path: str | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("GATEWAY_BASE_URL", "http://localhost:8085")
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("GATEWAY_API_KEY", "")
        self.session_id = (
            session_id if session_id is not None else os.environ.get("GATEWAY_SESSION_ID", "")
        )
        self.command_timeout = int(os.environ.get("MCP_GATEWAY_COMMAND_TIMEOUT", "120"))
        self.async_job_timeout = int(os.environ.get("MCP_GATEWAY_ASYNC_JOB_TIMEOUT", "3600"))
        self.job_timeout = int(os.environ.get("MCP_GATEWAY_JOB_TIMEOUT", "180"))
        self._http_timeout = int(os.environ.get("MCP_GATEWAY_HTTP_TIMEOUT", "120"))

        self._reconnect_lock = threading.Lock()
        self._ssh_host = ssh_host if ssh_host is not None else os.environ.get("GATEWAY_SSH_HOST", "")
        self._ssh_port = ssh_port if ssh_port is not None else int(os.environ.get("GATEWAY_SSH_PORT", "22"))
        self._ssh_user = ssh_user if ssh_user is not None else (
            os.environ.get("GATEWAY_SSH_USER", "")
            or os.environ.get("GATEWAY_SSH_USERNAME", "")
        )
        self._ssh_password = (
            ssh_password if ssh_password is not None else os.environ.get("GATEWAY_SSH_PASSWORD", "")
        )
        self._ssh_private_key = (
            ssh_private_key
            if ssh_private_key is not None
            else os.environ.get("GATEWAY_SSH_PRIVATE_KEY", "")
        )
        if not self._ssh_private_key:
            key_path = ssh_key_path if ssh_key_path is not None else os.environ.get("GATEWAY_SSH_KEY_PATH", "")
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
            "reuse_existing": True,
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
        assert self.session_id is not None
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
            timeout=self._http_timeout,
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
            if self._ssh_host and self._ssh_user:
                # No session configured yet, but SSH credentials to
                # establish one ARE present -- raise with the same
                # _SESSION_NOT_FOUND sentinel _retry_on_session_not_found
                # already looks for (every caller of this method is
                # wrapped with that decorator), so the auto-reconnect
                # path already built for a session that went STALE also
                # covers a session that never existed in the first
                # place. Confirmed live: a real GPT/MCP client hit
                # "GATEWAY_SESSION_ID is required" on git_status against
                # a deployment with valid GATEWAY_SSH_HOST/USERNAME/
                # KEY_PATH already configured -- nothing was ever
                # attempting to use them for a first connection.
                raise GatewayClientError(
                    f"{self._SESSION_NOT_FOUND}: no active session "
                    "(GATEWAY_SESSION_ID not set) -- auto-connecting"
                )
            raise GatewayClientError(
                "GATEWAY_SESSION_ID is required (no session configured and "
                "no GATEWAY_SSH_HOST/GATEWAY_SSH_USER to auto-connect one)"
            )
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
        from app.workspace.registry import get_registry

        info = get_registry().project_info(project)
        cwd = str(info["root"])
        import shlex as _shlex
        argv = _shlex.split(command)
        return self._post(
            "/api/ssh/execute-argv",
            {
                "session_id": sid,
                "argv": argv,
                "cwd": cwd,
                "timeout_s": self.command_timeout,
            },
        )

    @_retry_on_session_not_found
    def execute_raw(
        self,
        command: str,
        redact_path_prefix: str | None = None,
        stdin: str = "",
        submission_key: str | None = None,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        """Execute a command directly without cd wrapping.

        Unlike execute_project_command, this does NOT prepend ``cd <root>/<project> &&``.
        Use for commands that carry their own working-directory semantics
        (e.g. ``uv --directory <dir>``) or commands that must not contain
        shell metacharacters (``&&`` is blocked by the server metachar gate).

        ``redact_path_prefix``: pass the absolute host project root when
        ``command`` embeds it (e.g. ``--directory <root>`` or a generated
        script's own absolute path) -- this always runs async_mode=True, so
        a caller that only gets a job_id back and polls job_result() later
        would otherwise have that absolute path echoed back verbatim in the
        job's command/stdout/stderr (M8).

        ``stdin``: data to feed the command's stdin (e.g. ``sh`` with a
        multi-line script piped in, avoiding a temp file entirely).
        """
        sid = self._require_session_id()
        payload: dict[str, Any] = {
            "session_id": sid,
            "command": command,
            "async_mode": True,
            "redact_output": True,
            "timeout": timeout_s or self.command_timeout,
        }
        if redact_path_prefix:
            payload["redact_path_prefix"] = redact_path_prefix
        if stdin:
            payload["stdin"] = stdin
        if submission_key:
            payload["submission_key"] = submission_key
        return self._post("/api/ssh/execute", payload)

    @_retry_on_session_not_found
    def execute_argv(
        self,
        argv: list[str],
        stdin: str = "",
        timeout_s: int = 30,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Execute explicit argv via /api/ssh/execute-argv.

        Uses shlex.join on the Gateway side — no bash -c wrapping.
        """
        sid = session_id or self._require_session_id()
        payload: dict[str, Any] = {
            "session_id": sid,
            "argv": argv,
            "stdin": stdin,
            "timeout_s": timeout_s,
        }
        if cwd:
            payload["cwd"] = cwd
        return self._post(
            "/api/ssh/execute-argv",
            payload,
        )

    def execute_script(
        self,
        script: str,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        """Execute a wrapped script on this client's SSH target without project cwd."""
        return self.execute_argv(
            ["sh"],
            stdin=_script_stdin_wrapper(script),
            timeout_s=timeout_s or self.command_timeout,
        )

    def execute_script_async(
        self,
        script: str,
        submission_key: str | None = None,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        """Submit a wrapped script to this client's SSH target without project cwd."""
        return self.execute_raw(
            "sh",
            stdin=_script_stdin_wrapper(script),
            submission_key=submission_key,
            timeout_s=timeout_s or self.async_job_timeout,
        )

    @_retry_on_session_not_found
    def execute_project_script(
        self,
        project: str,
        script: str,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        """Pipe a wrapped bash script to ``sh`` over SSH via stdin.

        Multi-line scripts with shell syntax (if/then, $(), heredocs, &&, ||)
        cannot survive shlex.split() → shlex.join(). The wrapper makes the
        target materialize the script to a /tmp temp file and run ``sh`` on
        it -- piping a bare ``sh`` via stdin is safe for short scripts, but
        any long-running child inherits the channel stdin and can consume
        the shell's un-parsed script tail (see _script_stdin_wrapper).
        This deliberately does NOT write to the project root: that
        directory is read-only in this process (bind-mounted ``:ro`` for the
        MCP container) even though it's writable on the real SSH target --
        an earlier version wrote a temp file under ``<root>/.ai-bridge/tmp``
        locally and always failed with EROFS before the script ever ran.
        """
        cwd: str | None = None
        try:
            from app.workspace.registry import get_registry

            info = get_registry().project_info(project)
            cwd = str(info["root"])
        except Exception:
            raise

        return self.execute_argv(
            ["sh"],
            stdin=_script_stdin_wrapper(script),
            timeout_s=timeout_s or self.command_timeout,
            cwd=cwd,
        )

    @_retry_on_session_not_found
    def execute_project_script_async(
        self,
        project: str,
        script: str,
        submission_key: str | None = None,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        """Submit a wrapped bash script to ``sh`` over SSH, asynchronously.

        Like execute_project_script, but submits through execute_raw
        (async_mode=True, stdin=<wrapper>) and returns the job_id
        immediately instead of blocking on the full run. See
        execute_project_script's docstring for why this never writes to the
        project root.
        """
        cwd: str | None = None
        try:
            from app.workspace.registry import get_registry

            info = get_registry().project_info(project)
            cwd = str(info["root"])
        except Exception:
            raise

        return self.execute_raw(
            "sh",
            stdin=_script_stdin_wrapper(script),
            redact_path_prefix=cwd,
            submission_key=submission_key,
            timeout_s=timeout_s or self.async_job_timeout,
        )

    @_retry_on_session_not_found
    def apply_patch(
        self,
        project: str,
        patch: str,
        expected_hashes: dict[str, str],
        strip: int = 1,
        dry_run: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply a unified diff patch to project files via Gateway."""
        sid = session_id or self._require_session_id()
        proj = _safe_project(project)
        return self._post(
            f"/api/projects/{proj}/apply-patch",
            {
                "session_id": sid,
                "project": proj,
                "patch": patch,
                "expected_hashes": expected_hashes,
                "strip": strip,
                "dry_run": dry_run,
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

        A job that outlives the wait window (e.g. a full test suite run
        taking longer than MCP_GATEWAY_JOB_TIMEOUT) does NOT fail server-side
        -- the gateway's own /wait endpoint returns {"wait_timed_out": True}
        while the job keeps running in the background, retrievable later via
        job_status()/job_result(). Silently `return`ing that dict as if it
        were the finished job's result (the old behavior) fed a bare
        {"job_id", "status": "running", "wait_timed_out": True} -- with no
        exit_code/stdout -- into callers that assumed a completed job shape,
        which read the missing exit_code as -1 and reported a fabricated
        "exit code -1" failure instead of "still running, check back with
        this job_id". Raise a structured, job_id-bearing error instead so
        callers (run_tool()'s GatewayClientError handling) can surface the
        job_id for the caller to actually poll, per _classify_gateway_error's
        WAIT_TIMEOUT handling.
        """
        effective_timeout = timeout_sec or self.job_timeout
        http_timeout = effective_timeout + 5

        def _wait_timeout_error() -> GatewayClientError:
            return GatewayClientError(
                f"Job {job_id} did not finish before timeout",
                body={"job_id": job_id, "status": "running", "wait_timed_out": True},
            )

        try:
            result = self._get(
                f"/api/jobs/{job_id}/wait",
                params={"timeout": effective_timeout},
                timeout=http_timeout,
            )
            if result.get("wait_timed_out"):
                raise _wait_timeout_error()
            return result
        except GatewayClientError as exc:
            if exc.body and exc.body.get("wait_timed_out"):
                raise

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
        raise _wait_timeout_error()

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
                result = self.execute_project_command(project, command)
            else:
                job = self.execute_restricted(command, session_id=session_id)
                result = self.wait_job(job["job_id"])
            if name == "tags" and isinstance(result, dict):
                stdout = result.get("stdout") or result.get("output") or ""
                lines = stdout.strip().split("\n")[:10]
                result["stdout"] = "\n".join(lines)
            output[name] = result
        all_failed = all(
            isinstance(r, dict) and r.get("exit_code", -1) != 0
            for r in output.values()
        )
        if all_failed:
            output["ok"] = False
        return output
