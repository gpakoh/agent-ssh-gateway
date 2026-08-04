"""Tests for examples.mcp_server.server._classify_gateway_error.

Regression coverage for a real bug reported by a live connected client:
a 401 from the gateway (retryable=false in the gateway's own structured
error body) was being reclassified as INTERNAL_ERROR/retryable=true by
the fallback branch at the end of _classify_gateway_error, since 401
had no explicit case and the catch-all defaulted to retryable=True.
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


def _classify(status_code, body, message="error"):
    from examples.mcp_server.gateway_client import GatewayClientError
    from examples.mcp_server.server import _classify_gateway_error

    exc = GatewayClientError(message, status_code=status_code, body=body)
    return _classify_gateway_error(exc)


def test_401_with_gateway_body_honors_retryable_false():
    """The exact bug reported live: gateway says retryable=false on a
    401, the tool layer must not override it to true.
    """
    code, retryable = _classify(
        401,
        {"detail": {"message": "Invalid or missing API key.", "code": "INVALID_API_KEY", "retryable": False}},
    )
    assert code == "AUTH_ERROR"
    assert retryable is False


def test_master_key_required_maps_to_auth_error_not_retryable():
    code, retryable = _classify(
        401,
        {"detail": {"message": "Master API key required", "code": "MASTER_KEY_REQUIRED", "retryable": False}},
    )
    assert code == "AUTH_ERROR"
    assert retryable is False


def test_forbidden_maps_to_permission_denied_not_retryable():
    code, retryable = _classify(
        403,
        {"detail": {"message": "Command denied by policy", "code": "FORBIDDEN", "retryable": False}},
    )
    assert code == "PERMISSION_DENIED"
    assert retryable is False


def test_session_not_found_keeps_its_own_code():
    code, retryable = _classify(
        404,
        {"detail": {"message": "Session not found", "code": "SESSION_NOT_FOUND", "retryable": False}},
    )
    assert code == "SESSION_NOT_FOUND"
    assert retryable is False


def test_session_not_found_flat_body_not_reclassified_as_file_not_found():
    """Regression: SSHManagerError's handler (session/connection/exec errors)
    returns a flat {message, code, retryable, hint, http_status} body with
    no "detail" wrapper — unlike most other endpoints. Before this fix, a
    404 with no "detail" key fell through to the blunt status-code
    heuristics and every bare 404 was reclassified as FILE_NOT_FOUND,
    turning "session not found" into a misleading "file not found".
    """
    code, retryable = _classify(
        404,
        {
            "message": "SSH operation failed",
            "code": "SESSION_NOT_FOUND",
            "retryable": False,
            "hint": "Create a session first via POST /api/ssh/connect",
            "http_status": 404,
        },
    )
    assert code == "SESSION_NOT_FOUND"
    assert retryable is False


def test_unmapped_gateway_code_still_honors_body_retryable_true():
    """A gateway code this MCP layer doesn't have a specific mapping for
    must still respect the gateway's own retryable flag rather than
    silently forcing True.
    """
    code, retryable = _classify(
        503,
        {"detail": {"message": "Service busy", "code": "SOME_FUTURE_CODE", "retryable": True}},
    )
    assert code == "INTERNAL_ERROR"
    assert retryable is True


def test_unmapped_gateway_code_with_retryable_false_is_not_forced_true():
    code, retryable = _classify(
        503,
        {"detail": {"message": "Maintenance", "code": "SOME_FUTURE_CODE", "retryable": False}},
    )
    assert code == "INTERNAL_ERROR"
    assert retryable is False


def test_no_body_falls_back_to_status_heuristics_401():
    """When the gateway body is missing entirely (e.g. a non-JSON error
    page from an intermediate proxy), fall back to the status-code
    heuristic — 401 must still be non-retryable, not the old
    catch-all True.
    """
    code, retryable = _classify(401, None)
    assert code == "AUTH_ERROR"
    assert retryable is False


def test_no_body_falls_back_to_status_heuristics_403():
    code, retryable = _classify(403, None)
    assert code == "PERMISSION_DENIED"
    assert retryable is False


def test_no_body_5xx_still_retryable():
    code, retryable = _classify(500, None)
    assert code == "INTERNAL_ERROR"
    assert retryable is True


def test_file_not_found_still_works_with_body_present():
    code, retryable = _classify(
        404,
        {"detail": {"message": "file not found", "code": "FILE_NOT_FOUND", "retryable": False}},
        message="cannot read file: file not found",
    )
    assert code == "FILE_NOT_FOUND"
    assert retryable is False


def test_workspace_readonly_maps_to_permission_denied_not_internal_error():
    """Regression: reachable on every write-tool call against the gateway's
    own default (WORKSPACE_READONLY=true) — not an edge case. This code
    goes through the normal HTTPException(detail=...) path (the nested
    shape works fine here), but _GATEWAY_ERROR_CODE_MAP simply had no entry
    for WORKSPACE_READONLY at all, so it fell through to INTERNAL_ERROR —
    an agent attempting a write got no signal that the fix is "don't write,
    or ask an operator to disable read-only mode", just a generic failure.
    """
    code, retryable = _classify(
        403,
        {
            "detail": {
                "message": "WORKSPACE_READONLY: write operations are disabled",
                "code": "WORKSPACE_READONLY",
                "retryable": False,
            }
        },
    )
    assert code == "PERMISSION_DENIED"
    assert retryable is False


def test_malformed_body_falls_back_to_status_heuristics():
    """body present but not the expected {"detail": {...}} shape must
    not crash, and must fall back to status-code heuristics.
    """
    code, retryable = _classify(401, {"unexpected": "shape"})
    assert code == "AUTH_ERROR"
    assert retryable is False


class TestRealGatewayResponseShapeSeam:
    """End-to-end across the actual seam, not a re-description of it.

    Regression: SSHManagerError's handler (session/connection/exec errors)
    returns a flat {message, code, retryable, hint, http_status} body with
    no "detail" wrapper — unlike most other gateway endpoints, which raise
    HTTPException(detail={...}) and get FastAPI's automatic {"detail": {...}}
    wrapping. Both sides were individually well-tested and correct in
    isolation: app/main.py's ssh_exception_handler tests asserted the flat
    shape, and this file's own tests asserted the nested shape — nobody fed
    one real side's actual output into the other real side's input. A bare
    404 with no "detail" key fell through to a blunt status-code heuristic
    that mapped every unrecognized 404 to FILE_NOT_FOUND, turning "session
    not found" into a misleading "file not found".

    These tests call the *real* app.main.ssh_exception_handler to produce a
    real HTTP response body, then feed that real body into the *real*
    examples.mcp_server.server._classify_gateway_error — no hand-written
    stand-in for either side's shape.
    """

    @staticmethod
    async def _real_gateway_body(exc) -> tuple[int, dict]:
        import json as _json

        import app.main as main_module

        resp = await main_module.ssh_exception_handler(None, exc)
        return resp.status_code, _json.loads(resp.body)

    @pytest.mark.asyncio
    async def test_session_not_found_survives_the_real_round_trip(self):
        from app.ssh_manager import SessionNotFoundError
        from examples.mcp_server.gateway_client import GatewayClientError
        from examples.mcp_server.server import _classify_gateway_error

        status, body = await self._real_gateway_body(SessionNotFoundError("Session x not found"))
        assert status == 404  # sanity: still the real handler's own status map

        exc = GatewayClientError("boom", status_code=status, body=body)
        code, retryable = _classify_gateway_error(exc)

        assert code == "SESSION_NOT_FOUND"
        assert retryable is False

    @pytest.mark.asyncio
    async def test_auth_error_survives_the_real_round_trip(self):
        from app.ssh_manager import AuthenticationError
        from examples.mcp_server.gateway_client import GatewayClientError
        from examples.mcp_server.server import _classify_gateway_error

        status, body = await self._real_gateway_body(AuthenticationError("bad credentials"))
        assert status == 401

        exc = GatewayClientError("boom", status_code=status, body=body)
        code, retryable = _classify_gateway_error(exc)

        assert code == "AUTH_ERROR"
        assert retryable is False

    @pytest.mark.asyncio
    async def test_session_limit_survives_the_real_round_trip(self):
        """Same systematic audit, found by running every SSHManagerError
        subtype through both real sides: SessionLimitError's gateway code
        (RATE_LIMIT_EXCEEDED) had no entry in _GATEWAY_ERROR_CODE_MAP either
        — this map already had "RATE_LIMITED" for the MCP vocabulary, just
        never wired to the gateway's actual name for it.
        """
        from app.ssh_manager import SessionLimitError
        from examples.mcp_server.gateway_client import GatewayClientError
        from examples.mcp_server.server import _classify_gateway_error

        status, body = await self._real_gateway_body(SessionLimitError("too many sessions"))
        assert status == 429

        exc = GatewayClientError("boom", status_code=status, body=body)
        code, retryable = _classify_gateway_error(exc)

        assert code == "RATE_LIMITED"
        assert retryable is True

    @pytest.mark.asyncio
    async def test_timeout_survives_the_real_round_trip(self):
        """TimeoutError's gateway code is GATEWAY_TIMEOUT, not the bare
        "TIMEOUT" this map already expected — a naming mismatch, not a
        missing entry.
        """
        from app.ssh_manager import TimeoutError as SSHTimeoutError
        from examples.mcp_server.gateway_client import GatewayClientError
        from examples.mcp_server.server import _classify_gateway_error

        status, body = await self._real_gateway_body(SSHTimeoutError("timed out"))
        assert status == 504

        exc = GatewayClientError("boom", status_code=status, body=body)
        code, retryable = _classify_gateway_error(exc)

        assert code == "TIMEOUT"
        assert retryable is True
