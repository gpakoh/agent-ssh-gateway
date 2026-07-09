"""Tests for tool_scopes module and scope enforcement integration."""

from __future__ import annotations

import json

from examples.mcp_server.tool_scopes import (
    ACCESS_PROFILES,
    TOOL_SCOPES,
    check_fleet_route,
    extract_tool_from_body,
    get_profile_scopes,
    get_required_scopes,
    has_required_scope,
)

# ── Basic data integrity ─────────────────────────────────────────


class TestScopeDataIntegrity:
    def test_all_profiles_defined(self):
        expected = {"viewer", "operator", "agent-runner", "infra", "full"}
        assert set(ACCESS_PROFILES) == expected

    def test_all_profiles_have_scopes(self):
        for name, scopes in ACCESS_PROFILES.items():
            assert len(scopes) > 0, f"Profile {name} has empty scopes"

    def test_too_l_scopes_count(self):
        assert len(TOOL_SCOPES) >= 50, f"Expected 50+, got {len(TOOL_SCOPES)}"

    def test_every_tool_has_nonempty_scopes(self):
        for tool, scopes in TOOL_SCOPES.items():
            assert len(scopes) > 0, f"Tool {tool} has empty scopes"

    def test_fail_closed_for_unknown_tool(self):
        assert get_required_scopes("nonexistent_tool_xyz") == ["mcp:admin"]

    def test_healthcheck_full_profile(self):
        scopes = get_profile_scopes("full")
        assert "mcp:admin" in scopes
        assert "mcp:execute" in scopes
        assert "mcp:agent-run" in scopes
        assert "mcp:docker" in scopes
        assert "mcp:postgres" in scopes

    def test_operator_default_scopes(self):
        scopes = get_profile_scopes("operator")
        assert "mcp:read" in scopes
        assert "mcp:project" in scopes
        assert "mcp:handoff" in scopes
        assert "mcp:repo" in scopes
        assert "mcp:docs" in scopes
        assert "mcp:docker" not in scopes
        assert "mcp:postgres" not in scopes
        assert "mcp:agent-run" not in scopes
        assert "mcp:execute" not in scopes
        assert "mcp:admin" not in scopes

    def test_viewer_restricted(self):
        scopes = get_profile_scopes("viewer")
        assert set(scopes) == {"mcp:read", "mcp:repo", "mcp:docs"}

    def test_infra_no_project(self):
        scopes = get_profile_scopes("infra")
        assert "mcp:read" in scopes
        assert "mcp:docker" in scopes
        assert "mcp:postgres" in scopes
        assert "mcp:project" not in scopes


# ── Scope checking logic ─────────────────────────────────────────


class TestScopeChecking:
    def test_has_required_scope_match(self):
        assert has_required_scope(["mcp:read"], "gateway_health")

    def test_has_required_scope_no_match(self):
        assert not has_required_scope(["mcp:docker"], "gateway_health")

    def test_has_required_scope_multiple_allowed(self):
        assert has_required_scope(["mcp:project"], "gateway_read_file")
        assert has_required_scope(["mcp:read"], "gateway_read_file")

    def test_has_required_scope_unknown_tool_admin_only(self):
        assert not has_required_scope(["mcp:read"], "unknown_tool")
        assert has_required_scope(["mcp:admin"], "unknown_tool")

    def test_has_required_scope_healthcheck_full(self):
        full = get_profile_scopes("full")
        for tool_name in list(TOOL_SCOPES)[:10]:
            assert has_required_scope(full, tool_name), f"full profile should allow {tool_name}"

    def test_has_required_scope_viewer_denied(self):
        viewer = get_profile_scopes("viewer")
        assert has_required_scope(viewer, "gateway_health")  # mcp:read
        assert not has_required_scope(viewer, "project_run_opencode")  # mcp:agent-run
        assert not has_required_scope(viewer, "gateway_project_run_pytest")
        assert not has_required_scope(viewer, "docker_ps")
        assert not has_required_scope(viewer, "postgres_select")

    def test_has_required_scope_agent_runner(self):
        ar = get_profile_scopes("agent-runner")
        assert has_required_scope(ar, "project_run_opencode")
        assert has_required_scope(ar, "gateway_project_run_mimo")
        assert not has_required_scope(ar, "docker_ps")
        assert not has_required_scope(ar, "gateway_execute_restricted")

    def test_has_required_scope_infra(self):
        infra = get_profile_scopes("infra")
        assert has_required_scope(infra, "docker_ps")
        assert has_required_scope(infra, "postgres_select")
        assert has_required_scope(infra, "gateway_health")
        assert not has_required_scope(infra, "gateway_project_tree")


# ── Tool extraction ──────────────────────────────────────────────


class TestToolExtraction:
    def test_extract_tools_call(self):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "gateway_health", "arguments": {}},
            }
        ).encode()
        assert extract_tool_from_body(body) == "gateway_health"

    def test_extract_initialize_returns_none(self):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
        ).encode()
        assert extract_tool_from_body(body) is None

    def test_extract_tools_list_returns_none(self):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
            }
        ).encode()
        assert extract_tool_from_body(body) is None

    def test_extract_invalid_json(self):
        assert extract_tool_from_body(b"not json") is None

    def test_extract_empty_body(self):
        assert extract_tool_from_body(b"") is None


# ── Fleet route scoping ──────────────────────────────────────────


class TestFleetRouteScoping:
    def test_fleet_gitea_allowed_with_repo(self):
        allowed, _ = check_fleet_route("/mcp/gitea", ["mcp:repo"])
        assert allowed

    def test_fleet_gitea_denied_without_repo(self):
        allowed, scope = check_fleet_route("/mcp/gitea", ["mcp:read"])
        assert not allowed
        assert scope == "mcp:repo"

    def test_fleet_github_allowed(self):
        allowed, _ = check_fleet_route("/mcp/github", ["mcp:repo"])
        assert allowed

    def test_fleet_docker_allowed(self):
        allowed, _ = check_fleet_route("/mcp/docker", ["mcp:docker"])
        assert allowed

    def test_fleet_postgres_allowed(self):
        allowed, _ = check_fleet_route("/mcp/postgres", ["mcp:postgres"])
        assert allowed

    def test_fleet_context7_allowed(self):
        allowed, _ = check_fleet_route("/mcp/context7", ["mcp:docs"])
        assert allowed

    def test_fleet_non_fleet_route(self):
        allowed, _ = check_fleet_route("/mcp", ["mcp:read"])
        assert allowed

    def test_fleet_deep_path_allowed(self):
        allowed, _ = check_fleet_route("/mcp/gitea/tools/list", ["mcp:repo"])
        assert allowed


# ── Specific tool mappings ───────────────────────────────────────


class TestSpecificToolMappings:
    def test_gateway_execute_restricted_requires_execute(self):
        assert get_required_scopes("gateway_execute_restricted") == ["mcp:execute"]
        assert not has_required_scope(["mcp:read"], "gateway_execute_restricted")

    def test_project_run_opencode_requires_agent_run(self):
        assert get_required_scopes("project_run_opencode") == ["mcp:agent-run"]

    def test_gateway_project_run_mimo_requires_agent_run(self):
        assert get_required_scopes("gateway_project_run_mimo") == ["mcp:agent-run"]

    def test_all_gitea_fleet_requires_repo(self):
        for tool in TOOL_SCOPES:
            if tool.startswith("gitea_"):
                assert "mcp:repo" in get_required_scopes(tool), f"{tool} missing mcp:repo"

    def test_all_github_fleet_requires_repo(self):
        for tool in TOOL_SCOPES:
            if tool.startswith("github_"):
                assert "mcp:repo" in get_required_scopes(tool), f"{tool} missing mcp:repo"

    def test_all_docker_fleet_requires_docker(self):
        for tool in TOOL_SCOPES:
            if tool.startswith("docker_"):
                scopes = get_required_scopes(tool)
                # Admin-only tools (mcp:docker:admin) are a superset of mcp:docker
                assert "mcp:docker" in scopes or "mcp:docker:admin" in scopes, (
                    f"{tool} missing mcp:docker"
                )

    def test_all_postgres_fleet_requires_postgres(self):
        for tool in TOOL_SCOPES:
            if tool.startswith("postgres_"):
                assert "mcp:postgres" in get_required_scopes(tool), f"{tool} missing mcp:postgres"


# ── Profile-to-scope resolution ──────────────────────────────────


class TestProfileResolution:
    def test_get_profile_scopes_known(self):
        assert get_profile_scopes("viewer") == ACCESS_PROFILES["viewer"]

    def test_get_profile_scopes_unknown_falls_back_to_operator(self):
        assert get_profile_scopes("bogus") == ACCESS_PROFILES["operator"]

    def test_profile_scopes_are_distinct(self):
        for name, scopes in ACCESS_PROFILES.items():
            assert len(set(scopes)) == len(scopes), f"Duplicate scopes in {name}"
