"""Read-only discovery; no tool dispatcher or mutable remote resource loader."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Literal

from pydantic import StrictBool, StrictInt
from tool_results import tool_error, tool_success

from app.skill_pack import SkillPackError, read_skill
from examples.mcp_server.gateway_catalog import build_catalog
from examples.mcp_server.gateway_client import GatewayClientError
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.schema_cache import schema_etag, validate_conditional_etag


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
    if_schema_etag: str | None = None,
) -> dict[str, Any]:
    """Start here: discover tool groups and skills with short explanations.

    No arguments returns a compact overview, not all tools or skill bodies.
    Open a group or search query; optionally read one tool's schema by name.
    Read an advertised skill with gateway_skill. Catalog visibility is server
    registration, not a grant of caller scopes or a client catalog refresh.

    For one exact tool_name with include_schema=True, an authenticated client
    may reuse its OWN previous schema when if_schema_etag matches the current
    session, build, registry and ready Gateway state. A cache miss always
    returns the full server schema. No cache state bypasses authorization.
    """

    def _catalog() -> dict[str, Any]:
        supplied = validate_conditional_etag(if_schema_etag)
        if supplied is not None and (not include_schema or not tool_name):
            raise ValueError("if_schema_etag requires include_schema and exact tool_name")

        instance = server_attr("mcp")
        unavailable = server_attr("_unavailable_tool_reasons")()
        catalog = build_catalog(
            instance._tool_manager.list_tools(),
            group=group,
            query=query,
            tool_name=tool_name,
            include_schema=include_schema,
            role=role,
            offset=offset,
            limit=limit,
            unavailable=unavailable,
        )
        if not include_schema or len(catalog["tools"]) != 1:
            return catalog

        # Never claim a reusable lease without a live authenticated lifecycle,
        # a fixed release SHA, and a ready Gateway command plane.
        etag = None
        owner: object | None = None
        try:
            # Only the real MCP ServerSession can own this weak lease.
            # lifespan_context is not necessarily session-scoped/hashable;
            # missing transport identity must return a full schema.
            owner = server_attr("_current_mcp_session")()
            identity = server_attr("_current_auth_reuse_key")()
            gateway_state = server_attr("get_gateway_client")().health()
            plane = (
                gateway_state.get("command_plane", {}).get("state")
                if gateway_state.get("ready") is True
                else None
            )
            etag = schema_etag(
                lifecycle_owner=owner,
                auth_identity=identity,
                build_sha=os.environ.get("BUILD_SHA"),
                plane_state=plane,
                toolset_hash=server_attr("compute_toolset_hash")(instance),
                request={
                    "group": group, "query": query, "tool_name": tool_name,
                    "include_schema": include_schema, "role": role,
                    "offset": offset, "limit": limit,
                },
                catalog=catalog,
            )
        except (AttributeError, GatewayClientError, KeyError, OSError, TypeError, ValueError):
            # A failed live health lookup is itself a readiness transition.
            # Observe it in the same weak session state so an old ready ETag
            # cannot become valid again just because the service recovered.
            if owner is not None:
                schema_etag(
                    lifecycle_owner=owner,
                    auth_identity=None,
                    build_sha=None,
                    plane_state=None,
                    toolset_hash=None,
                    request={},
                    catalog={},
                )
            etag = None

        if etag is None:
            catalog["schema_cache"] = {
                "state": "uncacheable",
                "etag": None,
                "reuse_permitted": False,
            }
            return catalog
        if supplied == etag:
            return {
                "source": "live-server-tool-registry",
                "tool_name": tool_name,
                "schema_not_modified": True,
                "schema_cache": {
                    "state": "not_modified",
                    "etag": etag,
                    "reuse_permitted": True,
                    "scope": "authenticated_mcp_session",
                },
            }
        catalog["schema_cache"] = {
            "state": "fresh",
            "etag": etag,
            "reuse_permitted": True,
            "scope": "authenticated_mcp_session",
        }
        return catalog

    return _read_result("gateway_catalog", fn=_catalog)


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
