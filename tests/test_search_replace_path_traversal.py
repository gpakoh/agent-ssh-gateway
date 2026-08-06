"""Regression tests: POST /api/search/global and /api/replace/global must
validate `path` the same way every other file-editing route does.

GlobalSearchReplace.search()/replace() build and run their own grep/find
commands directly (bypassing command_policy entirely, like snapshot_manager.py
did), and replace() writes through FileEditor.write_file() -- also with no
path validation of its own. Unlike every route in routers/files.py, the
search/replace router never called validate_path() on `path`, so a caller
could point at any absolute path -- including FORBIDDEN_PATHS like /etc,
/proc, /sys, /dev, /boot -- and search or overwrite files there. Both routes
are master-key gated, but validate_path()'s forbidden-path denylist is meant
to apply uniformly regardless of privilege level, matching every other
file-editing route in this codebase.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client():
    with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


@pytest.fixture(autouse=True)
def _auth_bypass(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-api-key")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


class TestGlobalSearchPathTraversal:
    @pytest.mark.parametrize("path", ["/etc/passwd", "/proc", "/sys", "/dev", "/boot"])
    def test_forbidden_path_rejected(self, client, path):
        resp = client.post(
            "/api/search/global",
            json={"session_id": "fake", "path": path, "query": "root"},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
        assert "forbidden" in resp.json()["detail"]["message"].lower()

    def test_traversal_rejected(self, client):
        resp = client.post(
            "/api/search/global",
            json={"session_id": "fake", "path": "/home/user/project/../../etc", "query": "root"},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400


class TestGlobalReplacePathTraversal:
    @pytest.mark.parametrize("path", ["/etc/passwd", "/proc", "/sys", "/dev", "/boot"])
    def test_forbidden_path_rejected(self, client, path):
        resp = client.post(
            "/api/replace/global",
            json={
                "session_id": "fake",
                "path": path,
                "search": "root:x:0:0",
                "replace": "pwned:x:0:0",
                "dry_run": False,
            },
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
        assert "forbidden" in resp.json()["detail"]["message"].lower()

    def test_forbidden_path_rejected_even_for_dry_run(self, client):
        """dry_run must not bypass path validation -- it only skips the
        readonly-mode gate later in the handler."""
        resp = client.post(
            "/api/replace/global",
            json={
                "session_id": "fake",
                "path": "/etc/passwd",
                "search": "root:x:0:0",
                "replace": "pwned:x:0:0",
                "dry_run": True,
            },
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400

    def test_traversal_rejected(self, client):
        resp = client.post(
            "/api/replace/global",
            json={
                "session_id": "fake",
                "path": "/home/user/project/../../root/.ssh",
                "search": "old-key",
                "replace": "attacker-key",
                "dry_run": False,
            },
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
