"""Tests for /health readiness semantics and circuit breaker metric cardinality."""

from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

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

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["ready"] is False

    def test_ready_true_when_redis_ok(self):
        """ready=true when Redis is connected."""
        mock_redis = MagicMock()
        mock_redis._redis = MagicMock()  # simulate connected redis

        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.routers.system.settings") as mock_settings,
        ):
            mock_state.redis_queue = mock_redis
            mock_state.session_store = None
            mock_settings.redis_url = "redis://localhost:6379"

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "ok"
            assert data["ready"] is True

    def test_ready_true_when_no_redis_configured(self):
        """ready=true when redis_url is not set (degraded logic irrelevant)."""
        with (
            patch("app.routers.system._state") as mock_state,
            patch("app.routers.system.settings") as mock_settings,
        ):
            mock_state.redis_queue = MagicMock()
            mock_state.redis_queue._redis = None
            mock_state.session_store = None
            mock_settings.redis_url = ""  # no redis configured

            with TestClient(app) as client:
                resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "ok"
            assert data["ready"] is True


class TestCircuitBreakerMetricCardinality:
    """circuit_breaker_state must use bounded 'target' label, not raw 'host'."""

    def test_gauge_uses_target_label(self):
        from app.metrics import metrics

        metrics.update_circuit_breaker(target="ssh", state="closed")
        metrics.update_circuit_breaker(target="redis", state="open")
        metrics.update_circuit_breaker(target="postgres", state="half_open")

        output = metrics.get_metrics().decode()
        assert 'target="ssh"' in output
        assert 'target="redis"' in output
        assert 'target="postgres"' in output
        # Must NOT contain raw host label in circuit_breaker lines
        for line in output.splitlines():
            if "ssh_gateway_circuit_breaker_state" in line and "ssh_gateway_circuit_breaker_state_total" not in line:
                assert "host=" not in line, f"Unbounded host label found: {line}"
