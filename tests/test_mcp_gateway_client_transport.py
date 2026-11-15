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


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    ("code", "headers", "expected"),
    [
        (
            "RATE_LIMIT_EXCEEDED",
            {"Retry-After": "7", "X-RateLimit-Bucket": "ip"},
            {"operation_class": "execute", "retry_after_seconds": 7, "bucket_class": "ip"},
        ),
        (
            "SESSION_LIMIT_EXCEEDED",
            {"Retry-After": "60", "X-RateLimit-Bucket": "session"},
            {"operation_class": "execute", "retry_after_seconds": 60, "bucket_class": "session"},
        ),
        (
            "RATE_LIMIT_EXCEEDED",
            {"Retry-After": "NaN", "X-RateLimit-Bucket": "secret-host token=unsafe"},
            {"operation_class": "execute"},
        ),
        (
            "RATE_LIMIT_EXCEEDED",
            {"Retry-After": "86401"},
            {"operation_class": "execute"},
        ),
        (
            "RATE_LIMIT_EXCEEDED",
            {"Retry-After": "0", "X-RateLimit-Bucket": "master"},
            {"operation_class": "execute", "retry_after_seconds": 1, "bucket_class": "master"},
        ),
    ],
)
def test_http_429_propagates_only_bounded_retry_and_trusted_bucket(
    method, code, headers, expected
):
    """GET/POST expose the same typed header hints without global reconnect cooldown."""
    from mcp_infra.gateway_errors import _gateway_error_details

    client = GatewayClient(base_url="https://gateway.invalid", api_key="test-key")
    body = {"code": code, "retryable": True, "details": {"operation_class": "execute"}}
    response = MagicMock()
    response.status_code = 429
    response.headers = headers
    response.text = "opaque"
    response.json.return_value = body
    before = GatewayClient._connect_retry_not_before

    with patch(
        "gateway_client.httpx.get" if method == "GET" else "gateway_client.httpx.post",
        return_value=response,
    ):
        with pytest.raises(GatewayClientError) as caught:
            if method == "GET":
                client._get("/api/status")
            else:
                client._post("/api/status", {"key": "value"})

    error = caught.value
    assert error.status_code == 429
    assert error.body is not body
    assert error.body["code"] == code
    assert error.body["details"] == expected
    projected = _gateway_error_details(error)
    assert projected is not None
    assert ("retry_after_seconds" in projected) == ("retry_after_seconds" in expected)
    if "retry_after_seconds" in expected:
        assert projected["retry_after_seconds"] == expected["retry_after_seconds"]
    assert GatewayClient._connect_retry_not_before == before
    assert "secret-host" not in str(error.body)
    assert "token=unsafe" not in str(error.body)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_http_429_nonjson_body_uses_bounded_retry_metadata_without_global_cooldown(method):
    from mcp_infra.gateway_errors import _gateway_error_details

    client = GatewayClient(base_url="https://gateway.invalid", api_key="test-key")
    response = MagicMock()
    response.status_code = 429
    response.headers = {"Retry-After": "12", "X-RateLimit-Bucket": "ip"}
    response.text = "upstream proxy invalid response"
    response.json.side_effect = ValueError("not JSON")
    before = GatewayClient._connect_retry_not_before
    with patch(
        "gateway_client.httpx.get" if method == "GET" else "gateway_client.httpx.post",
        return_value=response,
    ):
        with pytest.raises(GatewayClientError) as caught:
            if method == "GET":
                client._get("/api/status")
            else:
                client._post("/api/status", {})
    assert caught.value.body == {
        "code": "RATE_LIMIT_EXCEEDED",
        "message": "Gateway rate limit response",
        "retryable": True,
        "details": {"retry_after_seconds": 12, "bucket_class": "ip"},
    }
    assert _gateway_error_details(caught.value)["retry_after_seconds"] == 12
    assert GatewayClient._connect_retry_not_before == before


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
