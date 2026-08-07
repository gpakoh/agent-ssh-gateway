"""Tests for examples.mcp_server.server._gateway_error_message/_gateway_error_hint.

Regression coverage for a real bug found via a live GPT self-test report:
job_status (and every other tool going through run_tool's GatewayClientError
branch) put str(exc) straight into tool_error()'s message -- and str(exc)
is "GET {path} failed: {status} {response.text}" (see GatewayClient._get),
i.e. the raw HTTP method+path plus the *entire serialized JSON response
body* crammed into one string. tool_error()'s own redaction then mangles
the leading "GET {path}" into "[API]", producing something like
'[API] failed: 404 {"detail": {"code": "JOB_NOT_FOUND", "message": "Job
xyz not found", ...}}' instead of a clean "Job xyz not found". The gateway
already computes a clean message/hint inside the structured body; this
just wasn't being used.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(EXAMPLES_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _set_auth_mode():
    with patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"}, clear=False):
        yield


def _make_job_not_found_exc():
    from examples.mcp_server.gateway_client import GatewayClientError

    raw_text = '{"detail": {"message": "Job xyz not found", "code": "JOB_NOT_FOUND", "retryable": false, "hint": "Use GET /api/jobs to list active jobs", "http_status": 404}}'
    return GatewayClientError(
        f"GET /api/jobs/xyz/status failed: 404 {raw_text}",
        status_code=404,
        body={
            "detail": {
                "message": "Job xyz not found",
                "code": "JOB_NOT_FOUND",
                "retryable": False,
                "hint": "Use GET /api/jobs to list active jobs",
                "http_status": 404,
            }
        },
    )


def test_gateway_error_message_prefers_structured_detail_message():
    from examples.mcp_server.server import _gateway_error_message

    exc = _make_job_not_found_exc()
    assert _gateway_error_message(exc) == "Job xyz not found"
    # Regression: the old str(exc)-based message embedded the raw JSON body.
    assert "detail" not in _gateway_error_message(exc)
    assert "GET /api/jobs" not in _gateway_error_message(exc)


def test_gateway_error_message_handles_flat_body():
    """SSHManagerError's handler returns a flat {message, code, ...} body
    with no "detail" wrapper -- must still extract the clean message."""
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.server import _gateway_error_message

    exc = GatewayClientError(
        "POST /api/ssh/execute failed: 404 {...}",
        status_code=404,
        body={"message": "Session not found", "code": "SESSION_NOT_FOUND", "retryable": False},
    )
    assert _gateway_error_message(exc) == "Session not found"


def test_gateway_error_message_falls_back_to_str_when_no_body():
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.server import _gateway_error_message

    exc = GatewayClientError("GET /health failed: 502 Bad Gateway", status_code=502, body=None)
    assert _gateway_error_message(exc) == "GET /health failed: 502 Bad Gateway"


def test_gateway_error_hint_prefers_structured_detail_hint():
    from examples.mcp_server.server import _gateway_error_hint

    exc = _make_job_not_found_exc()
    assert _gateway_error_hint(exc, "JOB_NOT_FOUND") == "Use GET /api/jobs to list active jobs"


def test_gateway_error_hint_falls_back_for_file_not_found_with_no_body_hint():
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.server import _gateway_error_hint

    exc = GatewayClientError("cannot read file: nope", status_code=404, body=None)
    assert (
        _gateway_error_hint(exc, "FILE_NOT_FOUND")
        == "The requested file does not exist at the specified path"
    )


def test_gateway_error_hint_none_when_nothing_available():
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.server import _gateway_error_hint

    exc = GatewayClientError("boom", status_code=500, body=None)
    assert _gateway_error_hint(exc, "INTERNAL_ERROR") is None


class TestJobStatusEndToEnd:
    """Feeds a realistic GatewayClientError through the real run_tool()
    path (via gateway_job_status) to prove the fix reaches an actual tool,
    not just the two helper functions in isolation.
    """

    def test_job_status_not_found_produces_clean_contract_v1_error(self, monkeypatch):
        from examples.mcp_server import server as mcp_server_mod

        # server.py's own except-clause does `isinstance(exc, GatewayClientError)`
        # against its own bare top-level import (`from gateway_client import
        # GatewayClientError`, since examples/mcp_server is on sys.path) --
        # constructing the exception via the `examples.mcp_server.gateway_client`
        # dotted path instead loads a second, distinct class object with the
        # same name, and the isinstance check would silently fail. Use the
        # class object server.py itself imported, to match its real identity.
        def _raise(job_id):
            raise mcp_server_mod.GatewayClientError(
                'GET /api/jobs/xyz/status failed: 404 {"detail": {"message": "Job xyz not found", "code": "JOB_NOT_FOUND", "retryable": false, "hint": "Use GET /api/jobs to list active jobs", "http_status": 404}}',
                status_code=404,
                body={
                    "detail": {
                        "message": "Job xyz not found",
                        "code": "JOB_NOT_FOUND",
                        "retryable": False,
                        "hint": "Use GET /api/jobs to list active jobs",
                        "http_status": 404,
                    }
                },
            )

        monkeypatch.setattr(mcp_server_mod.client, "job_status", _raise)

        result = mcp_server_mod.gateway_job_status("xyz")
        assert result["ok"] is False
        assert result["error"]["code"] == "JOB_NOT_FOUND"
        assert result["error"]["message"] == "Job xyz not found"
        assert result["error"]["hint"] == "Use GET /api/jobs to list active jobs"
        assert result["error"]["retryable"] is False
        # Regression: no transport garbage (raw JSON blob, [API] placeholder).
        assert "detail" not in result["error"]["message"]
        assert "[API]" not in result["error"]["message"]


class TestRunTestsWaitTimeoutEndToEnd:
    """A long test suite (run_tests -> _run_uv_tool -> wait_job) that
    outlives the wait window used to surface neither a result nor a job_id
    -- wait_job() silently returned the gateway's {"wait_timed_out": True}
    dict as if it were a finished job, which _run_uv_tool read as
    exit_code=-1 and reported as a generic, misleading failure, with no
    reliable way for the caller to find the job_id and check on it later.
    Feeds a realistic timeout through the real run_tool()/gateway_run_tests
    path (not just the two helper functions in isolation).
    """

    def test_run_tests_timeout_surfaces_job_id_and_wait_timeout_code(self, monkeypatch):
        from pathlib import Path

        import mcp_client_tools

        from examples.mcp_server import server as mcp_server_mod

        monkeypatch.setattr(mcp_client_tools, "_resolve_project", lambda _: Path("/project"))

        calls = {"n": 0}

        def _execute_raw(cmd):
            calls["n"] += 1
            return {"job_id": f"j{calls['n']}"}

        def _wait_job(job_id, **kw):
            if job_id == "j1":  # the "command -v uv" check
                return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
            # the real pytest run outlives the wait window
            raise mcp_server_mod.GatewayClientError(
                f"Job {job_id} did not finish before timeout",
                body={"job_id": job_id, "status": "running", "wait_timed_out": True},
            )

        monkeypatch.setattr(mcp_server_mod.client, "execute_raw", _execute_raw)
        monkeypatch.setattr(mcp_server_mod.client, "wait_job", _wait_job)

        result = mcp_server_mod.gateway_run_tests("proj")

        assert result["ok"] is False
        assert result["error"]["code"] == "WAIT_TIMEOUT"
        assert result["error"]["retryable"] is True
        assert result["error"]["details"]["job_id"] == "j2"
        assert "job_status" in result["error"]["hint"]
