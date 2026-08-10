"""Global runtime state shared across the MCP server package.

Owned by server.py's startup sequence: server.py creates the FastMCP
instance and confirm store, then hands them to this module so that
decorator-time tool registration (register_tool) can reach the MCP
instance without importing server.py.
"""

from __future__ import annotations

_mcp = None
_confirm_store = None


def set_mcp(mcp) -> None:
    global _mcp
    _mcp = mcp


def get_mcp():
    if _mcp is None:
        raise RuntimeError("MCP instance not set; call runtime.set_mcp() during startup")
    return _mcp


def set_confirm_store(store) -> None:
    global _confirm_store
    _confirm_store = store


def get_confirm_store():
    if _confirm_store is None:
        raise RuntimeError("confirm store not set; call runtime.set_confirm_store() during startup")
    return _confirm_store
