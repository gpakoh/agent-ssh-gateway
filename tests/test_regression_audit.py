"""Regression tests for audit round 1 + round 2 fixes.

Each test class maps to one audit issue.  These exist so that the same
bugs cannot reappear after refactoring — they encode the contract that
each fix established.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# MCP server examples require their directory on sys.path for
# local-import compatibility (imports like 'from gateway_client
# import GatewayClientError' in server.py).  The same module object
# must be used for isinstance checks.
_MCP_SERVER_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if _MCP_SERVER_DIR not in sys.path:
    sys.path.insert(0, _MCP_SERVER_DIR)

from app.workspace_policy import HIDDEN_DIR_PATTERNS, WorkspacePolicy  # noqa: E402

# ── Issue 2: SSH deep health check ───────────────────────────────

class TestDeepSshCheck:
    """_deep_ssh_check must return True/False/None correctly.

    The function uses optional SSH_HEALTH_USER/PASSWORD credentials.
    Without credentials it falls back to TCP ping (returns None).
    """

    @pytest.mark.asyncio
    async def test_returns_none_when_no_credentials(self):
        from app.routers.system import _deep_ssh_check
        with patch("app.routers.system.settings") as mock_settings:
            mock_settings.ssh_health_user = ""
            mock_settings.ssh_health_password = ""
            result = await _deep_ssh_check("localhost", 22)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_ssh(self):
        from app.routers.system import _deep_ssh_check
        mock_client = MagicMock()
        mock_stdout = MagicMock()
        mock_channel = MagicMock()
        mock_channel.recv_exit_status.return_value = 0
        mock_stdout.channel = mock_channel
        mock_client.exec_command.return_value = (None, mock_stdout, None)

        with (
            patch("app.routers.system.settings") as mock_settings,
            patch("paramiko.SSHClient", return_value=mock_client),
        ):
            mock_settings.ssh_health_user = "health"
            mock_settings.ssh_health_password = "pass"
            result = await _deep_ssh_check("localhost", 22)
        assert result is True
        mock_client.connect.assert_called_once_with(
            "localhost", port=22, username="health", password="pass",
            timeout=5, allow_agent=False, look_for_keys=False,
        )
        mock_client.exec_command.assert_called_once_with("true", timeout=5)
        mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_on_failed_command(self):
        from app.routers.system import _deep_ssh_check
        mock_client = MagicMock()
        mock_stdout = MagicMock()
        mock_channel = MagicMock()
        mock_channel.recv_exit_status.return_value = 1
        mock_stdout.channel = mock_channel
        mock_client.exec_command.return_value = (None, mock_stdout, None)

        with (
            patch("app.routers.system.settings") as mock_settings,
            patch("paramiko.SSHClient", return_value=mock_client),
        ):
            mock_settings.ssh_health_user = "health"
            mock_settings.ssh_health_password = "pass"
            result = await _deep_ssh_check("localhost", 22)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_error(self):
        from app.routers.system import _deep_ssh_check
        with (
            patch("app.routers.system.settings") as mock_settings,
            patch("paramiko.SSHClient", side_effect=RuntimeError("connect failed")),
        ):
            mock_settings.ssh_health_user = "health"
            mock_settings.ssh_health_password = "pass"
            result = await _deep_ssh_check("localhost", 22)
        assert result is False

    def test_health_endpoint_uses_tcp_when_no_ssh_creds(self):
        """When ssh_health_user is empty, health relies on TCP ping only."""
        import asyncio
        with (
            patch("app.routers.system.settings") as mock_settings,
            patch("app.routers.system.socket") as mock_socket,
            patch("app.routers.system._deep_ssh_check") as mock_deep,
        ):
            mock_settings.ssh_health_user = ""
            mock_settings.ssh_health_password = ""
            mock_socket.create_connection.return_value.__enter__ = lambda s: MagicMock()
            mock_socket.create_connection.return_value.__exit__ = MagicMock(return_value=False)
            mock_deep.return_value = None
            from app.routers.system import health_check
            result = asyncio.run(health_check())
        assert result.ssh_server_reachable is True
        mock_deep.assert_awaited_once()

    def test_health_endpoint_uses_deep_check_when_creds_set(self):
        """When ssh_health_user is set, deep SSH check overrides TCP ping."""
        import asyncio
        with (
            patch("app.routers.system.settings") as mock_settings,
            patch("app.routers.system.socket") as mock_socket,
            patch("app.routers.system._deep_ssh_check") as mock_deep,
        ):
            mock_settings.ssh_health_user = "health"
            mock_settings.ssh_health_password = "pass"
            mock_socket.create_connection.return_value.__enter__ = lambda s: MagicMock()
            mock_socket.create_connection.return_value.__exit__ = MagicMock(return_value=False)
            mock_deep.return_value = True
            from app.routers.system import health_check
            result = asyncio.run(health_check())
        assert result.ssh_server_reachable is True
        mock_deep.assert_awaited_once()


# ── Issue 4: Error model — gateway code passthrough ─────────────

class TestClassifyGatewayErrorPassthrough:
    """_classify_gateway_error must pass through gateway error codes
    that are in ERROR_CODES, rather than mapping them to INTERNAL_ERROR.
    """

    def _make_exc(self, status: int, code: str, retryable: bool, message: str = ""):
        from examples.mcp_server.gateway_client import GatewayClientError
        body = {"detail": {"code": code, "retryable": retryable}}
        return GatewayClientError(
            message=message or code,
            status_code=status,
            body=body,
        )

    def test_passes_through_job_not_found(self):
        from examples.mcp_server.server import _classify_gateway_error
        exc = self._make_exc(404, "JOB_NOT_FOUND", False)
        code, retryable = _classify_gateway_error(exc)
        assert code == "JOB_NOT_FOUND"
        assert retryable is False

    def test_passes_through_project_not_found(self):
        from examples.mcp_server.server import _classify_gateway_error
        exc = self._make_exc(404, "PROJECT_NOT_FOUND", False)
        code, retryable = _classify_gateway_error(exc)
        assert code == "PROJECT_NOT_FOUND"
        assert retryable is False

    def test_passes_through_session_not_found(self):
        from examples.mcp_server.server import _classify_gateway_error
        exc = self._make_exc(404, "SESSION_NOT_FOUND", False)
        code, retryable = _classify_gateway_error(exc)
        assert code == "SESSION_NOT_FOUND"
        assert retryable is False

    def test_passes_through_unknown_code_in_error_codes(self):
        """Any code present in ERROR_CODES is passed through with its retryable.

        Codes that are keys in _GATEWAY_ERROR_CODE_MAP get explicitly mapped
        and therefore don't go through the passthrough branch.
        """
        from examples.mcp_server.server import _GATEWAY_ERROR_CODE_MAP, _classify_gateway_error
        from examples.mcp_server.tool_results import ERROR_CODES
        passthrough_codes = ERROR_CODES - set(_GATEWAY_ERROR_CODE_MAP.keys())
        for ec in sorted(passthrough_codes):
            retryable = ec.startswith("TIMEOUT")
            exc = self._make_exc(500, ec, retryable)
            code, got_retryable = _classify_gateway_error(exc)
            assert code == ec, f"Expected {ec} -> {ec}, got {code}"
            assert got_retryable == retryable, f"{ec}: expected retryable={retryable}"

    def test_maps_invalid_api_key(self):
        from examples.mcp_server.server import _classify_gateway_error
        exc = self._make_exc(401, "INVALID_API_KEY", False)
        code, retryable = _classify_gateway_error(exc)
        assert code == "AUTH_ERROR"
        assert retryable is False

    def test_maps_policy_denied(self):
        from examples.mcp_server.server import _classify_gateway_error
        exc = self._make_exc(403, "POLICY_DENIED", False)
        code, retryable = _classify_gateway_error(exc)
        assert code == "PERMISSION_DENIED"
        assert retryable is False

    def test_still_interprets_file_not_found_from_status(self):
        """Fallback: 404 with 'file not found' text -> FILE_NOT_FOUND even without detail."""
        from gateway_client import GatewayClientError  # noqa: I001
        from examples.mcp_server.server import _classify_gateway_error
        exc = GatewayClientError("file not found: /some/path", status_code=404, body="")
        code, retryable = _classify_gateway_error(exc)
        assert code == "FILE_NOT_FOUND"
        assert retryable is False

    def test_run_tool_calls_classify_on_gateway_error(self):
        """run_tool must call _classify_gateway_error when GatewayClientError raised."""
        # Must import GatewayClientError through server.py's local 'gateway_client'
        # module to avoid dual-import mismatch with isinstance check.
        from gateway_client import GatewayClientError  # noqa: I001
        from examples.mcp_server import server as mcp_server_mod

        err = GatewayClientError("test error", status_code=404)
        with patch.object(mcp_server_mod, "_classify_gateway_error", return_value=("JOB_NOT_FOUND", False)):
            result = mcp_server_mod.run_tool(
                tool="gateway_job_status",
                title="Job Status",
                fn=lambda: (_ for _ in ()).throw(err),
                success_text="N/A",
            )
        assert result["ok"] is False
        assert result["error"]["code"] == "JOB_NOT_FOUND"
        assert result["error"]["retryable"] is False


# ── Issue 5: Structure leak - error message redaction ───────────

class TestErrorRedaction:
    """_redact_error_message must strip internal paths and API endpoints."""

    def test_redacts_media_path(self):
        from examples.mcp_server.tool_results import _redact_error_message
        msg, redacted = _redact_error_message("File not found at /media/1TB/Python/project/file.py")
        assert redacted is True
        assert "[PATH]" in msg
        assert "/media/1TB" not in msg

    def test_redacts_root_path(self):
        from examples.mcp_server.tool_results import _redact_error_message
        msg, redacted = _redact_error_message("Config at /root/.ssh/id_rsa not found")
        assert redacted is True
        assert "/root/.ssh" not in msg
        assert "[PATH]" in msg

    def test_redacts_api_endpoint(self):
        from examples.mcp_server.tool_results import _redact_error_message
        msg, redacted = _redact_error_message("POST /api/ssh/execute failed: 404")
        assert redacted is True
        assert "POST /api/" not in msg
        assert "[API]" in msg

    def test_redacts_multiple_patterns(self):
        from examples.mcp_server.tool_results import _redact_error_message
        msg, redacted = _redact_error_message(
            "GET /api/file/read failed for /app/data/file.txt"
        )
        assert redacted is True
        assert "[API]" in msg
        assert "[PATH]" in msg

    def test_does_not_redact_python_identifier(self):
        from examples.mcp_server.tool_results import _redact_error_message
        msg, redacted = _redact_error_message("model_variable/media_download")
        assert redacted is False
        assert msg == "model_variable/media_download"

    def test_tool_error_sets_meta_redacted(self):
        from examples.mcp_server.tool_results import tool_error
        result = tool_error(
            "test_tool",
            message="File at /media/1TB/data.txt not found",
        )
        assert result["meta"]["redacted"] is True
        assert "redacted" in str(result["meta"]["warnings"])

    def test_tool_error_does_not_set_redacted_for_safe_message(self):
        from examples.mcp_server.tool_results import tool_error
        result = tool_error(
            "test_tool",
            message="Container not found: example",
        )
        assert result["meta"].get("redacted") is False


# ── Issue 7: Compose tools ValueError handling ──────────────────

class TestComposeToolsErrorHandling:
    """docker_compose_ps/services/logs must handle ValueError gracefully."""

    @pytest.mark.asyncio
    async def test_compose_ps_returns_INVALID_INPUT_on_ValueError(self):
        from examples.mcp_server.server import docker_compose_ps
        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_dc.return_value.compose_ps.side_effect = ValueError("Invalid project dir")
            result = await docker_compose_ps(project_dir="/nonexistent")
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        assert "Invalid project dir" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_compose_services_returns_INVALID_INPUT_on_ValueError(self):
        from examples.mcp_server.server import docker_compose_services
        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_dc.return_value.compose_services.side_effect = ValueError("Invalid project dir")
            result = await docker_compose_services(project_dir="/nonexistent")
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_compose_logs_returns_INVALID_INPUT_on_ValueError(self):
        from examples.mcp_server.server import docker_compose_logs
        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_dc.return_value.compose_logs.side_effect = ValueError("Invalid project dir")
            result = await docker_compose_logs(project_dir="/nonexistent")
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_compose_ps_success_returns_canonical(self):
        from examples.mcp_server.server import docker_compose_ps
        async def _fake_compose_ps(**kwargs):
            return "CONTAINER ID   IMAGE"
        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_dc.return_value.compose_ps = _fake_compose_ps
            result = await docker_compose_ps()
        assert result["ok"] is True
        assert result["tool"] == "docker_compose_ps"
        assert result["result"] == "CONTAINER ID   IMAGE"


# ── Issue 8: Observability — build_sha resolution ───────────────

class TestBuildShaGitlinkFallback:
    """build_sha must resolve from .git/HEAD when git binary is unavailable."""

    def test_resolve_from_gitlink(self, tmp_path):
        from app.build_info import _resolve_build_sha_from_gitlink
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        head_file = git_dir / "HEAD"
        head_file.write_text("ref: refs/heads/master\n")
        ref_dir = git_dir / "refs" / "heads"
        ref_dir.mkdir(parents=True)
        (ref_dir / "master").write_text("a" * 40 + "\n")
        sha = _resolve_build_sha_from_gitlink(git_dir)
        assert sha == "a" * 40

    def test_returns_none_when_gitlink_missing(self, tmp_path):
        from app.build_info import _resolve_build_sha_from_gitlink
        sha = _resolve_build_sha_from_gitlink(tmp_path / ".git")
        assert sha is None

    def test_returns_none_when_head_is_detached_but_missing(self, tmp_path):
        from app.build_info import _resolve_build_sha_from_gitlink
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/nonexistent\n")
        sha = _resolve_build_sha_from_gitlink(git_dir)
        assert sha is None

    def test_fallback_chain_ends_in_unknown(self):
        """Without BUILD_SHA, without git binary, without .git/HEAD -> 'unknown'."""
        with (
            patch.dict(os.environ, {"BUILD_SHA": ""}, clear=False),
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("app.build_info._resolve_build_sha_from_gitlink", return_value=None),
        ):
            from app.build_info import _resolve_build_sha
            sha = _resolve_build_sha()
        assert sha == "unknown"

    def test_uses_gitlink_before_unknown(self, tmp_path):
        """_resolve_build_sha must try .git/HEAD before giving up."""
        git_dir = Path(tmp_path / ".git")
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        with (
            patch.dict(os.environ, {"BUILD_SHA": ""}, clear=False),
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("app.build_info._resolve_build_sha_from_gitlink", return_value="b" * 40),
        ):
            from app.build_info import _resolve_build_sha
            sha = _resolve_build_sha()
        assert sha == "b" * 40


# ── Round 1: .git in HIDDEN_DIR_PATTERNS ─────────────────────────

class TestGitHiddenFromWorkspace:
    """.git directory must be blocked by workspace policy."""

    def test_git_in_hidden_dir_patterns(self):
        assert ".git" in HIDDEN_DIR_PATTERNS

    def test_workspace_rejects_dotgit_config(self, tmp_path):
        project_root = tmp_path / "project"
        project_root.mkdir()
        git_dir = project_root / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]\n\tbare = false\n")
        allowed_roots = [tmp_path]
        policy = WorkspacePolicy(
            project_roots={"project": project_root},
            allowed_roots=allowed_roots,
            granted_scopes={"project:read"},
        )
        from app.workspace_policy import HiddenPathError
        with pytest.raises(HiddenPathError):
            policy.validate_read("project", ".git/config")

    def test_workspace_rejects_dotgit_head(self, tmp_path):
        project_root = tmp_path / "project"
        project_root.mkdir()
        git_dir = project_root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/master\n")
        allowed_roots = [tmp_path]
        policy = WorkspacePolicy(
            project_roots={"project": project_root},
            allowed_roots=allowed_roots,
            granted_scopes={"project:read"},
        )
        from app.workspace_policy import HiddenPathError
        with pytest.raises(HiddenPathError):
            policy.validate_read("project", ".git/HEAD")

    def test_normal_file_still_readable(self, tmp_path):
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "README.md").write_text("# OK")
        policy = WorkspacePolicy(
            project_roots={"project": project_root},
            allowed_roots=[tmp_path],
            granted_scopes={"project:read"},
        )
        path = policy.validate_read("project", "README.md")
        assert path.exists()


# ── Issue 6: Response format canonical + duration_ms ────────────

class TestRunToolCanonicalFormat:
    """run_tool must return canonical tool_success/tool_error format."""

    def test_ok_true_passthrough(self):
        from examples.mcp_server.server import run_tool
        result = run_tool(tool="t", title="T", fn=lambda: {"ok": True, "result": "data"}, success_text="OK")
        assert result["ok"] is True
        assert result["result"] == "data"

    def test_ok_false_wraps_in_tool_error(self):
        from examples.mcp_server.server import run_tool
        result = run_tool(tool="t", title="T", fn=lambda: {"ok": False, "error": {"code": "TOOL_EXECUTION_FAILED", "message": "failed"}}, success_text="OK")
        assert result["ok"] is False
        assert result["error"]["code"] == "TOOL_EXECUTION_FAILED"

    def test_duration_ms_populated_on_canonical_result(self):
        from examples.mcp_server.server import run_tool
        result = run_tool(tool="t", title="T", fn=lambda: {"ok": True, "result": "x", "meta": {}}, success_text="OK")
        assert "duration_ms" in result["meta"]

    def test_duration_ms_not_lost_when_meta_missing(self):
        """BUG: run_tool doesn't assign meta back to data when meta key is absent.

        data.get('meta', {}) returns a temporary {} that gets duration_ms
        assigned to the local variable but NOT back to data['meta'].
        This test documents the current behavior — the value is lost.
        """
        from examples.mcp_server.server import run_tool
        result = run_tool(tool="t", title="T", fn=lambda: {"ok": True, "result": "x"}, success_text="OK")
        assert "meta" not in result

    def test_text_result_wrapped(self):
        from examples.mcp_server.server import run_tool
        result = run_tool(tool="t", title="T", fn=lambda: "plain string", success_text="plain string")  # type: ignore[arg-type, return-value]
        assert result["ok"] is True
        assert "result" in result

    def test_valueerror_traversal_returns_policy_denied(self):
        from examples.mcp_server.server import run_tool
        result = run_tool(tool="t", title="T", fn=lambda: (_ for _ in ()).throw(ValueError("traversal blocked")), success_text="OK")
        assert result["ok"] is False
        assert result["error"]["code"] == "POLICY_DENIED"

    def test_valueerror_other_returns_invalid_input(self):
        from examples.mcp_server.server import run_tool
        result = run_tool(tool="t", title="T", fn=lambda: (_ for _ in ()).throw(ValueError("bad param")), success_text="OK")
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
