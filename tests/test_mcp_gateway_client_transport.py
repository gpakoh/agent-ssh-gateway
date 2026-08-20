"""Transport-boundary regressions for the MCP GatewayClient."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
for _path in (str(MCP_SERVER_DIR), str(EXAMPLES_DIR.parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from gateway_client import GatewayClient, GatewayClientError  # noqa: E402


def _connect_error() -> httpx.ConnectError:
    request = httpx.Request("GET", "https://secret-gateway.invalid/health?token=do-not-leak")
    return httpx.ConnectError(
        "[Errno -2] Name or service not known: secret-gateway.invalid",
        request=request,
    )


def _timeout_error() -> httpx.ReadTimeout:
    request = httpx.Request("GET", "https://secret-gateway.invalid/health?token=do-not-leak")
    return httpx.ReadTimeout("read timed out at secret-gateway.invalid", request=request)


def _assert_safe_transport_error(exc: GatewayClientError, *, code: str, message: str) -> None:
    assert exc.status_code is None
    assert exc.body == {
        "message": message,
        "code": code,
        "retryable": True,
    }
    rendered = f"{exc} {exc.body}"
    assert "secret-gateway.invalid" not in rendered
    assert "do-not-leak" not in rendered


def test_health_normalizes_connect_error_without_leaking_request_details():
    client = GatewayClient(base_url="https://secret-gateway.invalid", api_key="test-key")

    with patch("gateway_client.httpx.get", side_effect=_connect_error()):
        with pytest.raises(GatewayClientError) as caught:
            client.health()

    _assert_safe_transport_error(
        caught.value,
        code="REMOTE_UNAVAILABLE",
        message="Gateway transport unavailable",
    )


def test_get_timeout_remains_distinct_from_remote_unavailable():
    client = GatewayClient(base_url="https://secret-gateway.invalid", api_key="test-key")

    with patch("gateway_client.httpx.get", side_effect=_timeout_error()):
        with pytest.raises(GatewayClientError) as caught:
            client.health()

    _assert_safe_transport_error(
        caught.value,
        code="TIMEOUT",
        message="Gateway request timed out",
    )


def test_post_normalizes_connect_error_at_same_boundary():
    client = GatewayClient(base_url="https://secret-gateway.invalid", api_key="test-key")

    with patch("gateway_client.httpx.post", side_effect=_connect_error()):
        with pytest.raises(GatewayClientError) as caught:
            client._post("/api/example", {"value": 1})

    _assert_safe_transport_error(
        caught.value,
        code="REMOTE_UNAVAILABLE",
        message="Gateway transport unavailable",
    )


def test_auto_reconnect_normalizes_connect_error_at_same_boundary():
    client = GatewayClient(
        base_url="https://secret-gateway.invalid",
        api_key="test-key",
        ssh_host="ssh.internal",
        ssh_user="agent",
    )

    with patch("gateway_client.httpx.post", side_effect=_connect_error()):
        with pytest.raises(GatewayClientError) as caught:
            client.connect()

    _assert_safe_transport_error(
        caught.value,
        code="REMOTE_UNAVAILABLE",
        message="Gateway transport unavailable",
    )


def test_real_gateway_client_transport_failure_is_bounded_by_aggregate_health():
    import examples.mcp_server.server as srv

    client = GatewayClient(base_url="https://secret-gateway.invalid", api_key="test-key")
    original_client = srv.client
    srv.client = client
    try:
        with patch("gateway_client.httpx.get", side_effect=_connect_error()):
            result = srv.gateway_health()
    finally:
        srv.client = original_client

    assert result["mcp"]["toolset_hash"].startswith("sha256:")
    assert result["gateway"]["status"] == "unreachable"
    assert result["gateway"]["ready"] is False
    assert result["gateway"]["error"]["code"] == "REMOTE_UNAVAILABLE"
    assert "secret-gateway.invalid" not in str(result)
    assert "do-not-leak" not in str(result)


def test_self_test_contains_transport_failure_and_continues_diagnostics():
    from self_test import run_self_test

    client = GatewayClient(base_url="https://secret-gateway.invalid", api_key="test-key")
    with patch("gateway_client.httpx.get", side_effect=_connect_error()):
        result = run_self_test(client)

    health = next(check for check in result["checks"] if check["name"] == "health")
    assert health["status"] == "fail"
    assert health["detail"] == "Gateway transport unavailable"
    assert any(check["name"] == "command_policy_allows_safe" for check in result["checks"])
    assert "secret-gateway.invalid" not in str(result)
    assert "do-not-leak" not in str(result)


def test_http_status_error_contract_is_unchanged():
    client = GatewayClient(base_url="https://gateway.invalid", api_key="test-key")
    body = {
        "detail": {
            "message": "Service busy",
            "code": "SOME_FUTURE_CODE",
            "retryable": True,
        }
    }
    response = MagicMock()
    response.status_code = 503
    response.text = "service busy"
    response.json.return_value = body

    with patch("gateway_client.httpx.get", return_value=response):
        with pytest.raises(GatewayClientError) as caught:
            client.health()

    assert caught.value.status_code == 503
    assert caught.value.body == body
