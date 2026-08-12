"""Regression tests for MCP tool results always reporting isError=false.

FastMCP's own dict/tuple return-value handling (mcp.server.lowlevel
.server.Server.call_tool()'s generic conversion, and FunctionMetadata
.convert_result()) always sets isError=False for a plain dict or
(content, structured) tuple return -- it has no notion of this
codebase's own {"ok": bool, ...} envelope convention. Since every tool
here returns that envelope as a plain dict (via tool_success()/
tool_error()), a confirmed domain error like PROJECT_NOT_FOUND or
POLICY_DENIED (body has "ok": false and a structured "error") was still
reported to the MCP client as isError=false -- indistinguishable from
success without inspecting the body.

Fixed centrally in register_tool() (every tool passes through it): wraps
the function's return value into an actual mcp.types.CallToolResult with
isError explicitly set from the envelope's "ok" field. That's the one
return shape FastMCP passes through completely unchanged.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

MCP_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


class TestEnvelopeToCallToolResult:
    """Unit-level: the conversion helper itself."""

    def test_ok_true_maps_to_is_error_false(self) -> None:
        from examples.mcp_server.server import _envelope_to_call_tool_result

        envelope = {"ok": True, "tool": "t", "result": {"x": 1}, "error": None, "meta": {}}
        result = _envelope_to_call_tool_result(envelope)

        assert result.isError is False
        assert result.structuredContent == envelope

    def test_ok_false_maps_to_is_error_true(self) -> None:
        from examples.mcp_server.server import _envelope_to_call_tool_result

        envelope = {
            "ok": False,
            "tool": "t",
            "result": None,
            "error": {"code": "PROJECT_NOT_FOUND", "message": "x", "retryable": False},
            "meta": {},
        }
        result = _envelope_to_call_tool_result(envelope)

        assert result.isError is True
        assert result.structuredContent == envelope


class TestRegisteredToolIsErrorViaRealFastMCP:
    """Integration-level: drive real tool functions through the actual
    FastMCP instance's call_tool(), the same path a real MCP client hits."""

    def test_successful_tool_call_is_error_false(self) -> None:
        from examples.mcp_server.server import mcp

        async def _call():
            return await mcp.call_tool("scan_command", {"command": "echo hi"})

        result = asyncio.run(_call())
        assert result.isError is False
        assert result.structuredContent["ok"] is True

    def test_real_error_tool_call_is_error_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git_status is only registered in mcp_client mode; re-import the
        module fresh with that mode selected (should_register_tool() is
        evaluated once, at decoration time) and confirm a real ok=false
        tool result (unknown project -> gateway/config error) is reported
        as isError=True through the real FastMCP dispatch -- not the
        previous always-false behavior."""
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
        for mod_name in list(sys.modules):
            if mod_name == "server" or mod_name.startswith("examples.mcp_server"):
                del sys.modules[mod_name]

        server = importlib.import_module("examples.mcp_server.server")

        def _deterministic_gateway_error(*args, **kwargs):
            raise server.GatewayClientError(
                "project not found",
                status_code=404,
                body={"detail": "project not found"},
            )

        monkeypatch.setattr(
            server.client,
            "execute_project_command",
            _deterministic_gateway_error,
        )

        async def _call():
            return await server.mcp.call_tool(
                "git_status", {"project": "this-project-does-not-exist-xyz"}
            )

        result = asyncio.run(_call())
        assert result.isError is True
        assert result.structuredContent["ok"] is False
