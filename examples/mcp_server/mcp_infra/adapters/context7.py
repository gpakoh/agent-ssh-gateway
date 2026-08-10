"""Context7 adapter: library resolution and docs queries.

Tools are registered explicitly via register_all() (called by server.py
after runtime.set_mcp) instead of import-time decorator side effects:
server.py may be importlib.reloaded, and the adapters are cached in
sys.modules, so import-time registration would miss the new FastMCP
instance.

The upstream callable is resolved through the server module at call time
(like tool_registry.run_tool resolves get_audit_logger): tests
monkeypatch server._call_context7_upstream and expect the patched value
to be visible here.
"""

from typing import Any

from tool_results import tool_error, tool_success

from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.tool_registry import register_tool


def _call_context7_upstream(name: str, args: dict) -> Any:
    return server_attr("_call_context7_upstream")(name, args)


async def resolve_library_id(query: str, libraryName: str) -> dict[str, Any]:
    """Resolve a package/product name to a Context7-compatible library ID."""
    try:
        text = await _call_context7_upstream(
            "resolve-library-id", {"query": query, "libraryName": libraryName}
        )
    except Exception as exc:
        return tool_error(
            tool="resolve_library_id",
            code="REMOTE_API_ERROR",
            message=str(exc),
            source="context7",
        )
    return tool_success("resolve_library_id", result=text, source="context7")


async def query_docs(libraryId: str, query: str) -> dict[str, Any]:
    """Query Context7 for documentation on a resolved library."""
    try:
        text = await _call_context7_upstream(
            "query-docs", {"libraryId": libraryId, "query": query}
        )
    except Exception as exc:
        return tool_error(
            tool="query_docs",
            code="REMOTE_API_ERROR",
            message=str(exc),
            source="context7",
        )
    return tool_success("query_docs", result=text, source="context7")


def register_all() -> None:
    register_tool("resolve_library_id")(resolve_library_id)
    register_tool("query_docs")(query_docs)
