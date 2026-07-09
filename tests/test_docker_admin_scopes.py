"""Tests for docker admin scope enforcement and tool registration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server"))

from tool_scopes import (
    ACCESS_PROFILES,
    TOOL_SCOPES,
    get_required_scopes,
    has_required_scope,
)


ADMIN_TOOLS = ["docker_exec", "docker_run", "docker_rmi", "docker_volume_rm"]


def test_admin_tools_require_admin_scope():
    for tool in ADMIN_TOOLS:
        scopes = get_required_scopes(tool)
        assert "mcp:docker:admin" in scopes, f"{tool} should require mcp:docker:admin"
        assert "mcp:docker" not in scopes, f"{tool} should not require mcp:docker"


def test_admin_tools_fail_with_only_docker_scope():
    token_scopes = ["mcp:docker"]
    for tool in ADMIN_TOOLS:
        assert not has_required_scope(token_scopes, tool), (
            f"{tool} should be rejected with only mcp:docker"
        )


def test_admin_tools_pass_with_combined_scopes():
    token_scopes = ["mcp:docker", "mcp:docker:admin"]
    for tool in ADMIN_TOOLS:
        assert has_required_scope(token_scopes, tool), (
            f"{tool} should pass with both scopes"
        )


def test_admin_tools_pass_with_admin_only():
    """Flat scope model: admin-only token can call admin but NOT regular docker tools."""
    token_scopes = ["mcp:docker:admin"]
    for tool in ADMIN_TOOLS:
        assert has_required_scope(token_scopes, tool), (
            f"{tool} should pass with only mcp:docker:admin"
        )


def test_infra_profile_has_admin_scope():
    infra = ACCESS_PROFILES.get("infra", [])
    assert "mcp:docker" in infra
    assert "mcp:docker:admin" in infra


def test_full_profile_has_admin_scope():
    full = ACCESS_PROFILES.get("full", [])
    assert "mcp:docker" in full
    assert "mcp:docker:admin" in full


def test_viewer_no_docker_scopes():
    viewer = ACCESS_PROFILES.get("viewer", [])
    assert "mcp:docker" not in viewer
    assert "mcp:docker:admin" not in viewer


def test_admin_scope_does_not_imply_docker():
    """Flat scope: admin without docker cannot call regular docker tools."""
    token_scopes = ["mcp:docker:admin"]
    assert has_required_scope(token_scopes, "docker_ps") is False


def test_admin_tools_unknown_tool_fail_closed():
    scopes = get_required_scopes("nonexistent_tool")
    assert scopes == ["mcp:admin"]
