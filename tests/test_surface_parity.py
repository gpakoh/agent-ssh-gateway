from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from examples.mcp_server.mcp_infra._server_ref import server_module
from examples.mcp_server.surface_parity import (
    MAX_CLIENT_VISIBLE_TOOL_NAME_BYTES,
    MAX_CLIENT_VISIBLE_TOOL_NAMES_BYTES,
    ClientSurfaceAttestation,
    clear_session_attestation,
    evaluate_required_guards,
    load_session_attestation,
    normalize_client_visible_tool_names,
    normalize_required_guard_tool_names,
    record_session_attestation,
)


@pytest.fixture(autouse=True)
def _clear_attestations() -> None:
    """Rebind helpers after tests that deliberately reload the MCP package."""

    parity = importlib.import_module("examples.mcp_server.surface_parity")
    globals().update(
        {
            "MAX_CLIENT_VISIBLE_TOOL_NAME_BYTES": parity.MAX_CLIENT_VISIBLE_TOOL_NAME_BYTES,
            "MAX_CLIENT_VISIBLE_TOOL_NAMES_BYTES": parity.MAX_CLIENT_VISIBLE_TOOL_NAMES_BYTES,
            "ClientSurfaceAttestation": parity.ClientSurfaceAttestation,
            "clear_all_attestations_for_tests": parity.clear_all_attestations_for_tests,
            "clear_session_attestation": parity.clear_session_attestation,
            "evaluate_required_guards": parity.evaluate_required_guards,
            "load_session_attestation": parity.load_session_attestation,
            "normalize_client_visible_tool_names": parity.normalize_client_visible_tool_names,
            "normalize_required_guard_tool_names": parity.normalize_required_guard_tool_names,
            "record_session_attestation": parity.record_session_attestation,
        }
    )
    parity.clear_all_attestations_for_tests()
    yield
    importlib.import_module("examples.mcp_server.surface_parity").clear_all_attestations_for_tests()


def test_session_attestation_round_trip_requires_same_owner_auth_and_hash() -> None:
    owner = object()
    saved = record_session_attestation(owner, "auth-a", "hash-a", ["z", "a"], True)

    assert saved.names == ("a", "z")
    assert load_session_attestation(owner, "auth-a", "hash-a") == saved
    assert load_session_attestation(owner, "auth-b", "hash-a") is None
    assert load_session_attestation(owner, "auth-a", "hash-b") is None


def test_parallel_lifecycles_with_same_auth_do_not_cross_overwrite_or_clear() -> None:
    first = object()
    second = object()
    record_session_attestation(first, "same-auth", "hash", ["alpha"], True)
    record_session_attestation(second, "same-auth", "hash", ["beta"], False)

    assert load_session_attestation(first, "same-auth", "hash").names == ("alpha",)  # type: ignore[union-attr]
    assert load_session_attestation(second, "same-auth", "hash").names == ("beta",)  # type: ignore[union-attr]

    clear_session_attestation(first)
    assert load_session_attestation(first, "same-auth", "hash") is None
    assert load_session_attestation(second, "same-auth", "hash") is not None


def test_none_lifecycle_cannot_be_stored() -> None:
    with pytest.raises(ValueError, match="lifecycle_owner"):
        record_session_attestation(None, "auth", "hash", ["alpha"], True)  # type: ignore[arg-type]


def test_client_tool_name_limits_and_grammar_are_fail_closed() -> None:
    with pytest.raises(ValueError, match="maximum count"):
        normalize_client_visible_tool_names([f"tool_{i}" for i in range(257)])

    too_long = "a" * (MAX_CLIENT_VISIBLE_TOOL_NAME_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds"):
        normalize_client_visible_tool_names([too_long])

    chunk = "a" * 128
    aggregate = [f"{i:03d}_{chunk}"[:128] for i in range(129)]
    assert sum(len(name.encode()) for name in aggregate) > MAX_CLIENT_VISIBLE_TOOL_NAMES_BYTES
    with pytest.raises(ValueError, match="aggregate"):
        normalize_client_visible_tool_names(aggregate)

    for invalid in ("", "bad name", "tool/slash", "tool:colon", " leading"):
        with pytest.raises(ValueError, match="tool name"):
            normalize_client_visible_tool_names([invalid])


def test_names_are_deduplicated_sorted_and_unknown_names_are_retained() -> None:
    assert normalize_client_visible_tool_names(
        ["known_tool", "unknown_but_valid", "known_tool"]
    ) == ("known_tool", "unknown_but_valid")


def test_required_guard_names_have_small_bounded_surface() -> None:
    assert normalize_required_guard_tool_names(["git_fetch_ref", "git_fetch_ref"]) == (
        "git_fetch_ref",
    )
    with pytest.raises(ValueError, match="maximum count"):
        normalize_required_guard_tool_names([f"guard_{i}" for i in range(33)])


def test_guard_evaluator_distinguishes_unknown_reported_present_and_reported_absent() -> None:
    required = ("git_fetch_ref",)
    assert evaluate_required_guards(None, required)["status"] == "unknown"

    incomplete = ClientSurfaceAttestation("auth", "hash", ("other",), False)
    incomplete_result = evaluate_required_guards(incomplete, required)
    assert incomplete_result["status"] == "unknown"
    assert incomplete_result["reported_absent"] == []
    assert incomplete_result["unknown"] == ["git_fetch_ref"]

    complete_absent = ClientSurfaceAttestation("auth", "hash", ("other",), True)
    absent_result = evaluate_required_guards(complete_absent, required)
    assert absent_result["status"] == "reported_absent"
    assert absent_result["reported_absent"] == ["git_fetch_ref"]

    complete_present = ClientSurfaceAttestation("auth", "hash", ("git_fetch_ref",), True)
    present_result = evaluate_required_guards(complete_present, required)
    assert present_result["status"] == "reported_present"
    assert present_result["reported_present"] == ["git_fetch_ref"]

    assert evaluate_required_guards(complete_present, ())["status"] == "no_dependency"


@pytest.mark.asyncio
async def test_mcp_lifespan_teardown_clears_attestation_before_network_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_server = server_module()
    cleared_during_release: list[bool] = []

    gateway_pool = live_server.GatewayClientSessionPool()
    agent_pool = live_server.GatewayClientSessionPool()

    def _tracking_detach(owner: object) -> list[tuple[Any, Any]]:
        cleared_during_release.append(
            load_session_attestation(owner, "auth", "hash") is None
        )
        return []

    monkeypatch.setattr(gateway_pool, "detach_owner", _tracking_detach)
    monkeypatch.setattr(agent_pool, "detach_owner", _tracking_detach)
    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(live_server, "_agent_client_sessions", agent_pool)

    async with live_server._mcp_lifespan(live_server.mcp) as owner:
        record_session_attestation(owner, "auth", "hash", ["alpha"], True)
        assert load_session_attestation(owner, "auth", "hash") is not None

    assert cleared_during_release == [True, True]
    assert load_session_attestation(owner, "auth", "hash") is None


def test_tools_manifest_unbound_report_is_one_shot_and_not_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_server = server_module()
    monkeypatch.setattr(live_server, "_current_mcp_lifecycle_owner", lambda: None)
    monkeypatch.setattr(live_server, "_current_auth_reuse_key", lambda: "auth")
    monkeypatch.setattr(live_server, "compute_toolset_hash", lambda _mcp: "hash")

    response = live_server.gateway_tools_manifest(
        client_visible_tool_names=["health", "external_only"],
        client_visible_tool_names_complete=True,
        include_descriptions=False,
    )

    assert response["ok"] is True
    observation = response["result"]["operator_surface_contract"]["client_observation"]
    assert observation["status"] == "supplied_unbound"
    assert load_session_attestation(object(), "auth", "hash") is None


def test_tools_manifest_invalid_report_does_not_overwrite_valid_session_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_server = server_module()
    owner = object()
    monkeypatch.setattr(live_server, "_current_mcp_lifecycle_owner", lambda: owner)
    monkeypatch.setattr(live_server, "_current_auth_reuse_key", lambda: "auth")
    monkeypatch.setattr(live_server, "compute_toolset_hash", lambda _mcp: "hash")

    valid = live_server.gateway_tools_manifest(
        client_visible_tool_names=["health"],
        client_visible_tool_names_complete=True,
        include_descriptions=False,
    )
    assert valid["ok"] is True
    before = load_session_attestation(owner, "auth", "hash")
    assert before is not None and before.names == ("health",)

    invalid = live_server.gateway_tools_manifest(
        client_visible_tool_names=["bad name"],
        client_visible_tool_names_complete=True,
        include_descriptions=False,
    )
    assert invalid["ok"] is False
    assert load_session_attestation(owner, "auth", "hash") == before


def test_tools_manifest_same_auth_parallel_lifecycles_keep_distinct_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_server = server_module()
    first_owner = object()
    second_owner = object()
    current_owner = [first_owner]
    monkeypatch.setattr(live_server, "_current_mcp_lifecycle_owner", lambda: current_owner[0])
    monkeypatch.setattr(live_server, "_current_auth_reuse_key", lambda: "same-auth")
    monkeypatch.setattr(live_server, "compute_toolset_hash", lambda _mcp: "hash")

    first = live_server.gateway_tools_manifest(
        client_visible_tool_names=["alpha"],
        include_descriptions=False,
    )
    assert first["ok"] is True

    current_owner[0] = second_owner
    second = live_server.gateway_tools_manifest(
        client_visible_tool_names=["beta"],
        include_descriptions=False,
    )
    assert second["ok"] is True

    current_owner[0] = first_owner
    first_again = live_server.gateway_tools_manifest(include_descriptions=False)
    assert first_again["ok"] is True
    assert first_again["result"]["operator_surface_contract"]["client_observation"][
        "reported_names"
    ] == ["alpha"]

    current_owner[0] = second_owner
    second_again = live_server.gateway_tools_manifest(include_descriptions=False)
    assert second_again["ok"] is True
    assert second_again["result"]["operator_surface_contract"]["client_observation"][
        "reported_names"
    ] == ["beta"]


def test_existing_git_mutations_are_not_parity_gated_and_no_static_dependency_map_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    sources = {
        "gitea_push_verified_commit": (
            root / "examples/mcp_server/mcp_infra/adapters/remote.py",
            "async def gitea_push_verified_commit",
        ),
        "git_update_branch_by_merge": (
            root / "examples/mcp_server/mcp_infra/adapters/gateway.py",
            "def gateway_git_update_branch_by_merge",
        ),
        "git_push": (
            root / "examples/mcp_server/mcp_infra/adapters/gateway.py",
            "def gateway_git_push",
        ),
    }
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in {item[0] for item in sources.values()}
    )
    assert "GUARD_DEPENDENCIES" not in combined

    for mutation, (path, marker) in sources.items():
        source = path.read_text(encoding="utf-8")
        start = source.find(marker)
        assert start != -1, mutation
        next_def = source.find("\ndef ", start + len(marker))
        next_async_def = source.find("\nasync def ", start + len(marker))
        candidates = [value for value in (next_def, next_async_def) if value != -1]
        end = min(candidates) if candidates else len(source)
        body = source[start:end]
        assert "evaluate_required_guards" not in body
        assert "EXTERNAL_RESOURCE_CATALOG_MISMATCH" not in body
