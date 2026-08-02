"""Tests for app.config.Settings construction robustness."""

from __future__ import annotations

from app.config import Settings


def test_settings_ignores_unrelated_env_vars(monkeypatch):
    """Settings must not crash when constructed in a process whose
    environment carries unrelated variables (e.g. the MCP connector
    systemd service's own GATEWAY_*/MCP_* env, which shares the same
    filesystem/venv as app.config but has nothing to do with the main
    gateway's own settings schema).

    Regression: model_config previously had no `extra` override, so
    pydantic-settings' default (forbid) raised ValidationError for any
    env var it didn't recognize — this broke every app.config import
    inside examples/mcp_server/server.py's workspace_file_write/edit/
    apply_patch tools when run under the MCP service's environment.
    """
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://127.0.0.1:8085")
    monkeypatch.setenv("GATEWAY_API_KEY", "unrelated-mcp-key")
    monkeypatch.setenv("GATEWAY_SSH_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
    monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
    monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "off")
    monkeypatch.setenv("WORKSPACE_READONLY", "true")

    cfg = Settings()
    assert cfg.workspace_readonly is True


def test_settings_rejects_placeholder_secrets(monkeypatch):
    """T79.13: Settings() refuses placeholder 'change-me' secrets."""
    import pytest

    monkeypatch.setenv("API_KEY", "change-me-generate-long-random-api-key")
    monkeypatch.setenv("AGENT_TOKEN", "change-me-generate-long-random-agent-token")
    monkeypatch.setenv("ENCRYPTION_KEY", "change-me-generate-fernet-key")
    monkeypatch.setenv("JWT_SECRET", "change-me-generate-long-random-jwt-secret")
    monkeypatch.setenv("SETUP_TOKEN", "change-me-generate-long-random-setup-token")

    with pytest.raises(ValueError, match="Placeholder secret"):
        Settings()


def test_settings_allows_real_secrets(monkeypatch):
    """Real-looking secrets construct fine."""
    monkeypatch.setenv("API_KEY", "live-api-key-abc123")
    monkeypatch.setenv("AGENT_TOKEN", "live-agent-token-xyz789")
    monkeypatch.setenv("ENCRYPTION_KEY", "live-encryption-key")
    monkeypatch.setenv("JWT_SECRET", "live-jwt-secret")
    monkeypatch.setenv("SETUP_TOKEN", "live-setup-token")

    cfg = Settings()
    assert cfg.api_key == "live-api-key-abc123"
