"""Session-scoped, conditional MCP tool-schema discovery and invalidation."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from examples.mcp_server.gateway_client import GatewayClient
from examples.mcp_server.mcp_infra.adapters import catalog
from examples.mcp_server.mcp_infra.tool_registry import compute_toolset_hash
from examples.mcp_server.schema_cache import schema_etag, validate_conditional_etag


class Owner:
    pass


class Gateway:
    def __init__(self) -> None:
        self.state = "ready"
        self.raise_health = False

    def health(self, *, timeout: int = 30) -> dict:
        assert timeout == 2  # Discovery must not inherit the 30s client default.
        if self.raise_health:
            raise OSError("no live Gateway")
        return {
            "ready": self.state == "ready",
            "command_plane": {"state": self.state},
        }


@pytest.fixture
def live_catalog(monkeypatch):
    instance = FastMCP("session-schema-cache")

    def health() -> str:
        return "healthy"

    instance._tool_manager.add_tool(health, name="health")
    owner = Owner()
    gateway = Gateway()
    session = {
        "owner": owner,
        "transport": owner,
        "identity": "principal-1",
        "unavailable": {},
    }
    mapping = {
        "mcp": instance,
        "_unavailable_tool_reasons": lambda: session["unavailable"],
        "_current_mcp_lifecycle_owner": lambda: session["owner"],
        "_current_mcp_session": lambda: session["transport"],
        "_current_auth_reuse_key": lambda: session["identity"],
        "get_gateway_client": lambda: gateway,
        "compute_toolset_hash": compute_toolset_hash,
    }
    monkeypatch.setattr(catalog, "server_attr", lambda name: mapping[name])
    monkeypatch.setenv("BUILD_SHA", "a" * 40)
    return instance, gateway, session


def _schema(**kwargs):
    return catalog.gateway_catalog(tool_name="health", include_schema=True, **kwargs)


def test_gateway_client_health_deadline_is_opt_in(monkeypatch):
    calls = []
    monkeypatch.setattr(
        GatewayClient, "_get",
        lambda self, path, timeout=30: calls.append((path, timeout)) or {"ready": True},
    )
    client = GatewayClient.__new__(GatewayClient)
    assert client.health() == {"ready": True}
    assert client.health(timeout=2) == {"ready": True}
    assert calls == [("/health", 30), ("/health", 2)]


def test_schema_etag_is_bound_to_session_auth_release_and_catalog():
    request = {"tool_name": "health", "role": "controller"}
    data = {"tools": [{"name": "health", "input_schema": {"type": "object"}}]}
    args = {
        "auth_identity": "principal-1",
        "build_sha": "a" * 40,
        "plane_state": "ready",
        "toolset_hash": "sha256:" + "b" * 64,
        "request": request,
        "catalog": data,
    }
    owner = Owner()
    token = schema_etag(lifecycle_owner=owner, **args)
    assert token is not None
    assert validate_conditional_etag(token) == token
    assert schema_etag(lifecycle_owner=owner, **args) == token
    assert schema_etag(lifecycle_owner=Owner(), **args) != token
    assert schema_etag(lifecycle_owner=owner, **{**args, "auth_identity": "principal-2"}) != token
    assert schema_etag(lifecycle_owner=owner, **{**args, "build_sha": "c" * 40}) != token
    assert schema_etag(lifecycle_owner=owner, **{**args, "toolset_hash": "sha256:" + "d" * 64}) != token
    assert schema_etag(lifecycle_owner=owner, **{**args, "catalog": {"tools": []}}) != token
    assert schema_etag(lifecycle_owner=owner, **{**args, "request": {"role": "worker"}}) != token
    assert schema_etag(lifecycle_owner=owner, **{**args, "plane_state": "starting"}) is None
    assert schema_etag(lifecycle_owner=None, **args) is None
    assert schema_etag(lifecycle_owner=[], **args) is None
    assert schema_etag(lifecycle_owner=owner, **{**args, "build_sha": "unknown"}) is None


@pytest.mark.parametrize("invalid", ["", "sha256:xyz", "sha256:" + "A" * 64, "a" * 129, 2, True, []])
def test_bad_etag_rejected_before_cache(invalid):
    with pytest.raises(ValueError):
        validate_conditional_etag(invalid)


def test_exact_session_etag_returns_conditional_result_not_full_schema(live_catalog):
    instance, gateway, session = live_catalog
    full = _schema()
    assert full["ok"] is True
    value = full["result"]
    assert value["tools"][0]["name"] == "health"
    assert "input_schema" in value["tools"][0]
    etag = value["schema_cache"]["etag"]
    assert validate_conditional_etag(etag) == etag
    second = _schema(if_schema_etag=etag)
    assert second["ok"] is True
    result = second["result"]
    assert result["schema_not_modified"] is True
    assert "tools" not in result
    assert result["schema_cache"]["state"] == "not_modified"
    assert result["schema_cache"]["etag"] == etag
    assert result["schema_cache"]["scope"] == "authenticated_mcp_session"


def test_no_transport_session_denies_etag_even_with_lifespan_owner(live_catalog):
    instance, gateway, session = live_catalog
    session["transport"] = None
    result = _schema()["result"]
    assert result["schema_cache"]["state"] == "uncacheable"
    assert result["schema_cache"]["etag"] is None
    assert "input_schema" in result["tools"][0]


def test_transport_change_invalidates_even_with_same_lifecycle(live_catalog):
    instance, gateway, session = live_catalog
    baseline = _schema()["result"]["schema_cache"]["etag"]
    session["transport"] = Owner()
    fresh = _schema(if_schema_etag=baseline)["result"]
    assert fresh["schema_cache"]["state"] == "fresh"
    assert fresh["schema_cache"]["etag"] != baseline
    assert "input_schema" in fresh["tools"][0]


def test_auth_or_lifecycle_change_cannot_reuse_old_schema(live_catalog):
    instance, gateway, session = live_catalog
    cached = _schema()["result"]["schema_cache"]["etag"]
    session["identity"] = "principal-2"
    result = _schema(if_schema_etag=cached)["result"]
    assert result["schema_cache"]["state"] == "fresh"
    assert "input_schema" in result["tools"][0]
    session["identity"] = "principal-1"
    session["owner"] = Owner()
    session["transport"] = session["owner"]
    result = _schema(if_schema_etag=cached)["result"]
    assert result["schema_cache"]["state"] == "fresh"
    assert result["schema_cache"]["etag"] != cached


def test_build_schema_or_availability_change_invalidates(live_catalog, monkeypatch):
    instance, gateway, session = live_catalog
    cached = _schema()["result"]["schema_cache"]["etag"]
    monkeypatch.setenv("BUILD_SHA", "b" * 40)
    result = _schema(if_schema_etag=cached)["result"]
    assert result["schema_cache"]["state"] == "fresh"
    monkeypatch.setenv("BUILD_SHA", "a" * 40)

    def other() -> str:
        return "ok"

    instance._tool_manager.add_tool(other, name="other")
    result = _schema(if_schema_etag=cached)["result"]
    assert result["schema_cache"]["state"] == "fresh"
    instance._tool_manager._tools.pop("other")
    session["unavailable"]["health"] = "missing_dependency"
    result = _schema(if_schema_etag=cached)["result"]
    assert result["schema_cache"]["state"] == "fresh"
    assert result["tools"][0]["unavailable_reason"] == "missing_dependency"


def test_partial_gateway_ready_false_cannot_reuse_even_if_plane_ready(
    live_catalog, monkeypatch
):
    instance, gateway, session = live_catalog
    token = _schema()["result"]["schema_cache"]["etag"]
    monkeypatch.setattr(
        gateway, "health",
        lambda: {"ready": False, "command_plane": {"state": "ready"}},
    )
    value = _schema(if_schema_etag=token)["result"]
    assert value["schema_cache"]["state"] == "uncacheable"
    assert "input_schema" in value["tools"][0]


def test_ready_after_observed_starting_does_not_revive_old_etag(live_catalog):
    instance, gateway, session = live_catalog
    old = _schema()["result"]["schema_cache"]["etag"]
    gateway.state = "starting"
    during = _schema(if_schema_etag=old)["result"]
    assert during["schema_cache"]["state"] == "uncacheable"
    gateway.state = "ready"
    new = _schema(if_schema_etag=old)["result"]
    assert new["schema_cache"]["state"] == "fresh"
    assert new["schema_cache"]["etag"] != old


def test_auth_or_build_round_trip_does_not_revive_old_etag(live_catalog, monkeypatch):
    instance, gateway, session = live_catalog
    old = _schema()["result"]["schema_cache"]["etag"]
    session["identity"] = "other-principal"
    assert _schema(if_schema_etag=old)["result"]["schema_cache"]["state"] == "fresh"
    session["identity"] = "principal-1"
    result = _schema(if_schema_etag=old)["result"]
    assert result["schema_cache"]["state"] == "fresh"
    assert result["schema_cache"]["etag"] != old

    second = result["schema_cache"]["etag"]
    monkeypatch.setenv("BUILD_SHA", "b" * 40)
    assert _schema(if_schema_etag=second)["result"]["schema_cache"]["state"] == "fresh"
    monkeypatch.setenv("BUILD_SHA", "a" * 40)
    restored = _schema(if_schema_etag=second)["result"]
    assert restored["schema_cache"]["state"] == "fresh"
    assert restored["schema_cache"]["etag"] != second


def test_health_failure_then_recovery_invalidates_previous_token(live_catalog):
    instance, gateway, session = live_catalog
    original = _schema()["result"]["schema_cache"]["etag"]
    gateway.raise_health = True
    during = _schema(if_schema_etag=original)["result"]
    assert during["schema_cache"]["state"] == "uncacheable"
    gateway.raise_health = False
    after = _schema(if_schema_etag=original)["result"]
    assert after["schema_cache"]["state"] == "fresh"
    assert after["schema_cache"]["etag"] != original


def test_not_ready_or_not_authenticated_never_returns_304(live_catalog):
    instance, gateway, session = live_catalog
    previous = _schema()["result"]["schema_cache"]["etag"]
    gateway.state = "starting"
    result = _schema(if_schema_etag=previous)["result"]
    assert result["schema_cache"] == {
        "state": "uncacheable", "etag": None, "reuse_permitted": False,
    }
    assert "input_schema" in result["tools"][0]
    gateway.state = "ready"
    session["identity"] = None
    result = _schema(if_schema_etag=previous)["result"]
    assert result["schema_cache"]["state"] == "uncacheable"
    assert "input_schema" in result["tools"][0]
    session["identity"] = "principal-1"
    gateway.raise_health = True
    result = _schema(if_schema_etag=previous)["result"]
    assert result["schema_cache"]["state"] == "uncacheable"
    assert "input_schema" in result["tools"][0]


def test_invalid_conditional_etag_rejected_without_gateway_probe(live_catalog):
    instance, gateway, session = live_catalog
    gateway.raise_health = True
    result = _schema(if_schema_etag="sha256:forged")
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "forged" not in str(result)

    token = "sha256:" + "a" * 64
    result = catalog.gateway_catalog(if_schema_etag=token)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


def test_native_mcp_schema_advertises_conditional_parameter_without_coercion():
    mcp = FastMCP("conditional-schema")
    tool = mcp._tool_manager.add_tool(catalog.gateway_catalog, name="gateway_catalog")
    assert "if_schema_etag" in tool.parameters["properties"]
    args = tool.fn_metadata.arg_model
    token = "sha256:" + "f" * 64
    assert args.model_validate({
        "tool_name": "health", "include_schema": True, "if_schema_etag": token,
    }).if_schema_etag == token
    with pytest.raises(ValidationError):
        args.model_validate({"if_schema_etag": ["untrusted"]})


@pytest.mark.asyncio
async def test_actual_registered_fastmcp_conditional_schema_roundtrip(monkeypatch):
    """The real MCP protocol advertises and returns the conditional lease."""
    from examples.mcp_server import server

    owner = Owner()
    gateway = Gateway()
    monkeypatch.setenv("BUILD_SHA", "d" * 40)
    monkeypatch.setattr(server, "_current_mcp_lifecycle_owner", lambda: owner)
    monkeypatch.setattr(server, "_current_mcp_session", lambda: owner)
    monkeypatch.setattr(server, "_current_auth_reuse_key", lambda: "principal-protocol")
    monkeypatch.setattr(server, "get_gateway_client", lambda: gateway)
    monkeypatch.setattr(server, "_unavailable_tool_reasons", lambda: {})

    first = await server.mcp.call_tool(
        "gateway_catalog", {"tool_name": "health", "include_schema": True}
    )
    assert first.isError is False
    full = first.structuredContent["result"]
    token = full["schema_cache"]["etag"]
    assert validate_conditional_etag(token) == token
    assert full["tools"][0]["name"] == "health"

    conditional = await server.mcp.call_tool(
        "gateway_catalog",
        {"tool_name": "health", "include_schema": True, "if_schema_etag": token},
    )
    assert conditional.isError is False
    result = conditional.structuredContent["result"]
    assert result["schema_not_modified"] is True
    assert "tools" not in result
    assert result["schema_cache"]["state"] == "not_modified"


def test_legacy_discovery_without_schema_preserves_result_shape(live_catalog):
    instance, gateway, session = live_catalog
    output = catalog.gateway_catalog(tool_name="health")
    assert output["ok"] is True
    assert "schema_cache" not in output["result"]
    assert output["result"]["tools"][0]["name"] == "health"
