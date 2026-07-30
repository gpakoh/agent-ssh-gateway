"""Tests for admin approval decision endpoint (ASK mode)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.policy_ask import create_approval_request


def _headers(api_key: str = "test-api-key-12345") -> dict[str, str]:
    return {"X-API-Key": api_key}


def _get_app():
    from app.main import app
    return app


def _patch_auth(monkeypatch):
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "")
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")


def _client(monkeypatch):
    _patch_auth(monkeypatch)
    return TestClient(_get_app(), raise_server_exceptions=False)


class TestApprovalEndpointAuth:
    def test_rejects_unauthenticated(self, monkeypatch):
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/approval/decision",
                json={"approval_id": "abc", "decision": "allow"},
            )
        assert resp.status_code == 401

    def test_rejects_invalid_decision(self, monkeypatch):
        aid = create_approval_request("rm -rf /", "default", "profile", "test").approval_id
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/approval/decision",
                json={"approval_id": aid, "decision": "invalid"},
                headers=_headers(),
            )
        assert resp.status_code == 422

    def test_rejects_unknown_approval_id(self, monkeypatch):
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/approval/decision",
                json={"approval_id": "nonexistent", "decision": "allow"},
                headers=_headers(),
            )
        assert resp.status_code == 404


class TestApprovalFlow:
    def test_approve_allow(self, monkeypatch):
        req = create_approval_request("docker rm -f", "ops", "profile", "dangerous")
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/approval/decision",
                json={"approval_id": req.approval_id, "decision": "allow", "operator": "admin"},
                headers=_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["approved"] is True
        assert body["decision"] == "allow"
        assert body["command"] == "docker rm -f"

    def test_approve_deny(self, monkeypatch):
        req = create_approval_request("rm -rf /", "readonly", "heredoc", "dangerous")
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/approval/decision",
                json={"approval_id": req.approval_id, "decision": "deny"},
                headers=_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["approved"] is False
        assert body["decision"] == "deny"

    def test_double_approve_rejected(self, monkeypatch):
        req = create_approval_request("reboot", "ops", "profile", "test")
        with _client(monkeypatch) as c:
            # First approve
            r1 = c.post(
                "/api/admin/approval/decision",
                json={"approval_id": req.approval_id, "decision": "allow"},
                headers=_headers(),
            )
            assert r1.status_code == 200
            # Second approve should fail (already decided)
            r2 = c.post(
                "/api/admin/approval/decision",
                json={"approval_id": req.approval_id, "decision": "allow"},
                headers=_headers(),
            )
            assert r2.status_code == 409

    def test_approval_expired(self, monkeypatch):
        from app.policy_ask import _APPROVAL_TTL_S

        _orig_ttl = _APPROVAL_TTL_S
        import app.policy_ask as pa

        pa._APPROVAL_TTL_S = -1  # force immediate expiry
        try:
            req = create_approval_request("test cmd", "default", None, "expired")
            with _client(monkeypatch) as c:
                resp = c.post(
                    "/api/admin/approval/decision",
                    json={"approval_id": req.approval_id, "decision": "allow"},
                    headers=_headers(),
                )
            assert resp.status_code == 404
        finally:
            pa._APPROVAL_TTL_S = _orig_ttl
