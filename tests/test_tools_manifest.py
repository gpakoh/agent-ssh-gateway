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
        FakeTool("search_text"),
        FakeTool("tools_manifest"),
        FakeTool("docker_restart"),
        FakeTool("docker_compose_up"),
        FakeTool("run_agent"),
    ]


class TestBuildManifest:
    def test_returns_dict(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, scope_enforcement="audit", mode_override="mcp_client")
        assert isinstance(result, dict)

    def test_contains_required_top_fields(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, scope_enforcement="enforce", mode_override="mcp_client")
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
        result = build_manifest(sample_tools, mode_override="mcp_client")
        assert isinstance(result["active_mode"], str)
        assert result["active_mode"] in ("minimal", "standard", "full", "mcp_client")

    def test_scope_enforcement_defaults_to_audit(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        assert result["scope_enforcement"] == "audit"

    def test_scope_enforcement_respected(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, scope_enforcement="enforce", mode_override="mcp_client")
        assert result["scope_enforcement"] == "enforce"

    def test_tool_count_matches(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        assert result["tool_count"] == len(sample_tools)

    def test_every_tool_has_name(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for tool in result["tools"]:
            assert "name" in tool
            assert isinstance(tool["name"], str)
            assert tool["name"]

    def test_no_duplicate_tool_names(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        names = [t["name"] for t in result["tools"]]
        assert len(names) == len(set(names)), f"Duplicates: {names}"

    def test_tools_have_scopes_field(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for tool in result["tools"]:
            assert "scopes" in tool
            assert isinstance(tool["scopes"], list)
            assert all(isinstance(s, str) for s in tool["scopes"])

    def test_tools_have_enabled_field(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for tool in result["tools"]:
            assert tool.get("enabled") is True

    def test_tools_have_description(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for tool in result["tools"]:
            assert "description" in tool

    def test_known_tool_search_text_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        names = [t["name"] for t in result["tools"]]
        assert "search_text" in names

    def test_known_tool_run_agent_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        names = [t["name"] for t in result["tools"]]
        assert "run_agent" in names

    def test_known_tool_docker_restart_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        names = [t["name"] for t in result["tools"]]
        assert "docker_restart" in names

    def test_known_tool_docker_compose_up_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        names = [t["name"] for t in result["tools"]]
        assert "docker_compose_up" in names

    def test_manifest_tool_itself_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        names = [t["name"] for t in result["tools"]]
        assert "tools_manifest" in names

    def test_scopes_for_known_tool(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        by_name = {t["name"]: t for t in result["tools"]}
        assert "mcp:project" in by_name["search_text"]["scopes"]

    def test_modes_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        assert isinstance(result["modes"], dict)
        for mode in ("minimal", "standard", "full", "mcp_client"):
            assert mode in result["modes"], f"Missing mode: {mode}"

    def test_mode_has_tool_count(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for _mode_name, mode_info in result["modes"].items():
            assert "tool_count" in mode_info
            assert isinstance(mode_info["tool_count"], int)

    def test_mode_has_tools_list(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for _mode_name, mode_info in result["modes"].items():
            assert "tools" in mode_info
            assert isinstance(mode_info["tools"], list)

    def test_access_profiles_present(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        assert isinstance(result["access_profiles"], dict)
        for profile in ("viewer", "operator", "agent-runner", "infra", "full"):
            assert profile in result["access_profiles"], f"Missing profile: {profile}"

    def test_access_profiles_are_scope_lists(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for _profile_name, scopes in result["access_profiles"].items():
            assert isinstance(scopes, list)
            assert all(isinstance(s, str) for s in scopes)

    def test_no_secret_values_in_manifest(self, sample_tools: list[FakeTool]) -> None:
        """Verify no token-like or password-like values leak into the manifest."""
        result = build_manifest(sample_tools, mode_override="mcp_client")
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
        result = build_manifest(sample_tools, mode_override="mcp_client")
        suspicious_keys = ("GITHUB_TOKEN", "GITEA_TOKEN", "API_KEY", "MCP_PUBLIC_TOKEN")
        serialized = str(result)
        for key in suspicious_keys:
            assert key not in serialized, f"Env var name leaked: {key}"

    def test_tool_has_modes_field(self, sample_tools: list[FakeTool]) -> None:
        """Each tool should list which modes it belongs to."""
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for tool in result["tools"]:
            assert "modes" in tool
            assert isinstance(tool["modes"], list)
            assert len(tool["modes"]) > 0

    def test_tool_has_mode_field(self, sample_tools: list[FakeTool]) -> None:
        """Each tool should show its active mode."""
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for tool in result["tools"]:
            assert "mode" in tool
            assert isinstance(tool["mode"], str)


class TestManifestModesReflectSafeModeFiltering:
    """modes["mcp_client"] must describe what should_register_tool() would
    actually register, not the raw TOOL_NAMES_BY_MODE["mcp_client"] set --
    the two diverge whenever MCP_CLIENT_SAFE_MODE=true (the recommended
    and, in the real deployment, the actually-configured setting), since
    should_register_tool() additionally subtracts MCP_CLIENT_BLOCKED_TOOLS
    in that case. Before this fix, the manifest advertised e.g.
    docker_start/docker_exec/workspace_file_write under "mcp_client" even
    though none of them are ever actually registered when safe mode is on.
    """

    def test_safe_mode_on_excludes_blocked_tools_from_mode_listing(
        self, sample_tools: list[FakeTool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
        result = build_manifest(sample_tools, mode_override="mcp_client")
        mcp_client_tools = result["modes"]["mcp_client"]["tools"]
        assert "docker_exec" not in mcp_client_tools
        assert "workspace_file_write" not in mcp_client_tools
        assert "run_opencode" not in mcp_client_tools

    def test_safe_mode_off_includes_full_raw_set(
        self, sample_tools: list[FakeTool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MCP_CLIENT_SAFE_MODE", raising=False)
        result = build_manifest(sample_tools, mode_override="mcp_client")
        mcp_client_tools = result["modes"]["mcp_client"]["tools"]
        assert "docker_exec" in mcp_client_tools
        assert "workspace_file_write" in mcp_client_tools

    def test_safe_mode_on_tool_count_matches_safe_tools(
        self, sample_tools: list[FakeTool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tool_modes import get_mcp_client_safe_tools

        monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
        result = build_manifest(sample_tools, mode_override="mcp_client")
        assert result["modes"]["mcp_client"]["tool_count"] == len(get_mcp_client_safe_tools())

    def test_other_modes_unaffected_by_safe_mode_flag(
        self, sample_tools: list[FakeTool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tool_modes import TOOL_NAMES_BY_MODE

        monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for mode in ("minimal", "standard", "full", "mcp_client_write"):
            assert result["modes"][mode]["tool_count"] == len(TOOL_NAMES_BY_MODE[mode])


class TestDockerStartNotPhantom:
    """docker_start has an impl function and a _CONFIRM_HANDLERS entry in
    server.py, but no @register_tool() ever wraps it -- there is no way
    to reach it through MCP. It must not be listed in any mode's tool set
    (which would make should_register_tool()/tools_manifest lie about its
    availability), matching every other mode that never listed it."""

    def test_docker_start_absent_from_mcp_client_mode(self) -> None:
        from tool_modes import TOOL_NAMES_BY_MODE

        assert "docker_start" not in TOOL_NAMES_BY_MODE["mcp_client"]

    def test_docker_start_absent_from_every_mode(self) -> None:
        from tool_modes import TOOL_NAMES_BY_MODE

        for mode, names in TOOL_NAMES_BY_MODE.items():
            assert "docker_start" not in names, mode

    def test_docker_start_sibling_actions_still_present(self) -> None:
        """Confirms the fix removed only docker_start, not its siblings."""
        from tool_modes import TOOL_NAMES_BY_MODE

        for name in ("docker_stop", "docker_restart", "docker_rm"):
            assert name in TOOL_NAMES_BY_MODE["mcp_client"]


class TestManifestFilteringAndPagination:
    """Regression coverage: before this fix, tools_manifest had no way to
    ask for a subset -- every call returned all ~100 tools' full
    descriptions unconditionally, an expensive and usually unnecessary
    amount of context for an agent that just wants e.g. the docker_* tool
    names or a single tool's scopes.
    """

    def test_name_prefix_filters_tools(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(
            sample_tools, mode_override="mcp_client", name_prefix="docker_"
        )
        names = {t["name"] for t in result["tools"]}
        assert names == {"docker_restart", "docker_compose_up"}
        assert result["returned_count"] == 2
        # tool_count stays the *total* registered count, unaffected by filters.
        assert result["tool_count"] == len(sample_tools)
        assert result["filtered_count"] == 2

    def test_scope_filters_tools(self, sample_tools: list[FakeTool]) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        # Pick a real scope from an unfiltered tool to filter by.
        target = next(t for t in result["tools"] if t["scopes"])
        scoped = build_manifest(
            sample_tools, mode_override="mcp_client", scope=target["scopes"][0]
        )
        assert all(target["scopes"][0] in t["scopes"] for t in scoped["tools"])
        assert len(scoped["tools"]) <= len(sample_tools)

    def test_mode_filter_narrows_tools_and_modes_dict(
        self, sample_tools: list[FakeTool]
    ) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client", mode="minimal")
        assert set(result["modes"].keys()) == {"minimal"}
        for t in result["tools"]:
            assert "minimal" in t["modes"]

    def test_include_descriptions_false_omits_description_field(
        self, sample_tools: list[FakeTool]
    ) -> None:
        result = build_manifest(
            sample_tools, mode_override="mcp_client", include_descriptions=False
        )
        for t in result["tools"]:
            assert "description" not in t

    def test_include_descriptions_true_by_default(
        self, sample_tools: list[FakeTool]
    ) -> None:
        result = build_manifest(sample_tools, mode_override="mcp_client")
        for t in result["tools"]:
            assert "description" in t

    def test_offset_and_limit_paginate(self, sample_tools: list[FakeTool]) -> None:
        full = build_manifest(sample_tools, mode_override="mcp_client")
        all_names = [t["name"] for t in full["tools"]]

        page = build_manifest(sample_tools, mode_override="mcp_client", offset=1, limit=2)
        assert [t["name"] for t in page["tools"]] == all_names[1:3]
        assert page["returned_count"] == 2
        assert page["offset"] == 1
        assert page["limit"] == 2
        assert page["tool_count"] == len(sample_tools)

    def test_limit_none_returns_all_after_offset(self, sample_tools: list[FakeTool]) -> None:
        full = build_manifest(sample_tools, mode_override="mcp_client")
        all_names = [t["name"] for t in full["tools"]]

        page = build_manifest(sample_tools, mode_override="mcp_client", offset=2)
        assert [t["name"] for t in page["tools"]] == all_names[2:]

    def test_negative_offset_rejected(self, sample_tools: list[FakeTool]) -> None:
        """P2 audit finding: offset=-1 used to silently select from the
        end of the filtered list instead of being rejected."""
        with pytest.raises(ValueError, match="offset"):
            build_manifest(sample_tools, mode_override="mcp_client", offset=-1)

    def test_zero_limit_rejected(self, sample_tools: list[FakeTool]) -> None:
        with pytest.raises(ValueError, match="limit"):
            build_manifest(sample_tools, mode_override="mcp_client", limit=0)

    def test_negative_limit_rejected(self, sample_tools: list[FakeTool]) -> None:
        with pytest.raises(ValueError, match="limit"):
            build_manifest(sample_tools, mode_override="mcp_client", limit=-5)
