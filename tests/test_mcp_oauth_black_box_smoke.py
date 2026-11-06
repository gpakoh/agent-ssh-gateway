"""Unit tests for scripts/mcp_oauth_black_box_smoke.py -- the full
DCR/OAuth authorization-code flow -> real tools/call git_status smoke.

No real network is used: the HTTP layer is a fake HTTPConnection that
answers the /register -> /authorize -> /oauth/consent -> /token -> /mcp
sequence from canned responses. The real end-to-end run (real sockets,
real container env) is exercised via `docker exec mcp-oauth python3
scripts/mcp_oauth_black_box_smoke.py` in the host-smoke workflow.

Audit #6 follow-up: this is the black-box MCP->Gateway->SSH->harmless
command->exact result scenario the old smoke scripts never covered
(they stopped at initialize + tools/list).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_module("mcp_oauth_black_box_smoke")


class _FakeResponse:
    def __init__(self, status: int, body: bytes, location: str = "", sid: str = "") -> None:
        self.status = status
        self._body = body
        self._location = location
        self._sid = sid
        self._read_once = False

    def read(self, _n: int) -> bytes:
        if self._read_once:
            return b""
        self._read_once = True
        return self._body

    def getheader(self, name: str, default: str = "") -> str:
        if name == "Location":
            return self._location
        if name == "mcp-session-id":
            return self._sid
        return default

    def close(self) -> None:
        pass


def _json(status: int, payload: dict) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode())


def _sse(payload: dict, sid: str = "") -> _FakeResponse:
    return _FakeResponse(200, f"data: {json.dumps(payload)}\n\n".encode(), sid=sid)


CONSENT_URL = (
    "http://localhost/oauth/consent?client_id=cid&redirect_uri=http%3A%2F%2Flocalhost%2Fcallback"
    "&scope=mcp%3Aread%20mcp%3Aproject&state=bb-smoke&code_challenge=ch&resource="
)
CALLBACK_URL = "http://localhost/callback?code=authcode"
GIT_STATUS_TEXT = json.dumps({
    "ok": True,
    "result": {"outcome": "passed", "exit_code": 0,
               "stdout": " M docker/docker-compose.yml\n"},
})


def _required_job_tool(*, include_expected_head: bool = True) -> dict:
    names = ["owner", "repo", "run_id", "job_id"]
    if include_expected_head:
        names.append("expected_head_sha")
    return {
        "name": "gitea_rerun_action_job",
        "inputSchema": {
            "type": "object",
            "properties": {name: {"type": "string"} for name in names},
            "required": names,
        },
    }


@pytest.fixture(autouse=True)
def _clear_schema_requirement(monkeypatch):
    monkeypatch.delenv("MCP_SMOKE_REQUIRED_TOOL", raising=False)
    monkeypatch.delenv("MCP_SMOKE_REQUIRED_INPUTS", raising=False)


class TestSmokeStageDeadlines:
    def test_timeout_stage_extractor_never_prints_captured_credentials(self):
        from tests.test_mcp_oauth_host_smoke import _last_safe_smoke_stage

        lines = (
            b"mcp_oauth_black_box_smoke: stage=initialize\n"
            b"private-bearer-token=never-leak\n"
            b"mcp_oauth_black_box_smoke: stage=git_status\n"
            b"mcp_oauth_black_box_smoke: stage=private_bearer_token\n"
        )
        assert _last_safe_smoke_stage(lines) == "git_status"
        assert _last_safe_smoke_stage(b"password=secret") == "unknown"
        assert _last_safe_smoke_stage(None) == "unknown"

    def test_host_smoke_timeout_reports_only_safe_stage(self, monkeypatch):
        import subprocess

        from tests import test_mcp_oauth_host_smoke as host

        monkeypatch.setattr(host, "_docker_available", lambda: True)
        monkeypatch.setattr(host, "_docker_exec", lambda *_args: "secret-password")

        def timeout_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd=args[0],
                timeout=kwargs.get("timeout", 90),
                stderr=(
                    b"mcp_oauth_black_box_smoke: stage=git_status\n"
                    b"Bearer super-private-token password=secret-password\n"
                ),
            )

        monkeypatch.setattr(host.subprocess, "run", timeout_run)
        with pytest.raises(pytest.fail.Exception, match="last_stage=git_status") as exc:
            host.test_mcp_oauth_black_box_smoke_full_flow()
        assert "super-private-token" not in str(exc.value)
        assert "secret-password" not in str(exc.value)

    def test_stage_names_do_not_reflect_private_urls_or_bodies(self, capsys):
        assert smoke._http_stage(
            "GET", "/authorize?state=private-token&code=private-code", None
        ) == "authorize"
        assert smoke._http_stage(
            "POST", "/mcp", {"method": "tools/call", "token": "private-secret"}
        ) == "git_status"
        assert smoke._http_stage(
            "GET", "/unknown/private-secret?token=private-token", None
        ) == "unknown"
        connection = MagicMock()
        connection.getresponse.return_value = _FakeResponse(
            302, b"", location="http://localhost/callback"
        )
        with patch.object(smoke.http.client, "HTTPConnection", return_value=connection):
            smoke._req("GET", "/authorize?state=private-token&code=private-code")
        output = capsys.readouterr().err
        assert "stage=authorize" in output
        assert "private-token" not in output
        assert "private-code" not in output

    def test_stage_timeout_is_smaller_than_outer_subprocess_budget(self, monkeypatch):
        monkeypatch.setattr(smoke, "TIMEOUT", 90.0)
        monkeypatch.setattr(smoke, "_SMOKE_DEADLINE", None)
        assert smoke._request_timeout("register") == smoke.MAX_HTTP_STAGE_SECONDS
        assert smoke._request_timeout("cleanup") == smoke.MAX_CLEANUP_SECONDS
        monkeypatch.setattr(smoke, "_SMOKE_DEADLINE", smoke.time.monotonic() - 1)
        with pytest.raises(smoke.SmokeError, match="deadline"):
            smoke._request_timeout("register")

    def test_real_stalled_header_raises_bounded_timeout_and_marks_stage(
        self, monkeypatch, capsys
    ):
        import http.server
        import threading
        import time

        release = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                release.wait(8.0)
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setattr(smoke, "BASE_HOST", "127.0.0.1")
            monkeypatch.setattr(smoke, "BASE_PORT", server.server_port)
            monkeypatch.setattr(smoke, "TIMEOUT", 5.0)
            monkeypatch.setattr(smoke, "MAX_HTTP_STAGE_SECONDS", 0.3)
            monkeypatch.setattr(smoke, "_SMOKE_DEADLINE", None)
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                smoke._req("POST", "/register", {"password": "never-echo-secret"})
            # Socket budget is 0.3s; allow substantial CI scheduling jitter
            # while still proving we did not wait for the 5s outer timeout.
            assert time.monotonic() - start < 4.5
            output = capsys.readouterr().err
            assert "stage=register" in output
            assert "never-echo-secret" not in output
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_main_resets_deadline_and_does_not_echo_exception_secrets(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "private-password")
        monkeypatch.setattr(smoke, "_SMOKE_DEADLINE", None)
        with patch.object(
            smoke, "_register_client",
            side_effect=RuntimeError("Bearer private-token private-password"),
        ):
            assert smoke.main() == 1
        assert smoke._SMOKE_DEADLINE is None
        output = capsys.readouterr().err
        assert "error=RuntimeError" in output
        assert "private-password" not in output
        assert "private-token" not in output


class TestPersistentMcpSse:
    def test_returns_first_event_without_waiting_for_eof(self):
        event = b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
        response = MagicMock()
        response.getheader.return_value = "text/event-stream; charset=utf-8"
        response.read1.return_value = event
        response.read.side_effect = AssertionError("read() waits for EOF on SSE")
        assert smoke._read_response_body(response) == event
        response.read1.assert_called_once_with(8192)
        response.read.assert_not_called()

    def test_keepalive_before_data_does_not_terminate_stream(self):
        response = MagicMock()
        response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = [
            b": ping\n\n",
            b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
            AssertionError("must stop after first completed data frame"),
        ]
        data = smoke._read_response_body(response)
        assert data.startswith(b": ping\n\n")
        assert data.endswith(b"}\n\n")
        assert response.read1.call_count == 2

    def test_fragmented_crlf_event_is_recognized(self):
        response = MagicMock()
        response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = [
            b'data: {"result":{}}\r\n',
            b"\r\n",
            AssertionError("reader must stop after complete SSE event"),
        ]
        assert smoke._read_response_body(response).endswith(b"\r\n\r\n")
        assert response.read1.call_count == 2

    def test_incomplete_sse_event_fails_closed(self):
        response = MagicMock()
        response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = [b"data: {", b""]
        with pytest.raises(smoke.SmokeError, match="complete event"):
            smoke._read_response_body(response)

    def test_large_sse_stream_fails_closed(self):
        response = MagicMock()
        response.getheader.return_value = "text/event-stream"
        response.read1.return_value = b"x" * (smoke.MAX_SMOKE_RESPONSE_BYTES + 1)
        with pytest.raises(smoke.SmokeError, match="byte limit"):
            smoke._read_response_body(response)

    def test_real_chunked_sse_with_open_connection_returns_first_event(self, monkeypatch):
        """Real HTTPResponse must not wait for an SSE stream's EOF or 8192 bytes."""
        import http.server
        import threading
        import time

        release = threading.Event()
        event = b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Mcp-Session-Id", "test-session")
                self.end_headers()
                self.wfile.write(f"{len(event):x}\r\n".encode() + event + b"\r\n")
                self.wfile.flush()
                release.wait(2.5)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setattr(smoke, "BASE_HOST", "127.0.0.1")
            monkeypatch.setattr(smoke, "BASE_PORT", server.server_port)
            monkeypatch.setattr(smoke, "TIMEOUT", 1.5)
            start = time.monotonic()
            status, result, _, sid = smoke._req("POST", "/mcp", {"jsonrpc": "2.0"})
            assert time.monotonic() - start < 1.5
            assert status == 200 and sid == "test-session"
            assert result == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class TestMcpOauthBlackBoxSmoke:
    def test_default_project_matches_registered_gateway_project(self):
        assert smoke.DEFAULT_PROJECT == "agent-ssh-gateway"
        assert smoke.PROJECT == smoke.DEFAULT_PROJECT

    def test_missing_password_fails(self, monkeypatch):
        monkeypatch.delenv("MCP_AUTHORIZE_PASSWORD", raising=False)
        assert smoke.main() == 1

    def test_success_full_flow(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        responses = [
            _json(201, {"client_id": "cid", "client_secret": ""}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location=CALLBACK_URL),
            _json(200, {"access_token": "at-1", "scope": "mcp:read mcp:project"}),
            _sse({"jsonrpc": "2.0", "id": 1, "result": {}}, sid="sid-1"),
            _sse({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": GIT_STATUS_TEXT}],
                },
            }),
            _FakeResponse(200, b""),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 0
        method, path, _body, headers = fake_conn.request.call_args_list[-1].args
        assert (method, path) == ("DELETE", "/mcp")
        assert headers["Mcp-Session-Id"] == "sid-1"
        assert headers["Authorization"] == "Bearer at-1"

    def test_register_failure_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = _json(400, {"error": "invalid_client_metadata"})
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 1

    def test_consent_no_code_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        responses = [
            _json(201, {"client_id": "cid"}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location="http://localhost/callback?error=denied"),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 1

    def test_token_failure_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        responses = [
            _json(201, {"client_id": "cid"}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location=CALLBACK_URL),
            _json(400, {"error": "invalid_grant"}),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 1

    def test_git_status_iserror_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        responses = [
            _json(201, {"client_id": "cid"}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location=CALLBACK_URL),
            _json(200, {"access_token": "at-1"}),
            _sse({"jsonrpc": "2.0", "id": 1, "result": {}}, sid="sid-1"),
            _sse({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": '{"error": "boom"}'}],
                },
            }),
            _FakeResponse(204, b""),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 1

    def test_git_status_clean_checkout_succeeds(self, monkeypatch):
        # git status --short on a clean checkout produces EMPTY stdout;
        # outcome=passed + exit_code=0 is still the exact result (the
        # SSH chain really executed via /api/ssh/execute-argv -> sshd).
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        responses = [
            _json(201, {"client_id": "cid"}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location=CALLBACK_URL),
            _json(200, {"access_token": "at-1"}),
            _sse({"jsonrpc": "2.0", "id": 1, "result": {}}, sid="sid-1"),
            _sse({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(
                        {"ok": True, "result": {"outcome": "passed",
                                                "exit_code": 0, "stdout": ""}}
                    )}],
                },
            }),
            _FakeResponse(204, b""),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 0

    def test_git_status_failed_outcome_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        responses = [
            _json(201, {"client_id": "cid"}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location=CALLBACK_URL),
            _json(200, {"access_token": "at-1"}),
            _sse({"jsonrpc": "2.0", "id": 1, "result": {}}, sid="sid-1"),
            _sse({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(
                        {"ok": False, "result": {"outcome": "failed",
                                                 "exit_code": 1,
                                                 "stdout": "boom"}}
                    )}],
                },
            }),
            _FakeResponse(204, b""),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 1

    def test_session_cleanup_failure_fails_closed(self, monkeypatch, capsys):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        responses = [
            _json(201, {"client_id": "cid"}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location=CALLBACK_URL),
            _json(200, {"access_token": "at-1"}),
            _sse({"jsonrpc": "2.0", "id": 1, "result": {}}, sid="sid-1"),
            _sse({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": GIT_STATUS_TEXT}],
                },
            }),
            _FakeResponse(500, b""),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 1
        captured = capsys.readouterr()
        assert "session cleanup failed" in captured.err
        assert "OK" not in captured.out

    def test_transport_error_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        with patch.object(
            smoke.http.client, "HTTPConnection", side_effect=OSError("connection refused")
        ):
            assert smoke.main() == 1

    def test_required_job_schema_succeeds_without_admin_tool_call(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        monkeypatch.setenv("MCP_SMOKE_REQUIRED_TOOL", "gitea_rerun_action_job")
        monkeypatch.setenv(
            "MCP_SMOKE_REQUIRED_INPUTS",
            "owner,repo,run_id,job_id,expected_head_sha",
        )
        responses = [
            _json(201, {"client_id": "cid", "client_secret": ""}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location=CALLBACK_URL),
            _json(200, {"access_token": "at-1", "scope": "mcp:read mcp:project"}),
            _sse({"jsonrpc": "2.0", "id": 1, "result": {}}, sid="sid-1"),
            _sse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"tools": [_required_job_tool()]},
                }
            ),
            _sse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": False,
                        "content": [{"type": "text", "text": GIT_STATUS_TEXT}],
                    },
                }
            ),
            _FakeResponse(204, b""),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 0

        mcp_bodies = [
            json.loads(call.args[2])
            for call in fake_conn.request.call_args_list
            if call.args[1] == "/mcp" and call.args[0] == "POST"
        ]
        assert [body["method"] for body in mcp_bodies] == [
            "initialize",
            "tools/list",
            "tools/call",
        ]
        assert mcp_bodies[-1]["params"]["name"] == "git_status"

    def test_partial_required_schema_fails_before_tool_call(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        monkeypatch.setenv("MCP_SMOKE_REQUIRED_TOOL", "gitea_rerun_action_job")
        monkeypatch.setenv(
            "MCP_SMOKE_REQUIRED_INPUTS",
            "owner,repo,run_id,job_id,expected_head_sha",
        )
        responses = [
            _json(201, {"client_id": "cid"}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location=CALLBACK_URL),
            _json(200, {"access_token": "at-1"}),
            _sse({"jsonrpc": "2.0", "id": 1, "result": {}}, sid="sid-1"),
            _sse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "tools": [_required_job_tool(include_expected_head=False)]
                    },
                }
            ),
            _FakeResponse(204, b""),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 1

        mcp_bodies = [
            json.loads(call.args[2])
            for call in fake_conn.request.call_args_list
            if call.args[1] == "/mcp" and call.args[0] == "POST"
        ]
        assert [body["method"] for body in mcp_bodies] == ["initialize", "tools/list"]

    def test_truncated_tools_list_fails_without_replaying_oauth(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        monkeypatch.setenv("MCP_SMOKE_REQUIRED_TOOL", "gitea_rerun_action_job")
        responses = [
            _json(201, {"client_id": "cid"}),
            _FakeResponse(302, b"", location="http://localhost" + CONSENT_URL),
            _FakeResponse(303, b"", location=CALLBACK_URL),
            _json(200, {"access_token": "at-1"}),
            _sse({"jsonrpc": "2.0", "id": 1, "result": {}}, sid="sid-1"),
            _FakeResponse(200, b'data: {"jsonrpc":"2.0","id":1'),
            _FakeResponse(204, b""),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert smoke.main() == 1

        register_calls = [
            call for call in fake_conn.request.call_args_list if call.args[1] == "/register"
        ]
        assert len(register_calls) == 1

    def test_timeout_after_initialize_does_not_replay_oauth_flow(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTHORIZE_PASSWORD", "secret")
        monkeypatch.setenv("MCP_SMOKE_REQUIRED_TOOL", "gitea_rerun_action_job")
        with (
            patch.object(smoke, "_register_client", return_value="cid") as register_client,
            patch.object(smoke, "_oauth_flow", return_value="at-1") as oauth_flow,
            patch.object(
                smoke,
                "_mcp_call",
                side_effect=[({}, "sid-1"), TimeoutError("tools/list timeout")],
            ) as mcp_call,
            patch.object(smoke, "_terminate_session") as terminate_session,
        ):
            assert smoke.main() == 1

        register_client.assert_called_once_with()
        oauth_flow.assert_called_once_with("cid", "secret")
        assert mcp_call.call_count == 2
        terminate_session.assert_called_once_with("at-1", "sid-1")
