"""Operator-controlled network isolation for allowlisted disposable images."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

import examples.mcp_server.server as srv
from examples.mcp_client_remote.fleet.docker_client import DockerClient, RunResult

OWNER = "examples.mcp_server.mcp_infra.adapters.docker._get_confirmation_owner_fingerprint"
SCOPES = "examples.mcp_server.mcp_infra.adapters.docker._get_token_scopes"
IMAGE = "192.168.1.103:3005/gpakoh/quart-core@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def clean_actions(monkeypatch):
    monkeypatch.delenv("MCP_DOCKER_RUN_ISOLATE_NETWORK", raising=False)
    srv._confirm_store._actions.clear()
    srv._confirm_store._token_map.clear()
    yield
    srv._confirm_store._actions.clear()
    srv._confirm_store._token_map.clear()


@pytest.mark.asyncio
async def test_isolation_survives_confirmation_and_cannot_be_downgraded(monkeypatch):
    monkeypatch.setenv("MCP_DOCKER_RUN_ALLOWED_IMAGES", IMAGE)
    monkeypatch.setenv("MCP_DOCKER_RUN_ISOLATE_NETWORK", "1")
    with patch(OWNER, return_value="owner-a"):
        pending = await srv.docker_run(image=IMAGE, command=["python", "--version"])
    assert pending["ok"] is True
    assert "network=none" in pending["result"]["summary"]

    # The pending action must keep the stricter policy even if config changes.
    monkeypatch.setenv("MCP_DOCKER_RUN_ISOLATE_NETWORK", "0")
    with patch(OWNER, return_value="owner-a"), patch(SCOPES, return_value=["mcp:docker:admin"]):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            docker = AsyncMock()
            docker.run.return_value = RunResult(stdout="ok", stderr="", exit_code=0)
            docker_cls.return_value = docker
            result = await srv.confirm_operation(action_id=pending["result"]["action_id"])
    assert result["ok"] is True
    docker.run.assert_awaited_once_with(
        IMAGE, ["python", "--version"], container_name=None, timeout=60, isolate_network=True
    )


@pytest.mark.asyncio
async def test_isolation_mode_never_weakens_image_or_scope_guard(monkeypatch):
    monkeypatch.setenv("MCP_DOCKER_RUN_ALLOWED_IMAGES", IMAGE)
    monkeypatch.setenv("MCP_DOCKER_RUN_ISOLATE_NETWORK", "1")
    refused = await srv.docker_run(image=IMAGE[:-1] + "b", command=["python", "--version"])
    assert refused["ok"] is False
    assert refused["error"]["code"] == "DOCKER_RUN_IMAGE_NOT_ALLOWED"
    with patch(OWNER, return_value="owner-a"):
        pending = await srv.docker_run(image=IMAGE, command=["python", "--version"])
    with patch(OWNER, return_value="owner-a"), patch(SCOPES, return_value=["mcp:docker"]):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            result = await srv.confirm_operation(action_id=pending["result"]["action_id"])
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIRM_SCOPE_DENIED"
    docker_cls.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_operator_network_policy_denies_before_confirmation(monkeypatch):
    monkeypatch.setenv("MCP_DOCKER_RUN_ALLOWED_IMAGES", IMAGE)
    monkeypatch.setenv("MCP_DOCKER_RUN_ISOLATE_NETWORK", "maybe")
    result = await srv.docker_run(image=IMAGE, command=["python", "--version"])
    assert result["ok"] is False
    assert result["error"]["code"] == "DOCKER_RUN_NETWORK_POLICY_INVALID"
    assert not srv._confirm_store._actions


@pytest.mark.asyncio
async def test_client_sets_none_before_exact_image_and_argv():
    client = DockerClient()
    with patch.object(
        client, "_run_with_result", new=AsyncMock(return_value=RunResult("", "", 0))
    ) as run:
        await client.run(IMAGE, ["python", "--version"], isolate_network=True)
    argv = run.await_args.args[0]
    assert argv[1:] == ["run", "--rm", "--network", "none", IMAGE, "python", "--version"]


def test_oauth_compose_passes_isolation_as_operator_opt_in():
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml").read_text()
    )
    env = compose["services"]["mcp-oauth"]["environment"]
    assert "MCP_DOCKER_RUN_ISOLATE_NETWORK=${MCP_DOCKER_RUN_ISOLATE_NETWORK:-0}" in env
