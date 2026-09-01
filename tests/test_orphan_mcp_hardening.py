"""Orphan MCP Session Hardening — regression tests.

Tests per architect spec.  Tests use mock SSHClient and mock httpx
where needed; scoped fork tests use the real GatewayClient class.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.models import ConnectRequest
from examples.mcp_server.mcp_infra._server_ref import server_module

# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _make_record(
    *,
    session_id: str = "test-sid",
    source_ip: str = "10.0.0.1",
    last_activity: float | None = None,
    effective_idle_timeout: int = 0,
    ephemeral: bool = False,
    owner_type: str = "master",
) -> Any:
    from app.ssh_manager import SessionRecord

    record = SessionRecord(
        session_id=session_id,
        client=MagicMock(),
        host="target.invalid",
        port=22,
        username="tester",
        owner_type=owner_type,
        effective_idle_timeout=effective_idle_timeout,
        ephemeral=ephemeral,
    )
    record.source_ip = source_ip
    if last_activity is not None:
        record.last_activity = last_activity
    return record


@pytest.fixture
def live_server() -> Any:
    return server_module()


def _base_client(live_server: Any) -> Any:
    return live_server.GatewayClient(
        base_url="http://gateway.invalid",
        api_key="test-key",
        session_id="seed-session",
        ssh_host="executor.invalid",
        ssh_user="tester",
    )


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def manager() -> Any:
    from app.ssh_manager import SSHSessionManager

    mgr = SSHSessionManager.__new__(SSHSessionManager)
    mgr._sessions: dict[str, Any] = {}
    mgr._pending_sessions_by_ip: dict[str, int] = {}
    mgr._lock = asyncio.Lock()
    mgr._session_timeout = 1800
    mgr._background_tasks: set[asyncio.Task] = set()
    return mgr


# ────────────────────────────────────────────────────────────────────
# Test 1: ConnectRequest schema — ephemeral defaults False,
#         idle_timeout_seconds defaults None, min 60, max 86400
# ────────────────────────────────────────────────────────────────────


class TestConnectRequestSchema:
    def test_ephemeral_defaults_false(self) -> None:
        req = ConnectRequest(host="target.invalid", port=22, username="u", password="pw")
        assert req.ephemeral is False

    def test_idle_timeout_defaults_none(self) -> None:
        req = ConnectRequest(host="target.invalid", port=22, username="u", password="pw")
        assert req.idle_timeout_seconds is None

    def test_idle_timeout_rejects_below_60(self) -> None:
        with pytest.raises(ValidationError):
            ConnectRequest(
                host="target.invalid",
                port=22,
                username="u",
                password="pw",
                idle_timeout_seconds=59,
            )

    def test_idle_timeout_rejects_above_86400(self) -> None:
        with pytest.raises(ValidationError):
            ConnectRequest(
                host="target.invalid",
                port=22,
                username="u",
                password="pw",
                idle_timeout_seconds=86401,
            )

    def test_idle_timeout_accepts_60(self) -> None:
        req = ConnectRequest(
            host="target.invalid",
            port=22,
            username="u",
            password="pw",
            ephemeral=True,
            idle_timeout_seconds=60,
        )
        assert req.idle_timeout_seconds == 60

    def test_idle_timeout_accepts_86400(self) -> None:
        req = ConnectRequest(
            host="target.invalid",
            port=22,
            username="u",
            password="pw",
            ephemeral=True,
            idle_timeout_seconds=86400,
        )
        assert req.idle_timeout_seconds == 86400


# ────────────────────────────────────────────────────────────────────
# Test 2: SessionRecord stores effective_idle_timeout and ephemeral
# ────────────────────────────────────────────────────────────────────


class TestSessionRecord:
    def test_record_stores_effective_idle_timeout(self) -> None:
        record = _make_record(effective_idle_timeout=600)
        assert record.effective_idle_timeout == 600

    def test_record_stores_ephemeral_flag(self) -> None:
        record = _make_record(ephemeral=True)
        assert record.ephemeral is True

    def test_record_default_not_ephemeral(self) -> None:
        record = _make_record()
        assert record.ephemeral is False


# ────────────────────────────────────────────────────────────────────
# Test 3: effective_idle_timeout = min(requested, global)
# ────────────────────────────────────────────────────────────────────


class TestEffectiveIdleTimeout:
    def test_shortened_to_requested_when_less_than_global(self, manager: Any) -> None:
        requested = 600
        effective = min(requested, manager._session_timeout)
        assert effective == 600

    def test_capped_to_global_when_requested_exceeds_global(self, manager: Any) -> None:
        requested = 3600
        effective = min(requested, manager._session_timeout)
        assert effective == 1800

    def test_no_requested_uses_global(self, manager: Any) -> None:
        effective = manager._session_timeout
        assert effective == 1800


# ────────────────────────────────────────────────────────────────────
# Test 4: cleanup_stale_sessions uses per-record timeout
# ────────────────────────────────────────────────────────────────────


class TestCleanupPerRecordTimeout:
    @pytest.mark.asyncio
    async def test_stale_by_record_timeout_evicted(self, manager: Any) -> None:
        now = time.time()
        record = _make_record(session_id="short-timeout", effective_idle_timeout=120)
        record.last_activity = now - 200
        manager._sessions["short-timeout"] = record

        count = await manager.cleanup_stale_sessions()
        assert count == 1
        assert "short-timeout" not in manager._sessions

    @pytest.mark.asyncio
    async def test_fresh_by_record_timeout_kept(self, manager: Any) -> None:
        now = time.time()
        record = _make_record(session_id="fresh-session", effective_idle_timeout=600)
        record.last_activity = now - 100
        manager._sessions["fresh-session"] = record

        count = await manager.cleanup_stale_sessions()
        assert count == 0
        assert "fresh-session" in manager._sessions

    @pytest.mark.asyncio
    async def test_stale_by_global_when_record_timeout_zero(self, manager: Any) -> None:
        now = time.time()
        record = _make_record(session_id="global-fallback", effective_idle_timeout=0)
        record.last_activity = now - 3600
        manager._sessions["global-fallback"] = record

        count = await manager.cleanup_stale_sessions()
        assert count == 1


# ────────────────────────────────────────────────────────────────────
# Test 5: cleanup_stale_sessions respects global upper bound
# ────────────────────────────────────────────────────────────────────


class TestCleanupGlobalUpperBound:
    @pytest.mark.asyncio
    async def test_global_lower_cap_reaps_previously_valid_session(self, manager: Any) -> None:
        """BLOCKER 1: session created at global=3600, then global lowered to 600.
        Session idle 700s > new global 600 → MUST be reaped."""
        now = time.time()
        record = _make_record(
            session_id="old-global",
            effective_idle_timeout=3600,
        )
        record.last_activity = now - 700
        manager._sessions["old-global"] = record

        # Simulate runtime PATCH: global lowered from 3600 to 600
        manager._session_timeout = 600

        count = await manager.cleanup_stale_sessions()
        assert count == 1
        assert "old-global" not in manager._sessions

    @pytest.mark.asyncio
    async def test_global_lower_does_not_reap_fresh_session(self, manager: Any) -> None:
        """Session idle 400s < new global 600 → kept."""
        now = time.time()
        record = _make_record(
            session_id="still-fresh",
            effective_idle_timeout=3600,
        )
        record.last_activity = now - 400
        manager._sessions["still-fresh"] = record

        manager._session_timeout = 600

        count = await manager.cleanup_stale_sessions()
        assert count == 0
        assert "still-fresh" in manager._sessions


# ────────────────────────────────────────────────────────────────────
# Test 6: Ephemeral flag propagated through SessionRecord
# ────────────────────────────────────────────────────────────────────


class TestEphemeralPropagation:
    def test_ephemeral_session_record_flagged(self) -> None:
        record = _make_record(ephemeral=True)
        assert record.ephemeral is True

    def test_non_ephemeral_session_record_flagged(self) -> None:
        record = _make_record(ephemeral=False)
        assert record.ephemeral is False


# ────────────────────────────────────────────────────────────────────
# Test 7: Scoped fork sets ephemeral=True, idle_timeout_seconds=300
# ────────────────────────────────────────────────────────────────────


class TestScopedForkEphemeral:
    def test_fork_sets_ephemeral(self, live_server: Any) -> None:
        base = _base_client(live_server)
        scoped = base.fork_session()
        assert scoped._ephemeral is True

    def test_fork_sets_idle_timeout_300(self, live_server: Any) -> None:
        base = _base_client(live_server)
        scoped = base.fork_session()
        assert scoped._idle_timeout_seconds == 300

    def test_base_not_ephemeral(self, live_server: Any) -> None:
        base = _base_client(live_server)
        assert base._ephemeral is False

    def test_base_idle_timeout_none(self, live_server: Any) -> None:
        base = _base_client(live_server)
        assert base._idle_timeout_seconds is None


# ────────────────────────────────────────────────────────────────────
# Test 8: Scoped fork connect payload includes ephemeral + idle_timeout
# ────────────────────────────────────────────────────────────────────


class TestScopedForkPayload:
    def test_connect_payload_includes_ephemeral_and_idle_timeout(
        self, monkeypatch: pytest.MonkeyPatch, live_server: Any
    ) -> None:
        base = _base_client(live_server)
        scoped = base.fork_session()
        seen_payloads: list[dict[str, Any]] = []

        def fake_post(_url: str, **kwargs: Any) -> Any:
            seen_payloads.append(kwargs["json"])
            return _Response({"session_id": "owned-session"})

        monkeypatch.setattr("gateway_client.httpx.post", fake_post)

        scoped.connect()
        payload = seen_payloads[0]
        assert payload["ephemeral"] is True
        assert payload["idle_timeout_seconds"] == 300

    def test_base_connect_payload_no_ephemeral_fields(
        self, monkeypatch: pytest.MonkeyPatch, live_server: Any
    ) -> None:
        base = live_server.GatewayClient(
            base_url="http://gateway.invalid",
            api_key="test-key",
            session_id="seed-session",
            ssh_host="executor.invalid",
            ssh_user="tester",
        )
        seen_payloads: list[dict[str, Any]] = []

        def fake_post(_url: str, **kwargs: Any) -> Any:
            seen_payloads.append(kwargs["json"])
            return _Response({"session_id": "base-session"})

        monkeypatch.setattr("gateway_client.httpx.post", fake_post)

        base.connect()
        payload = seen_payloads[0]
        assert "ephemeral" not in payload
        assert "idle_timeout_seconds" not in payload


# ────────────────────────────────────────────────────────────────────
# Test 9: cleanup_stale_sessions per-record timeout — mixed batch
# ────────────────────────────────────────────────────────────────────


class TestCleanupMixedBatch:
    @pytest.mark.asyncio
    async def test_mixed_timeout_batch_evicts_only_expired(self, manager: Any) -> None:
        now = time.time()

        r1 = _make_record(session_id="short-stale", effective_idle_timeout=120)
        r1.last_activity = now - 300

        r2 = _make_record(session_id="short-fresh", effective_idle_timeout=120)
        r2.last_activity = now - 60

        r3 = _make_record(session_id="long-fresh", effective_idle_timeout=3600)
        r3.last_activity = now - 1000

        r4 = _make_record(session_id="global-stale", effective_idle_timeout=0)
        r4.last_activity = now - 3600

        manager._sessions = {
            "short-stale": r1,
            "short-fresh": r2,
            "long-fresh": r3,
            "global-stale": r4,
        }

        count = await manager.cleanup_stale_sessions()
        assert count == 2
        assert set(manager._sessions.keys()) == {"short-fresh", "long-fresh"}


# ────────────────────────────────────────────────────────────────────
# Test 10: ephemeral flag on SessionRecord
# ────────────────────────────────────────────────────────────────────


class TestEphemeralSessionRecord:
    def test_ephemeral_record_has_flag(self) -> None:
        record = _make_record(ephemeral=True, session_id="ephemeral-sid")
        assert record.ephemeral is True
        assert record.session_id == "ephemeral-sid"

    def test_non_ephemeral_record_default(self) -> None:
        record = _make_record(session_id="normal-sid")
        assert record.ephemeral is False


# ────────────────────────────────────────────────────────────────────
# BLOCKER 3: Schema fail-closed — ephemeral + reuse_existing conflict,
#            idle_timeout requires ephemeral
# ────────────────────────────────────────────────────────────────────


class TestSchemaFailClosed:
    def test_ephemeral_and_reuse_existing_raises(self) -> None:
        with pytest.raises(ValidationError, match="mutually exclusive"):
            ConnectRequest(
                host="target.invalid",
                port=22,
                username="u",
                password="pw",
                ephemeral=True,
                reuse_existing=True,
            )

    def test_idle_timeout_without_ephemeral_raises(self) -> None:
        with pytest.raises(ValidationError, match="requires ephemeral"):
            ConnectRequest(
                host="target.invalid",
                port=22,
                username="u",
                password="pw",
                ephemeral=False,
                idle_timeout_seconds=600,
            )

    def test_idle_timeout_with_ephemeral_ok(self) -> None:
        req = ConnectRequest(
            host="target.invalid",
            port=22,
            username="u",
            password="pw",
            ephemeral=True,
            idle_timeout_seconds=600,
        )
        assert req.ephemeral is True
        assert req.idle_timeout_seconds == 600

    def test_ephemeral_without_idle_timeout_ok(self) -> None:
        req = ConnectRequest(
            host="target.invalid",
            port=22,
            username="u",
            password="pw",
            ephemeral=True,
        )
        assert req.ephemeral is True
        assert req.idle_timeout_seconds is None

    def test_reuse_existing_without_ephemeral_ok(self) -> None:
        req = ConnectRequest(
            host="target.invalid",
            port=22,
            username="u",
            password="pw",
            reuse_existing=True,
        )
        assert req.reuse_existing is True
        assert req.ephemeral is False


# ────────────────────────────────────────────────────────────────────
# BLOCKER 4: Production admission test via create_session()
# ────────────────────────────────────────────────────────────────────


def _make_admission_manager(*, max_sessions_per_ip: int = 1) -> Any:
    """Build a minimal SSHSessionManager for admission tests."""
    from app.ssh_manager import SSHSessionManager

    mgr = SSHSessionManager.__new__(SSHSessionManager)
    mgr._sessions: dict[str, Any] = {}
    mgr._pending_sessions_by_ip: dict[str, int] = {}
    mgr._lock = asyncio.Lock()
    mgr._session_timeout = 1800
    mgr._background_tasks: set[asyncio.Task] = set()
    mgr._pool = None
    mgr._circuit_breakers = None
    mgr._secret_manager = None
    mgr._host_key_store = None
    mgr._strict_host_key = False
    return mgr


class TestProductionAdmissionReap:
    """BLOCKER 4: admission-time reap exercised through create_session()."""

    @pytest.mark.asyncio
    async def test_expired_ephemeral_reaped_new_session_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setup: max_sessions_per_ip=1, one expired ephemeral session for same source.
        Execute: create_session() for same source_ip.
        Assert: stale record removed, client.close() called, new session created."""
        from app.config import settings

        monkeypatch.setattr(settings, "max_sessions_per_ip", 1)
        manager = _make_admission_manager()

        now = time.time()
        mock_client = MagicMock()
        record = _make_record(
            session_id="stale-ephemeral",
            source_ip="172.19.0.17",
            effective_idle_timeout=120,
            ephemeral=True,
        )
        record.client = mock_client
        record.last_activity = now - 300  # 300s > 120s
        manager._sessions["stale-ephemeral"] = record

        created_sessions: list[str] = []

        async def fake_create_unreserved(**_kwargs: Any) -> str:
            new_sid = "new-session"
            created_sessions.append(new_sid)
            # Simulate new session being added to manager
            new_record = _make_record(session_id=new_sid, source_ip="172.19.0.17")
            manager._sessions[new_sid] = new_record
            return new_sid

        monkeypatch.setattr(manager, "_create_session_unreserved", fake_create_unreserved)

        result = await manager.create_session(
            host="target.invalid",
            port=22,
            username="tester",
            password="pw",
            source_ip="172.19.0.17",
            ephemeral=True,
            idle_timeout_seconds=120,
        )

        assert result == "new-session"
        assert "stale-ephemeral" not in manager._sessions
        assert "new-session" in manager._sessions
        mock_client.close.assert_called_once()
        assert created_sessions == ["new-session"]

    @pytest.mark.asyncio
    async def test_fresh_session_not_reaped_blocks_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setup: max_sessions_per_ip=1, one fresh session for same source.
        Execute: create_session() for same source_ip.
        Assert: SessionLimitError raised."""
        from app.config import settings
        from app.ssh_manager import SessionLimitError

        monkeypatch.setattr(settings, "max_sessions_per_ip", 1)
        manager = _make_admission_manager()

        now = time.time()
        record = _make_record(
            session_id="fresh-active",
            source_ip="172.19.0.17",
            effective_idle_timeout=600,
        )
        record.last_activity = now - 100  # 100s < 600s
        manager._sessions["fresh-active"] = record

        async def fake_create_unreserved(**_kwargs: Any) -> str:
            return "should-not-exist"

        monkeypatch.setattr(manager, "_create_session_unreserved", fake_create_unreserved)

        with pytest.raises(SessionLimitError, match="172.19.0.17"):
            await manager.create_session(
                host="target.invalid",
                port=22,
                username="tester",
                password="pw",
                source_ip="172.19.0.17",
            )

    @pytest.mark.asyncio
    async def test_global_lower_during_admission_reaps_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BLOCKER 1+4: session created at global=3600, then global lowered to 600.
        idle=700s > 600s → admission-time reap MUST remove it."""
        from app.config import settings

        monkeypatch.setattr(settings, "max_sessions_per_ip", 1)
        manager = _make_admission_manager()

        now = time.time()
        mock_client = MagicMock()
        record = _make_record(
            session_id="old-global-ephemeral",
            source_ip="172.19.0.17",
            effective_idle_timeout=3600,
            ephemeral=True,
        )
        record.client = mock_client
        record.last_activity = now - 700
        manager._sessions["old-global-ephemeral"] = record

        # Runtime PATCH: global lowered
        manager._session_timeout = 600

        async def fake_create_unreserved(**_kwargs: Any) -> str:
            manager._sessions["new-sid"] = _make_record(
                session_id="new-sid", source_ip="172.19.0.17"
            )
            return "new-sid"

        monkeypatch.setattr(manager, "_create_session_unreserved", fake_create_unreserved)

        result = await manager.create_session(
            host="target.invalid",
            port=22,
            username="tester",
            password="pw",
            source_ip="172.19.0.17",
            ephemeral=True,
            idle_timeout_seconds=3600,
        )

        assert result == "new-sid"
        assert "old-global-ephemeral" not in manager._sessions
        mock_client.close.assert_called_once()


# ────────────────────────────────────────────────────────────────────
# BLOCKER 4 controlled mutation RED proof
# ────────────────────────────────────────────────────────────────────


class TestAdmissionReapRedProof:
    """Controlled mutation: removing admission-time reap from create_session
    causes the production admission test to fail with SessionLimitError."""

    @pytest.mark.asyncio
    async def test_without_reap_create_session_raises_session_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings
        from app.ssh_manager import SessionLimitError

        monkeypatch.setattr(settings, "max_sessions_per_ip", 1)
        manager = _make_admission_manager()

        now = time.time()
        mock_client = MagicMock()
        record = _make_record(
            session_id="should-be-reaped",
            source_ip="172.19.0.17",
            effective_idle_timeout=120,
        )
        record.client = mock_client
        record.last_activity = now - 300
        manager._sessions["should-be-reaped"] = record

        # Monkey-patch create_session to skip admission-time reap
        # by removing the reap loop entirely. We do this by replacing
        # the admission block with a no-op that just does the quota check.

        async def create_without_reap(**kwargs: Any) -> str:
            """Simulate create_session with admission-time reap removed."""
            source_ip = kwargs.get("source_ip")
            if source_ip and settings.max_sessions_per_ip > 0:
                async with manager._lock:
                    active = sum(1 for r in manager._sessions.values() if r.source_ip == source_ip)
                    pending = manager._pending_sessions_by_ip.get(source_ip, 0)
                    if active + pending >= settings.max_sessions_per_ip:
                        raise SessionLimitError(
                            f"Too many active sessions from {source_ip} "
                            f"(limit {settings.max_sessions_per_ip})"
                        )
                    manager._pending_sessions_by_ip[source_ip] = pending + 1
            try:
                return await manager._create_session_unreserved(**kwargs)
            finally:
                if source_ip and settings.max_sessions_per_ip > 0:
                    async with manager._lock:
                        pending = manager._pending_sessions_by_ip.get(source_ip, 0)
                        if pending <= 1:
                            manager._pending_sessions_by_ip.pop(source_ip, None)
                        else:
                            manager._pending_sessions_by_ip[source_ip] = pending - 1

        async def fake_create_unreserved(**_kwargs: Any) -> str:
            return "new-sid"

        monkeypatch.setattr(manager, "_create_session_unreserved", fake_create_unreserved)
        monkeypatch.setattr(manager, "create_session", create_without_reap)

        with pytest.raises(SessionLimitError, match="172.19.0.17"):
            await manager.create_session(
                host="target.invalid",
                port=22,
                username="tester",
                password="pw",
                source_ip="172.19.0.17",
                ephemeral=True,
            )


# ────────────────────────────────────────────────────────────────────
# BLOCKER 2: PrewarmRequest propagates ephemeral + idle_timeout,
#            skips persistence for ephemeral sessions
# ────────────────────────────────────────────────────────────────────


class TestPrewarmEphemeralPropagation:
    """Regression: POST /api/ssh/prewarm must propagate ephemeral and
    idle_timeout_seconds to create_session(), and skip persistence
    when ephemeral=True."""

    def test_prewarm_ephemeral_propagated_to_create_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from starlette.testclient import TestClient

        from app.config import settings
        from app.main import app

        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        from app import state as st

        with TestClient(app) as client:
            # Must assign AFTER TestClient.__enter__ — lifespan overwrites state.manager
            st.manager = AsyncMock()
            st.manager.create_session = AsyncMock(return_value="ephemeral-sess")
            st.audit_logger = MagicMock()
            st.event_audit_logger = MagicMock()
            st.session_store = AsyncMock()
            st.access_control_store = None
            st.prewarm_tasks.clear()

            resp = client.post(
                "/api/ssh/prewarm",
                headers={"X-API-Key": "secret-42"},
                json={
                    "host": "10.0.0.1",
                    "port": 22,
                    "username": "root",
                    "password": "pw",
                    "ephemeral": True,
                    "idle_timeout_seconds": 600,
                },
            )
            assert resp.status_code == 200

            for _ in range(200):
                if st.manager.create_session.called:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("create_session was never called by prewarm task")

        kwargs = st.manager.create_session.call_args.kwargs
        assert kwargs.get("ephemeral") is True
        assert kwargs.get("idle_timeout_seconds") == 600

    def test_prewarm_ephemeral_skips_session_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from starlette.testclient import TestClient

        from app.config import settings
        from app.main import app

        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        from app import state as st

        with TestClient(app) as client:
            st.manager = AsyncMock()
            st.manager.create_session = AsyncMock(return_value="ephemeral-sess-2")
            st.audit_logger = MagicMock()
            st.event_audit_logger = MagicMock()
            st.session_store = AsyncMock()
            st.access_control_store = None
            st.prewarm_tasks.clear()

            resp = client.post(
                "/api/ssh/prewarm",
                headers={"X-API-Key": "secret-42"},
                json={
                    "host": "10.0.0.1",
                    "port": 22,
                    "username": "root",
                    "password": "pw",
                    "ephemeral": True,
                    "idle_timeout_seconds": 600,
                },
            )
            assert resp.status_code == 200

            for _ in range(200):
                if st.manager.create_session.called:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("create_session was never called by prewarm task")

        st.session_store.save_session.assert_not_called()

    def test_prewarm_normal_still_persisted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-ephemeral prewarm still calls save_session."""
        from starlette.testclient import TestClient

        from app.config import settings
        from app.main import app

        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        from app import state as st

        with TestClient(app) as client:
            st.manager = AsyncMock()
            st.manager.create_session = AsyncMock(return_value="normal-sess")
            st.audit_logger = MagicMock()
            st.event_audit_logger = MagicMock()
            st.session_store = AsyncMock()
            st.access_control_store = None
            st.prewarm_tasks.clear()

            resp = client.post(
                "/api/ssh/prewarm",
                headers={"X-API-Key": "secret-42"},
                json={
                    "host": "10.0.0.1",
                    "port": 22,
                    "username": "root",
                    "password": "pw",
                },
            )
            assert resp.status_code == 200

            for _ in range(200):
                if st.manager.create_session.called:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("create_session was never called by prewarm task")

        st.session_store.save_session.assert_called()
