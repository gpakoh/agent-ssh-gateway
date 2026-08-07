"""Contract v1 regression tests for Context7 docs tools.

Regression context: the audit (defect #5) found that resolve_library_id /
query_docs returned only {"result": "..."} -- no ok/error/meta envelope --
so a unified MCP client could not handle errors, tracing or metadata the
same way for these two tools as for every other tool in the gateway. The
fix wraps both tools (MCP surface in server.py and the fleet adapter) in
the canonical tool_success / tool_error envelope with source="context7".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parents[1]
_MCP_CLIENT_REMOTE = _BASE / "examples" / "mcp_client_remote"
sys.path.insert(0, str(_MCP_CLIENT_REMOTE))
sys.path.insert(0, str(_BASE / "tests"))

import fleet.context7_server as ctx7  # noqa: E402
from helpers import assert_tool_envelope  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_globals():
    ctx7._session = None
    ctx7._exit_stack = None
    yield
    ctx7._session = None
    ctx7._exit_stack = None


def _upstream(text: str = "lib-id-42"):
    async def _fake_call_upstream(name: str, args: dict) -> str:
        return text

    return _fake_call_upstream


class TestFleetContext7ContractV1:
    @pytest.mark.asyncio
    async def test_resolve_library_id_returns_contract_v1_envelope(self, monkeypatch):
        monkeypatch.setattr(ctx7, "_call_upstream", _upstream())

        result = await ctx7.resolve_library_id("query", "libraryName")

        assert_tool_envelope(result, ok=True, tool="resolve_library_id", source="context7")
        assert result["result"] == "lib-id-42"
        assert result["error"] is None
        assert result["meta"]["contract_version"] == "1"

    @pytest.mark.asyncio
    async def test_query_docs_returns_contract_v1_envelope(self, monkeypatch):
        monkeypatch.setattr(ctx7, "_call_upstream", _upstream("docs text"))

        result = await ctx7.query_docs("lib-id-42", "query")

        assert_tool_envelope(result, ok=True, tool="query_docs", source="context7")
        assert result["result"] == "docs text"

    @pytest.mark.asyncio
    async def test_resolve_library_id_upstream_failure_is_error_envelope(self, monkeypatch):
        async def _fail(name: str, args: dict) -> str:
            raise RuntimeError("upstream down")

        monkeypatch.setattr(ctx7, "_call_upstream", _fail)

        result = await ctx7.resolve_library_id("query", "libraryName")

        assert_tool_envelope(result, ok=False, tool="resolve_library_id", source="context7")
        assert result["error"]["code"] == "REMOTE_API_ERROR"
        assert "upstream down" in result["error"]["message"]
        assert result["result"] is None

    @pytest.mark.asyncio
    async def test_query_docs_upstream_failure_is_error_envelope(self, monkeypatch):
        async def _fail(name: str, args: dict) -> str:
            raise RuntimeError("upstream down")

        monkeypatch.setattr(ctx7, "_call_upstream", _fail)

        result = await ctx7.query_docs("lib-id-42", "query")

        assert_tool_envelope(result, ok=False, tool="query_docs", source="context7")
        assert result["error"]["code"] == "REMOTE_API_ERROR"
        assert result["result"] is None


class TestMcpContext7ContractV1:
    """MCP surface (server.py): resolve_library_id/query_docs must return
    the full envelope with meta.contract_version / meta.request_id, not a
    bare {"result": ...} string.
    """

    @pytest.mark.asyncio
    async def test_resolve_library_id_returns_full_meta(self, monkeypatch):
        import examples.mcp_server.server as server

        async def _fake(query: str, libraryName: str) -> str:
            return "lib-id-42"

        monkeypatch.setattr(server, "_call_context7_upstream", _fake)

        result = await server.resolve_library_id("query", "libraryName")

        assert_tool_envelope(
            result, ok=True, tool="resolve_library_id", source="context7", check_meta_contract=True
        )
        assert result["result"] == "lib-id-42"

    @pytest.mark.asyncio
    async def test_query_docs_returns_full_meta(self, monkeypatch):
        import examples.mcp_server.server as server

        async def _fake(libraryId: str, query: str) -> str:
            return "docs text"

        monkeypatch.setattr(server, "_call_context7_upstream", _fake)

        result = await server.query_docs("lib-id-42", "query")

        assert_tool_envelope(
            result, ok=True, tool="query_docs", source="context7", check_meta_contract=True
        )
        assert result["result"] == "docs text"

    @pytest.mark.asyncio
    async def test_resolve_library_id_failure_is_error_envelope(self, monkeypatch):
        import examples.mcp_server.server as server

        async def _fail(query: str, libraryName: str) -> str:
            raise RuntimeError("upstream down")

        monkeypatch.setattr(server, "_call_context7_upstream", _fail)

        result = await server.resolve_library_id("query", "libraryName")

        assert_tool_envelope(
            result, ok=False, tool="resolve_library_id", source="context7", check_meta_contract=True
        )
        assert result["error"]["code"] == "REMOTE_API_ERROR"
        assert "upstream down" in result["error"]["message"]
        assert result["result"] is None

    @pytest.mark.asyncio
    async def test_query_docs_failure_is_error_envelope(self, monkeypatch):
        import examples.mcp_server.server as server

        async def _fail(libraryId: str, query: str) -> str:
            raise RuntimeError("upstream down")

        monkeypatch.setattr(server, "_call_context7_upstream", _fail)

        result = await server.query_docs("lib-id-42", "query")

        assert_tool_envelope(
            result, ok=False, tool="query_docs", source="context7", check_meta_contract=True
        )
        assert result["error"]["code"] == "REMOTE_API_ERROR"
        assert result["result"] is None
