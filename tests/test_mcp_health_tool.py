"""Tests for the MCP aggregated health tool."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# server.py needs sys.path set up for its internal imports
EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))


@pytest.fixture(autouse=True)
def _mcp_started():
    """Ensure _mcp_started_at is set on the server module."""
    import examples.mcp_server.server as srv

    if not hasattr(srv, "_mcp_started_at"):
        import time

        srv._mcp_started_at = time.time()
    yield


def test_health_tool_returns_mcp_and_gateway_keys():
    """The health tool must return a dict with 'mcp' and 'gateway' keys."""
    from examples.mcp_server.server import gateway_health

    mock_client = MagicMock()
    mock_client.health.return_value = {
        "status": "ok",
        "build_sha": "gw_sha",
        "build_time": "gw_time",
        "started_at": "gw_started",
        "version": "0.1.30",
    }

    import examples.mcp_server.server as srv
    original_client = srv.client
    srv.client = mock_client
    try:
        result = gateway_health()
    finally:
        srv.client = original_client

    assert "mcp" in result
    assert "gateway" in result
    gw = result["gateway"]
    assert gw["build_sha"] == "gw_sha"
    assert gw["version"] == "0.1.30"


def test_mcp_section_has_toolset_hash():
    """MCP section must include toolset_hash, tools_count, contract_version."""
    from examples.mcp_server.server import gateway_health

    mock_client = MagicMock()
    mock_client.health.return_value = {"status": "ok"}

    import examples.mcp_server.server as srv
    original_client = srv.client
    srv.client = mock_client
    try:
        result = gateway_health()
    finally:
        srv.client = original_client

    mcp = result["mcp"]
    assert "toolset_hash" in mcp
    assert mcp["toolset_hash"].startswith("sha256:")
    assert "tools_count" in mcp
    assert isinstance(mcp["tools_count"], int)
    assert mcp["contract_version"] == "1"


def test_gateway_dns_failure_is_bounded_and_preserves_mcp_truth():
    """A failed gateway transport must not collapse the aggregate health tool."""
    from examples.mcp_server.server import GatewayClientError, gateway_health

    mock_client = MagicMock()
    mock_client.health.side_effect = GatewayClientError(
        "Gateway transport unavailable",
        body={
            "message": "Gateway transport unavailable",
            "code": "REMOTE_UNAVAILABLE",
            "retryable": True,
        },
    )

    import examples.mcp_server.server as srv
    original_client = srv.client
    srv.client = mock_client
    try:
        result = gateway_health()
    finally:
        srv.client = original_client

    assert result["mcp"]["toolset_hash"].startswith("sha256:")
    assert isinstance(result["mcp"]["tools_count"], int)
    assert result["mcp"]["contract_version"] == "1"
    assert result["gateway"]["status"] == "unreachable"
    assert result["gateway"]["ready"] is False
    assert result["gateway"]["error"] == {
        "code": "REMOTE_UNAVAILABLE",
        "message": "Gateway health endpoint is unreachable",
        "retryable": True,
    }


def test_gateway_health_timeout_is_bounded_but_distinct():
    from examples.mcp_server.server import GatewayClientError, gateway_health

    mock_client = MagicMock()
    mock_client.health.side_effect = GatewayClientError(
        "Gateway request timed out",
        body={
            "message": "Gateway request timed out",
            "code": "TIMEOUT",
            "retryable": True,
        },
    )

    import examples.mcp_server.server as srv
    original_client = srv.client
    srv.client = mock_client
    try:
        result = gateway_health()
    finally:
        srv.client = original_client

    assert result["gateway"]["status"] == "unreachable"
    assert result["gateway"]["ready"] is False
    assert result["gateway"]["error"]["code"] == "TIMEOUT"
    assert result["gateway"]["error"]["message"] == "Gateway health check timed out"
    assert result["gateway"]["error"]["retryable"] is True


def test_gateway_http_error_is_bounded_as_degraded_without_echoing_raw_body():
    from examples.mcp_server.server import GatewayClientError, gateway_health

    mock_client = MagicMock()
    mock_client.health.side_effect = GatewayClientError(
        "GET /health failed: 503 secret-host.internal token=do-not-leak",
        status_code=503,
        body=None,
    )

    import examples.mcp_server.server as srv
    original_client = srv.client
    srv.client = mock_client
    try:
        result = gateway_health()
    finally:
        srv.client = original_client

    assert result["gateway"]["status"] == "degraded"
    assert result["gateway"]["ready"] is False
    assert result["gateway"]["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "Gateway health check failed",
        "retryable": True,
    }
    assert "secret-host.internal" not in str(result)
    assert "do-not-leak" not in str(result)


def test_gateway_health_does_not_swallow_programming_errors():
    """Only expected GatewayClientError failures are bounded."""
    from examples.mcp_server.server import gateway_health

    mock_client = MagicMock()
    mock_client.health.side_effect = RuntimeError("programmer bug")

    import examples.mcp_server.server as srv
    original_client = srv.client
    srv.client = mock_client
    try:
        with pytest.raises(RuntimeError, match="programmer bug"):
            gateway_health()
    finally:
        srv.client = original_client
