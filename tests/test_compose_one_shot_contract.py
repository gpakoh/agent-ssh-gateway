"""P1 #879: bounded one-shot Compose runner / admin confirmation regressions.

These tests have no live Docker effect. They assert that an operator-owned
service policy, not caller-provided argv/env/network, is the execution source.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from examples.mcp_client_remote.fleet.docker_client import DockerClient, RunResult
from examples.mcp_server.mcp_infra.adapters import docker as adapter

IMAGE = "registry.example/org/postgres@sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
SECRET = "confidential-value"


def _policy() -> dict:
    return {"version": 1, "contracts": {
        "pg-ops": {
            "project_id": "infra-quart",
            "compose_project": "infra-quart",
            "service": "postgres",
            "image": IMAGE,
            "network": "internal",
            "network_name": "infra-quart_internal",
            "env_names": ["PGUSER", "PGPASSWORD", "PGDATABASE"],
            "volume_sources": {"/var/lib/postgresql/data": "pgdata"},
            "operations": {
                "readiness": ["pg_isready", "--timeout=3"],
                "migration": ["alembic", "upgrade", "head"],
                "catalog_schemas": "postgres_catalog",
            },
        }
    }}


def _compose() -> dict:
    return {
        "name": "infra-quart",
        "networks": {"internal": {"name": "infra-quart_internal", "internal": True}},
        "volumes": {"pgdata": {"name": "infra-quart_pgdata"}},
        "services": {"postgres": {
            "image": IMAGE,
            "networks": {"internal": None},
            "environment": {
                "PGUSER": "postgres", "PGPASSWORD": SECRET, "PGDATABASE": "app",
            },
            "volumes": [{"type": "volume", "source": "pgdata",
                         "target": "/var/lib/postgresql/data"}],
        }},
    }


@pytest.fixture
def harness(monkeypatch, tmp_path):
    import examples.mcp_server.server as srv

    monkeypatch.setenv("MCP_COMPOSE_ONE_SHOT_CONTRACTS", json.dumps(_policy()))
    registry = SimpleNamespace(project_info=lambda project: {"root": str(tmp_path)})
    monkeypatch.setattr(srv, "_get_workspace_registry", lambda: registry)
    client = SimpleNamespace(
        _validate_project_dir=lambda path: None,
        compose_one_shot_config=AsyncMock(return_value=_compose()),
        image_id=AsyncMock(return_value=IMAGE_ID),
        compose_one_shot=AsyncMock(return_value=RunResult("public\n", "", 0)),
    )
    monkeypatch.setattr(adapter, "_docker_client", lambda: client)
    srv._confirm_store._actions.clear()
    yield client, srv, str(tmp_path)
    srv._confirm_store._actions.clear()


@pytest.mark.asyncio
async def test_preflight_confirmation_is_high_risk_admin_and_does_not_leak_env(harness):
    client, srv, root = harness
    result = await adapter.docker_compose_one_shot("pg-ops", "readiness")
    assert result["ok"] is True
    receipt = result["result"]
    assert receipt["status"] == "confirmation_required"
    action = srv._confirm_store._actions[receipt["action_id"]]
    assert action.tool == "docker_compose_one_shot"
    assert action.required_scope == "mcp:docker:admin"
    assert action.risk == "high"
    assert action.kwargs["expected_root"] == root
    assert len(action.kwargs["expected_fingerprint"]) == 64
    assert SECRET not in repr(result)
    assert SECRET not in repr(action.kwargs)
    assert client.compose_one_shot.await_count == 0


@pytest.mark.asyncio
async def test_confirmed_readiness_runs_only_fixed_operator_argv_and_suppresses_output(harness):
    client, srv, root = harness
    prepared = await adapter.docker_compose_one_shot("pg-ops", "readiness", timeout=55)
    action = srv._confirm_store._actions[prepared["result"]["action_id"]]
    client.compose_one_shot.return_value = RunResult(SECRET, SECRET, 0)
    result = await adapter._docker_compose_one_shot_impl(**action.kwargs)
    assert result["ok"] is True
    assert result["result"]["completion"] == "command_exit_verified"
    assert result["result"]["output_redacted"] is True
    assert SECRET not in repr(result)
    client.compose_one_shot.assert_awaited_once_with(
        project_dir=root,
        service="postgres",
        argv=("pg_isready", "--timeout=3"),
        timeout=55,
    )


@pytest.mark.asyncio
async def test_secret_change_after_preflight_denies_post_without_dispatch(harness):
    client, srv, root = harness
    action_receipt = await adapter.docker_compose_one_shot("pg-ops", "migration")
    action = srv._confirm_store._actions[action_receipt["result"]["action_id"]]
    changed = _compose()
    changed["services"]["postgres"]["environment"]["PGPASSWORD"] = "changed-secret"
    client.compose_one_shot_config.return_value = changed
    result = await adapter._docker_compose_one_shot_impl(**action.kwargs)
    assert result["ok"] is False
    assert result["error"]["code"] == "ONE_SHOT_PRECONDITION_CHANGED"
    assert result["error"]["retryable"] is False
    assert client.compose_one_shot.await_count == 0
    assert "changed-secret" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"expected_image_id": "sha256:" + "c" * 64},
    {"expected_root": "/different/path"},
    {"expected_fingerprint": "f" * 64},
])
async def test_confirmation_identity_tamper_fails_closed(harness, changed):
    client, srv, root = harness
    staged = await adapter.docker_compose_one_shot("pg-ops", "readiness")
    action = srv._confirm_store._actions[staged["result"]["action_id"]]
    kwargs = {**action.kwargs, **changed}
    result = await adapter._docker_compose_one_shot_impl(**kwargs)
    assert result["ok"] is False
    assert result["error"]["code"] == "ONE_SHOT_PRECONDITION_CHANGED"
    assert client.compose_one_shot.await_count == 0


@pytest.mark.asyncio
async def test_unknown_contract_has_zero_side_effect_and_no_confirmation(harness):
    client, srv, root = harness
    result = await adapter.docker_compose_one_shot("other", "migration")
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert client.compose_one_shot.await_count == 0
    assert len(srv._confirm_store._actions) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readiness", "migration", "catalog_schemas"])
async def test_ambiguous_transport_error_does_not_replay_or_echo_secret(harness, mode):
    client, srv, root = harness
    receipt = await adapter.docker_compose_one_shot("pg-ops", mode)
    action = srv._confirm_store._actions[receipt["result"]["action_id"]]
    client.compose_one_shot.side_effect = RuntimeError(SECRET)
    result = await adapter._docker_compose_one_shot_impl(**action.kwargs)
    assert result["ok"] is False
    assert result["error"]["code"] == "ONE_SHOT_OUTCOME_UNKNOWN"
    assert result["error"]["retryable"] is False
    assert SECRET not in repr(result)
    assert client.compose_one_shot.await_count == 1


@pytest.mark.asyncio
async def test_catalog_mode_only_returns_bounded_valid_identifiers(harness):
    client, srv, root = harness
    receipt = await adapter.docker_compose_one_shot("pg-ops", "catalog_schemas")
    action = srv._confirm_store._actions[receipt["result"]["action_id"]]
    client.compose_one_shot.return_value = RunResult("BEGIN\npublic\nCOMMIT\n", "", 0)
    result = await adapter._docker_compose_one_shot_impl(**action.kwargs)
    assert result["ok"] is True
    assert result["result"]["catalog"] == ["public"]
    assert "BEGIN TRANSACTION READ ONLY" in client.compose_one_shot.await_args.kwargs["argv"][-1]


@pytest.mark.asyncio
async def test_catalog_output_is_not_accepted_when_secret_like(harness):
    client, srv, root = harness
    receipt = await adapter.docker_compose_one_shot("pg-ops", "catalog_schemas")
    action = srv._confirm_store._actions[receipt["result"]["action_id"]]
    client.compose_one_shot.return_value = RunResult("PGPASSWORD=" + SECRET, "", 0)
    result = await adapter._docker_compose_one_shot_impl(**action.kwargs)
    assert result["ok"] is False
    assert result["error"]["code"] == "ONE_SHOT_EVIDENCE_INVALID"
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_nonzero_migration_exit_does_not_claim_rollback(harness):
    client, srv, root = harness
    receipt = await adapter.docker_compose_one_shot("pg-ops", "migration")
    action = srv._confirm_store._actions[receipt["result"]["action_id"]]
    client.compose_one_shot.return_value = RunResult(SECRET, SECRET, 9)
    result = await adapter._docker_compose_one_shot_impl(**action.kwargs)
    assert result["ok"] is False
    assert result["error"]["code"] == "ONE_SHOT_COMMAND_FAILED"
    assert result["error"]["retryable"] is False
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_docker_client_enforces_compose_run_no_publish_no_dependencies(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_COMPOSE_RUNNER_IMAGE", raising=False)
    client = DockerClient()
    monkeypatch.setattr(client, "_validate_project_dir", lambda path: None)
    run = AsyncMock(return_value=RunResult("hidden", "", 0))
    monkeypatch.setattr(client, "_run_with_result", run)
    await client.compose_one_shot(
        project_dir=str(tmp_path), service="postgres",
        argv=("pg_isready", "-q"), timeout=50,
    )
    args = run.await_args.args[0]
    assert args[:2] == ["/usr/bin/docker", "compose"]
    assert args[args.index("run"):args.index("run")+5] == [
        "run", "--rm", "--no-deps", "-T", "postgres",
    ]
    assert not any(arg in args for arg in ("--publish", "-p", "--service-ports", "-P"))
    assert "pg_isready" in args
    assert "PGPASSWORD" not in repr(args)


@pytest.mark.asyncio
async def test_compose_config_errors_are_secret_opaque(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_COMPOSE_RUNNER_IMAGE", raising=False)
    client = DockerClient()
    monkeypatch.setattr(client, "_validate_project_dir", lambda path: None)
    run = AsyncMock(return_value=RunResult(SECRET, SECRET, 1))
    monkeypatch.setattr(client, "_run_with_result", run)
    with pytest.raises(RuntimeError) as exc:
        await client.compose_one_shot_config(str(tmp_path), "postgres")
    assert SECRET not in str(exc.value)


def test_server_export_and_mcp_scope_are_admin_only():
    import examples.mcp_server.server as server
    from examples.mcp_server.tool_scopes import TOOL_SCOPES

    assert server.docker_compose_one_shot is adapter.docker_compose_one_shot
    assert TOOL_SCOPES["docker_compose_one_shot"] == ["mcp:docker:admin"]
    assert server._docker_compose_one_shot_impl is adapter._docker_compose_one_shot_impl


def test_new_one_shot_visible_in_write_modes_but_blocked_in_safe_mode():
    from examples.mcp_server.tool_modes import (
        MCP_CLIENT_BLOCKED_TOOLS,
        TOOL_NAMES_BY_MODE,
        get_mcp_client_safe_tools,
        should_register_tool,
    )

    tool = "docker_compose_one_shot"
    assert tool in TOOL_NAMES_BY_MODE["mcp_client"]
    assert tool in TOOL_NAMES_BY_MODE["mcp_client_write"]
    assert tool not in TOOL_NAMES_BY_MODE["standard"]
    assert tool in MCP_CLIENT_BLOCKED_TOOLS
    assert tool not in get_mcp_client_safe_tools()
    assert should_register_tool(tool, "mcp_client")
    assert should_register_tool(tool, "mcp_client_write")
    assert not should_register_tool(tool, "minimal")
