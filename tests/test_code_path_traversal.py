"""Regression tests: POST /api/code/search and /api/code/insert must
validate `path` the same way every other file-editing route does.

CodeIntelligence.search_code() builds and runs its own grep command
directly (bypassing command_policy entirely), and find_insertion_point()
reads through FileEditor.read_file() while code_insert's handler then
writes through FileEditor.edit_file() -- neither does any path validation
of its own, just shell-quoting/escaping. Unlike every route in
routers/files.py, routers/code.py never called validate_path() on `path`,
so a caller could point at any absolute path -- including FORBIDDEN_PATHS
like /etc/passwd, /root/.ssh, /proc, /sys, /dev, /boot -- and search or
overwrite files there, entirely outside command_policy.
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


class TestCodeSearchPathTraversal:
    @pytest.mark.parametrize("path", ["/etc/passwd", "/proc", "/sys", "/dev", "/boot"])
    def test_forbidden_path_rejected(self, client, path):
        resp = client.post(
            "/api/code/search",
            json={"session_id": "fake", "path": path, "query": "root"},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
        assert "forbidden" in resp.json()["detail"]["message"].lower()

    def test_traversal_rejected(self, client):
        resp = client.post(
            "/api/code/search",
            json={"session_id": "fake", "path": "/home/user/project/../../etc", "query": "root"},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400


class TestCodeInsertPathTraversal:
    @pytest.mark.parametrize("path", ["/etc/passwd", "/proc", "/sys", "/dev", "/boot"])
    def test_forbidden_path_rejected(self, client, path):
        resp = client.post(
            "/api/code/insert",
            json={
                "context_id": "fake",
                "path": path,
                "instruction": "add a health endpoint",
            },
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
        assert "forbidden" in resp.json()["detail"]["message"].lower()

    def test_traversal_rejected(self, client):
        resp = client.post(
            "/api/code/insert",
            json={
                "context_id": "fake",
                "path": "/home/user/project/../../root/.ssh",
                "instruction": "add a health endpoint",
            },
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
