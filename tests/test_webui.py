"""Tests for the Web UI (#3): web-ui JWT on WebSocket query, SessionInfo status/created_at."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth_middleware import AuthIdentity
from app.config import settings
from app.main import app
from app.user_auth import create_jwt


def _patch_base(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
    monkeypatch.setattr("app.auth_middleware.is_ip_allowed", lambda ip, nets: True)

    async def _fake_is_agent_token_valid(settings, provided, token_store=None) -> AuthIdentity | None:
        return None

    monkeypatch.setattr(
        "app.auth_middleware.is_agent_token_valid",
        _fake_is_agent_token_valid,
    )


class TestWebSocketWebUiToken:
    """WebSocket ?token= must authenticate a web-ui JWT (browser WS cannot set headers)."""

    def test_pty_stream_accepts_webui_jwt_via_query(self, monkeypatch):
        _patch_base(monkeypatch)
        token = create_jwt("tester", 1, role="admin")

        # Auth passes → connection reaches the "session not found" business
        # check (4403) instead of the 1008 auth rejection.
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(
                    f"/api/ssh/pty/sess-webui/stream?token={token}"
                ):
                    pass
            assert exc.value.code == 4403, (
                f"Expected 4403 (auth passed), got {exc.value.code}: {exc.value.reason}"
            )

    def test_pty_stream_rejects_bad_jwt_via_query(self, monkeypatch):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(
                    "/api/ssh/pty/sess-webui/stream?token=not-a-real-jwt"
                ):
                    pass
            assert exc.value.code == 1008

    def test_pty_stream_rejects_no_token(self, monkeypatch):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect("/api/ssh/pty/sess-webui/stream"):
                    pass
            assert exc.value.code == 1008

    def test_pty_stream_cookie_takes_priority_over_query_token(self, monkeypatch):
        """T79.10 follow-up: the httpOnly cookie must be checked before the
        ?token= query param, so a browser session (cookie set) isn't
        derailed by a stray/garbage query token — and the common browser
        case never even touches the query-string channel.
        """
        _patch_base(monkeypatch)
        from app.user_auth import AUTH_COOKIE_NAME

        good_token = create_jwt("tester", 1, role="admin")
        with TestClient(app) as client:
            client.cookies.set(AUTH_COOKIE_NAME, good_token)
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(
                    "/api/ssh/pty/sess-webui/stream?token=garbage-not-a-jwt"
                ):
                    pass
            # 4403 = auth passed (via cookie), rejected only by the
            # business-level "session not found" check — proves the cookie
            # won even though the query token was invalid.
            assert exc.value.code == 4403, (
                f"Expected 4403 (cookie auth passed), got {exc.value.code}: {exc.value.reason}"
            )

    def test_execute_stream_accepts_webui_jwt_via_query(self, monkeypatch):
        _patch_base(monkeypatch)
        token = create_jwt("tester", 1, role="admin")
        # Auth passes → websocket is accepted; a missing session_id yields a
        # business error instead of the 1008 auth rejection.
        with TestClient(app) as client:
            with client.websocket_connect(
                f"/api/ssh/execute/stream?token={token}"
            ) as ws:
                ws.send_json({"session_id": "", "command": ""})
                resp = ws.receive_json()
                assert resp.get("type") == "error"


class TestSessionInfoStatus:
    """Session list must expose status (active/idle/reconnecting) and created_at."""

    def _base_mock(self):
        mgr = MagicMock()
        mgr.list_sessions = AsyncMock(return_value=[])
        mgr.disconnect = AsyncMock()
        mgr.stop_cleanup_task = AsyncMock()
        mgr.start_cleanup_task = AsyncMock()
        mgr.reconnect = AsyncMock(return_value=True)
        return mgr

    def _session_rec(self, sid, connected, idle_seconds, connected_at=1000000.0, now=1000000.0):
        rec = MagicMock()
        rec.session_id = sid
        rec.host = "10.0.0.1"
        rec.port = 22
        rec.username = "root"
        rec.connected_at = connected_at
        rec.last_activity = now - idle_seconds
        rec.owner_type = "master"
        rec.owner_name = None
        rec.owner_token_fingerprint = None
        rec.is_connected = MagicMock(return_value=connected)
        return rec

    def test_sessions_list_has_status_and_created_at(self, monkeypatch):
        _patch_base(monkeypatch)
        now = 1000000.0
        recs = [
            self._session_rec("s-active", True, 10.0, now=now),       # idle < 60 → active
            self._session_rec("s-idle", True, 300.0, now=now),        # idle >= 60 → idle
            self._session_rec("s-rec", False, 5000.0, connected_at=now - 5000, now=now),  # not connected → reconnecting
        ]
        mgr = self._base_mock()
        mgr.list_sessions = AsyncMock(return_value=recs)

        # Keep idle_seconds small and deterministic: patch time.time used by router.
        import app.routers.ssh as ssh_router

        monkeypatch.setattr(ssh_router.time, "time", lambda: now)

        from app import state as _app_state

        with TestClient(app) as client:
            _app_state.manager = mgr
            resp = client.get("/api/ssh/sessions", headers={"X-API-Key": "secret-42"})

        assert resp.status_code == 200
        data = resp.json()
        by_id = {s["session_id"]: s for s in data["sessions"]}
        assert by_id["s-active"]["status"] == "active"
        assert by_id["s-idle"]["status"] == "idle"
        assert by_id["s-rec"]["status"] == "reconnecting"
        assert by_id["s-active"]["created_at"] is not None
        assert by_id["s-active"]["created_at"] == by_id["s-active"]["connected_at"]

    def test_sessions_list_status_defaults(self, monkeypatch):
        """Records without is_connected (legacy mocks) must not crash — MagicMock is truthy → active."""
        _patch_base(monkeypatch)
        rec = MagicMock()
        rec.session_id = "s-legacy"
        rec.host = "10.0.0.1"
        rec.port = 22
        rec.username = "root"
        rec.connected_at = 1000000.0
        rec.last_activity = 1000000.0
        rec.owner_type = "master"
        rec.owner_name = None

        mgr = self._base_mock()
        mgr.list_sessions = AsyncMock(return_value=[rec])

        with TestClient(app) as client:
            from app import state as _app_state

            _app_state.manager = mgr
            resp = client.get("/api/ssh/sessions", headers={"X-API-Key": "secret-42"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["sessions"][0]["status"] in ("active", "idle")
        assert data["sessions"][0]["created_at"] is not None


class TestWebUiAssets:
    """Static assets required by the Web UI must be served."""

    def test_index_html_served(self):
        with TestClient(app) as client:
            resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_xterm_vendor_served(self):
        with TestClient(app) as client:
            resp = client.get("/static/vendor/xterm/xterm.min.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
