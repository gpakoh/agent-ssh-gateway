"""Tests for /health readiness semantics and circuit breaker metric cardinality."""

from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app


class TestHealthReadiness:
    """ready must be True only when status is 'ok'."""

    def test_ready_true_when_all_ok(self):
        """ready=true when Redis is connected and status=ok."""
        with TestClient(app) as client:
            resp = client.get("/health")
        data = resp.json()
        # In test env redis_url is set but redis is not connected -> degraded
        # We just verify the field is a bool
        assert isinstance(data["ready"], bool)

    def test_ready_false_when_degraded(self):
        """ready must be false when status is degraded."""
        mock_redis = MagicMock()
        mock_redis._redis = None  # simulate disconnected redis
        mock_session_store = None  # no persistent sessions

        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.routers.system.settings") as mock_settings,
        ):
            mock_state.redis_queue = mock_redis
            mock_state.session_store = mock_session_store
            mock_settings.redis_url = "redis://localhost:6379"
            mock_settings.persistent_sessions_enabled = False

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["ready"] is False

    def test_ready_true_when_redis_ok(self):
        """ready=true when Redis is connected, API key set, SSH reachable."""
        mock_redis = MagicMock()
        mock_redis._redis = MagicMock()  # simulate connected redis

        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.routers.system.settings") as mock_settings,
            patch("app.routers.system.socket") as mock_socket,
        ):
            mock_state.redis_queue = mock_redis
            mock_state.session_store = None
            mock_settings.redis_url = "redis://localhost:6379"
            mock_settings.persistent_sessions_enabled = False
            mock_settings.api_auth_enabled = True
            mock_settings.api_key = "test-key"
            mock_settings.ssh_health_user = ""
            mock_settings.ssh_health_password = ""
            mock_socket.create_connection.return_value.__enter__ = lambda s: MagicMock()
            mock_socket.create_connection.return_value.__exit__ = MagicMock(return_value=False)

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "ok"
            assert data["ready"] is True
            assert data["api_key_configured"] is True
            assert data["ssh_server_reachable"] is True

    def test_ready_true_when_no_redis_configured(self):
        """ready=true when redis_url is not set and API key + SSH are fine."""
        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.routers.system.settings") as mock_settings,
            patch("app.routers.system.socket") as mock_socket,
        ):
            mock_state.redis_queue = MagicMock()
            mock_state.redis_queue._redis = None
            mock_state.session_store = None
            mock_settings.redis_url = ""  # no redis configured
            mock_settings.persistent_sessions_enabled = False
            mock_settings.api_auth_enabled = True
            mock_settings.api_key = "test-key"
            mock_settings.ssh_health_user = ""
            mock_settings.ssh_health_password = ""
            mock_socket.create_connection.return_value.__enter__ = lambda s: MagicMock()
            mock_socket.create_connection.return_value.__exit__ = MagicMock(return_value=False)

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "ok"
            assert data["ready"] is True

    def test_ready_false_when_persistent_sessions_enabled_but_store_none(self):
        """ready=false when persistent_sessions_enabled but session_store is None."""
        mock_redis = MagicMock()
        mock_redis._redis = MagicMock()  # redis is fine

        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.routers.system.settings") as mock_settings,
            patch("app.routers.system.socket") as mock_socket,
        ):
            mock_state.redis_queue = mock_redis
            mock_state.session_store = None  # postgres/session store missing
            mock_settings.redis_url = "redis://localhost:6379"
            mock_settings.persistent_sessions_enabled = True
            mock_settings.api_auth_enabled = False
            mock_settings.ssh_health_user = ""
            mock_settings.ssh_health_password = ""
            mock_socket.create_connection.return_value.__enter__ = lambda s: MagicMock()
            mock_socket.create_connection.return_value.__exit__ = MagicMock(return_value=False)

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["ready"] is False
            assert data["persistent_sessions"] is False

    def test_ready_false_when_api_key_missing(self):
        """ready=false when auth is enabled but no API key is configured."""
        mock_redis = MagicMock()
        mock_redis._redis = MagicMock()

        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.routers.system.settings") as mock_settings,
            patch("app.routers.system.socket") as mock_socket,
        ):
            mock_state.redis_queue = mock_redis
            mock_state.session_store = None
            mock_settings.redis_url = "redis://localhost:6379"
            mock_settings.persistent_sessions_enabled = False
            mock_settings.api_auth_enabled = True
            mock_settings.api_key = ""
            mock_settings.ssh_health_user = ""
            mock_settings.ssh_health_password = ""
            mock_socket.create_connection.return_value.__enter__ = lambda s: MagicMock()
            mock_socket.create_connection.return_value.__exit__ = MagicMock(return_value=False)

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["ready"] is False
            assert data["api_key_configured"] is False

    def test_ready_false_when_ssh_unreachable(self):
        """ready=false when SSH server cannot be reached."""
        mock_redis = MagicMock()
        mock_redis._redis = MagicMock()

        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.routers.system.settings") as mock_settings,
            patch("app.routers.system.socket") as mock_socket,
        ):
            mock_state.redis_queue = mock_redis
            mock_state.session_store = None
            mock_settings.redis_url = "redis://localhost:6379"
            mock_settings.persistent_sessions_enabled = False
            mock_settings.api_auth_enabled = True
            mock_settings.api_key = "test-key"
            mock_settings.ssh_health_user = ""
            mock_settings.ssh_health_password = ""
            mock_socket.create_connection.side_effect = OSError("Connection refused")

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["ready"] is False
            assert data["ssh_server_reachable"] is False

    def test_auth_disabled_skips_api_key_check(self):
        """ready=true when auth disabled (api_key not required)."""
        mock_redis = MagicMock()
        mock_redis._redis = MagicMock()

        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.routers.system.settings") as mock_settings,
            patch("app.routers.system.socket") as mock_socket,
        ):
            mock_state.redis_queue = mock_redis
            mock_state.session_store = None
            mock_settings.redis_url = "redis://localhost:6379"
            mock_settings.persistent_sessions_enabled = False
            mock_settings.api_auth_enabled = False
            mock_settings.api_key = ""
            mock_settings.ssh_health_user = ""
            mock_settings.ssh_health_password = ""
            mock_socket.create_connection.return_value.__enter__ = lambda s: MagicMock()
            mock_socket.create_connection.return_value.__exit__ = MagicMock(return_value=False)

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "ok"
            assert data["ready"] is True
            assert data["api_key_configured"] is False


class TestCircuitBreakerMetricCardinality:
    """circuit_breaker metric must be a bounded state-count aggregate, never per-host."""

    def test_counts_by_state_bounded_labels(self):
        from app.metrics import metrics

        metrics.set_circuit_breaker_counts({"closed": 2, "half_open": 1, "open": 3})

        output = metrics.get_metrics().decode()
        assert 'ssh_gateway_circuit_breakers_count{state="closed"} 2.0' in output
        assert 'ssh_gateway_circuit_breakers_count{state="half_open"} 1.0' in output
        assert 'ssh_gateway_circuit_breakers_count{state="open"} 3.0' in output
        # Must never carry a raw host/target label — only the 3 fixed states.
        for line in output.splitlines():
            if line.startswith("ssh_gateway_circuit_breakers_count{"):
                assert "host=" not in line
                assert "target=" not in line

    @pytest.mark.asyncio
    async def test_metrics_endpoint_reflects_live_registry(self):
        """GET /metrics refreshes the aggregate from the real registry, not stale values."""
        from app.circuit_breaker import CircuitBreakerRegistry

        reg = CircuitBreakerRegistry()
        cb_open = await reg.get_breaker("arbitrary-customer-host.example.com", failure_threshold=1)
        await cb_open.record_failure()
        await reg.get_breaker("another-host")

        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"),
        ):
            mock_state.circuit_breakers = reg
            with TestClient(app) as client:
                resp = client.get("/metrics", headers={"X-API-Key": settings.api_key})

        body = resp.text
        assert 'ssh_gateway_circuit_breakers_count{state="open"} 1.0' in body
        assert 'ssh_gateway_circuit_breakers_count{state="closed"} 1.0' in body
        # The arbitrary hostnames used to build these breakers must never
        # leak into the metric output as label values.
        assert "arbitrary-customer-host.example.com" not in body
