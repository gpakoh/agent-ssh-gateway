"""Tests for docker admin scope enforcement and tool registration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server"))

from tool_scopes import ACCESS_PROFILES, get_required_scopes, has_required_scope

ADMIN_TOOLS = ["docker_exec", "docker_run", "docker_rmi", "docker_volume_rm"]


def test_admin_tools_require_admin_scope():
    for tool in ADMIN_TOOLS:
        scopes = get_required_scopes(tool)
        assert "mcp:docker:admin" in scopes, f"{tool} should require mcp:docker:admin"
        assert "mcp:docker" not in scopes, f"{tool} should not require mcp:docker"


def test_admin_tools_reject_docker_only_scope():
    docker_only = {"mcp:docker"}
    for tool in ADMIN_TOOLS:
        assert not has_required_scope(list(docker_only), tool), (
            f"{tool} should reject mcp:docker-only tokens"
        )


def test_admin_tools_accept_admin_scope():
    admin_scope = {"mcp:docker:admin"}
    for tool in ADMIN_TOOLS:
        assert has_required_scope(list(admin_scope), tool), f"{tool} should accept mcp:docker:admin"


def test_admin_tools_accept_full_scope():
    full_scopes = {"mcp:read", "mcp:docker:admin", "mcp:project", "mcp:execute"}
    for tool in ADMIN_TOOLS:
        assert has_required_scope(list(full_scopes), tool), f"{tool} should accept full scopes"


def test_non_admin_tools_do_not_require_admin():
    non_admin = [
        "docker_ps",
        "docker_images",
        "docker_inspect",
        "docker_logs",
        "docker_stats",
        "docker_compose_ps",
        "docker_compose_services",
        "docker_start",
        "docker_stop",
        "docker_restart",
        "docker_compose_up",
        "docker_compose_restart",
        "docker_compose_build",
        "docker_compose_logs",
        "docker_rm",
        "docker_compose_down",
        "docker_prune",
        "docker_confirm",
        "docker_pending_actions",
    ]
    for tool in non_admin:
        scopes = get_required_scopes(tool)
        assert "mcp:docker:admin" not in scopes, f"{tool} should NOT require mcp:docker:admin"


def test_admin_profile_includes_admin_scope():
    assert "mcp:docker:admin" in ACCESS_PROFILES["infra"]
    assert "mcp:docker:admin" in ACCESS_PROFILES["full"]


def test_viewer_profile_excludes_admin_scope():
    assert "mcp:docker:admin" not in ACCESS_PROFILES["viewer"]
    assert "mcp:docker" not in ACCESS_PROFILES["viewer"]


def test_operator_profile_excludes_admin_scope():
    assert "mcp:docker:admin" not in ACCESS_PROFILES["operator"]
    assert "mcp:docker" not in ACCESS_PROFILES["operator"]


def test_agent_runner_profile_excludes_admin_scope():
    assert "mcp:docker:admin" not in ACCESS_PROFILES["agent-runner"]
    assert "mcp:docker" not in ACCESS_PROFILES["agent-runner"]


def test_admin_scope_does_not_imply_docker():
    token_scopes = ["mcp:docker:admin"]
    assert has_required_scope(token_scopes, "docker_ps") is False


def test_admin_tools_unknown_tool_fail_closed():
    scopes = get_required_scopes("nonexistent_tool")
    assert scopes == ["mcp:admin"]
