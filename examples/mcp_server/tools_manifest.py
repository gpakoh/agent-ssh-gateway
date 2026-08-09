"""Build a read-only manifest of all registered MCP tools, modes, scopes, and profiles.

No network calls, no env dumps, no secrets, no tool execution — only registry introspection.
"""

from __future__ import annotations

from typing import Any

from tool_modes import (
    MCP_CLIENT_BLOCKED_TOOLS,
    TOOL_NAMES_BY_MODE,
    get_tool_mode,
    is_mcp_client_safe_mode,
)
from tool_results import validate_pagination
from tool_scopes import ACCESS_PROFILES, get_required_scopes


def build_manifest(
    registered_tools: list[Any],
    scope_enforcement: str = "audit",
    *,
    mode_override: str | None = None,
    scope: str | None = None,
    mode: str | None = None,
    name_prefix: str | None = None,
    include_descriptions: bool = True,
    offset: int = 0,
    limit: int | None = None,
    unavailable_tool_reasons: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the tools manifest from registries.

    Args:
        registered_tools: List of FastMCP Tool objects from
                          ``mcp._tool_manager.list_tools()``. Each object must
                          have ``.name`` and ``.description`` attributes.
        scope_enforcement: Current scope enforcement mode
                           (``"off" | "audit" | "enforce"``).
        mode_override: Optional explicit mode (bypasses env lookup) --
                       controls ``active_mode`` and each tool entry's own
                       ``mode`` field.
        scope: Optional filter -- only include tools that require this
              scope (e.g. ``"ssh:execute"``).
        mode: Optional filter -- only include tools that belong to this
             mode (e.g. ``"mcp_client"``), and narrow ``modes`` to just
             that one entry. Distinct from mode_override: this filters
             *which tools/modes are reported*, not which mode is "active".
        name_prefix: Optional filter -- only include tools whose name
                    starts with this prefix (e.g. ``"docker_"``).
        include_descriptions: When False, omit each tool's (often long)
                             description -- the manifest otherwise returns
                             every registered tool's full description
                             unconditionally, which is expensive context
                             for an agent that just wants names/scopes.
        offset: Pagination offset into the filtered tools list.
        limit: Pagination page size. None returns all (post-filter) tools.
        unavailable_tool_reasons: Optional {tool_name: reason} map for
            tools that are registered (reachable in this mode/scope) but
            whose actual runtime dependency isn't present -- e.g. no
            docker CLI in this image, no npx for context7, Postgres not
            configured. MAJOR audit finding: "enabled": True used to be
            unconditional, so a client had no way to tell "this tool
            exists" from "this tool will actually work" short of calling
            it and getting a runtime error. The caller (server.py, which
            has actual access to shutil.which()/PG_DSN/etc.) computes
            this map; build_manifest() stays pure registry introspection.
    """
    validate_pagination(offset, "offset", min_value=0, max_value=10_000)
    if limit is not None:
        validate_pagination(limit, "limit", max_value=10_000)

    active_mode = mode_override or get_tool_mode()
    registered_names = {t.name for t in registered_tools}
    name_to_tool = {t.name: t for t in registered_tools}
    unavailable_tool_reasons = unavailable_tool_reasons or {}

    # Forward map: tool name -> list of modes it belongs to
    tool_to_modes: dict[str, list[str]] = {}
    for m, tool_set in TOOL_NAMES_BY_MODE.items():
        for name in tool_set:
            tool_to_modes.setdefault(name, []).append(m)

    # Build tools list (only registered — active in current mode)
    all_tools_list: list[dict[str, Any]] = []
    for name in sorted(registered_names):
        tool = name_to_tool.get(name)
        reason = unavailable_tool_reasons.get(name)
        entry: dict[str, Any] = {
            "name": name,
            "mode": active_mode,
            "modes": tool_to_modes.get(name, [active_mode]),
            "scopes": get_required_scopes(name),
            "enabled": True,
            "available": reason is None,
        }
        if reason is not None:
            entry["unavailable_reason"] = reason
        if include_descriptions:
            entry["description"] = tool.description if tool else ""
        all_tools_list.append(entry)

    # Apply scope/mode/name_prefix filters (all optional, all AND-combined).
    tools_list = all_tools_list
    if scope is not None:
        tools_list = [t for t in tools_list if scope in t["scopes"]]
    if mode is not None:
        tools_list = [t for t in tools_list if mode in t["modes"]]
    if name_prefix is not None:
        tools_list = [t for t in tools_list if t["name"].startswith(name_prefix)]

    filtered_count = len(tools_list)
    paged_tools_list = tools_list[offset:] if limit is None else tools_list[offset : offset + limit]

    # Build mode details. "mcp_client" gets a second, env-dependent filter
    # on top of TOOL_NAMES_BY_MODE["mcp_client"] -- should_register_tool()
    # additionally subtracts MCP_CLIENT_BLOCKED_TOOLS whenever
    # MCP_CLIENT_SAFE_MODE=true (the recommended, and here the actually
    # configured, setting). Reporting the raw, unfiltered set here made
    # this section advertise tools (e.g. docker admin/agent-launch) that
    # are never actually registered in the real deployment and never
    # appear in `tools`/`tool_count` above.
    safe_mode_on = is_mcp_client_safe_mode()
    modes_dict: dict[str, dict[str, Any]] = {}
    for m, tool_set in TOOL_NAMES_BY_MODE.items():
        if mode is not None and m != mode:
            continue
        effective_set = tool_set
        if m == "mcp_client" and safe_mode_on:
            effective_set = tool_set - MCP_CLIENT_BLOCKED_TOOLS
        modes_dict[m] = {
            "tool_count": len(effective_set),
            "tools": sorted(effective_set),
        }

    # Build access profiles (scope lists only — no token values)
    profiles_dict: dict[str, list[str]] = {
        name: sorted(scopes) for name, scopes in ACCESS_PROFILES.items()
    }

    return {
        "active_mode": active_mode,
        "scope_enforcement": scope_enforcement,
        "tool_count": len(all_tools_list),
        "filtered_count": filtered_count,
        "returned_count": len(paged_tools_list),
        "offset": offset,
        "limit": limit,
        "tools": paged_tools_list,
        "modes": modes_dict,
        "access_profiles": profiles_dict,
    }
