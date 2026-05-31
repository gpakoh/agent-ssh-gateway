"""Tests for session ownership — agent tokens must not access other agents' sessions."""

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from app.main import app
from app.auth_middleware import token_fingerprint, ensure_session_owner, AuthIdentity
from app.config import settings


class FakeSession:
    """Minimal stand-in for SessionRecord ownership fields."""
    def __init__(self, owner_type="master", owner_name=None, owner_token_fingerprint=None):
        self.owner_type = owner_type
        self.owner_name = owner_name
        self.owner_token_fingerprint = owner_token_fingerprint


class TestTokenFingerprint:
    """token_fingerprint() must be stable, non-reversible, and unique per token."""

    def test_returns_sha256_hex(self):
        result = token_fingerprint("secret-42")
        expected = hashlib.sha256(b"secret-42").hexdigest()
        assert result == expected
        assert len(result) == 64

    def test_consistent_same_input(self):
        assert token_fingerprint("abc") == token_fingerprint("abc")

    def test_different_inputs_differ(self):
        assert token_fingerprint("abc") != token_fingerprint("xyz")


class TestEnsureSessionOwner:
    """ensure_session_owner() controls session access per caller identity."""

    # ------------------------------------------------------------------
    # Master key
    # ------------------------------------------------------------------

    def test_master_owns_agent_session(self):
        ident = AuthIdentity(token_type="master", token="m1", name="admin")
        session = FakeSession(owner_type="agent", owner_name="bot1", owner_token_fingerprint="other")
        ensure_session_owner(session, ident)

    def test_master_owns_master_session(self):
        ident = AuthIdentity(token_type="master", token="m1", name="admin")
        session = FakeSession(owner_type="master", owner_name="admin", owner_token_fingerprint="m1-fp")
        ensure_session_owner(session, ident)

    def test_master_owns_no_owner_session(self):
        ident = AuthIdentity(token_type="master", token="m1", name="admin")
        session = FakeSession()
        ensure_session_owner(session, ident)

    # ------------------------------------------------------------------
    # Agent token — own session
    # ------------------------------------------------------------------

    def test_agent_owns_own_session(self):
        fp = token_fingerprint("agent-token-a")
        ident = AuthIdentity(token_type="agent", token="agent-token-a", name="bot-a")
        session = FakeSession(owner_type="agent", owner_name="bot-a", owner_token_fingerprint=fp)
        ensure_session_owner(session, ident)

    # ------------------------------------------------------------------
    # Agent token — cross-tenant forbidden
    # ------------------------------------------------------------------

    def test_agent_cannot_access_another_agents_session(self):
        ident = AuthIdentity(token_type="agent", token="agent-token-a", name="bot-a")
        fp_b = token_fingerprint("agent-token-b")
        session = FakeSession(owner_type="agent", owner_name="bot-b", owner_token_fingerprint=fp_b)
        with pytest.raises(HTTPException) as exc:
            ensure_session_owner(session, ident)
        assert exc.value.status_code == 403

    def test_agent_cannot_access_master_session(self):
        ident = AuthIdentity(token_type="agent", token="agent-token-a", name="bot-a")
        session = FakeSession(owner_type="master", owner_name="admin", owner_token_fingerprint="admin-fp")
        with pytest.raises(HTTPException) as exc:
            ensure_session_owner(session, ident)
        assert exc.value.status_code == 403

    def test_agent_cannot_access_session_without_owner(self):
        ident = AuthIdentity(token_type="agent", token="agent-token-a", name="bot-a")
        session = FakeSession()
        with pytest.raises(HTTPException) as exc:
            ensure_session_owner(session, ident)
        assert exc.value.status_code == 403

    def test_agent_cannot_access_session_with_none_fingerprint(self):
        ident = AuthIdentity(token_type="agent", token="agent-token-a", name="bot-a")
        session = FakeSession(owner_type="agent", owner_name="bot-b", owner_token_fingerprint=None)
        with pytest.raises(HTTPException) as exc:
            ensure_session_owner(session, ident)
        assert exc.value.status_code == 403

    # ------------------------------------------------------------------
    # AuthIdentity fingerprint property
    # ------------------------------------------------------------------

    def test_auth_identity_fingerprint_property(self):
        ident = AuthIdentity(token_type="agent", token="hello-token", name="test")
        expected = hashlib.sha256(b"hello-token").hexdigest()
        assert ident.fingerprint == expected

    def test_auth_identity_is_frozen(self):
        ident = AuthIdentity(token_type="agent", token="x", name="test")
        with pytest.raises(AttributeError):
            ident.token_type = "master"


# -------------------------------------------------------------------
# HTTP Integration Tests
# -------------------------------------------------------------------


class TestSessionOwnershipHTTP:
    """Verify the ownership checks work through the real HTTP middleware path."""

    @classmethod
    def _base_mock(cls):
        mgr = MagicMock()
        mgr.execute = AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})
        mgr.disconnect = AsyncMock()
        mgr.stop_cleanup_task = AsyncMock()
        mgr.list_sessions = AsyncMock(return_value=[])
        mgr.start_cleanup_task = AsyncMock()
        mgr.reconnect = AsyncMock(return_value=True)
        return mgr

    @classmethod
    def _make_session_mock(cls):
        mgr = cls._base_mock()
        mgr.get_session = AsyncMock(return_value=MagicMock(
            owner_type="agent",
            owner_name="bot-a",
            owner_token_fingerprint=token_fingerprint("agent-token-a"),
            is_connected=MagicMock(return_value=True),
        ))
        return mgr

    @classmethod
    def _make_cross_tenant_session_mock(cls):
        mgr = cls._base_mock()
        mgr.get_session = AsyncMock(return_value=MagicMock(
            owner_type="agent",
            owner_name="bot-b",
            owner_token_fingerprint=token_fingerprint("agent-token-b"),
            is_connected=MagicMock(return_value=True),
        ))
        return mgr

    @classmethod
    def _make_master_session_mock(cls):
        mgr = cls._base_mock()
        mgr.get_session = AsyncMock(return_value=MagicMock(
            owner_type="master",
            owner_name="admin",
            owner_token_fingerprint=token_fingerprint("secret-42"),
            is_connected=MagicMock(return_value=True),
        ))
        return mgr

    @classmethod
    def _patch_base(cls, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(settings, "agent_token_scopes",
                            ["ssh:connect", "ssh:execute", "ssh:disconnect", "ssh:files"])
        monkeypatch.setattr(settings, "agent_token_expires_at", None)

        monkeypatch.setattr(
            "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
        )

    def _override_manager(self, client, mock_mgr):
        """Replace manager on the live app after TestClient lifespan."""
        from app import state as _app_state
        _app_state.manager = mock_mgr

    def _make_file_editor_mock(self):
        fe = MagicMock()
        fe.read_file = AsyncMock(return_value="mock content")
        fe.edit_file = AsyncMock(return_value={"success": True, "operations_applied": 1})
        fe.write_file = AsyncMock(return_value=None)
        fe.apply_patch = AsyncMock(return_value={"success": True})
        return fe

    def test_master_can_execute_on_agent_session(self, monkeypatch):
        self._patch_base(monkeypatch)
        monkeypatch.setattr(settings, "agent_token", "agent-token-a")
        monkeypatch.setattr(settings, "agent_token_ttl", 3600)

        with TestClient(app) as client:
            self._override_manager(client, self._make_session_mock())
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-1", "command": "ls"},
            )
        assert resp.status_code in (200, 404), f"Expected 200 or 404, got {resp.status_code}: {resp.text}"

    def test_agent_can_execute_on_own_session(self, monkeypatch):
        self._patch_base(monkeypatch)
        monkeypatch.setattr(settings, "agent_token", "agent-token-a")
        monkeypatch.setattr(settings, "agent_token_ttl", 3600)

        with TestClient(app) as client:
            self._override_manager(client, self._make_session_mock())
            resp = client.post(
                "/api/ssh/execute",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"session_id": "s-1", "command": "ls"},
            )
        assert resp.status_code in (200, 404), f"Expected 200 or 404, got {resp.status_code}: {resp.text}"

    def test_agent_cannot_execute_on_other_agent_session(self, monkeypatch):
        self._patch_base(monkeypatch)
        monkeypatch.setattr(settings, "agent_token", "agent-token-a")
        monkeypatch.setattr(settings, "agent_token_ttl", 3600)

        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            resp = client.post(
                "/api/ssh/execute",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"session_id": "s-2", "command": "ls"},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        assert "SESSION_OWNERSHIP" in resp.text or "cannot access" in resp.text

    def test_agent_cannot_execute_on_master_session(self, monkeypatch):
        self._patch_base(monkeypatch)
        monkeypatch.setattr(settings, "agent_token", "agent-token-a")
        monkeypatch.setattr(settings, "agent_token_ttl", 3600)

        with TestClient(app) as client:
            self._override_manager(client, self._make_master_session_mock())
            resp = client.post(
                "/api/ssh/execute",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"session_id": "s-3", "command": "ls"},
            )
        assert resp.status_code == 403

    def test_disconnect_ownership_agent_blocked(self, monkeypatch):
        self._patch_base(monkeypatch)
        monkeypatch.setattr(settings, "agent_token", "agent-token-a")
        monkeypatch.setattr(settings, "agent_token_ttl", 3600)

        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            resp = client.post(
                "/api/ssh/disconnect",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"session_id": "s-2"},
            )
        assert resp.status_code == 403

    def test_disconnect_ownership_master_bypasses(self, monkeypatch):
        self._patch_base(monkeypatch)
        monkeypatch.setattr(settings, "agent_token", "agent-token-b")
        monkeypatch.setattr(settings, "agent_token_ttl", 3600)

        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            resp = client.post(
                "/api/ssh/disconnect",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-2"},
            )
        assert resp.status_code == 200

    def test_file_read_ownership_agent_blocked(self, monkeypatch):
        self._patch_base(monkeypatch)
        monkeypatch.setattr(settings, "agent_token", "agent-token-a")
        monkeypatch.setattr(settings, "agent_token_ttl", 3600)

        with TestClient(app) as client:
            mock_mgr = self._make_cross_tenant_session_mock()
            self._override_manager(client, mock_mgr)
            from app import state as _app_state
            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/file/read",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"session_id": "s-2", "path": "/etc/hostname"},
            )
        assert resp.status_code == 403

    def test_file_read_ownership_master_bypasses(self, monkeypatch):
        self._patch_base(monkeypatch)
        monkeypatch.setattr(settings, "agent_token", "agent-token-b")
        monkeypatch.setattr(settings, "agent_token_ttl", 3600)

        with TestClient(app) as client:
            mock_mgr = self._make_cross_tenant_session_mock()
            self._override_manager(client, mock_mgr)
            from app import state as _app_state
            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/file/read",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-2", "path": "/etc/hostname"},
            )
        assert resp.status_code in (200, 422), f"Expected 200 or 422, got {resp.status_code}: {resp.text}"


class TestAuthIdentityFingerprint:
    """Verify AuthIdentity.fingerprint works correctly with dataclass."""

    def test_fingerprint_stable(self):
        a = AuthIdentity(token_type="agent", token="t1", name="bot")
        b = AuthIdentity(token_type="agent", token="t1", name="bot")
        assert a.fingerprint == b.fingerprint

    def test_fingerprint_differs_by_token(self):
        a = AuthIdentity(token_type="agent", token="t1", name="bot")
        b = AuthIdentity(token_type="agent", token="t2", name="bot")
        assert a.fingerprint != b.fingerprint

    def test_fingerprint_matches_token_fingerprint(self):
        ident = AuthIdentity(token_type="agent", token="custom-token", name="test")
        assert ident.fingerprint == token_fingerprint("custom-token")
