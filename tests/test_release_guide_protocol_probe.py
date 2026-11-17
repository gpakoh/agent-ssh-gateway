import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from examples.mcp_server.mcp_infra import runtime
from examples.mcp_server.mcp_infra.adapters import release_guide


@pytest.mark.asyncio
async def test_project_release_guide_uses_real_registered_mcp_protocol(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client_write")
    mcp = FastMCP("release-guide-protocol-test")
    monkeypatch.setattr(runtime, "get_mcp", lambda: mcp)
    release_guide.register_all()

    result = await mcp.call_tool("project_release_guide", {"project": "../secrets"})
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "INVALID_INPUT"
