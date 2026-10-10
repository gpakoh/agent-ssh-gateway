"""Parity invariants tying tool additions and schema changes to the live surface.

Covers the "regression tests for new tool additions and schema changes"
acceptance: anything registered in the live MCP tool manager must appear
identically in ``mcp tools/list`` and the tools_manifest, and any name or
schema addition must change the toolset hash so a stale client catalog can be
detected.  Also covers the long-lived session contract: a client that supplies
the toolset hash its catalog was built from receives an explicit
``current`` / ``stale`` / ``unproven`` state with a safe refresh action.
"""

from __future__ import annotations

import importlib
import os
from typing import Any
from unittest.mock import patch

import pytest

from examples.mcp_server.mcp_infra.tool_registry import compute_toolset_hash
from examples.mcp_server.tools_manifest import build_manifest

_LONG_TOOLSET_HASH = "sha256:" + "a" * 64


class TestNewToolAndSchemaParity:
    """Lone-fastmcp regression: additions and schema edits propagate everywhere."""

    def test_new_tool_addition_appears_in_list_and_manifest_and_changes_hash(self) -> None:
        from mcp.server.fastmcp import FastMCP

        app = FastMCP("parity-invariant")
        registered_names: set[str] = set()

        @app.tool()
        def base_tool(value: int) -> str:
            return str(value)

        registered_names.add("base_tool")
        hash_before = compute_toolset_hash(app)
        list_before = {t.name for t in app._tool_manager.list_tools()}
        manifest_before = build_manifest(
            app._tool_manager.list_tools(), mode_override="mcp_client"
        )
        assert set(manifest_before["operator_surface_contract"]["server_surface"]["names"]) == list_before
        assert "base_tool" in list_before

        @app.tool()
        def added_tool(marker: str) -> str:
            return marker

        registered_names.add("added_tool")
        list_after = {t.name for t in app._tool_manager.list_tools()}
        manifest_after = build_manifest(
            app._tool_manager.list_tools(), mode_override="mcp_client"
        )
        assert "added_tool" in list_after
        assert list_after == registered_names
        assert any(t["name"] == "added_tool" for t in manifest_after["tools"])
        assert set(manifest_after["operator_surface_contract"]["server_surface"]["names"]) == list_after
        assert compute_toolset_hash(app) != hash_before

    def test_schema_change_changes_hash_and_visible_schema_in_both_views(self) -> None:
        from mcp.server.fastmcp import FastMCP

        app = FastMCP("parity-schema-invariant")

        @app.tool()
        def schema_tool(value: str) -> str:
            return value

        hash_before = compute_toolset_hash(app)
        tool = app._tool_manager.get_tool("schema_tool")
        assert tool is not None
        assert "value" in (tool.parameters or {}).get("properties", {})

        new_parameters = dict(tool.parameters or {})
        properties = dict(new_parameters.get("properties", {}))
        required = list(new_parameters.get("required", []))
        properties["marker"] = {"type": "string", "description": "new field"}
        required = sorted(set(required) | {"marker"})
        new_parameters["properties"] = properties
        new_parameters["required"] = required
        tool.parameters = new_parameters

        hash_after = compute_toolset_hash(app)
        assert hash_after != hash_before

        live = app._tool_manager.get_tool("schema_tool")
        assert "marker" in (live.parameters or {}).get("properties", {})

        manifest = build_manifest(
            app._tool_manager.list_tools(),
            mode_override="mcp_client",
            server_toolset_hash=hash_after,
        )
        entry = next(t for t in manifest["tools"] if t["name"] == "schema_tool")
        assert entry["enabled"] is True and entry["available"] is True
        assert (
            manifest["operator_surface_contract"]["server_surface"]["toolset_hash"]
            == hash_after
        )


class TestLiveServerToolSurface:
    """Fresh-session contract against the real registration paths."""

    TARGETS = {
        "gitea_create_branch_at_sha": ["owner", "repo", "branch", "expected_head_sha"],
        "git_fetch_ref": ["project"],
        "git_refresh_branch_to_head": [
            "project",
            "branch",
            "expected_current_head",
            "target_head",
        ],
    }

    @pytest.fixture()
    def live_server(self) -> Any:
        with patch.dict(
            os.environ,
            {
                "MCP_GATEWAY_TOOL_MODE": "mcp_client_write",
                "MCP_CLIENT_SAFE_MODE": "true",
            },
            clear=False,
        ):
            import examples.mcp_server.server as srv

            importlib.reload(srv)
            return srv

    def test_guarded_tools_registered_with_exact_schema(self, live_server: Any) -> None:
        tools = {t.name: t for t in live_server.mcp._tool_manager.list_tools()}
        for name, required in self.TARGETS.items():
            assert name in tools, f"{name} missing from live tools/list"
            schema = tools[name].parameters or {}
            assert sorted(required) == sorted(schema.get("required", []))

    def test_manifest_reports_guarded_tools_and_matches_tools_list(
        self, live_server: Any
    ) -> None:
        live_names = {t.name for t in live_server.mcp._tool_manager.list_tools()}
        live_hash = compute_toolset_hash(live_server.mcp)
        response = live_server.gateway_tools_manifest(
            client_catalog_toolset_hash=live_hash,
            include_descriptions=False,
        )
        assert response["ok"] is True
        result = response["result"]
        contract = result["operator_surface_contract"]
        assert contract["server_surface"]["toolset_hash"] == live_hash
        assert set(contract["server_surface"]["names"]) == live_names
        assert result["tool_count"] == len(live_names)
        listed = {t["name"] for t in result["tools"]}
        assert listed == live_names
        for name in self.TARGETS:
            entry = next(t for t in result["tools"] if t["name"] == name)
            assert entry["enabled"] is True and entry["available"] is True
        assert contract["catalog_refresh"]["catalog_state"] == "current"

    def test_stale_client_hash_returns_typed_refresh_action(
        self, live_server: Any
    ) -> None:
        live_hash = compute_toolset_hash(live_server.mcp)
        assert live_hash != _LONG_TOOLSET_HASH
        response = live_server.gateway_tools_manifest(
            client_catalog_toolset_hash=_LONG_TOOLSET_HASH,
            include_descriptions=False,
        )
        assert response["ok"] is True
        refresh = response["result"]["operator_surface_contract"]["catalog_refresh"]
        assert refresh["catalog_state"] == "stale"
        assert refresh["refresh_required"] is True
        assert refresh["server_toolset_hash"] == live_hash
        assert refresh["client_catalog_toolset_hash"] == _LONG_TOOLSET_HASH
        assert refresh["refresh_action"]["tool"] == "tools/list"
        assert (
            refresh["refresh_action"]["server_side_reconnect_required"] is False
        )

    def test_unproven_when_client_hash_not_supplied(self, live_server: Any) -> None:
        response = live_server.gateway_tools_manifest(include_descriptions=False)
        assert response["ok"] is True
        refresh = response["result"]["operator_surface_contract"]["catalog_refresh"]
        assert refresh["catalog_state"] == "unproven"
        assert refresh["refresh_required"] is False

    def test_invalid_client_hash_fails_closed_without_state_write(
        self, live_server: Any
    ) -> None:
        response = live_server.gateway_tools_manifest(
            client_catalog_toolset_hash="sha256:NOTHEX",
            include_descriptions=False,
        )
        assert response["ok"] is False
        assert "error" in response