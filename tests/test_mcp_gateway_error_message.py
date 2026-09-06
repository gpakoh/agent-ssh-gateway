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
    # Regression: the gateway's REST hint ("Use GET /api/jobs...") must NOT
    # leak into the MCP surface — there is no such MCP command. JOB_NOT_FOUND
    # gets an MCP-native hint instead.
    hint = _gateway_error_hint(exc, "JOB_NOT_FOUND")
    assert "GET /api/jobs" not in hint
    assert "job_status" in hint


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


def test_gateway_error_details_preserves_validation_errors():
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.mcp_infra.gateway_errors import _gateway_error_details

    exc = GatewayClientError(
        "POST /api/ssh/execute failed: 422 {...}",
        status_code=422,
        body={
            "message": "Request validation failed",
            "code": "VALIDATION_ERROR",
            "retryable": False,
            "errors": [{"field": "session_id", "error": "required", "type": "missing"}],
            "total_errors": 1,
        },
    )

    assert _gateway_error_details(exc) == {
        "errors": [{"field": "session_id", "error": "required", "type": "missing"}],
        "total_errors": 1,
    }


def test_gateway_error_details_preserves_rate_limit_retry_metadata():
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.mcp_infra.gateway_errors import _gateway_error_details

    exc = GatewayClientError(
        "POST /api/ssh/execute failed: 429 {...}",
        status_code=429,
        body={
            "detail": {
                "message": "Rate limit exceeded: 180 per 1 minute",
                "code": "RATE_LIMIT_EXCEEDED",
                "retryable": True,
                "details": {
                    "retry_after_seconds": 42,
                    "bucket_class": "master",
                    "operation_class": "execute",
                    "limit": "180 per 1 minute",
                },
            }
        },
    )

    assert _gateway_error_details(exc) == {
        "gateway_code": "RATE_LIMIT_EXCEEDED",
        "retry_after_seconds": 42,
        "bucket_class": "master",
        "operation_class": "execute",
        "limit": "180 per 1 minute",
    }


def test_gateway_transport_errors_get_recovery_hints():
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.mcp_infra.gateway_errors import (
        _classify_gateway_error,
        _gateway_error_hint,
    )

    exc = GatewayClientError(
        "Gateway transport unavailable",
        body={"message": "Gateway transport unavailable", "code": "REMOTE_UNAVAILABLE", "retryable": True},
    )

    code, retryable = _classify_gateway_error(exc)

    assert code == "REMOTE_UNAVAILABLE"
    assert retryable is True
    assert _gateway_error_hint(exc, code)


def test_gateway_timeout_errors_get_recovery_hints():
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.mcp_infra.gateway_errors import (
        _classify_gateway_error,
        _gateway_error_hint,
    )

    exc = GatewayClientError(
        "Gateway request timed out",
        body={"message": "Gateway request timed out", "code": "TIMEOUT", "retryable": True},
    )

    code, retryable = _classify_gateway_error(exc)

    assert code == "TIMEOUT"
    assert retryable is True
    assert _gateway_error_hint(exc, code)


def test_gateway_error_details_preserves_nested_job_status():
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.mcp_infra.gateway_errors import _gateway_error_details

    exc = GatewayClientError(
        "timeout",
        body={
            "detail": {
                "code": "TIMEOUT",
                "retryable": True,
                "details": {"attempt": 2},
                "job_id": "job-1",
                "status": "running",
                "wait_timed_out": True,
            }
        },
    )

    assert _gateway_error_details(exc) == {
        "attempt": 2,
        "job_id": "job-1",
        "status": "running",
        "wait_timed_out": True,
    }


@pytest.mark.asyncio
async def test_job_cancel_protocol_preserves_gateway_error(monkeypatch):
    from gateway_client import GatewayClientError

    from examples.mcp_server.mcp_infra.adapters import gateway as gateway_adapter

    class Client:
        def cancel_job(self, job_id: str) -> dict[str, str]:
            raise GatewayClientError(
                "POST /api/jobs/job-1/cancel failed: 404 {...}",
                status_code=404,
                body={
                    "detail": {
                        "code": "JOB_NOT_FOUND",
                        "message": "Job job-1 not found",
                        "retryable": False,
                    }
                },
            )

    monkeypatch.setattr(gateway_adapter, "_server_client", lambda: Client())

    result = await gateway_adapter.gateway_job_cancel_protocol("job-1")

    assert result["ok"] is False
    assert result["error"]["message"] == "Job job-1 not found"
    assert "POST /api" not in result["error"]["message"]


@pytest.mark.asyncio
async def test_job_cancel_protocol_returns_cancel_status(monkeypatch):
    from examples.mcp_server.mcp_infra.adapters import gateway as gateway_adapter

    class Client:
        def cancel_job(self, job_id: str) -> dict[str, str]:
            return {"status": "cancelling", "job_id": job_id}

    async def reconcile(job_id, data):
        return data

    monkeypatch.setattr(gateway_adapter, "_server_client", lambda: Client())
    monkeypatch.setattr(gateway_adapter, "_reconcile_fleet_result", reconcile)

    result = await gateway_adapter.gateway_job_cancel_protocol("job-1")

    assert result["ok"] is True
    assert result["result"] == {"status": "cancelling", "job_id": "job-1"}


@pytest.mark.asyncio
async def test_job_cancel_protocol_preserves_not_cancellable_conflict(monkeypatch):
    from gateway_client import GatewayClientError

    from examples.mcp_server.mcp_infra.adapters import gateway as gateway_adapter

    class Client:
        def cancel_job(self, job_id: str) -> dict[str, str]:
            raise GatewayClientError(
                "POST /api/jobs/job-1/cancel failed: 409 {...}",
                status_code=409,
                body={
                    "detail": {
                        "code": "JOB_NOT_CANCELLABLE",
                        "message": "Cannot cancel job with status: ambiguous",
                        "retryable": False,
                    }
                },
            )

    monkeypatch.setattr(gateway_adapter, "_server_client", lambda: Client())

    result = await gateway_adapter.gateway_job_cancel_protocol("job-1")

    assert result["ok"] is False
    assert result["error"]["code"] == "JOB_NOT_CANCELLABLE"
    assert result["error"]["message"] == "Cannot cancel job with status: ambiguous"


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
        # Regression: MCP hint must not leak the REST endpoint (no such MCP
        # command exists).
        assert result["error"]["hint"] != "Use GET /api/jobs to list active jobs"
        assert "job_status" in result["error"]["hint"]
        assert result["error"]["retryable"] is False
        # Regression: no transport garbage (raw JSON blob, [API] placeholder).
        assert "detail" not in result["error"]["message"]
        assert "[API]" not in result["error"]["message"]


class TestRunTestsAsyncSubmit:
    """run_tests must submit the pytest job and return its job_id
    immediately (async) instead of synchronously waiting out the full
    suite (audit finding: run_tests unusable for a full suite — it
    blocks and times out with neither result nor job_id). The caller
    then polls with gateway_job_status / gateway_job_result. The
    targeted run_pytest path keeps its synchronous wait; run_tests is
    the full-suite tool.
    """

    def test_run_tests_returns_job_id_immediately(self, monkeypatch):
        from pathlib import Path

        import mcp_client_tools

        from examples.mcp_server import server as mcp_server_mod

        monkeypatch.setattr(mcp_client_tools, "_resolve_project", lambda _: Path("/project"))

        calls = {"n": 0}

        def _execute_raw(cmd, **kw):
            calls["n"] += 1
            return {"job_id": f"j{calls['n']}"}

        def _wait_job(job_id, **kw):
            # only the quick "command -v uv" probe and the venv usability
            # probe may be waited on; the pytest job must never be
            # synchronously waited on by run_tests
            if job_id == "j1":
                return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
            if job_id == "j2":
                return {"exit_code": 0, "stdout": "", "stderr": ""}
            raise AssertionError(f"run_tests must not wait_job on {job_id}")

        monkeypatch.setattr(mcp_server_mod.client, "execute_raw", _execute_raw)
        monkeypatch.setattr(mcp_server_mod.client, "wait_job", _wait_job)

        result = mcp_server_mod.gateway_run_tests("proj")

        assert result["ok"] is True
        assert result["result"]["job_id"] == "j3"
        assert result["result"]["status"] == "running"
        assert "job_status" in result["meta"]["warnings"][0]

    def test_run_pytest_keeps_sync_wait_timeout_surface(self, monkeypatch):
        """The targeted run_pytest path keeps waiting; a suite that
        outlives the window still surfaces a job_id-bearing WAIT_TIMEOUT
        error (regression guard for the previous fix)."""
        from pathlib import Path

        import mcp_client_tools

        from examples.mcp_server import server as mcp_server_mod

        monkeypatch.setattr(mcp_client_tools, "_resolve_project", lambda _: Path("/project"))

        calls = {"n": 0}

        def _execute_raw(cmd, **kw):
            calls["n"] += 1
            return {"job_id": f"j{calls['n']}"}

        def _wait_job(job_id, **kw):
            if job_id == "j1":
                return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
            raise mcp_server_mod.GatewayClientError(
                f"Job {job_id} did not finish before timeout",
                body={"job_id": job_id, "status": "running", "wait_timed_out": True},
            )

        monkeypatch.setattr(mcp_server_mod.client, "execute_raw", _execute_raw)
        monkeypatch.setattr(mcp_server_mod.client, "wait_job", _wait_job)

        result = mcp_server_mod.gateway_run_pytest("proj", "tests/test_x.py")

        assert result["ok"] is False
        assert result["error"]["code"] == "WAIT_TIMEOUT"
        assert result["error"]["retryable"] is True
        assert result["error"]["details"]["job_id"] == "j2"
        assert "job_status" in result["error"]["hint"]


class TestExecuteArgvGatewayErrorContract:
    def test_execute_argv_preserves_session_not_found_contract(self, monkeypatch):
        """Regression: gateway_execute_argv used to bypass run_tool's
        GatewayClientError classifier and returned TOOL_EXECUTION_FAILED with
        a redacted raw REST JSON blob instead of SESSION_NOT_FOUND.
        """
        from examples.mcp_server import server as mcp_server_mod

        def _raise(**_kwargs):
            raise mcp_server_mod.GatewayClientError(
                'POST /api/ssh/execute-argv failed: 404 {"message":"Session not found"}',
                status_code=404,
                body={
                    "message": "Session not found",
                    "code": "SESSION_NOT_FOUND",
                    "retryable": False,
                    "hint": "Create a session first via /api/ssh/connect",
                    "http_status": 404,
                },
            )

        monkeypatch.setattr(mcp_server_mod.client, "execute_argv", _raise)

        result = mcp_server_mod.gateway_execute_argv("dead-session", ["git", "status"])

        assert result["ok"] is False
        assert result["error"]["code"] == "SESSION_NOT_FOUND"
        assert result["error"]["message"] == "Session not found"
        assert result["error"]["retryable"] is False
        assert "TOOL_EXECUTION_FAILED" not in result["error"]["message"]
        assert "[API]" not in result["error"]["message"]
        assert "detail" not in result["error"]["message"]

    def test_execute_argv_preserves_wait_timeout_job_id(self, monkeypatch):
        from examples.mcp_server import server as mcp_server_mod

        def _raise(**_kwargs):
            raise mcp_server_mod.GatewayClientError(
                "Job j-timeout did not finish before timeout",
                body={"job_id": "j-timeout", "status": "running", "wait_timed_out": True},
            )

        monkeypatch.setattr(mcp_server_mod.client, "execute_argv", _raise)

        result = mcp_server_mod.gateway_execute_argv("sid", ["sleep", "60"], timeout_s=1)

        assert result["ok"] is False
        assert result["error"]["code"] == "WAIT_TIMEOUT"
        assert result["error"]["retryable"] is True
        assert result["error"]["details"]["job_id"] == "j-timeout"
        assert "job_status" in result["error"]["hint"]


class TestManualGatewayAdapterErrorPropagation:
    """Manual GatewayClientError catch blocks must preserve structured codes.

    These adapters cannot rely on run_tool() to classify GatewayClientError,
    so they must use the same shared helper instead of emitting generic
    TOOL_EXECUTION_FAILED with a redacted REST JSON blob.
    """

    def test_execute_argv_preserves_session_not_found(self, monkeypatch):
        from examples.mcp_server import server as mcp_server_mod

        def _raise(**kwargs):
            raise mcp_server_mod.GatewayClientError(
                'POST /api/ssh/execute failed: 404 {"message":"Session not found"}',
                status_code=404,
                body={
                    "message": "Session not found",
                    "code": "SESSION_NOT_FOUND",
                    "retryable": False,
                },
            )

        monkeypatch.setattr(mcp_server_mod.client, "execute_argv", _raise)

        result = mcp_server_mod.gateway_execute_argv("old-session", ["pwd"])

        assert result["ok"] is False
        assert result["error"]["code"] == "SESSION_NOT_FOUND"
        assert result["error"]["message"] == "Session not found"
        assert "[API]" not in result["error"]["message"]
        assert "POST /api" not in result["error"]["message"]

    def test_apply_patch_preserves_structured_gateway_error(self, monkeypatch):
        from examples.mcp_server import server as mcp_server_mod

        def _raise(**kwargs):
            raise mcp_server_mod.GatewayClientError(
                'POST /api/projects/apply-patch failed: 403 {"detail":{"message":"Workspace is read-only"}}',
                status_code=403,
                body={
                    "detail": {
                        "message": "Workspace is read-only",
                        "code": "WORKSPACE_READONLY",
                        "retryable": False,
                    }
                },
            )

        monkeypatch.setattr(mcp_server_mod.client, "apply_patch", _raise)

        result = mcp_server_mod.gateway_apply_patch(
            session_id="sid",
            project="proj",
            patch="diff --git a/a b/a\n",
            expected_hashes={},
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "PERMISSION_DENIED"
        assert result["error"]["message"] == "Workspace is read-only"
        assert "[API]" not in result["error"]["message"]
        assert "POST /api" not in result["error"]["message"]

    def test_job_wait_preserves_clean_wait_timeout_message_and_details(self, monkeypatch):
        from examples.mcp_server import server as mcp_server_mod

        def _raise(job_id, **kwargs):
            raise mcp_server_mod.GatewayClientError(
                f"GET /api/jobs/{job_id}/wait failed: 504 {{...}}",
                status_code=504,
                body={
                    "message": "Job j1 did not finish before timeout",
                    "code": "GATEWAY_TIMEOUT",
                    "retryable": True,
                    "job_id": "j1",
                    "wait_timed_out": True,
                },
            )

        monkeypatch.setattr(mcp_server_mod.client, "wait_job", _raise)

        result = mcp_server_mod.gateway_job_wait("j1", timeout_sec=1)

        assert result["ok"] is False
        assert result["error"]["code"] == "WAIT_TIMEOUT"
        assert result["error"]["message"] == "Job j1 did not finish before timeout"
        assert result["error"]["details"]["job_id"] == "j1"
        assert result["error"]["retryable"] is True
