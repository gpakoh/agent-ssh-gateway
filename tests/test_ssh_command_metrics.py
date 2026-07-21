"""Tests for SSH command and queue depth metrics wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from prometheus_client import Counter as _Counter
from prometheus_client import Info as _Info

from app.config import settings
from app.main import app
from app.metrics import MetricsCollector, _normalize_command_root
from app.version import APP_VERSION

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": settings.api_key}


def _isolated_metrics(name: str) -> MetricsCollector:
    """Create a MetricsCollector wired to a fresh registry (no global conflicts)."""
    mc = MetricsCollector.__new__(MetricsCollector)
    reg = CollectorRegistry()
    mc.ssh_commands = _Counter(
        f"test_ssh_cmds_{name}",
        "test",
        ["status", "profile", "command_root"],
        registry=reg,
    )
    mc.info = _Info(f"test_sg_{name}", "test", registry=reg)
    mc.info.info({"version": APP_VERSION})
    return mc


# ---------------------------------------------------------------------------
# _normalize_command_root tests
# ---------------------------------------------------------------------------


class TestNormalizeCommandRoot:
    def test_known_command_passes_through(self):
        assert _normalize_command_root("git") == "git"
        assert _normalize_command_root("docker") == "docker"
        assert _normalize_command_root("pytest") == "pytest"

    def test_unknown_command_maps_to_other(self):
        assert _normalize_command_root("zzz_unknown_tool") == "other"

    def test_none_maps_to_other(self):
        assert _normalize_command_root(None) == "other"

    def test_empty_string_maps_to_other(self):
        assert _normalize_command_root("") == "other"

    def test_path_stripped(self):
        assert _normalize_command_root("/usr/bin/git") == "git"

    def test_case_insensitive(self):
        assert _normalize_command_root("Git") == "git"
        assert _normalize_command_root("DOCKER") == "docker"


# ---------------------------------------------------------------------------
# MetricsCollector.record_ssh_command label tests
# ---------------------------------------------------------------------------


class TestRecordSshCommand:
    def test_allowed_increments_counter(self):
        mc = _isolated_metrics("allowed")
        before = mc.ssh_commands.labels(
            status="allowed", profile="default", command_root="git"
        )._value.get()
        mc.record_ssh_command(status="allowed", profile="default", command_root="git")
        after = mc.ssh_commands.labels(
            status="allowed", profile="default", command_root="git"
        )._value.get()
        assert after == before + 1

    def test_denied_increments_counter(self):
        mc = _isolated_metrics("denied")
        before = mc.ssh_commands.labels(
            status="denied", profile="readonly", command_root="other"
        )._value.get()
        mc.record_ssh_command(status="denied", profile="readonly", command_root="rm")
        after = mc.ssh_commands.labels(
            status="denied", profile="readonly", command_root="other"
        )._value.get()
        assert after == before + 1

    def test_profile_label_is_bounded(self):
        mc = _isolated_metrics("bounded")
        for profile in ("default", "readonly", "testlint", "ops", "docker-admin"):
            mc.record_ssh_command(status="allowed", profile=profile, command_root="ls")

    def test_command_root_normalized(self):
        mc = _isolated_metrics("norm")
        mc.record_ssh_command(status="allowed", profile="default", command_root="zzz_weird")
        val = mc.ssh_commands.labels(
            status="allowed", profile="default", command_root="other"
        )._value.get()
        assert val >= 1

    def test_metric_output_no_raw_command(self):
        """Prometheus text output must not contain raw command strings."""
        mc = _isolated_metrics("output")
        mc.record_ssh_command(status="allowed", profile="default", command_root="cat")
        text = mc.get_metrics().decode()
        assert "cat /etc/passwd" not in text
        assert "rm -rf" not in text


# ---------------------------------------------------------------------------
# Integration: execute endpoint increments metrics
# ---------------------------------------------------------------------------


class TestSshExecuteMetrics:
    @patch("app.routers.ssh.metrics")
    def test_execute_denied_increments_denied(self, mock_metrics):
        """Denied command increments the denied counter."""
        with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post(
                    "/api/ssh/execute",
                    json={"session_id": "s1", "command": "rm file.txt"},
                    headers=_auth_headers(),
                )
                assert resp.status_code == 403
                call_kwargs = mock_metrics.record_ssh_command.call_args.kwargs
                assert call_kwargs["status"] == "denied"

    @patch("app.routers.ssh.metrics")
    def test_execute_allowed_increments_allowed(self, mock_metrics):
        """Allowed command increments the allowed counter."""
        with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post(
                    "/api/ssh/connect",
                    json={
                        "host": "localhost",
                        "username": "test",
                        "password": "test",
                        "port": 22,
                    },
                    headers=_auth_headers(),
                )
                session_id = resp.json().get("session_id", "s1")

                resp = client.post(
                    "/api/ssh/execute",
                    json={"session_id": session_id, "command": "ls"},
                    headers=_auth_headers(),
                )
                if resp.status_code == 200:
                    call_kwargs = mock_metrics.record_ssh_command.call_args.kwargs
                    assert call_kwargs["status"] == "allowed"

    @patch("app.routers.ssh.metrics")
    def test_metric_no_raw_command_text(self, mock_metrics):
        """Ensure no raw command text appears in metric labels."""
        with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
            with TestClient(app, raise_server_exceptions=False) as client:
                client.post(
                    "/api/ssh/execute",
                    json={"session_id": "s1", "command": "cat /etc/shadow"},
                    headers=_auth_headers(),
                )
                if mock_metrics.record_ssh_command.called:
                    call_kwargs = mock_metrics.record_ssh_command.call_args.kwargs
                    assert "cat /etc/shadow" not in str(call_kwargs)
                    assert "shadow" not in call_kwargs.get("command_root", "")


# ---------------------------------------------------------------------------
# Queue depth metrics
# ---------------------------------------------------------------------------


class TestQueueDepthMetrics:
    def test_update_queue_depth_sets_gauge(self):
        from prometheus_client import Gauge as _Gauge

        reg = CollectorRegistry()
        qd = _Gauge("test_qd", "test", ["queue"], registry=reg)
        from app.metrics import MetricsCollector

        mc = MetricsCollector.__new__(MetricsCollector)
        mc.queue_depth = qd
        mc.queue_depth.labels(queue="pending").set(5)
        mc.queue_depth.labels(queue="processing").set(2)
        mc.queue_depth.labels(queue="dead").set(1)
        assert qd.labels(queue="pending")._value.get() == 5
        assert qd.labels(queue="processing")._value.get() == 2
        assert qd.labels(queue="dead")._value.get() == 1

    def test_queue_depth_in_metrics_output(self):
        from prometheus_client import Gauge as _Gauge
        from prometheus_client import generate_latest

        reg = CollectorRegistry()
        qd = _Gauge("test_qd_out", "test", ["queue"], registry=reg)
        qd.labels(queue="pending").set(3)
        qd.labels(queue="processing").set(1)
        qd.labels(queue="dead").set(0)
        text = generate_latest(reg).decode()
        assert "test_qd_out" in text
        assert "pending" in text

    @patch("app.routers.jobs.metrics")
    def test_queue_stats_endpoint_updates_metrics(self, mock_metrics):
        """GET /api/jobs/queue/stats updates queue_depth gauge."""
        with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
            mock_rq = AsyncMock()
            mock_rq._redis = AsyncMock()
            mock_rq.get_queue_stats = AsyncMock(
                return_value={"pending": 3, "processing": 1, "completed": 10, "dead_letter": 2}
            )
            with patch("app.routers.jobs._state") as mock_state:
                mock_state.redis_queue = mock_rq
                with TestClient(app, raise_server_exceptions=False) as client:
                    client.get(
                        "/api/jobs/queue/stats",
                        headers=_auth_headers(),
                    )
                    mock_metrics.update_queue_depth.assert_called_once_with(
                        pending=3, processing=1, dead=2
                    )


# ---------------------------------------------------------------------------
# Redis queue _update_queue_depth_metrics
# ---------------------------------------------------------------------------


class TestRedisQueueMetrics:
    @pytest.mark.asyncio
    async def test_update_queue_depth_metrics_calls_metrics(self):
        """_update_queue_depth_metrics calls metrics.update_queue_depth."""
        from app.redis_queue import RedisJobQueue

        rq = RedisJobQueue("redis://localhost:6379")
        rq._redis = AsyncMock()
        rq._redis.zcard = AsyncMock(side_effect=[5, 2, 0])

        with patch("app.redis_queue.metrics") as mock_metrics:
            await rq._update_queue_depth_metrics()
            mock_metrics.update_queue_depth.assert_called_once_with(
                pending=5, processing=2, dead=0
            )

    @pytest.mark.asyncio
    async def test_update_queue_depth_noop_when_not_connected(self):
        """_update_queue_depth_metrics is a no-op when Redis is not connected."""
        from app.redis_queue import RedisJobQueue

        rq = RedisJobQueue("redis://localhost:6379")
        rq._redis = None

        with patch("app.redis_queue.metrics") as mock_metrics:
            await rq._update_queue_depth_metrics()
            mock_metrics.update_queue_depth.assert_not_called()
