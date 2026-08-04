"""RBAC: role resolution, permission checks and tenant isolation."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth_middleware import AuthIdentity, ensure_session_owner, token_fingerprint
from app.config import settings
from app.main import app
from app.rbac import (
    BUILTIN_ROLES,
    CONNECT,
    EXECUTE,
    UPLOAD,
    default_role_for_scopes,
    get_role,
    job_visible_to,
    labels_overlap,
    role_allows_scope,
    scopes_for_role,
    session_visible_to,
)

MASTER_HEADERS = {"X-API-Key": "secret-42"}


def _run(awaitable):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(awaitable)


def _make_session(sid: str, owner_fp: str | None, labels: tuple[str, ...] = ()) -> MagicMock:
    rec = MagicMock()
    rec.session_id = sid
    rec.host = "10.0.0.1"
    rec.port = 22
    rec.username = "root"
    rec.connected_at = 1000000.0
    rec.last_activity = 1000000.0
    rec.owner_type = "agent"
    rec.owner_name = "bot"
    rec.owner_token_fingerprint = owner_fp
    rec.tenant_labels = labels
    return rec


def _make_job(job_id: str, owner_id: str) -> MagicMock:
    job = MagicMock()
    job.job_id = job_id
    job.owner_id = owner_id
    return job


def _identity(
    token: str = "agent-token-a",
    scopes: tuple[str, ...] = (),
    role: str | None = None,
    labels: tuple[str, ...] = (),
    token_type: str = "agent",
) -> AuthIdentity:
    return AuthIdentity(
        token_type=token_type,
        token=token,
        name="agent",
        scopes=scopes,
        role=role,
        tenant_labels=labels,
    )


# ---------------------------------------------------------------------------
# Role model
# ---------------------------------------------------------------------------


class TestRoleModel:
    def test_admin_has_all_permissions(self):
        role = BUILTIN_ROLES["admin"]
        assert role.permissions == {CONNECT, EXECUTE, UPLOAD, "admin"}

    def test_operator_has_connect_execute_upload(self):
        role = BUILTIN_ROLES["operator"]
        assert CONNECT in role.permissions
        assert EXECUTE in role.permissions
        assert UPLOAD in role.permissions
        assert "admin" not in role.permissions

    def test_viewer_has_only_connect(self):
        role = BUILTIN_ROLES["viewer"]
        assert role.permissions == {CONNECT}

    def test_custom_has_empty_permissions(self):
        assert BUILTIN_ROLES["custom"].permissions == frozenset()

    def test_resource_selector_defaults_to_all(self):
        assert BUILTIN_ROLES["admin"].resource_selector == ()

    def test_get_role_unknown_returns_none(self):
        assert get_role("superadmin") is None


class TestDefaultRoleForScopes:
    def test_wildcard_scopes_default_admin(self):
        assert default_role_for_scopes(["*"]) == "admin"

    def test_wildcard_tuple_default_admin(self):
        assert default_role_for_scopes(("*",)) == "admin"

    def test_empty_scopes_no_role(self):
        assert default_role_for_scopes([]) is None

    def test_explicit_scopes_no_role(self):
        assert default_role_for_scopes(["ssh:connect"]) is None

    def test_none_scopes_no_role(self):
        assert default_role_for_scopes(None) is None


class TestScopesForRole:
    def test_admin_gets_wildcard(self):
        assert scopes_for_role("admin") == ("*",)

    def test_operator_gets_execute_scopes(self):
        scopes = scopes_for_role("operator")
        assert "ssh:execute" in scopes
        assert "ssh:connect" in scopes
        assert "ssh:files" in scopes
        assert "*" not in scopes

    def test_viewer_gets_only_connect_scopes(self):
        scopes = scopes_for_role("viewer")
        assert "ssh:connect" in scopes
        assert "ssh:execute" not in scopes
        assert "*" not in scopes

    def test_unknown_role_gets_nothing(self):
        assert scopes_for_role("superadmin") == ()

    def test_none_role_gets_nothing(self):
        assert scopes_for_role(None) == ()


class TestRoleAllowsScope:
    def test_admin_allows_every_scope(self):
        assert role_allows_scope("admin", None, "ssh:execute")
        assert role_allows_scope("admin", None, "ssh:files")

    def test_operator_allows_execute(self):
        assert role_allows_scope("operator", None, "ssh:execute")
        assert role_allows_scope("operator", None, "ssh:execute:argv")

    def test_operator_allows_connect_and_upload(self):
        assert role_allows_scope("operator", None, "ssh:connect")
        assert role_allows_scope("operator", None, "ssh:files")

    def test_viewer_allows_connect_denies_execute(self):
        assert role_allows_scope("viewer", None, "ssh:connect")
        assert not role_allows_scope("viewer", None, "ssh:execute")

    def test_custom_uses_provided_permissions(self):
        assert role_allows_scope("custom", frozenset({EXECUTE}), "ssh:execute")
        assert not role_allows_scope("custom", frozenset({EXECUTE}), "ssh:files")

    def test_no_role_denies(self):
        assert not role_allows_scope(None, None, "ssh:connect")

    def test_unknown_scope_denied(self):
        assert not role_allows_scope("viewer", None, "project:patch")


class TestLabelsOverlap:
    def test_shared_label_overlaps(self):
        assert labels_overlap(("team:a",), ("team:a", "env:prod"))

    def test_disjoint_labels_do_not_overlap(self):
        assert not labels_overlap(("team:a",), ("team:b",))

    def test_empty_labels_do_not_overlap(self):
        assert not labels_overlap((), ("team:a",))

    def test_non_collection_is_false(self):
        assert not labels_overlap(("a",), None)
        assert not labels_overlap(("a",), "not-a-list")


# ---------------------------------------------------------------------------
# Tenant visibility
# ---------------------------------------------------------------------------


class TestSessionVisibleTo:
    def test_master_sees_everything(self):
        sess = _make_session("s-1", None)
        identity = _identity(token="", token_type="master")
        assert session_visible_to(sess, identity)

    def test_admin_role_sees_everything(self):
        sess = _make_session("s-1", "other-fp", ("team:b",))
        identity = _identity(role="admin")
        assert session_visible_to(sess, identity)

    def test_agent_sees_own_session(self):
        fp = token_fingerprint("agent-token-a")
        sess = _make_session("s-1", fp)
        assert session_visible_to(sess, _identity())

    def test_agent_hidden_from_foreign_session(self):
        fp_other = token_fingerprint("agent-token-b")
        sess = _make_session("s-2", fp_other)
        assert not session_visible_to(sess, _identity())

    def test_tenant_overlap_grants_visibility(self):
        fp_other = token_fingerprint("agent-token-b")
        sess = _make_session("s-2", fp_other, ("team:a", "env:prod"))
        identity = _identity(labels=("team:a",))
        assert session_visible_to(sess, identity)

    def test_tenant_mismatch_denies(self):
        fp_other = token_fingerprint("agent-token-b")
        sess = _make_session("s-2", fp_other, ("team:b",))
        identity = _identity(labels=("team:a",))
        assert not session_visible_to(sess, identity)


class TestJobVisibleTo:
    """Regression coverage for the jobs IDOR fix: GET /api/jobs and friends
    used to have zero per-owner authorization, letting any caller with
    jobs:read/jobs:run see or act on every tenant's jobs.
    """

    def test_master_sees_everything(self):
        job = _make_job("j-1", owner_id="someone-elses-fingerprint")
        identity = _identity(token="", token_type="master")
        assert job_visible_to(job, identity)

    def test_admin_role_sees_everything(self):
        job = _make_job("j-1", owner_id="someone-elses-fingerprint")
        identity = _identity(role="admin")
        assert job_visible_to(job, identity)

    def test_owner_sees_own_job(self):
        fp = token_fingerprint("agent-token-a")
        job = _make_job("j-1", owner_id=fp)
        assert job_visible_to(job, _identity())

    def test_non_owner_hidden_from_foreign_job(self):
        fp_other = token_fingerprint("agent-token-b")
        job = _make_job("j-2", owner_id=fp_other)
        assert not job_visible_to(job, _identity())

    def test_job_with_no_owner_id_hidden_from_non_admin(self):
        job = _make_job("j-3", owner_id="")
        assert not job_visible_to(job, _identity())


class TestEnsureSessionOwner:
    def test_foreign_session_raises(self):
        fp_other = token_fingerprint("agent-token-b")
        sess = _make_session("s-2", fp_other)
        with pytest.raises(Exception) as exc:
            ensure_session_owner(sess, _identity())
        assert exc.value.status_code == 403

    def test_tenant_grant_passes(self):
        fp_other = token_fingerprint("agent-token-b")
        sess = _make_session("s-2", fp_other, ("team:a",))
        ensure_session_owner(sess, _identity(labels=("team:a",)))

    def test_admin_role_passes_foreign(self):
        fp_other = token_fingerprint("agent-token-b")
        sess = _make_session("s-2", fp_other)
        ensure_session_owner(sess, _identity(role="admin"))


# ---------------------------------------------------------------------------
# Integration: role-based scope enforcement
# ---------------------------------------------------------------------------


class TestRoleScopeEnforcement:
    def _patch_agent(self, monkeypatch, identity: AuthIdentity):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(
            "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
        )

        async def _fake(settings_, provided, token_store=None):
            return identity if provided == "agent-token-a" else None

        monkeypatch.setattr("app.auth_middleware.is_agent_token_valid", _fake)

    @staticmethod
    def _base_mgr() -> MagicMock:
        mgr = MagicMock()
        mgr.start_cleanup_task = AsyncMock()
        mgr.stop_cleanup_task = AsyncMock()
        mgr.list_sessions = AsyncMock(return_value=[])
        mgr.disconnect = AsyncMock()
        return mgr

    def test_operator_role_grants_execute_scope(self, monkeypatch):
        identity = _identity(role="operator", scopes=())
        self._patch_agent(monkeypatch, identity)
        with TestClient(app) as client:
            resp = client.post(
                "/api/ssh/execute",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"session_id": "s-1", "command": "ls"},
            )
        assert resp.status_code in (200, 404), resp.text

    def test_viewer_role_denied_execute_scope(self, monkeypatch):
        identity = _identity(role="viewer", scopes=())
        self._patch_agent(monkeypatch, identity)
        with TestClient(app) as client:
            resp = client.post(
                "/api/ssh/execute",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"session_id": "s-1", "command": "ls"},
            )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["code"] == "MISSING_SCOPE"

    def test_viewer_role_grants_connect_scope(self, monkeypatch):
        identity = _identity(role="viewer", scopes=())
        self._patch_agent(monkeypatch, identity)

        mgr = self._base_mgr()
        mgr.create_session = AsyncMock(return_value="s-1")

        with TestClient(app) as client:
            from app import state as _app_state

            _app_state.manager = mgr
            resp = client.post(
                "/api/ssh/connect",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"host": "10.0.0.1", "port": 22, "username": "root", "password": "test-pass"},
            )
        assert resp.status_code == 200, resp.text

    def test_legacy_scopes_still_work_without_role(self, monkeypatch):
        identity = _identity(scopes=("ssh:connect",))
        self._patch_agent(monkeypatch, identity)

        mgr = self._base_mgr()
        mgr.create_session = AsyncMock(return_value="s-1")

        with TestClient(app) as client:
            from app import state as _app_state

            _app_state.manager = mgr
            resp = client.post(
                "/api/ssh/connect",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"host": "10.0.0.1", "port": 22, "username": "root", "password": "test-pass"},
            )
        assert resp.status_code == 200, resp.text

    def test_legacy_token_without_scope_denied(self, monkeypatch):
        identity = _identity(scopes=())
        self._patch_agent(monkeypatch, identity)
        with TestClient(app) as client:
            resp = client.post(
                "/api/ssh/execute",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"session_id": "s-1", "command": "ls"},
            )
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Integration: tenant-filtered session list
# ---------------------------------------------------------------------------


class TestTenantSessionList:
    def _patch_agent(self, monkeypatch, identity: AuthIdentity):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(
            "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
        )

        async def _fake(settings_, provided, token_store=None):
            return identity if provided == "agent-token-a" else None

        monkeypatch.setattr("app.auth_middleware.is_agent_token_valid", _fake)

    def _override_manager(self, client, mgr):
        from app import state as _app_state

        _app_state.manager = mgr

    @staticmethod
    def _base_mgr() -> MagicMock:
        mgr = MagicMock()
        mgr.start_cleanup_task = AsyncMock()
        mgr.stop_cleanup_task = AsyncMock()
        mgr.disconnect = AsyncMock()
        return mgr

    def test_agent_with_labels_sees_own_and_tenant_sessions(self, monkeypatch):
        fp_a = token_fingerprint("agent-token-a")
        fp_b = token_fingerprint("agent-token-b")
        identity = _identity(labels=("team:a",), scopes=("ssh:execute",))
        self._patch_agent(monkeypatch, identity)

        mgr = self._base_mgr()
        mgr.list_sessions = AsyncMock(
            return_value=[
                _make_session("s-a", fp_a, ("team:a",)),
                _make_session("s-b", fp_b, ("team:a",)),  # cross-tenant grant
                _make_session("s-c", fp_b, ("team:b",)),  # hidden
            ]
        )

        with TestClient(app) as client:
            self._override_manager(client, mgr)
            resp = client.get(
                "/api/ssh/sessions",
                headers={"Authorization": "Bearer agent-token-a"},
            )
        assert resp.status_code == 200
        session_ids = {s["session_id"] for s in resp.json()["sessions"]}
        assert session_ids == {"s-a", "s-b"}

    def test_admin_role_sees_all_sessions(self, monkeypatch):
        fp_b = token_fingerprint("agent-token-b")
        identity = _identity(role="admin", scopes=())
        self._patch_agent(monkeypatch, identity)

        mgr = self._base_mgr()
        mgr.list_sessions = AsyncMock(
            return_value=[
                _make_session("s-b", fp_b, ("team:b",)),
                _make_session("s-c", fp_b, ("team:c",)),
            ]
        )

        with TestClient(app) as client:
            self._override_manager(client, mgr)
            resp = client.get(
                "/api/ssh/sessions",
                headers={"Authorization": "Bearer agent-token-a"},
            )
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_agent_without_labels_sees_only_own(self, monkeypatch):
        fp_a = token_fingerprint("agent-token-a")
        fp_b = token_fingerprint("agent-token-b")
        identity = _identity(scopes=("ssh:execute",))
        self._patch_agent(monkeypatch, identity)

        mgr = self._base_mgr()
        mgr.list_sessions = AsyncMock(
            return_value=[
                _make_session("s-a", fp_a),
                _make_session("s-b", fp_b),
            ]
        )

        with TestClient(app) as client:
            self._override_manager(client, mgr)
            resp = client.get(
                "/api/ssh/sessions",
                headers={"Authorization": "Bearer agent-token-a"},
            )
        assert resp.status_code == 200
        session_ids = {s["session_id"] for s in resp.json()["sessions"]}
        assert session_ids == {"s-a"}


# ---------------------------------------------------------------------------
# Integration: agent token generation with role + labels
# ---------------------------------------------------------------------------


class TestAgentTokenRbac:
    @staticmethod
    def _mock_store():
        store = MagicMock()
        store.connected = True
        store.set_token = AsyncMock()
        store.validate_token = AsyncMock(
            return_value=(True, ["ssh:connect"], {"scopes": ["ssh:connect"]})
        )
        store.disconnect = AsyncMock()
        return store

    def _patch_auth(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(
            "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
        )

    def test_create_token_with_role_and_labels(self, monkeypatch):
        self._patch_auth(monkeypatch)
        store = self._mock_store()
        with TestClient(app) as client:
            monkeypatch.setattr("app.state.agent_token_store", store)
            resp = client.post(
                "/api/agent/token",
                headers=MASTER_HEADERS,
                json={
                    "name": "bot",
                    "ttl_seconds": 3600,
                    "scopes": ["ssh:connect"],
                    "role": "viewer",
                    "labels": ["team:a"],
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["role"] == "viewer"
        assert data["labels"] == ["team:a"]
        store.set_token.assert_awaited_once()
        _, kwargs = store.set_token.call_args
        assert kwargs["role"] == "viewer"
        assert kwargs["labels"] == ["team:a"]

    def test_create_token_invalid_role_rejected(self, monkeypatch):
        self._patch_auth(monkeypatch)
        store = self._mock_store()
        with TestClient(app) as client:
            monkeypatch.setattr("app.state.agent_token_store", store)
            resp = client.post(
                "/api/agent/token",
                headers=MASTER_HEADERS,
                json={"name": "bot", "ttl_seconds": 3600, "role": "superadmin"},
            )
        assert resp.status_code == 400, resp.text

    def test_create_token_without_role_and_wildcard_scope_gets_admin(self, monkeypatch):
        self._patch_auth(monkeypatch)
        store = self._mock_store()
        store.validate_token = AsyncMock(
            return_value=(True, ["*"], {"scopes": ["*"]})
        )
        with TestClient(app) as client:
            monkeypatch.setattr("app.state.agent_token_store", store)
            resp = client.post(
                "/api/agent/token",
                headers=MASTER_HEADERS,
                json={"name": "bot", "ttl_seconds": 3600, "scopes": []},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] is None

    def test_legacy_wildcard_env_token_resolves_admin(self, monkeypatch):
        monkeypatch.setattr(settings, "agent_token", "legacy-token")
        monkeypatch.setattr(settings, "agent_token_scopes", ["*"])
        monkeypatch.setattr(settings, "agent_token_expires_at", None)

        from app.auth_middleware import is_agent_token_valid

        identity = _run(is_agent_token_valid(settings, "legacy-token"))
        assert identity is not None
        assert identity.role == "admin"

    def test_create_token_explicit_scopes_keep_no_role(self, monkeypatch):
        self._patch_auth(monkeypatch)
        store = self._mock_store()
        with TestClient(app) as client:
            monkeypatch.setattr("app.state.agent_token_store", store)
            resp = client.post(
                "/api/agent/token",
                headers=MASTER_HEADERS,
                json={"name": "bot", "ttl_seconds": 3600, "scopes": ["ssh:connect"]},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] is None

    def test_refresh_token_preserves_role_and_labels(self, monkeypatch):
        self._patch_auth(monkeypatch)
        store = self._mock_store()
        store.validate_token = AsyncMock(
            return_value=(
                True,
                ["ssh:connect"],
                {"scopes": ["ssh:connect"], "role": "viewer", "labels": ["team:a"]},
            )
        )
        with TestClient(app) as client:
            monkeypatch.setattr("app.state.agent_token_store", store)
            resp = client.post(
                "/api/agent/token/refresh",
                headers=MASTER_HEADERS,
                json={"token": "old-token", "ttl_seconds": 3600},
            )
        assert resp.status_code == 200, resp.text
        _, kwargs = store.set_token.call_args
        assert kwargs["role"] == "viewer"
        assert kwargs["labels"] == ["team:a"]


# ---------------------------------------------------------------------------
# JWT identities carry resolved roles
# ---------------------------------------------------------------------------


class TestJwtCarriesRole:
    def test_create_jwt_includes_admin_role(self):
        from app import user_auth

        token = user_auth.create_jwt(username="alice", user_id=1)
        payload = user_auth.verify_jwt(token)
        assert payload is not None
        assert payload["role"] == "admin"
        assert payload["type"] == "web-ui"

    def test_auth_check_jwt_identity_carries_role(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(
            "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
        )

        from app import user_auth

        token = user_auth.create_jwt(username="alice", user_id=1)
        with TestClient(app) as client:
            resp = client.get(
                "/api/ssh/sessions",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code in (200, 404, 401), resp.text
