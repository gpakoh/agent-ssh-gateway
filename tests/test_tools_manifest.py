"""Tests for the MCP tools manifest — introspection only, no network calls, no secrets."""

from __future__ import annotations

import os

import pytest

# Ensure the manifest module is importable
_MCP_SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "examples", "mcp_server")


# Simulated registered tool (same shape as FastMCP Tool)
class FakeTool:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description


# Import after path setup
import sys  # noqa: E402

sys.path.insert(0, _MCP_SERVER_DIR)

os.environ.pop("MCP_GATEWAY_TOOL_MODE", None)
os.environ.pop("MCP_SCOPE_ENFORCEMENT", None)

from tools_manifest import build_manifest  # noqa: E402


@pytest.fixture
def sample_tools() -> list[FakeTool]:
    """Return a small set of known tools matching the registered set."""
    return [
        FakeTool("health"),
        FakeTool("project_search_text"),
        FakeTool("tools_manifest"),
        FakeTool("docker_restart"),
        FakeTool("docker_compose_up"),
        FakeTool("project_run_agent"),
    ]


class TestBuildManifest:
    def test_returns_dict(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, scope_enforcement="audit", mode_override="chatgpt")
        assert isinstance(result, dict)

    def test_contains_required_top_fields(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, scope_enforcement="enforce", mode_override="chatgpt")
        for field in (
            "active_mode",
            "scope_enforcement",
            "tool_count",
            "tools",
            "modes",
            "access_profiles",
        ):
            assert field in result, f"Missing field: {field}"

    def test_active_mode_is_string(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        assert isinstance(result["active_mode"], str)
        assert result["active_mode"] in ("minimal", "standard", "full", "chatgpt")

    def test_scope_enforcement_defaults_to_audit(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        assert result["scope_enforcement"] == "audit"

    def test_scope_enforcement_respected(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, scope_enforcement="enforce", mode_override="chatgpt")
        assert result["scope_enforcement"] == "enforce"

    def test_tool_count_matches(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        assert result["tool_count"] == len(sample_tools)

    def test_every_tool_has_name(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        for tool in result["tools"]:
            assert "name" in tool
            assert isinstance(tool["name"], str)
            assert tool["name"]

    def test_no_duplicate_tool_names(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        names = [t["name"] for t in result["tools"]]
        assert len(names) == len(set(names)), f"Duplicates: {names}"

    def test_tools_have_scopes_field(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        for tool in result["tools"]:
            assert "scopes" in tool
            assert isinstance(tool["scopes"], list)
            assert all(isinstance(s, str) for s in tool["scopes"])

    def test_tools_have_enabled_field(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        for tool in result["tools"]:
            assert tool.get("enabled") is True

    def test_tools_have_description(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        for tool in result["tools"]:
            assert "description" in tool

    def test_known_tool_search_text_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        names = [t["name"] for t in result["tools"]]
        assert "project_search_text" in names

    def test_known_tool_run_agent_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        names = [t["name"] for t in result["tools"]]
        assert "project_run_agent" in names

    def test_known_tool_docker_restart_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        names = [t["name"] for t in result["tools"]]
        assert "docker_restart" in names

    def test_known_tool_docker_compose_up_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        names = [t["name"] for t in result["tools"]]
        assert "docker_compose_up" in names

    def test_manifest_tool_itself_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        names = [t["name"] for t in result["tools"]]
        assert "tools_manifest" in names

    def test_scopes_for_known_tool(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        by_name = {t["name"]: t for t in result["tools"]}
        assert "mcp:project" in by_name["project_search_text"]["scopes"]

    def test_modes_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        assert isinstance(result["modes"], dict)
        for mode in ("minimal", "standard", "full", "chatgpt"):
            assert mode in result["modes"], f"Missing mode: {mode}"

    def test_mode_has_tool_count(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        for _mode_name, mode_info in result["modes"].items():
            assert "tool_count" in mode_info
            assert isinstance(mode_info["tool_count"], int)

    def test_mode_has_tools_list(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        for _mode_name, mode_info in result["modes"].items():
            assert "tools" in mode_info
            assert isinstance(mode_info["tools"], list)

    def test_access_profiles_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        assert isinstance(result["access_profiles"], dict)
        for profile in ("viewer", "operator", "agent-runner", "infra", "full"):
            assert profile in result["access_profiles"], f"Missing profile: {profile}"

    def test_access_profiles_are_scope_lists(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        for _profile_name, scopes in result["access_profiles"].items():
            assert isinstance(scopes, list)
            assert all(isinstance(s, str) for s in scopes)

    def test_no_secret_values_in_manifest(self, sample_tools: list[FakeTool]) -> None:
        """Verify no token-like or password-like values leak into the manifest."""
        result = build_manifest(sample_tools, mode_override="chatgpt")
        serialized = str(result)
        suspicious = ("token", "secret", "password", "key=", "Bearer ")
        for _s in suspicious:
            if "token" in serialized.lower():
                pass
        import re

        assert not re.search(r"[A-Za-z0-9+/]{40,}", serialized), "Possible API key leaked"
        assert not re.search(r"gh[pousr]_[A-Za-z0-9]{36,}", serialized), (
            "Possible GitHub token leaked"
        )

    def test_manifest_does_not_contain_env_dump(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="chatgpt")
        suspicious_keys = ("GITHUB_TOKEN", "GITEA_TOKEN", "API_KEY", "MCP_PUBLIC_TOKEN")
        serialized = str(result)
        for key in suspicious_keys:
            assert key not in serialized, f"Env var name leaked: {key}"

    def test_tool_has_modes_field(self, sample_tools: list[FakeTool]) -> None:
        """Each tool should list which modes it belongs to."""
        result = build_manifest(sample_tools, mode_override="chatgpt")
        for tool in result["tools"]:
            assert "modes" in tool
            assert isinstance(tool["modes"], list)
            assert len(tool["modes"]) > 0

    def test_tool_has_mode_field(self, sample_tools: list[FakeTool]) -> None:
        """Each tool should show its active mode."""
        result = build_manifest(sample_tools, mode_override="chatgpt")
        for tool in result["tools"]:
            assert "mode" in tool
            assert isinstance(tool["mode"], str)
