"""429 responses must preserve safe retry hints without echoing proxy payloads (#885)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

MCP_SERVER_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
if str(MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER_DIR))

from gateway_client import GatewayClient, GatewayClientError  # noqa: E402

SECRET = "secret=PRIVATE-API-TOKEN-NEVER-ECHO"
SAFE_HEADERS = {
    "Retry-After": "9",
    "X-RateLimit-Bucket": "ip",
    "X-RateLimit-Bucket-ID": "bkt_0123456789abcdef01234567",
}


def _response(payload: dict[str, object] | None) -> httpx.Response:
    request = httpx.Request("GET", "https://gateway.invalid/health")
    if payload is None:
        return httpx.Response(
            429, text=f"upstream 429 {SECRET}", headers=SAFE_HEADERS, request=request
        )
    return httpx.Response(429, json=payload, headers=SAFE_HEADERS, request=request)


def _assert_no_proxy_leak(exc: GatewayClientError) -> None:
    assert exc.status_code == 429
    rendered = str(exc) + " " + repr(exc.body)
    assert SECRET not in rendered
    assert "PRIVATE-API-TOKEN" not in rendered
    assert "upstream 429" not in rendered
    assert len(rendered) < 1000
    assert exc.body is not None
    assert exc.body["code"] in {"RATE_LIMIT_EXCEEDED", "SESSION_LIMIT_EXCEEDED"}
    assert exc.body["details"]["retry_after_seconds"] == 9
    assert exc.body["details"]["bucket_class"] == "ip"


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {
            "code": "RATE_LIMIT_EXCEEDED",
            "retryable": True,
            "message": SECRET,
            "secret": SECRET,
            "details": {
                "operation_class": "execute",
                "bucket_id": "bkt_0123456789abcdef01234567",
                "token": SECRET,
            },
        },
        {
            "detail": {
                "code": "SESSION_LIMIT_EXCEEDED",
                "message": SECRET,
                "retryable": True,
                "details": {"operation_class": "connect", "internal_url": SECRET},
            }
        },
    ],
)
def test_sync_http_429_sanitizes_untrusted_json_and_raw_text(method, payload):
    client = GatewayClient(base_url="https://gateway.invalid", api_key="test")
    response = _response(payload)
    original_deadline = GatewayClient._connect_retry_not_before
    with patch(
        "gateway_client.httpx.get" if method == "GET" else "gateway_client.httpx.post",
        return_value=response,
    ) as sender:
        with pytest.raises(GatewayClientError) as caught:
            if method == "GET":
                client.health()
            else:
                client._post("/api/ssh/execute", {"value": "1"})
    sender.assert_called_once()
    _assert_no_proxy_leak(caught.value)
    if payload is not None and "detail" in payload:
        assert caught.value.body["code"] == "SESSION_LIMIT_EXCEEDED"
    if payload is not None and "details" in payload:
        assert caught.value.body["details"]["operation_class"] == "execute"
        assert caught.value.body["details"]["bucket_id"] == "bkt_0123456789abcdef01234567"
    assert GatewayClient._connect_retry_not_before == original_deadline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {
            "code": "RATE_LIMIT_EXCEEDED",
            "message": SECRET,
            "details": {"token": SECRET, "operation_class": "request"},
        },
    ],
)
async def test_async_post_429_has_same_safe_typed_metadata_without_body_echo(
    monkeypatch, payload
):
    response = _response(payload)
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            return response

    monkeypatch.setattr("gateway_client.httpx.AsyncClient", lambda **_: FakeClient())
    client = GatewayClient(base_url="https://gateway.invalid", api_key="test")
    with pytest.raises(GatewayClientError) as caught:
        await client._post_async("/api/ssh/heartbeat", {"session_id": "session"}, timeout=2)

    _assert_no_proxy_leak(caught.value)
    assert len(calls) == 1


def test_unknown_rate_limit_codes_and_metadata_are_not_forwarded():
    response = _response({
        "code": SECRET,
        "message": SECRET,
        "hint": SECRET,
        "details": {"bucket_id": SECRET, "bucket_class": SECRET, "operation_class": SECRET},
    })
    response.headers.pop("X-RateLimit-Bucket-ID")
    client = GatewayClient(base_url="https://gateway.invalid", api_key="test")
    with patch("gateway_client.httpx.get", return_value=response):
        with pytest.raises(GatewayClientError) as caught:
            client.health()
    _assert_no_proxy_leak(caught.value)
    assert caught.value.body["code"] == "RATE_LIMIT_EXCEEDED"
    assert "bucket_id" not in caught.value.body["details"]
    assert "operation_class" not in caught.value.body["details"]


@pytest.mark.parametrize(
    "bad_details",
    [
        {
            "operation_class": ["execute"],
            "bucket_class": {"token": SECRET},
            "bucket_id": [SECRET],
            "retry_after_seconds": True,
        },
        {
            "operation_class": 99,
            "bucket_class": None,
            "bucket_id": SECRET,
            "retry_after_seconds": 999999999999,
        },
    ],
)
def test_http_429_rejects_wrong_typed_metadata_without_crashing(bad_details):
    response = _response({
        "code": "RATE_LIMIT_EXCEEDED",
        "message": SECRET,
        "details": bad_details,
    })
    client = GatewayClient(base_url="https://gateway.invalid", api_key="test")
    with patch("gateway_client.httpx.get", return_value=response):
        with pytest.raises(GatewayClientError) as caught:
            client.health()
    _assert_no_proxy_leak(caught.value)
    assert "operation_class" not in caught.value.body["details"]
    assert caught.value.body["details"]["retry_after_seconds"] == 9


def test_http_429_bounded_body_only_retry_hint_survives_without_headers():
    response = _response({
        "code": "RATE_LIMIT_EXCEEDED",
        "retryable": False,
        "details": {
            "operation_class": "request",
            "retry_after_seconds": 11,
            "bucket_id": "bkt_0123456789abcdef01234567",
        },
    })
    response.headers.clear()
    client = GatewayClient(base_url="https://gateway.invalid", api_key="test")
    with patch("gateway_client.httpx.get", return_value=response):
        with pytest.raises(GatewayClientError) as caught:
            client.health()
    assert caught.value.body == {
        "code": "RATE_LIMIT_EXCEEDED",
        "message": "Gateway rate limit response",
        "retryable": False,
        "details": {
            "operation_class": "request",
            "retry_after_seconds": 11,
            "bucket_id": "bkt_0123456789abcdef01234567",
        },
    }


@pytest.mark.parametrize(
    "code",
    ["RATE_LIMIT_EXCEEDED", "SESSION_LIMIT_EXCEEDED", SECRET],
)
def test_auto_reconnect_429_does_not_echo_untrusted_proxy_body(monkeypatch, code):
    monkeypatch.setattr(GatewayClient, "_connect_retry_not_before", 0.0)
    response = _response({
        "code": code,
        "message": SECRET,
        "hint": SECRET,
        "details": {"token": SECRET, "operation_class": "connect"},
    })
    client = GatewayClient(
        base_url="https://gateway.invalid",
        api_key="test",
        ssh_host="ssh.internal",
        ssh_user="root",
        ssh_private_key="fake-key",
    )
    with patch("gateway_client.httpx.post", return_value=response) as post:
        with pytest.raises(GatewayClientError) as caught:
            client.connect()
    post.assert_called_once()
    _assert_no_proxy_leak(caught.value)
    assert "connect" == caught.value.body["details"]["operation_class"]
    if code == "SESSION_LIMIT_EXCEEDED":
        assert caught.value.body["code"] == code
        assert GatewayClient._connect_retry_not_before == 0
    else:
        assert caught.value.body["code"] == "RATE_LIMIT_EXCEEDED"
        assert GatewayClient._connect_retry_not_before > 0


def test_unrelated_http_503_response_contract_remains_unchanged():
    payload = {"detail": {"code": "HTTP_ERROR", "message": "Service unavailable"}}
    response = httpx.Response(
        503, json=payload, request=httpx.Request("GET", "https://gateway.invalid/health")
    )
    client = GatewayClient(base_url="https://gateway.invalid", api_key="test")
    with patch("gateway_client.httpx.get", return_value=response):
        with pytest.raises(GatewayClientError) as caught:
            client.health()
    assert caught.value.status_code == 503
    assert caught.value.body == payload
