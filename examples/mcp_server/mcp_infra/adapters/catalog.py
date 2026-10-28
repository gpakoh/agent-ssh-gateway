"""Read-only discovery; no tool dispatcher or mutable remote resource loader."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import StrictBool, StrictInt
from tool_results import tool_error, tool_success

from app.skill_pack import SkillPackError, read_skill
from examples.mcp_server.gateway_catalog import build_catalog
from examples.mcp_server.mcp_infra._server_ref import server_attr


def _read_result(tool: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        result = fn()
    except SkillPackError:
        return tool_error(
            tool=tool,
            code="SKILL_PACK_UNAVAILABLE",
            message="Bundled skill pack is unavailable or invalid",
            source="gateway",
        )
    except ValueError as exc:
        return tool_error(tool=tool, code="INVALID_INPUT", message=str(exc), source="gateway")
    return tool_success(tool, result=result, source="gateway")


def gateway_catalog(
    group: str | None = None,
    query: str = "",
    tool_name: str | None = None,
    include_schema: StrictBool = False,
    role: Literal["controller", "worker"] = "controller",
    offset: StrictInt = 0,
    limit: StrictInt = 20,
) -> dict[str, Any]:
    """Start here: discover tool groups and skills with short explanations.

    No arguments returns a compact overview, not all tools or skill bodies.
    Open a group or search query; optionally read one tool's schema by name.
    Read an advertised skill with gateway_skill. Catalog visibility is server
    registration, not a grant of caller scopes or a client catalog refresh.
    """
    from examples.mcp_server.mcp_infra import runtime

    return _read_result(
        "gateway_catalog",
        fn=lambda: build_catalog(
            runtime.get_mcp()._tool_manager.list_tools(),
            group=group,
            query=query,
            tool_name=tool_name,
            include_schema=include_schema,
            role=role,
            offset=offset,
            limit=limit,
            unavailable=server_attr("_unavailable_tool_reasons")(),
        ),
    )


def gateway_skill(
    name: str,
    resource: str = "SKILL.md",
    role: Literal["controller", "worker"] = "controller",
    expected_pack_sha256: str | None = None,
) -> dict[str, Any]:
    """Load one bundled skill or advertised reference, on demand.

    Read names/roles/resources from gateway_catalog. Optional pack hash detects
    a release change between discovery and loading. No arbitrary path, URL,
    installation, shell execution or authorization change is supported.
    """
    return _read_result(
        "gateway_skill",
        fn=lambda: read_skill(
            name,
            resource,
            role=role,
            expected_pack_sha256=expected_pack_sha256,
        ),
    )


def register_all() -> None:
    from examples.mcp_server.mcp_infra.tool_registry import register_tool

    register_tool("gateway_catalog")(gateway_catalog)
    register_tool("gateway_skill")(gateway_skill)
