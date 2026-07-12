"""Tests for expanded /health endpoint with build metadata."""

from starlette.testclient import TestClient

from app.main import app


def test_health_includes_build_metadata():
    """GET /health must include build_sha, build_time, started_at, version."""
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "build_sha" in data
    assert "build_time" in data
    assert "started_at" in data
    assert "version" in data
    assert isinstance(data["build_sha"], str)
    assert isinstance(data["version"], str)
    assert len(data["version"]) > 0


def test_health_existing_fields_unchanged():
    """Original health fields must still be present."""
    with TestClient(app) as client:
        resp = client.get("/health")
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert isinstance(data["redis"], bool)
    assert isinstance(data["ready"], bool)
