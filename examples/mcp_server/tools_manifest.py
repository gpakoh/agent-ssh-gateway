"""Build a read-only manifest of all registered MCP tools, modes, scopes, and profiles.

No network calls, no env dumps, no secrets, no tool execution — only registry introspection.
"""

from __future__ import annotations

from typing import Any

from tool_modes import TOOL_NAMES_BY_MODE, get_tool_mode
from tool_scopes import ACCESS_PROFILES, get_required_scopes


def build_manifest(
    registered_tools: list[Any],
    scope_enforcement: str = "audit",
    *,
    mode_override: str | None = None,
) -> dict[str, Any]:
    """Build the tools manifest from registries.

    Args:
        registered_tools: List of FastMCP Tool objects from
                          ``mcp._tool_manager.list_tools()``. Each object must
                          have ``.name`` and ``.description`` attributes.
        scope_enforcement: Current scope enforcement mode
                           (``"off" | "audit" | "enforce"``).
        mode_override: Optional explicit mode (bypasses env lookup).
    """
    mode = mode_override or get_tool_mode()
    registered_names = {t.name for t in registered_tools}
    name_to_tool = {t.name: t for t in registered_tools}

    # Forward map: tool name -> list of modes it belongs to
    tool_to_modes: dict[str, list[str]] = {}
    for m, tool_set in TOOL_NAMES_BY_MODE.items():
        for name in tool_set:
            tool_to_modes.setdefault(name, []).append(m)

    # Build tools list (only registered — active in current mode)
    tools_list: list[dict[str, Any]] = []
    for name in sorted(registered_names):
        tool = name_to_tool.get(name)
        tools_list.append(
            {
                "name": name,
                "mode": mode,
                "modes": tool_to_modes.get(name, [mode]),
                "scopes": get_required_scopes(name),
                "enabled": True,
                "description": tool.description if tool else "",
            }
        )

    # Build mode details
    modes_dict: dict[str, dict[str, Any]] = {}
    for m, tool_set in TOOL_NAMES_BY_MODE.items():
        modes_dict[m] = {
            "tool_count": len(tool_set),
            "tools": sorted(tool_set),
        }

    # Build access profiles (scope lists only — no token values)
    profiles_dict: dict[str, list[str]] = {
        name: sorted(scopes) for name, scopes in ACCESS_PROFILES.items()
    }

    return {
        "active_mode": mode,
        "scope_enforcement": scope_enforcement,
        "tool_count": len(tools_list),
        "tools": tools_list,
        "modes": modes_dict,
        "access_profiles": profiles_dict,
    }
