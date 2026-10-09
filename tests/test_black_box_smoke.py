"""Unit tests for the authenticated black-box smoke scripts deploy-
from-registry.sh runs (via `docker exec`) after a real deploy.

P1 BLOCKER audit finding: the only prior post-deploy check was each
container's own HEALTHCHECK (process readiness), never a real
authenticated request through the actual API/MCP-protocol boundary.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.smoke

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway_smoke = _load_module("gateway_black_box_smoke")
mcp_smoke = _load_module("mcp_black_box_smoke")


class TestGatewayBlackBoxSmoke:
    def test_missing_api_key_fails(self, monkeypatch):
        monkeypatch.delenv("API_KEY", raising=False)
        assert gateway_smoke.main() == 1

    def test_success_with_expected_shape(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "test-key")
        body = json.dumps({"sessions": [], "count": 0}).encode()
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        fake_resp.read = MagicMock(return_value=body)
        with patch.object(gateway_smoke.urllib.request, "urlopen", return_value=fake_resp):
            assert gateway_smoke.main() == 0

    def test_sends_api_key_header(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "my-secret-key")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["headers"] = dict(req.headers)
            captured["url"] = req.full_url
            fake_resp = MagicMock()
            fake_resp.status = 200
            fake_resp.__enter__ = MagicMock(return_value=fake_resp)
            fake_resp.__exit__ = MagicMock(return_value=False)
            fake_resp.read = MagicMock(
                return_value=json.dumps({"sessions": [], "count": 0}).encode()
            )
            return fake_resp

        with patch.object(gateway_smoke.urllib.request, "urlopen", side_effect=fake_urlopen):
            assert gateway_smoke.main() == 0
        # urllib.request.Request title-cases header names it's given.
        assert captured["headers"].get("X-api-key") == "my-secret-key"
        assert "/api/ssh/sessions" in captured["url"]

    def test_count_mismatch_fails(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "test-key")
        body = json.dumps({"sessions": [{"id": "a"}], "count": 5}).encode()
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        fake_resp.read = MagicMock(return_value=body)
        with patch.object(gateway_smoke.urllib.request, "urlopen", return_value=fake_resp):
            assert gateway_smoke.main() == 1

    def test_unexpected_shape_fails(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "test-key")
        body = json.dumps({"unexpected": "shape"}).encode()
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        fake_resp.read = MagicMock(return_value=body)
        with patch.object(gateway_smoke.urllib.request, "urlopen", return_value=fake_resp):
            assert gateway_smoke.main() == 1

    def test_non_200_status_fails(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "test-key")
        fake_resp = MagicMock()
        fake_resp.status = 403
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch.object(gateway_smoke.urllib.request, "urlopen", return_value=fake_resp):
            assert gateway_smoke.main() == 1

    def test_http_error_fails(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "wrong-key")
        import urllib.error

        err = urllib.error.HTTPError(
            "http://localhost:8085/api/ssh/sessions", 401, "Unauthorized", {}, io.BytesIO()
        )
        with patch.object(gateway_smoke.urllib.request, "urlopen", side_effect=err):
            assert gateway_smoke.main() == 1


class _FakeMcpResponse:
    def __init__(self, sse_body: bytes, session_id: str = "") -> None:
        self._body = sse_body
        self._session_id = session_id
        self._read_once = False

    def read(self, _n: int) -> bytes:
        if self._read_once:
            return b""
        self._read_once = True
        return self._body

    def getheader(self, _name: str, default: str = "") -> str:
        return self._session_id or default

    def close(self) -> None:
        pass


def _sse_frame(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_eof(payload: dict) -> bytes:
    """Single valid SSE data line terminated by HTTP EOF, without a blank frame separator."""
    return f"data: {json.dumps(payload)}\n".encode()


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
def _clear_mcp_smoke_schema_env(monkeypatch):
    for name in (
        "MCP_SMOKE_REQUIRED_TOOL",
        "MCP_SMOKE_REQUIRED_INPUTS",
        "MCP_SMOKE_ATTEMPTS",
        "MCP_SMOKE_RETRY_DELAY",
    ):
        monkeypatch.delenv(name, raising=False)


class TestMcpBlackBoxSmoke:
    def test_missing_token_fails(self, monkeypatch):
        monkeypatch.delenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", raising=False)
        assert mcp_smoke.main() == 1

    def test_success_with_tools(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")

        responses = [
            _FakeMcpResponse(_sse_frame({"jsonrpc": "2.0", "id": 1, "result": {}}), session_id="sid-1"),
            _FakeMcpResponse(
                _sse_frame({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "health"}]}})
            ),
        ]

        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 0

    def test_eof_terminated_sse_payload_is_accepted(self, monkeypatch):
        """Regression for deploy #15036: valid SSE data can end at HTTP EOF without a blank line."""
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MCP_SMOKE_ATTEMPTS", "2")
        monkeypatch.setenv("MCP_SMOKE_RETRY_DELAY", "0")
        responses = [
            _FakeMcpResponse(
                _sse_eof({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-1",
            ),
            _FakeMcpResponse(
                _sse_eof(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"tools": [{"name": "health"}]},
                    }
                )
            ),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 0
        assert fake_conn.getresponse.call_count == 2

    def test_no_session_id_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = _FakeMcpResponse(
            _sse_frame({"jsonrpc": "2.0", "id": 1, "result": {}}), session_id=""
        )
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 1

    def test_initialize_error_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "wrong-token")
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = _FakeMcpResponse(
            _sse_frame({"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "unauthorized"}}),
            session_id="sid-1",
        )
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 1

    def test_empty_tools_list_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        responses = [
            _FakeMcpResponse(_sse_frame({"jsonrpc": "2.0", "id": 1, "result": {}}), session_id="sid-1"),
            _FakeMcpResponse(_sse_frame({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 1

    def test_transport_error_fails_after_bounded_retries(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MCP_SMOKE_ATTEMPTS", "2")
        monkeypatch.setenv("MCP_SMOKE_RETRY_DELAY", "0")
        with patch.object(
            mcp_smoke.http.client, "HTTPConnection", side_effect=OSError("connection refused")
        ) as http_connection:
            assert mcp_smoke.main() == 1
        assert http_connection.call_count == 2

    def test_required_job_schema_succeeds(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MCP_SMOKE_REQUIRED_TOOL", "gitea_rerun_action_job")
        monkeypatch.setenv(
            "MCP_SMOKE_REQUIRED_INPUTS",
            "owner,repo,run_id,job_id,expected_head_sha",
        )
        responses = [
            _FakeMcpResponse(
                _sse_frame({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-1",
            ),
            _FakeMcpResponse(
                _sse_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"tools": [_required_job_tool()]},
                    }
                )
            ),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 0

    def test_required_job_schema_missing_tool_fails_without_retry(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MCP_SMOKE_REQUIRED_TOOL", "gitea_rerun_action_job")
        monkeypatch.setenv("MCP_SMOKE_ATTEMPTS", "3")
        responses = [
            _FakeMcpResponse(
                _sse_frame({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-1",
            ),
            _FakeMcpResponse(
                _sse_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"tools": [{"name": "health"}]},
                    }
                )
            ),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 1
        assert fake_conn.getresponse.call_count == 2

    def test_partial_required_schema_fails_without_retry(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MCP_SMOKE_REQUIRED_TOOL", "gitea_rerun_action_job")
        monkeypatch.setenv(
            "MCP_SMOKE_REQUIRED_INPUTS",
            "owner,repo,run_id,job_id,expected_head_sha",
        )
        monkeypatch.setenv("MCP_SMOKE_ATTEMPTS", "3")
        responses = [
            _FakeMcpResponse(
                _sse_frame({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-1",
            ),
            _FakeMcpResponse(
                _sse_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "tools": [_required_job_tool(include_expected_head=False)]
                        },
                    }
                )
            ),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 1
        assert fake_conn.getresponse.call_count == 2

    def test_truncated_initialize_retries_with_fresh_session(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MCP_SMOKE_ATTEMPTS", "2")
        monkeypatch.setenv("MCP_SMOKE_RETRY_DELAY", "0")
        responses = [
            _FakeMcpResponse(b'data: {"jsonrpc":"2.0","id":1'),
            _FakeMcpResponse(
                _sse_frame({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-2",
            ),
            _FakeMcpResponse(
                _sse_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"tools": [{"name": "health"}]},
                    }
                )
            ),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 0
        assert fake_conn.request.call_count == 3
        first_headers = fake_conn.request.call_args_list[0].args[3]
        retry_headers = fake_conn.request.call_args_list[1].args[3]
        list_headers = fake_conn.request.call_args_list[2].args[3]
        assert "Mcp-Session-Id" not in first_headers
        assert "Mcp-Session-Id" not in retry_headers
        assert list_headers["Mcp-Session-Id"] == "sid-2"

    def test_timeout_retries_read_only_handshake_once(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MCP_SMOKE_ATTEMPTS", "2")
        monkeypatch.setenv("MCP_SMOKE_RETRY_DELAY", "0")
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = [
            _FakeMcpResponse(
                _sse_frame({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-2",
            ),
            _FakeMcpResponse(
                _sse_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"tools": [{"name": "health"}]},
                    }
                )
            ),
        ]
        with patch.object(
            mcp_smoke.http.client,
            "HTTPConnection",
            side_effect=[TimeoutError("timed out"), fake_conn, fake_conn],
        ) as http_connection:
            assert mcp_smoke.main() == 0
        assert http_connection.call_count == 3

    def test_complete_partial_tools_result_is_not_retried(self, monkeypatch):
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("MCP_SMOKE_ATTEMPTS", "3")
        monkeypatch.setenv("MCP_SMOKE_RETRY_DELAY", "0")
        responses = [
            _FakeMcpResponse(
                _sse_frame({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-1",
            ),
            _FakeMcpResponse(_sse_frame({"jsonrpc": "2.0", "id": 2, "result": {}})),
        ]
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = responses
        with patch.object(mcp_smoke.http.client, "HTTPConnection", return_value=fake_conn):
            assert mcp_smoke.main() == 1
        assert fake_conn.getresponse.call_count == 2
