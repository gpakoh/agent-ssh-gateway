"""C3 contract tests: command policy enforcement across REST, WebSocket, and MCP.

Tests verify the COMMAND_POLICY_DENIED response contract for each endpoint type.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

# ---------------------------------------------------------------------------
# MCP server sys.path (needed for gateway_client/server imports)
# ---------------------------------------------------------------------------
_EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
_MCP_SERVER_DIR = _EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(_MCP_SERVER_DIR))
sys.path.insert(0, str(_EXAMPLES_DIR.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)


def _auth_headers():
    return {"X-API-Key": settings.api_key}


def _setup_test(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-c3")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )
    from app import state as _app_state

    _app_state.audit_logger = MagicMock()
    _app_state.manager = MagicMock()
    return _app_state


def _setup_enforce_readonly(monkeypatch):
    """Configure enforce mode with readonly profile for real policy evaluation."""
    monkeypatch.setattr(settings, "command_policy_mode", "enforce")
    monkeypatch.setattr(settings, "command_policy_profile", "readonly")


def _new_gateway_client(**overrides):
    """Create a GatewayClient with defaults for testing."""
    from examples.mcp_server.gateway_client import GatewayClient

    c = GatewayClient.__new__(GatewayClient)
    c.base_url = overrides.get("base_url", "http://test:8085")
    c.api_key = overrides.get("api_key", "test-key")
    c.session_id = overrides.get("session_id", "test-session")
    c.command_timeout = overrides.get("command_timeout", 30)
    c.job_timeout = overrides.get("job_timeout", 180)
    c._reconnect_lock = MagicMock()
    c._ssh_host = ""
    c._ssh_port = 22
    c._ssh_user = ""
    c._ssh_password = ""
    c._ssh_private_key = ""
    return c


# ---------------------------------------------------------------------------
# REST /api/ssh/execute — enforce mode contract
# ---------------------------------------------------------------------------


class TestExecuteEnforcePolicy:
    """POST /api/ssh/execute must return 403 with FORBIDDEN error code."""

    def test_denied_returns_403_with_correct_envelope(self, client, monkeypatch):
        _setup_test(monkeypatch)
        _setup_enforce_readonly(monkeypatch)

        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "systemctl restart nginx"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403
        body = resp.json()
        assert "detail" in body
        assert body["detail"]["code"] == "FORBIDDEN"
        assert "Command denied by policy" in body["detail"]["message"]

    def test_pipe_blocked_in_enforce(self, client, monkeypatch):
        _setup_test(monkeypatch)
        _setup_enforce_readonly(monkeypatch)

        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "echo x | cat"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403
        body = resp.json()
        assert "Command denied by policy" in body["detail"]["message"]

    def test_allowed_command_proceeds(self, client, monkeypatch):
        _setup_test(monkeypatch)
        _setup_enforce_readonly(monkeypatch)

        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        _app_state = _setup_test(monkeypatch)
        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute = AsyncMock(
            return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
        )

        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "ls -la"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

    def test_denied_with_async_mode_still_403(self, client, monkeypatch):
        """async_mode does not bypass command policy — denied is still 403."""
        _setup_test(monkeypatch)
        _setup_enforce_readonly(monkeypatch)

        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "systemctl restart nginx",
                "async_mode": True,
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# REST /api/ssh/execute-argv — enforce mode contract
# ---------------------------------------------------------------------------


class TestExecuteArgvEnforcePolicy:
    """POST /api/ssh/execute-argv must return 403 with FORBIDDEN error code."""

    def test_denied_returns_403_with_correct_envelope(self, client, monkeypatch):
        _setup_test(monkeypatch)
        _setup_enforce_readonly(monkeypatch)

        resp = client.post(
            "/api/ssh/execute-argv",
            json={"session_id": "sid", "argv": ["systemctl", "restart", "nginx"]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["code"] == "FORBIDDEN"
        assert "Command denied by policy" in body["detail"]["message"]

    def test_pipe_in_argv_denied(self, client, monkeypatch):
        """Even if argv has no semicolons, the joined command contains metachar."""
        _setup_test(monkeypatch)
        _setup_enforce_readonly(monkeypatch)

        resp = client.post(
            "/api/ssh/execute-argv",
            json={"session_id": "sid", "argv": ["echo", "x | cat"]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403

    def test_allowed_argv_proceeds(self, client, monkeypatch):
        _setup_test(monkeypatch)
        _setup_enforce_readonly(monkeypatch)

        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        _app_state = _setup_test(monkeypatch)
        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute_argv = AsyncMock(
            return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
        )

        resp = client.post(
            "/api/ssh/execute-argv",
            json={"session_id": "sid", "argv": ["ls", "-la"]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# WebSocket /api/ssh/execute/stream — enforce mode contract
# ---------------------------------------------------------------------------

WS_EXECUTE = "ws://testserver/api/ssh/execute/stream"


class TestWebSocketEnforcePolicy:
    """WebSocket execute must return type=error, code=COMMAND_POLICY_DENIED."""

    @pytest.fixture
    def agent_with_execute(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "c3-ws-key")
        monkeypatch.setattr(settings, "agent_token", "c3-agent")
        monkeypatch.setattr(settings, "agent_token_scopes", ["ssh:execute"])
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")
        monkeypatch.setattr("app.auth_middleware.is_ip_allowed", lambda ip, nets: True)

    def test_denied_command_returns_policy_denied_code(self, agent_with_execute):
        with TestClient(app) as client:
            with client.websocket_connect(
                WS_EXECUTE, headers={"X-API-Key": "c3-agent"}
            ) as ws:
                ws.send_json(
                    {"session_id": "test-session", "command": "systemctl restart nginx"}
                )
                resp = ws.receive_json()
                assert resp.get("type") == "error"
                assert resp.get("code") == "COMMAND_POLICY_DENIED"
                assert "Command denied by policy" in resp.get("message", "")

    def test_allowed_command_does_not_error(self, agent_with_execute):
        with TestClient(app) as client:
            with client.websocket_connect(
                WS_EXECUTE, headers={"X-API-Key": "c3-agent"}
            ) as ws:
                ws.send_json({"session_id": "test-session", "command": "ls"})
                resp = ws.receive_json()
                # Allowed command proceeds — may get result or close
                assert resp.get("code") != "COMMAND_POLICY_DENIED"


# ---------------------------------------------------------------------------
# MCP execute_restricted — client-side contract
# ---------------------------------------------------------------------------


class TestMcpExecuteRestricted:
    """MCP execute_restricted must call /api/ssh/execute with async_mode=true."""

    def test_calls_execute_with_async_mode(self):
        client = _new_gateway_client()

        with patch.object(
            client,
            "_post",
            return_value={"job_id": "job-1", "status": "running"},
        ) as mock_post:
            result = client.execute_restricted("ls -la")

        mock_post.assert_called_once_with(
            "/api/ssh/execute",
            {
                "session_id": "test-session",
                "command": "ls -la",
                "async_mode": True,
                "redact_output": True,
                "timeout": 30,
            },
        )
        assert result["job_id"] == "job-1"

    def test_denied_command_propagates_error(self):
        """If server returns 403, execute_restricted should propagate the error."""
        from examples.mcp_server.gateway_client import GatewayClientError

        client = _new_gateway_client()

        # validate_readonly_command has its own MCP-level allowlist;
        # mock it to bypass client-side check so we test server-side propagation.
        with patch(
            "examples.mcp_server.gateway_client.validate_readonly_command",
            return_value="systemctl restart nginx",
        ):
            with patch.object(
                client, "_post", side_effect=GatewayClientError("Request failed: 403")
            ):
                with pytest.raises(GatewayClientError, match="403"):
                    client.execute_restricted("systemctl restart nginx")


# ---------------------------------------------------------------------------
# MCP execute_argv — client-side contract
# ---------------------------------------------------------------------------


class TestMcpExecuteArgvContract:
    """MCP execute_argv must call /api/ssh/execute-argv with correct payload."""

    def test_calls_correct_endpoint(self):
        client = _new_gateway_client()

        with patch.object(
            client,
            "_post",
            return_value={"exit_code": 0, "stdout": "hi", "stderr": "", "duration": 0.1},
        ) as mock_post:
            result = client.execute_argv(
                argv=["python3", "-c", "print('hi')"],
                stdin="input-data",
                timeout_s=15,
            )

        mock_post.assert_called_once_with(
            "/api/ssh/execute-argv",
            {
                "session_id": "test-session",
                "argv": ["python3", "-c", "print('hi')"],
                "stdin": "input-data",
                "timeout_s": 15,
            },
        )
        assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# MCP project_run_pytest — client-side contract
# ---------------------------------------------------------------------------


class TestMcpProjectRunPytest:
    """MCP project_run_pytest must call execute_raw with correct command."""

    def test_calls_execute_raw(self):
        client = _new_gateway_client()

        # Two calls to execute_raw: (1) uv check, (2) pytest run
        mock_responses = [
            {"exit_code": 0, "stdout": "", "stderr": ""},  # uv check
            {"job_id": "pytest-job-1", "status": "running"},  # pytest run
        ]

        with patch(
            "examples.mcp_server.chatgpt_tools._resolve_project",
            return_value=Path("/srv/projects/myproj"),
        ):
            with patch(
                "examples.mcp_server.chatgpt_tools._build_uv_argv",
                return_value=["uv", "run", "--frozen", "--directory", "/srv/projects/myproj", "--", "pytest", "--", "tests/"],
            ):
                with patch.object(
                    client,
                    "execute_raw",
                    side_effect=mock_responses,
                ) as mock_exec:
                    with patch.object(
                        client,
                        "wait_job",
                        return_value={"exit_code": 0, "stdout": "passed", "stderr": ""},
                    ):
                        with patch(
                            "examples.mcp_server.chatgpt_tools.build_command_result",
                            return_value={"outcome": "passed", "exit_code": 0},
                        ):
                            from examples.mcp_server.chatgpt_tools import project_run_pytest

                            project_run_pytest(client, "myproj", ["tests/"])

        # First call: check for uv (command -v uv)
        # Second call: run pytest command
        assert mock_exec.call_count == 2
        uv_check_call = mock_exec.call_args_list[0]
        assert "command -v uv" in uv_check_call[0][0]
        pytest_call = mock_exec.call_args_list[1]
        assert "pytest" in pytest_call[0][0]


# ---------------------------------------------------------------------------
# Command policy profile coverage
# ---------------------------------------------------------------------------


class TestCommandPolicyProfileIntegration:
    """Verify each profile interacts correctly with enforce mode."""

    def test_readonly_denies_rm(self, client, monkeypatch):
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "rm file.txt"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403

    def test_readonly_allows_cat(self, client, monkeypatch):
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        _app_state = _setup_test(monkeypatch)
        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute = AsyncMock(
            return_value={
                "stdout": "content",
                "stderr": "",
                "exit_code": 0,
                "duration": 0.1,
            }
        )

        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "cat /etc/hosts"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

    def test_testlint_allows_pytest(self, client, monkeypatch):
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "testlint")

        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        _app_state = _setup_test(monkeypatch)
        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute = AsyncMock(
            return_value={
                "stdout": "passed",
                "stderr": "",
                "exit_code": 0,
                "duration": 0.1,
            }
        )

        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "pytest -q"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

    def test_testlint_denies_unlisted_command(self, client, monkeypatch):
        """testlint profile blocks commands not in its allowlist (e.g. systemctl)."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "testlint")

        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "systemctl restart nginx"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Audit fields — effective profile is logged, not caller-requested profile
# ---------------------------------------------------------------------------


class TestAuditEffectiveProfile:
    """COMMAND_POLICY_DECISION audit events must include effective profile,
    policy_mode, command_root, allowed, reason.  Must NOT include
    caller-requested profile or raw secrets."""

    def test_audit_logs_effective_profile_from_settings(self, client, monkeypatch):
        """Audit event profile= field matches the server settings, not any caller input."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        _app_state = _setup_test(monkeypatch)
        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute = AsyncMock(
            return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
        )

        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "ls -la"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

        calls = _app_state.audit_logger.log_security_event.call_args_list
        policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
        assert len(policy_calls) >= 1
        detail = policy_calls[0][0][1]
        # Effective profile must be "readonly" (from settings), not anything else
        assert "profile=readonly" in detail
        assert "mode=enforce" in detail
        assert "allowed=True" in detail

    def test_audit_does_not_log_caller_requested_profile(self, client, monkeypatch):
        """Even if the HTTP body contained a profile field, audit logs server's effective profile."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        _app_state = _setup_test(monkeypatch)
        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute = AsyncMock(
            return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
        )

        # Send a body with a fake "profile" field (should be ignored by the server)
        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "ls -la", "profile": "docker-admin"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

        calls = _app_state.audit_logger.log_security_event.call_args_list
        policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
        assert len(policy_calls) >= 1
        detail = policy_calls[0][0][1]
        # Must NOT contain docker-admin — server used readonly from settings
        assert "profile=readonly" in detail
        assert "docker-admin" not in detail

    def test_execute_argv_audit_logs_effective_profile(self, client, monkeypatch):
        """execute_argv audit event includes effective profile from settings."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        _app_state = _setup_test(monkeypatch)
        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute_argv = AsyncMock(
            return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
        )

        resp = client.post(
            "/api/ssh/execute-argv",
            json={"session_id": "sid", "argv": ["ls", "-la"]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

        calls = _app_state.audit_logger.log_security_event.call_args_list
        policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
        assert len(policy_calls) >= 1
        detail = policy_calls[0][0][1]
        assert "profile=readonly" in detail
        assert "mode=enforce" in detail
        assert "command_root=ls" in detail


# ---------------------------------------------------------------------------
# No-escalation: caller cannot raise profile via body or headers
# ---------------------------------------------------------------------------


class TestNoEscalation:
    """Caller must not be able to escalate from readonly/default to
    docker-admin, ops, or any higher-privilege profile."""

    def test_readonly_cannot_escalate_to_docker_admin(self, client, monkeypatch):
        """POST /api/ssh/execute with profile=docker-admin in body → still readonly."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "docker system prune -f",
                "profile": "docker-admin",
            },
            headers=_auth_headers(),
        )
        # docker system prune is denied by readonly — escalation attempt must fail
        assert resp.status_code == 403

    def test_default_cannot_escalate_to_ops(self, client, monkeypatch):
        """POST /api/ssh/execute with profile=ops in body → still default.
        Claiming higher-profile must not bypass server settings.
        docker system prune is allowed by default but denied by readonly;
        this test proves body.profile is ignored regardless."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "docker system prune -f",
                "profile": "ops",
            },
            headers=_auth_headers(),
        )
        # docker denied by readonly — claiming ops must not bypass
        assert resp.status_code == 403

    def test_readonly_cannot_escalate_to_project_automation(self, client, monkeypatch):
        """POST /api/ssh/execute with profile=project-automation → still readonly."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "git push origin main",
                "profile": "project-automation",
            },
            headers=_auth_headers(),
        )
        # git push is denied by readonly — escalation attempt must fail
        assert resp.status_code == 403

    def test_execute_argv_no_escalation_via_body(self, client, monkeypatch):
        """execute_argv: body.profile is ignored, server uses settings."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        resp = client.post(
            "/api/ssh/execute-argv",
            json={
                "session_id": "sid",
                "argv": ["docker", "system", "prune", "-f"],
                "profile": "docker-admin",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# execute_argv — metachar denial (redirect/pipe still blocked)
# ---------------------------------------------------------------------------


class TestExecuteArgvMetacharStillBlocked:
    """execute_argv must block shell metacharacters even when body.profile claims
    a higher-privilege profile."""

    def test_redirect_gt_still_denied_with_profile_claim(self, client, monkeypatch):
        """argv=['sh','-c','echo x > f'] with profile=docker-admin → still FORBIDDEN."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        resp = client.post(
            "/api/ssh/execute-argv",
            json={
                "session_id": "sid",
                "argv": ["sh", "-c", "echo x > f"],
                "profile": "docker-admin",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "FORBIDDEN"

    def test_pipe_still_denied_with_profile_claim(self, client, monkeypatch):
        """argv=['sh','-c','echo x | cat'] with profile=ops → still FORBIDDEN."""
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        resp = client.post(
            "/api/ssh/execute-argv",
            json={
                "session_id": "sid",
                "argv": ["sh", "-c", "echo x | cat"],
                "profile": "ops",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Jobs / batch use canonical policy (no profile override)
# ---------------------------------------------------------------------------


class TestJobsBulkBatchCanonicalPolicy:
    """Jobs and batch endpoints use settings.command_policy_profile, never body.profile."""

    @pytest.mark.asyncio
    async def test_jobs_uses_settings_profile(self, monkeypatch):
        """POST /api/jobs/run with profile=docker-admin in body → still readonly."""
        from httpx import ASGITransport, AsyncClient

        from app import state as state_module
        from app.main import app as main_app

        monkeypatch.setattr(settings, "api_auth_enabled", False)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")
        state_module.audit_logger = MagicMock()
        state_module.manager = MagicMock()
        state_module.manager.get_session = AsyncMock(return_value={"id": "s1"})

        transport = ASGITransport(app=main_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/jobs/run",
                json={
                    "session_id": "s1",
                    "command": "docker system prune -f",
                    "profile": "docker-admin",
                },
            )
            assert resp.status_code == 403  # docker denied by readonly

            calls = state_module.audit_logger.log_security_event.call_args_list
            policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
            assert len(policy_calls) >= 1
            detail = policy_calls[0][0][1]
            assert "profile=readonly" in detail
            assert "docker-admin" not in detail

    @pytest.mark.asyncio
    async def test_bulk_execute_uses_settings_profile(self, monkeypatch):
        """POST /api/bulk/execute with profile=ops → still uses settings profile."""
        from httpx import ASGITransport, AsyncClient

        from app import state as state_module
        from app.main import app as main_app

        monkeypatch.setattr(settings, "api_auth_enabled", False)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")
        state_module.audit_logger = MagicMock()
        state_module.manager = MagicMock()

        transport = ASGITransport(app=main_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/bulk/execute",
                json={
                    "session_id": "s1",
                    "commands": ["docker system prune -f"],
                    "profile": "ops",
                },
            )
            assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Server-owned profile routing — integration test
# ---------------------------------------------------------------------------


class TestServerOwnedProfileRouting:
    """Prove that COMMAND_POLICY_KEY_PROFILES maps API key fingerprint → profile,
    that the effective profile is used for policy evaluation, that body.profile
    is ignored, and that no client can escalate."""

    @pytest.fixture(autouse=True)
    def _setup_key_profile(self, monkeypatch):
        """Set up API key 'secret-c3' with fingerprint mapped to testlint."""
        import hashlib

        # Compute fingerprint for the test API key
        fingerprint = hashlib.sha256(b"secret-c3").hexdigest()[:12]

        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-c3")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "access_control_enabled", False)
        # Server default is readonly — but key mapping overrides to testlint
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")
        monkeypatch.setattr(
            settings,
            "command_policy_key_profiles",
            json.dumps({fingerprint: "testlint"}),
        )
        monkeypatch.setattr(
            "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
        )

        from app import state as _app_state

        _app_state.audit_logger = MagicMock()
        _app_state.manager = MagicMock()
        # Mock async methods for successful execution path
        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute = AsyncMock(
            return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
        )

    def test_command_v_uv_allowed(self, client):
        """'command -v uv' is allowed under testlint (via key mapping)."""
        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "command -v uv"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

    def test_uv_run_pytest_allowed(self, client):
        """'uv run --directory /tmp/proj -- pytest -q' is allowed under testlint."""
        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "uv run --directory /tmp/proj -- pytest -q",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

    def test_tee_blocked(self, client):
        """'tee out.txt' is blocked by testlint (not in allowlist)."""
        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "tee out.txt"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403
        assert "testlint" in resp.json()["detail"]["message"]

    def test_python_c_exec_blocked(self, client):
        """'python3 -c ...' is blocked by testlint (exec flag detection)."""
        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "python3 -c 'import os; print(os.getcwd())'",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 403

    def test_body_profile_docker_admin_ignored(self, client):
        """body.profile=docker-admin is ignored — server uses testlint from key mapping."""
        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "command -v uv",
                "profile": "docker-admin",
            },
            headers=_auth_headers(),
        )
        # command -v uv is allowed by testlint — body.profile ignored, not escalated
        assert resp.status_code == 200

        # Verify audit shows testlint, not docker-admin
        from app import state as _app_state

        calls = _app_state.audit_logger.log_security_event.call_args_list
        policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
        assert len(policy_calls) >= 1
        detail = policy_calls[0][0][1]
        assert "profile=testlint" in detail
        assert "docker-admin" not in detail

    def test_body_profile_ops_ignored(self, client):
        """body.profile=ops is ignored — server uses testlint from key mapping."""
        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "command -v uv",
                "profile": "ops",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

        from app import state as _app_state

        calls = _app_state.audit_logger.log_security_event.call_args_list
        policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
        assert len(policy_calls) >= 1
        detail = policy_calls[0][0][1]
        assert "profile=testlint" in detail

    def test_no_escalation_tee_via_docker_admin_claim(self, client):
        """'tee out.txt' with profile=docker-admin → still blocked (testlint enforced)."""
        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "tee out.txt",
                "profile": "docker-admin",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 403

    def test_no_escalation_python_exec_via_ops_claim(self, client):
        """'python3 -c ...' with profile=ops → still blocked (testlint enforced)."""
        resp = client.post(
            "/api/ssh/execute",
            json={
                "session_id": "sid",
                "command": "python3 -c 'import os'",
                "profile": "ops",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 403

    def test_unmapped_key_uses_server_default(self, client, monkeypatch):
        """A different API key (not in key_profiles) falls back to server default (readonly)."""
        import hashlib

        # Different API key — not in the key_profiles mapping
        other_key = "other-key-xyz"

        monkeypatch.setattr(settings, "api_key", other_key)
        # Keep same key_profiles — the other key is NOT in it
        fingerprint_c3 = hashlib.sha256(b"secret-c3").hexdigest()[:12]
        monkeypatch.setattr(
            settings,
            "command_policy_key_profiles",
            json.dumps({fingerprint_c3: "testlint"}),
        )

        from app import state as _app_state

        _app_state.audit_logger = MagicMock()

        # 'tee out.txt' is denied by both readonly and testlint — 403
        resp = client.post(
            "/api/ssh/execute",
            json={"session_id": "sid", "command": "tee out.txt"},
            headers={"X-API-Key": other_key},
        )
        assert resp.status_code == 403

        # Verify audit shows readonly (server default), not testlint
        calls = _app_state.audit_logger.log_security_event.call_args_list
        policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
        assert len(policy_calls) >= 1
        detail = policy_calls[0][0][1]
        assert "profile=readonly" in detail
        assert "testlint" not in detail
