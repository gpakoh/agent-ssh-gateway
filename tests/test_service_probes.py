"""Safety and typed outcome tests for the fixed-target MCP-network probes."""

from __future__ import annotations

import asyncio
import inspect
import socket

import httpx
import pytest
from tool_modes import should_register_tool
from tool_scopes import get_required_scopes

from examples.mcp_server.mcp_infra.adapters import service_probes as probes


def _http(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        probes, "_http_client",
        lambda _timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected_host", "expected_port", "expected_path"),
    [
        ("gateway", "web-ssh-gateway", 8085, "/health"),
        ("mcp", "mcp-server", 8087, "/healthz"),
    ],
)
async def test_http_fixed_target_get_has_no_auth_or_redirect(
    monkeypatch, target, expected_host, expected_port, expected_path
) -> None:
    def handler(request):
        assert request.method == "GET"
        assert request.url.host == expected_host
        assert request.url.port == expected_port
        assert request.url.path == expected_path
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ready":true}'))

    _http(monkeypatch, handler)
    result = await probes.service_http_probe(target=target, timeout_ms=800, max_response_bytes=32)
    assert result["ok"] is True
    value = result["result"]
    assert value["status_code"] == 200
    assert value["healthy"] is True
    assert value["network_namespace"] == "mcp_container"
    assert value["host_port_owner_known"] is False
    assert value["response_body_included"] is False
    assert b"ready" not in str(result).encode()
    assert value["response_truncated"] is False
    assert value["response_bytes_sampled"] == 14


@pytest.mark.asyncio
async def test_http_redirect_not_followed_and_location_not_returned(monkeypatch) -> None:
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/secret?token=LEAK"}, stream=httpx.ByteStream(b""))

    _http(monkeypatch, handler)
    result = await probes.service_http_probe("gateway")
    assert result["ok"] is True
    assert result["result"]["status_code"] == 302
    assert result["result"]["failure_class"] == "redirect_not_followed"
    assert result["result"]["redirect_followed"] is False
    assert len(calls) == 1
    assert "LEAK" not in str(result)
    assert "169.254.169.254" not in str(result)


@pytest.mark.asyncio
async def test_http_non_2xx_retains_status_not_raw_body(monkeypatch) -> None:
    _http(monkeypatch, lambda _: httpx.Response(503, stream=httpx.ByteStream(b"private-error-detail-TOKEN")))
    result = await probes.service_http_probe("mcp", max_response_bytes=100)
    assert result["ok"] is True
    assert result["result"]["status_code"] == 503
    assert result["result"]["failure_class"] == "http_non_2xx"
    assert result["result"]["healthy"] is False
    assert "TOKEN" not in str(result)


@pytest.mark.asyncio
async def test_http_large_body_bounded_and_never_returned(monkeypatch) -> None:
    _http(monkeypatch, lambda _: httpx.Response(200, stream=httpx.ByteStream(b"S" * 100_000)))
    result = await probes.service_http_probe(max_response_bytes=10)
    assert result["ok"] is True
    assert result["result"]["response_bytes_sampled"] == 10
    assert result["result"]["response_truncated"] is True
    assert result["meta"]["truncated"] is True
    assert "SSSS" not in str(result)


@pytest.mark.asyncio
async def test_http_zero_bytes_does_not_read_response(monkeypatch) -> None:
    _http(monkeypatch, lambda _: httpx.Response(200, stream=httpx.ByteStream(b"secret")))
    result = await probes.service_http_probe(max_response_bytes=0)
    assert result["ok"] is True
    assert result["result"]["response_bytes_sampled"] == 0
    assert result["result"]["response_body_included"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "timeout_ms", "size", "field"),
    [
        ("other", 900, 10, "target"),
        ("http://example.com", 900, 10, "target"),
        (None, 900, 10, "target"),
        ("gateway", True, 10, "timeout_ms"),
        ("gateway", 0, 10, "timeout_ms"),
        ("gateway", 5001, 10, "timeout_ms"),
        ("gateway", 900, -1, "max_response_bytes"),
        ("gateway", 900, 4097, "max_response_bytes"),
        ("gateway", 900, True, "max_response_bytes"),
    ],
)
async def test_http_validation_before_network(monkeypatch, target, timeout_ms, size, field) -> None:
    def forbidden(_timeout):
        raise AssertionError("No network before input validation")

    monkeypatch.setattr(probes, "_http_client", forbidden)
    result = await probes.service_http_probe(target, timeout_ms, size)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["details"]["field"] == field


@pytest.mark.asyncio
async def test_http_dns_and_refused_are_typed_and_redacted(monkeypatch) -> None:
    def dns(request):
        raise httpx.ConnectError("secret-private-url", request=request) from socket.gaierror(-2, "secret DNS")

    _http(monkeypatch, dns)
    outcome = await probes.service_http_probe("mcp")
    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "REMOTE_UNAVAILABLE"
    assert outcome["error"]["details"]["failure_class"] == "dns_resolution"
    assert "secret" not in str(outcome)

    def refused(request):
        raise httpx.ConnectError("private-container-address", request=request) from ConnectionRefusedError()

    _http(monkeypatch, refused)
    outcome = await probes.service_http_probe("gateway")
    assert outcome["error"]["details"]["failure_class"] == "connection_refused"
    assert "private-container-address" not in str(outcome)


@pytest.mark.asyncio
async def test_http_timeout_is_bounded_and_typed(monkeypatch) -> None:
    async def handler(_):
        await asyncio.sleep(0.3)
        return httpx.Response(200)

    _http(monkeypatch, handler)
    outcome = await probes.service_http_probe(timeout_ms=200)
    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "TIMEOUT"
    assert outcome["error"]["details"]["failure_class"] == "timeout"


@pytest.mark.asyncio
async def test_tcp_fixed_alias_connect_no_data_and_socket_closed(monkeypatch) -> None:
    called = []
    class Writer:
        def close(self):
            called.append("close")
        async def wait_closed(self):
            called.append("wait_closed")

    async def open_connection(host, port):
        called.append((host, port))
        return object(), Writer()

    monkeypatch.setattr(probes.asyncio, "open_connection", open_connection)
    result = await probes.service_tcp_probe("gateway")
    assert result["ok"] is True
    assert result["result"]["connected"] is True
    assert result["result"]["application_health"] == "unknown"
    assert result["result"]["data_sent"] is False
    assert called == [("web-ssh-gateway", 8085), "close", "wait_closed"]


@pytest.mark.asyncio
async def test_tcp_refusal_redacted_and_bad_target_no_connect(monkeypatch) -> None:
    async def refused(_host, _port):
        raise ConnectionRefusedError("secret")

    monkeypatch.setattr(probes.asyncio, "open_connection", refused)
    result = await probes.service_tcp_probe("mcp")
    assert result["ok"] is False
    assert result["error"]["details"]["failure_class"] == "connection_refused"
    assert "secret" not in str(result)

    result = await probes.service_tcp_probe("example.org")
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_tcp_timeout_is_typed_and_never_sends_bytes(monkeypatch) -> None:
    async def slow(_host, _port):
        await asyncio.sleep(0.3)
        raise AssertionError("TCP connect should have timed out")

    monkeypatch.setattr(probes.asyncio, "open_connection", slow)
    result = await probes.service_tcp_probe("mcp", timeout_ms=200)
    assert result["ok"] is False
    assert result["error"]["code"] == "TIMEOUT"
    assert result["error"]["details"]["failure_class"] == "timeout"
    assert result["error"]["details"]["network_namespace"] == "mcp_container"


def test_tcp_boolean_timeout_rejected_before_socket_connect(monkeypatch) -> None:
    async def forbidden(_host, _port):
        raise AssertionError("No socket for invalid input")

    monkeypatch.setattr(probes.asyncio, "open_connection", forbidden)
    # Direct validation also protects non-MCP callers which bypass schema parsing.
    assert probes._validate("service_tcp_probe", "gateway", True)["error"]["code"] == "INVALID_INPUT"


def test_registration_modes_scopes_and_no_arbitrary_user_url() -> None:
    assert get_required_scopes("service_http_probe") == ["mcp:read"]
    assert get_required_scopes("service_tcp_probe") == ["mcp:read"]
    for name in ("service_http_probe", "service_tcp_probe"):
        for mode in ("standard", "full", "mcp_client", "mcp_client_write"):
            assert should_register_tool(name, mode=mode)
        assert not should_register_tool(name, mode="minimal")
    for fn in (probes.service_http_probe, probes.service_tcp_probe):
        fields = set(inspect.signature(fn).parameters)
        assert "url" not in fields
        assert "host" not in fields
        assert "path" not in fields
        assert "headers" not in fields
        assert "method" not in fields