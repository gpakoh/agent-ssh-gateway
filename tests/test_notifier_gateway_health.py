"""Tests for GatewayAuditClient.health()."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.notifier.gateway import GatewayAuditClient, GatewayHealthError


class _FakeResponse:
    """Minimal mock that supports ``await resp.json()``."""

    def __init__(self, status: int = 200, reason: str = "OK", json_data: dict | None = None):
        self.status = status
        self.reason = reason
        self._json_data = json_data or {}

    async def json(self):
        return self._json_data


class _FakeContextManager:
    """Mock for ``aiohttp.ClientSession.get()`` — returns an async context manager."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


def _make_client(api_key: str = "test-key", response: _FakeResponse | None = None):
    if response is None:
        response = _FakeResponse()
    session = MagicMock()
    session.get = MagicMock(return_value=_FakeContextManager(response))
    client = GatewayAuditClient(
        base_url="http://localhost:8085",
        api_key=api_key,
        session=session,
    )
    return client, session


class TestHealthSuccess:
    async def test_returns_parsed_json(self):
        client, _ = _make_client(response=_FakeResponse(200, "OK", {"status": "ok", "redis": True, "version": "0.1.0"}))
        result = await client.health()
        assert result["status"] == "ok"
        assert result["redis"] is True
        assert result["version"] == "0.1.0"

    async def test_sends_api_key_header(self):
        client, session = _make_client(api_key="secret-123")
        await client.health()
        session.get.assert_called_once()
        call_args = session.get.call_args
        assert call_args[1]["headers"]["X-API-Key"] == "secret-123"

    async def test_requests_correct_url(self):
        client, session = _make_client()
        await client.health()
        call_args = session.get.call_args
        assert call_args[0][0] == "http://localhost:8085/health"


class TestHealthFailure:
    async def test_503_raises_gateway_health_error(self):
        client, _ = _make_client(response=_FakeResponse(503, "Service Unavailable"))
        with pytest.raises(GatewayHealthError) as exc_info:
            await client.health()
        assert exc_info.value.status == 503
        assert exc_info.value.reason == "Service Unavailable"

    async def test_401_raises_gateway_health_error(self):
        client, _ = _make_client(response=_FakeResponse(401, "Unauthorized"))
        with pytest.raises(GatewayHealthError) as exc_info:
            await client.health()
        assert exc_info.value.status == 401

    async def test_500_raises_gateway_health_error(self):
        client, _ = _make_client(response=_FakeResponse(500, "Internal Server Error"))
        with pytest.raises(GatewayHealthError) as exc_info:
            await client.health()
        assert exc_info.value.status == 500

    async def test_exception_str_contains_status(self):
        client, _ = _make_client(response=_FakeResponse(503, "Service Unavailable"))
        with pytest.raises(GatewayHealthError) as exc_info:
            await client.health()
        assert "503" in str(exc_info.value)
        assert "Service Unavailable" in str(exc_info.value)

    async def test_missing_api_key_raises_runtime_error(self):
        client, _ = _make_client(api_key="")
        with pytest.raises(RuntimeError, match="GATEWAY_NOTIFIER_API_KEY"):
            await client.health()


class TestApiKeyLeakPrevention:
    async def test_api_key_not_in_exception_str(self):
        client, _ = _make_client(api_key="super-secret-key-xyz", response=_FakeResponse(503, "Service Unavailable"))
        with pytest.raises(GatewayHealthError) as exc_info:
            await client.health()
        assert "super-secret-key-xyz" not in str(exc_info.value)
        assert "X-API-Key" not in str(exc_info.value)

    async def test_api_key_not_in_exception_repr(self):
        client, _ = _make_client(api_key="super-secret-key-xyz", response=_FakeResponse(503, "Service Unavailable"))
        with pytest.raises(GatewayHealthError) as exc_info:
            await client.health()
        assert "super-secret-key-xyz" not in repr(exc_info.value)

    async def test_exception_only_has_status_and_reason(self):
        client, _ = _make_client(api_key="super-secret-key-xyz", response=_FakeResponse(503, "Service Unavailable"))
        with pytest.raises(GatewayHealthError) as exc_info:
            await client.health()
        assert not hasattr(exc_info.value, "api_key")
        assert exc_info.value.status == 503
        assert exc_info.value.reason == "Service Unavailable"
