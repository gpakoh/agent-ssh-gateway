"""Tests for MCP server AuthSettings configuration."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# server.py needs sys.path set up for its internal imports
EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))


@pytest.fixture(autouse=True)
def reset_env():
    """Ensure clean MCP_AUTH_MODE for each test that doesn't override it."""
    with patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"}, clear=False):
        import importlib

        import examples.mcp_server.server as srv

        importlib.reload(srv)
        yield


def test_auth_enabled_by_default():
    """Default MCP_AUTH_MODE=oauth should configure auth."""
    from examples.mcp_server.server import mcp

    assert mcp.settings.auth is not None
    assert mcp.settings.auth.client_registration_options.enabled is True


@patch.dict(os.environ, {"MCP_GATEWAY_TOOL_MODE": "mcp_client_write"}, clear=False)
def test_execute_argv_registered_in_live_write_server():
    """mcp_client_write bootstrap must register execute_argv for live MCP."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    names = {tool.name for tool in srv.mcp._tool_manager.list_tools()}
    assert "execute_argv" in names


@patch.dict(os.environ, {"MCP_GATEWAY_TOOL_MODE": "mcp_client_write"}, clear=False)
def test_supervisor_tools_registered_in_live_server():
    """mcp_client_write bootstrap must register both admin-only integration tools."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    names = {tool.name for tool in srv.mcp._tool_manager.list_tools()}
    assert "supervisor_integrate_file" in names
    assert "supervisor_recover_integrations" in names
    assert "gitea_create_pull_request" in names
    assert "gitea_merge_pull_request" in names


@patch.dict(os.environ, {"MCP_AUTH_MODE": "token", "MCP_PUBLIC_TOKEN": "test-token"})
def test_token_mode_initializes_provider():
    """Token mode initializes GatewayOAuthProvider with MCP_PUBLIC_TOKEN."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv._auth_provider is not None
    token = srv._auth_provider.verify_access_token("test-token")
    assert token is not None
    assert token.client_id == "mcp_static_client"


@patch.dict(os.environ, {"MCP_AUTH_MODE": "token", "MCP_PUBLIC_TOKEN": ""})
def test_token_mode_requires_token():
    """Token mode raises ValueError if MCP_PUBLIC_TOKEN is empty."""
    import importlib

    import examples.mcp_server.server as srv

    with pytest.raises(ValueError, match="MCP_PUBLIC_TOKEN is required"):
        importlib.reload(srv)


@patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"})
def test_oauth_provider_initialized_in_oauth_mode():
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv._auth_provider is not None


@patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"})
def test_oauth_mode_configures_auth():
    """In oauth mode, FastMCP auth is configured with provider and settings."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv.mcp.settings.auth is not None
    assert srv.mcp.settings.auth.client_registration_options.enabled is True
    assert "mcp:read" in (srv.mcp.settings.auth.client_registration_options.valid_scopes or [])


@patch.dict(
    os.environ,
    {
        "MCP_AUTH_MODE": "oauth",
        "MCP_EXTRA_TOKENS_JSON": '{"extra-token-1": "mcp_client_sfae"}',
    },
)
def test_extra_token_unknown_profile_fails_closed():
    """P0 audit finding: a typo'd extra-token profile must NOT resolve to
    any named profile's scopes (the old "fix" fell back to operator,
    which is itself fail-open -- operator still grants mcp:project/
    mcp:handoff/etc. nobody asked for). Startup must fail loudly instead,
    so a misconfigured MCP_EXTRA_TOKENS_JSON is caught immediately rather
    than silently under/over-privileging a token."""
    import importlib

    import examples.mcp_server.server as srv

    with pytest.raises(ValueError, match="Unknown access profile"):
        importlib.reload(srv)


@patch.dict(
    os.environ,
    {"MCP_AUTH_MODE": "oauth", "MCP_HEALTHCHECK_BEARER_TOKEN": "health-tok-1"},
)
def test_healthcheck_token_scoped_to_read_only():
    """Regression: MAJOR finding from a live security audit. The
    healthcheck bearer token -- whose entire purpose is a liveness probe --
    was registered with the full SUPPORTED_SCOPES set (admin/execute/docker
    included) and never expires. If it ever leaked, its blast radius was
    indistinguishable from a real operator credential. It only needs to
    call `health`, which requires just "mcp:read" (see tool_scopes.py).
    """
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv._auth_provider is not None
    token = srv._auth_provider.verify_access_token("health-tok-1")
    assert token is not None
    assert token.scopes == ["mcp:read"]
    assert "mcp:admin" not in token.scopes
    assert "mcp:execute" not in token.scopes
    assert "mcp:docker" not in token.scopes
    assert "mcp:project" not in token.scopes


class TestBuildPgDsn:
    """MAJOR audit finding: postgres_* tools were completely non-
    functional in the Docker deployment despite docker-compose.yml
    setting PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD directly as
    container env vars -- PG_DSN's only resolution path read a systemd-
    only host file (/etc/agent-mcp-postgres.env) that never exists inside
    a container, so PG_DSN stayed None forever there. Confirmed live
    against the running mcp-oauth container before this fix.
    """

    def test_builds_dsn_from_complete_vars(self):
        import examples.mcp_server.server as srv

        dsn = srv._build_pg_dsn(
            {
                "PGHOST": "mcp-postgres",
                "PGPORT": "5432",
                "PGDATABASE": "gateway",
                "PGUSER": "postgres",
                "PGPASSWORD": "s3cr3t",
            }
        )
        assert dsn is not None
        assert dsn.startswith("postgresql://postgres:s3cr3t@")
        assert "/gateway" in dsn

    def test_returns_none_when_password_missing(self):
        import examples.mcp_server.server as srv

        dsn = srv._build_pg_dsn(
            {
                "PGHOST": "mcp-postgres",
                "PGDATABASE": "gateway",
                "PGUSER": "postgres",
            }
        )
        assert dsn is None

    def test_returns_none_for_empty_vars(self):
        import examples.mcp_server.server as srv

        assert srv._build_pg_dsn({}) is None

    def test_url_encodes_special_characters_in_password(self):
        import examples.mcp_server.server as srv

        dsn = srv._build_pg_dsn(
            {
                "PGHOST": "mcp-postgres",
                "PGDATABASE": "gateway",
                "PGUSER": "postgres",
                "PGPASSWORD": "p@ss/word:1",
            }
        )
        assert dsn is not None
        assert "p@ss/word:1" not in dsn  # must be percent-encoded, not raw


class TestUnavailableToolReasons:
    """MAJOR audit finding: docker_*/resolve_library_id/query_docs/
    postgres_* were reported "enabled": True in tools_manifest even in a
    deployment missing docker/npx or without Postgres configured.
    """

    def test_marks_docker_tools_unavailable_when_docker_missing(self, monkeypatch):
        import examples.mcp_server.server as srv

        monkeypatch.setattr(srv.shutil, "which", lambda name: None)
        reasons = srv._unavailable_tool_reasons()
        assert "docker_ps" in reasons
        assert "docker_exec" in reasons  # mcp:docker:admin, not just mcp:docker

    def test_marks_docker_tools_unavailable_when_daemon_unreachable(self, monkeypatch):
        import examples.mcp_server.server as srv

        # CLI present, but neither the socket nor DOCKER_HOST is available
        # (mcp-server keeps the socket unmounted; mcp-oauth mounts it).
        monkeypatch.setattr(srv.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(srv.os, "environ", {})
        monkeypatch.setattr(srv.os.path, "exists", lambda path: False)
        reasons = srv._unavailable_tool_reasons()
        assert "docker_ps" in reasons
        assert "docker_exec" in reasons

    def test_marks_context7_tools_unavailable_when_npx_missing(self, monkeypatch):
        import examples.mcp_server.server as srv

        monkeypatch.setattr(srv.shutil, "which", lambda name: None)
        reasons = srv._unavailable_tool_reasons()
        assert "resolve_library_id" in reasons
        assert "query_docs" in reasons

    def test_marks_postgres_tools_unavailable_when_pg_dsn_none(self, monkeypatch):
        import examples.mcp_server.server as srv

        monkeypatch.setattr(srv, "PG_DSN", None)
        reasons = srv._unavailable_tool_reasons()
        assert "postgres_select" in reasons
        assert "postgres_health" in reasons

    def test_no_reasons_when_everything_present(self, monkeypatch):
        import examples.mcp_server.server as srv

        monkeypatch.setattr(srv.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(srv.os, "environ", {"DOCKER_HOST": "unix:///var/run/docker.sock"})
        monkeypatch.setattr(srv, "PG_DSN", "postgresql://u:p@h:5432/d")
        assert srv._unavailable_tool_reasons() == {}

    def test_unrelated_tools_never_marked_unavailable(self, monkeypatch):
        import examples.mcp_server.server as srv

        monkeypatch.setattr(srv.shutil, "which", lambda name: None)
        monkeypatch.setattr(srv, "PG_DSN", None)
        reasons = srv._unavailable_tool_reasons()
        assert "health" not in reasons
        assert "search_text" not in reasons
        assert "run_agent" not in reasons


@patch.dict(
    os.environ,
    {
        "MCP_AUTH_MODE": "oauth",
        "PGHOST": "mcp-postgres",
        "PGPORT": "5432",
        "PGDATABASE": "gateway",
        "PGUSER": "postgres",
        "PGPASSWORD": "s3cr3t",
    },
)
def test_pg_dsn_falls_back_to_env_vars_when_legacy_host_file_absent():
    """Integration-level check of the actual module wiring (not just the
    pure _build_pg_dsn function): with the legacy systemd host file
    absent -- the real case inside a Docker container -- module load must
    still resolve PG_DSN from plain PG* env vars, matching what
    docker-compose.yml's mcp-oauth/mcp-server services already set.
    """
    import importlib

    with patch("os.path.exists", return_value=False):
        import examples.mcp_server.server as srv

        importlib.reload(srv)

    assert srv.PG_DSN is not None
    assert srv.PG_DSN.startswith("postgresql://postgres:s3cr3t@")
