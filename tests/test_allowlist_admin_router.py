"""Tests for the admin allowlist management endpoint.

Before this router existed, app/allowlist.py's Allowlist.add() had zero
callers anywhere in the running application — Gate 0 in
evaluate_command_policy() always checked an allowlist that could never
have an entry in it. These tests exercise the real REST surface an
operator now uses to populate it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.allowlist import get_allowlist, reset_allowlist
from app.config import settings


def _headers(api_key: str = "test-api-key-12345") -> dict[str, str]:
    return {"X-API-Key": api_key}


def _patch_auth(monkeypatch):
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "")
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")


def _client(monkeypatch):
    _patch_auth(monkeypatch)
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def _reset(monkeypatch):
    reset_allowlist()


class TestAllowlistAddEndpoint:
    def test_rejects_unauthenticated(self, monkeypatch):
        _reset(monkeypatch)
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/allowlist",
                json={"layer": "system", "selector_type": "exact", "selector_value": "ls"},
            )
        assert resp.status_code == 401

    def test_rejects_agent_token(self, monkeypatch):
        """Only master key may add allowlist entries — an entry here
        bypasses every command policy gate, so a narrower-scoped agent
        token must never be able to add one."""
        _reset(monkeypatch)
        monkeypatch.setattr(settings, "agent_token", "agent-secret")
        monkeypatch.setattr(settings, "agent_token_scopes", ["ssh:execute"])
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/allowlist",
                json={"layer": "system", "selector_type": "exact", "selector_value": "ls"},
                headers=_headers("agent-secret"),
            )
        assert resp.status_code == 401

    def test_add_valid_entry(self, monkeypatch):
        _reset(monkeypatch)
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/allowlist",
                json={
                    "layer": "system",
                    "selector_type": "exact",
                    "selector_value": "systemctl status nginx",
                    "reason": "read-only status check",
                },
                headers=_headers(),
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["layer"] == "system"
        assert body["selector_value"] == "systemctl status nginx"
        assert body["reason"] == "read-only status check"
        assert get_allowlist().check("systemctl status nginx") is not None

    def test_rejects_invalid_layer(self, monkeypatch):
        _reset(monkeypatch)
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/allowlist",
                json={"layer": "bogus", "selector_type": "exact", "selector_value": "ls"},
                headers=_headers(),
            )
        assert resp.status_code == 422

    def test_rejects_invalid_selector_type(self, monkeypatch):
        _reset(monkeypatch)
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/allowlist",
                json={"layer": "system", "selector_type": "bogus", "selector_value": "ls"},
                headers=_headers(),
            )
        assert resp.status_code == 422

    def test_rejects_invalid_regex(self, monkeypatch):
        _reset(monkeypatch)
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/allowlist",
                json={"layer": "system", "selector_type": "regex", "selector_value": "("},
                headers=_headers(),
            )
        assert resp.status_code == 422


class TestAllowlistListEndpoint:
    def test_list_all(self, monkeypatch):
        _reset(monkeypatch)
        get_allowlist().add("system", "exact", "ls")
        get_allowlist().add("agent", "exact", "pwd")
        with _client(monkeypatch) as c:
            resp = c.get("/api/admin/allowlist", headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2

    def test_list_filtered_by_layer(self, monkeypatch):
        _reset(monkeypatch)
        get_allowlist().add("system", "exact", "ls")
        get_allowlist().add("agent", "exact", "pwd")
        with _client(monkeypatch) as c:
            resp = c.get("/api/admin/allowlist", params={"layer": "system"}, headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["entries"][0]["selector_value"] == "ls"


class TestAllowlistRemoveEndpoint:
    def test_remove_existing(self, monkeypatch):
        _reset(monkeypatch)
        entry = get_allowlist().add("system", "exact", "ls")
        with _client(monkeypatch) as c:
            resp = c.delete(f"/api/admin/allowlist/{entry.id}", headers=_headers())
        assert resp.status_code == 200
        assert resp.json()["removed"] is True
        assert get_allowlist().check("ls") is None

    def test_remove_missing_returns_404(self, monkeypatch):
        _reset(monkeypatch)
        with _client(monkeypatch) as c:
            resp = c.delete("/api/admin/allowlist/nonexistent", headers=_headers())
        assert resp.status_code == 404


class TestAllowlistClearEndpoint:
    def test_clear_single_layer(self, monkeypatch):
        _reset(monkeypatch)
        get_allowlist().add("system", "exact", "ls")
        get_allowlist().add("agent", "exact", "pwd")
        with _client(monkeypatch) as c:
            resp = c.delete("/api/admin/allowlist", params={"layer": "system"}, headers=_headers())
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 1
        assert get_allowlist().check("ls") is None
        assert get_allowlist().check("pwd") is not None

    def test_clear_all(self, monkeypatch):
        _reset(monkeypatch)
        get_allowlist().add("system", "exact", "ls")
        get_allowlist().add("agent", "exact", "pwd")
        with _client(monkeypatch) as c:
            resp = c.delete("/api/admin/allowlist", headers=_headers())
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 2
