import pytest
from mcp.types import CallToolResult


@pytest.mark.asyncio
async def test_project_release_guide_uses_real_registered_mcp_protocol(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client_write")
    from examples.mcp_server import server

    result = await server.mcp.call_tool("project_release_guide", {"project": "../secrets"})
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "INVALID_INPUT"
