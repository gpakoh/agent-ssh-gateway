"""Exercise the real FastMCP schemas and protocol result envelopes."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import ValidationError

from app.skill_pack import SkillPackError
from examples.mcp_server.mcp_infra.adapters import catalog
from examples.mcp_server.tool_modes import TOOL_NAMES_BY_MODE, should_register_tool
from examples.mcp_server.tool_scopes import get_required_scopes


def test_discovery_is_read_only_in_all_existing_modes(monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
    for name in ("gateway_catalog", "gateway_skill"):
        assert get_required_scopes(name) == ["mcp:read"]
        for mode in TOOL_NAMES_BY_MODE:
            assert should_register_tool(name, mode)


def test_native_mcp_schema_rejects_coerced_numbers_booleans_and_roles():
    mcp = FastMCP("catalog-schema")
    tool = mcp._tool_manager.add_tool(catalog.gateway_catalog, name="gateway_catalog")
    args = tool.fn_metadata.arg_model
    for field, value in (
        ("limit", True),
        ("limit", "5"),
        ("offset", 1.2),
        ("include_schema", "true"),
        ("role", "admin"),
    ):
        with pytest.raises(ValidationError):
            args.model_validate({field: value})
    assert args.model_validate({"group": "sites", "limit": 5}).limit == 5


@pytest.mark.asyncio
async def test_real_registered_catalog_and_skill_protocol(monkeypatch):
    from examples.mcp_server import server

    monkeypatch.setattr(server, "_unavailable_tool_reasons", lambda: {})
    result = await server.mcp.call_tool("gateway_catalog", {})
    assert isinstance(result, CallToolResult)
    assert result.isError is False
    overview = result.structuredContent["result"]
    assert overview["tools"] == []
    assert len(overview["skills"]) == 4
    assert overview["tool_count"] == len(server.mcp._tool_manager.list_tools())
    loaded = await server.mcp.call_tool(
        "gateway_skill",
        {
            "name": "site-design",
            "expected_pack_sha256": overview["pack_sha256"],
        },
    )
    assert loaded.isError is False
    assert "Сохранить основу" in loaded.structuredContent["result"]["content"]
    invalid = await server.mcp.call_tool(
        "gateway_skill", {"name": "site-design", "resource": "../../etc/passwd"}
    )
    assert invalid.isError is True
    assert invalid.structuredContent["error"]["code"] == "INVALID_INPUT"


def test_pack_failure_is_diagnostic_and_does_not_leak_paths(monkeypatch):
    def unavailable(*args, **kwargs):
        raise SkillPackError("/private/operator/root")

    monkeypatch.setattr(catalog, "read_skill", unavailable)
    result = catalog.gateway_skill("site-design")
    assert result["error"]["code"] == "SKILL_PACK_UNAVAILABLE"
    assert "/private/operator" not in str(result)
