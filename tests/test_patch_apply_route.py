"""Tests for POST /api/projects/{project}/apply-patch endpoint."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from app.config import settings


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def _patch_base(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
    monkeypatch.setattr("app.auth_middleware.is_ip_allowed", lambda ip, nets: True)


def _auth_headers():
    return {"X-API-Key": "secret-42"}


def _make_session_mock():
    mgr = MagicMock()
    mgr.get_session = AsyncMock(
        return_value=MagicMock(
            owner_type="master",
            owner_name="admin",
            owner_token_fingerprint="fp",
            is_connected=MagicMock(return_value=True),
        )
    )
    return mgr


def _override_manager(client, mock_mgr):
    from app import state as _app_state

    _app_state.manager = mock_mgr


def test_apply_patch_requires_auth(client, monkeypatch):
    _patch_base(monkeypatch)
    resp = client.post(
        "/api/projects/myproject/apply-patch",
        json={
            "session_id": "x",
            "patch": "--- a/f\n+++ b/f\n",
            "expected_hashes": {},
        },
    )
    assert resp.status_code == 401


def test_apply_patch_empty_patch_rejected(client, monkeypatch):
    _patch_base(monkeypatch)
    resp = client.post(
        "/api/projects/myproject/apply-patch",
        json={
            "session_id": "x",
            "patch": "",
            "expected_hashes": {},
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_apply_patch_session_not_found(client, monkeypatch):
    _patch_base(monkeypatch)
    mock_mgr = MagicMock()
    mock_mgr.get_session = AsyncMock(return_value=None)
    _override_manager(client, mock_mgr)

    resp = client.post(
        "/api/projects/myproject/apply-patch",
        json={
            "session_id": "nonexistent",
            "project": "myproject",
            "patch": "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n",
            "expected_hashes": {},
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
