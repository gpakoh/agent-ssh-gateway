"""Tests for gateway status rendering."""

from __future__ import annotations

from app.notifier.status import render_health_status


class TestRenderHealthStatus:
    def test_ok_status(self):
        health = {
            "version": "0.1.37a0",
            "status": "ok",
            "ready": True,
            "redis": True,
            "postgres": True,
            "persistent_sessions": True,
        }
        text = render_health_status(health)
        assert "✅" in text
        assert "0.1.37a0" in text
        assert "redis: 🟢" in text

    def test_degraded_status(self):
        health = {
            "version": "0.1.37a0",
            "status": "degraded",
            "ready": False,
            "redis": False,
            "postgres": True,
        }
        text = render_health_status(health)
        assert "⚠️" in text
        assert "redis: 🔴" in text
        assert "ready: 🔴" in text

    def test_missing_fields(self):
        health = {"version": "0.1.37a0"}
        text = render_health_status(health)
        assert "0.1.37a0" in text
        assert "redis" not in text
        assert "postgres" not in text

    def test_no_secrets(self):
        health = {
            "version": "0.1.37a0",
            "status": "ok",
            "api_key": "super-secret-key-123",
            "password": "hunter2",
        }
        text = render_health_status(health)
        assert "super-secret-key-123" not in text
        assert "hunter2" not in text

    def test_readonly_field(self):
        health = {
            "version": "0.1.37a0",
            "status": "ok",
            "readonly": True,
        }
        text = render_health_status(health)
        assert "readonly: 🟢" in text

    def test_mode_field(self):
        health = {
            "version": "0.1.37a0",
            "status": "ok",
            "mode": "enforce",
        }
        text = render_health_status(health)
        assert "mode: <code>enforce</code>" in text

    def test_empty_health(self):
        health = {}
        text = render_health_status(health)
        assert "Gateway Status" in text
        assert "unknown" in text
