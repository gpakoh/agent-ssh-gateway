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

from examples.mcp_server.surface_parity import ClientSurfaceAttestation, evaluate_required_guards


def _agent_guidance() -> dict[str, Any]:
    """Return static, host-path-free workflow guidance for tool users.

    The manifest is often the first discovery surface an agent sees.  Keep
    this intentionally small and non-project-specific: project-specific cwd,
    verification commands, and write-plane hints belong in info(project).
    """
    return {
        "project_metadata_tool": "info",
        "before_project_writes": "Call info(project), inspect workspace.recommended_write_plane, and preserve workspace.git_state for guarded commit/push workflows.",
        "before_verification": "Call info(project) and run verification commands from verification.cwd.",
        "agent_run_diagnostics": "For an existing agent task, poll agent_status(project, task_id) first; escalate to inspect_agent_task only when compact status/verdict needs bounded log-backed diagnostics.",
        "path_policy": "Manifest guidance never exposes host filesystem paths; project tools use project-relative paths.",
    }


def _operator_surface_contract(
    active_mode: str,
    scope_enforcement: str,
    *,
    registered_names: set[str],
    server_toolset_hash: str | None,
    client_attestation: ClientSurfaceAttestation | None,
    client_observation_status: str,
    required_guard_tools: tuple[str, ...],
) -> dict[str, Any]:
    """Render authoritative server state beside an unverified client report."""

    server_names = sorted(registered_names)
    client_names = list(client_attestation.names) if client_attestation is not None else []
    complete = client_attestation.complete if client_attestation is not None else False
    unexpected_in_client = sorted(set(client_names) - registered_names)
    missing_from_client = (
        sorted(registered_names - set(client_names))
        if client_attestation is not None and complete
        else []
    )
    mismatch = bool(missing_from_client or unexpected_in_client)
    diagnostic_code = (
        "EXTERNAL_RESOURCE_CATALOG_MISMATCH"
        if mismatch
        else "EXTERNAL_RESOURCE_CATALOG_UNVERIFIED"
    )
    if client_observation_status == "supplied_bound":
        binding_status = "session_bound"
    elif client_observation_status == "supplied_unbound":
        binding_status = "unbound"
    else:
        binding_status = "not_supplied"

    return {
        "server_tool_manager_verified": True,
        "server_tool_manager_surface": "mcp.tools/list",
        "active_mode": active_mode,
        "scope_enforcement": scope_enforcement,
        "external_resource_catalog": "api_tool.list_resources",
        "external_resource_catalog_verified": False,
        "authoritative_for_external_schema_visibility": False,
        "diagnostic_code": diagnostic_code,
        "server_surface": {
            "authoritative": True,
            "surface": "mcp.tools/list",
            "toolset_hash": server_toolset_hash,
            "names": server_names,
        },
        "client_observation": {
            "status": client_observation_status,
            "complete": complete,
            "binding_status": binding_status,
            "reported_names": client_names,
            "reported_name_count": len(client_names),
            "independently_verified": False,
            "omissions_prove_absence": bool(complete),
        },
        "missing_from_client": missing_from_client,
        "unexpected_in_client": unexpected_in_client,
        "guard_coverage": evaluate_required_guards(client_attestation, required_guard_tools),
        "operator_guidance": "Server state is authoritative only for the local MCP tool manager. Client-visible names are client-reported and never independently verified; use missing/unexpected diagnostics to investigate the external resource catalog without treating the report as mutation authority. An externally missing tool is not proof that the MCP server failed to register it.",
    }


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
    server_toolset_hash: str | None = None,
    client_attestation: ClientSurfaceAttestation | None = None,
    client_observation_status: str | None = None,
    required_guard_tools: tuple[str, ...] = (),
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

    def _effective_tools_for_mode(mode_name: str, tool_set: set[str]) -> set[str]:
        effective_set = set(tool_set)
        if mode_name == "mcp_client" and safe_mode_on:
            effective_set -= MCP_CLIENT_BLOCKED_TOOLS
        return effective_set

    modes_dict: dict[str, dict[str, Any]] = {}
    effective_by_mode: dict[str, set[str]] = {}
    for m, tool_set in TOOL_NAMES_BY_MODE.items():
        effective_set = _effective_tools_for_mode(m, tool_set)
        effective_by_mode[m] = effective_set
        if mode is not None and m != mode:
            continue
        modes_dict[m] = {
            "tool_count": len(effective_set),
            "tools": sorted(effective_set),
        }

    active_expected_names = effective_by_mode.get(active_mode, set())
    missing_registered_tools = [
        {
            "name": name,
            "reason": f"configured for active mode {active_mode!r} but not registered in live MCP tool manager",
        }
        for name in sorted(active_expected_names - registered_names)
    ]
    unexpected_registered_tools = [
        {
            "name": name,
            "reason": f"registered in live MCP tool manager but absent from active mode {active_mode!r} configuration",
        }
        for name in sorted(registered_names - active_expected_names)
    ]
    catalog_consistency = {
        "active_mode": active_mode,
        "ok": not missing_registered_tools and not unexpected_registered_tools,
        "missing_registered_tools": missing_registered_tools,
        "unexpected_registered_tools": unexpected_registered_tools,
    }

    if client_observation_status is None:
        client_observation_status = (
            "supplied_bound" if client_attestation is not None else "not_supplied"
        )
    if client_observation_status not in {"not_supplied", "supplied_bound", "supplied_unbound"}:
        raise ValueError("invalid client_observation_status")

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
        "agent_guidance": _agent_guidance(),
        "operator_surface_contract": _operator_surface_contract(
            active_mode,
            scope_enforcement,
            registered_names=registered_names,
            server_toolset_hash=server_toolset_hash,
            client_attestation=client_attestation,
            client_observation_status=client_observation_status,
            required_guard_tools=required_guard_tools,
        ),
        "catalog_consistency": catalog_consistency,
    }
