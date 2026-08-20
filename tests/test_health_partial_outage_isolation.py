"""Regression / fault-injection tests for GET /health partial-outage isolation.

Adversarial matrix
------------------
1. SSH connect failure  + other components healthy
2. SSH timeout          + other components healthy
3. All components healthy
4. Critical configured dependency down (Redis when redis_url set)
5. Simultaneous distinct failure classes

Captures RED evidence on base: these tests assert per-component granularity
that the base health endpoint does NOT provide (single boolean per component,
no structured ``components`` dict).  After the fix each test must pass.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.testclient import TestClient

from app.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _healthy_state() -> MagicMock:
    """Return a mock _state with all subsystems healthy."""
    state = MagicMock()
    state.redis_queue._redis = MagicMock()  # redis connected
    state.redis_queue._redis.ping = AsyncMock(return_value=True)
    state.session_store = MagicMock()       # persistent sessions OK
    connection = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=False)
    state.session_store._engine = MagicMock()
    state.session_store._engine.connect.return_value = context
    return state


def _healthy_settings(**overrides) -> MagicMock:
    """Return mock settings with all subsystems green, applying overrides."""
    s = MagicMock()
    s.redis_url = "redis://localhost:6379"
    s.persistent_sessions_enabled = False
    s.api_auth_enabled = True
    s.api_key = "test-key"
    s.ssh_health_user = ""
    s.ssh_health_password = ""
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _ssh_ok_socket() -> MagicMock:
    """Mock socket that succeeds (SSH reachable)."""
    sock = MagicMock()
    sock.create_connection.return_value.__enter__ = lambda s: MagicMock()
    sock.create_connection.return_value.__exit__ = MagicMock(return_value=False)
    return sock


def _ssh_fail_socket(exc: BaseException = OSError("Connection refused")) -> MagicMock:
    """Mock socket that fails (SSH unreachable)."""
    sock = MagicMock()
    sock.create_connection.side_effect = exc
    return sock


def _get_health(state: MagicMock | None = None, settings: MagicMock | None = None,
                sock_mod: MagicMock | None = None) -> dict:
    """Single GET /health call with injected mocks, returns JSON body."""
    state = state or _healthy_state()
    settings = settings or _healthy_settings()
    sock_mod = sock_mod or _ssh_ok_socket()
    with (
        patch("app.routers.system._state", state),
        patch("app.routers.system.settings", settings),
        patch("app.routers.system.socket", sock_mod),
        TestClient(app) as client,
    ):
        resp = client.get("/health")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# RED evidence: base does NOT expose per-component structured status
# ---------------------------------------------------------------------------

class TestPartialOutageIsolationRequiresComponentsField:
    """The ``components`` field MUST exist for per-component observability."""

    def test_components_key_present_on_base(self):
        """FAILS on base: ``components`` key absent from response."""
        data = _get_health()
        assert "components" in data, (
            "HealthResponse missing 'components' key — per-component "
            "isolation is not available on the base.  RED evidence captured."
        )


class TestComponentGranularity:
    """Each component must expose its own status independently."""

    def test_ssh_failure_shows_component_degraded(self):
        """FAILS on base: ``components.ssh`` not present."""
        data = _get_health(sock_mod=_ssh_fail_socket())
        assert "components" in data
        ssh = data["components"].get("ssh", {})
        assert ssh.get("status") == "degraded", (
            "SSH component should be degraded when TCP connect fails"
        )

    def test_redis_failure_shows_component_degraded(self):
        """FAILS on base: ``components.redis`` not present."""
        state = _healthy_state()
        state.redis_queue._redis = None  # redis disconnected
        data = _get_health(state=state)
        assert "components" in data
        redis = data["components"].get("redis", {})
        assert redis.get("status") == "degraded", (
            "Redis component should be degraded when connection is None"
        )

    def test_all_healthy_shows_ok(self):
        """FAILS on base: ``components`` absent."""
        data = _get_health()
        assert "components" in data
        for name, comp in data["components"].items():
            assert comp.get("status") == "ok", (
                f"Component {name!r} should be 'ok' when all subsystems healthy"
            )


class TestAdversarialMatrix:
    """Simultaneous distinct failure classes must be independently observable."""

    def test_ssh_down_redis_down_simultaneous(self):
        """Two components degraded at once; each must report independently."""
        state = _healthy_state()
        state.redis_queue._redis = None  # redis down
        data = _get_health(
            state=state,
            sock_mod=_ssh_fail_socket(),
        )
        assert "components" in data
        assert data["components"]["redis"]["status"] == "degraded"
        assert data["components"]["ssh"]["status"] == "degraded"
        # The remaining component (e.g. sessions) should still be ok
        for name, comp in data["components"].items():
            if name not in ("redis", "ssh"):
                assert comp["status"] == "ok", (
                    f"Component {name!r} should be ok when only redis+ssh are down"
                )

    def test_ssh_ok_redis_ok_api_key_missing(self):
        """Auth component degraded while others healthy."""
        data = _get_health(settings=_healthy_settings(api_key=""))
        assert "components" in data
        assert data["components"]["ssh"]["status"] == "ok"
        assert data["components"]["redis"]["status"] == "ok"
        assert data["components"]["auth"]["status"] == "degraded"

    def test_persistent_sessions_down_others_ok(self):
        """Persistent sessions component degraded; rest ok."""
        state = _healthy_state()
        state.session_store = None
        data = _get_health(
            state=state,
            settings=_healthy_settings(persistent_sessions_enabled=True),
        )
        assert "components" in data
        assert data["components"]["persistent_sessions"]["status"] == "degraded"
        assert data["components"]["postgres"]["status"] == "degraded"
        for name, comp in data["components"].items():
            if name not in ("persistent_sessions", "postgres"):
                assert comp["status"] == "ok"


class TestLiveDependencyFaultInjection:
    """Live handles must not be treated as proof of dependency health."""

    def test_redis_live_handle_connect_failure_is_not_false_healthy(self):
        state = _healthy_state()
        state.redis_queue._redis.ping = AsyncMock(
            side_effect=RedisConnectionError("redis://secret.internal:6379 refused")
        )
        data = _get_health(state=state)
        assert data["redis"] is False
        assert data["ready"] is False
        assert data["components"]["redis"]["status"] == "degraded"
        assert data["components"]["redis"]["failure_class"] == "connect_error"
        assert data["components"]["ssh"]["status"] == "ok"
        assert "secret.internal" not in str(data)

    def test_redis_live_handle_timeout_is_distinct(self):
        state = _healthy_state()
        state.redis_queue._redis.ping = AsyncMock(side_effect=TimeoutError("redis timed out"))
        data = _get_health(state=state)
        assert data["redis"] is False
        assert data["components"]["redis"]["failure_class"] == "timeout"

    def test_postgres_live_store_connect_failure_is_not_false_healthy(self):
        state = _healthy_state()
        state.session_store._engine.connect.side_effect = OSError("db.internal refused")
        data = _get_health(
            state=state,
            settings=_healthy_settings(persistent_sessions_enabled=True),
        )
        assert data["persistent_sessions"] is False
        assert data["postgres"] is False
        assert data["ready"] is False
        assert data["components"]["postgres"]["failure_class"] == "connect_error"
        assert data["components"]["redis"]["status"] == "ok"
        assert "db.internal" not in str(data)

    def test_postgres_live_store_timeout_is_distinct(self):
        state = _healthy_state()
        state.session_store._engine.connect.side_effect = TimeoutError("db timed out")
        data = _get_health(
            state=state,
            settings=_healthy_settings(persistent_sessions_enabled=True),
        )
        assert data["components"]["postgres"]["failure_class"] == "timeout"

    def test_two_distinct_failures_preserve_component_truth(self):
        state = _healthy_state()
        state.redis_queue._redis.ping = AsyncMock(side_effect=TimeoutError("redis timed out"))
        data = _get_health(state=state, sock_mod=_ssh_fail_socket(OSError("dns/connect failed")))
        assert data["components"]["redis"]["failure_class"] == "timeout"
        assert data["components"]["ssh"]["failure_class"] == "connect_error"
        assert data["components"]["auth"]["status"] == "ok"

    def test_optional_unconfigured_dependencies_do_not_degrade(self):
        state = _healthy_state()
        state.redis_queue._redis = None
        state.session_store = None
        data = _get_health(
            state=state,
            settings=_healthy_settings(redis_url="", persistent_sessions_enabled=False),
        )
        assert data["status"] == "ok"
        assert data["ready"] is True
        assert data["components"]["redis"]["status"] == "ok"
        assert data["components"]["redis"]["required"] is False
        assert data["components"]["postgres"]["status"] == "ok"
        assert data["components"]["postgres"]["required"] is False


class TestBackwardCompatibility:
    """Existing flat HealthResponse fields must remain unchanged."""

    def test_flat_fields_still_present(self):
        data = _get_health()
        for key in ("status", "redis", "persistent_sessions", "postgres",
                     "ready", "api_key_configured", "ssh_server_reachable",
                     "build_sha", "build_time", "started_at", "version"):
            assert key in data, f"Flat field {key!r} missing — backward compat broken"

    def test_status_degraded_when_any_component_degraded(self):
        """Top-level status must still be 'degraded' if anything fails."""
        data = _get_health(sock_mod=_ssh_fail_socket())
        assert data["status"] == "degraded"
        assert data["ready"] is False

    def test_status_ok_when_all_green(self):
        data = _get_health()
        assert data["status"] == "ok"
        assert data["ready"] is True


class TestNoHostnameOrUrlLeak:
    """Ensure no raw hostname or internal URL leaks into the response."""

    def test_no_hostname_in_response(self):
        data = _get_health()
        import json
        body = json.dumps(data)
        assert "sshd" not in body.lower(), "Raw hostname 'sshd' leaked into health response"
        assert "localhost" not in body, "Internal URL 'localhost' leaked into health response"

    def test_no_port_in_component_details(self):
        """Component status messages must not expose TCP ports."""
        data = _get_health(sock_mod=_ssh_fail_socket())
        import json
        body = json.dumps(data["components"])
        assert ":22" not in body, "Port 22 leaked into component health details"


class TestSshTimeoutVsConnectFailure:
    """Timeout and connection-refused must both surface as component degraded,
    but the top-level ``ssh_server_reachable`` must remain the flat bool."""

    def test_timeout_shows_component_degraded(self):
        data = _get_health(
            sock_mod=_ssh_fail_socket(TimeoutError("timed out")),
        )
        assert "components" in data
        assert data["components"]["ssh"]["status"] == "degraded"
        assert data["components"]["ssh"]["failure_class"] == "timeout"
        assert data["ssh_server_reachable"] is False

    def test_connect_refused_shows_component_degraded(self):
        data = _get_health(
            sock_mod=_ssh_fail_socket(OSError("Connection refused")),
        )
        assert "components" in data
        assert data["components"]["ssh"]["status"] == "degraded"
        assert data["components"]["ssh"]["failure_class"] == "connect_error"
        assert data["ssh_server_reachable"] is False
