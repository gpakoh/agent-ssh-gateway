"""Docker run must preserve immutable image identity and existing guards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import examples.mcp_server.server as srv
from examples.mcp_client_remote.fleet.docker_client import DockerClient, RunResult

OWNER = "examples.mcp_server.mcp_infra.adapters.docker._get_confirmation_owner_fingerprint"
SCOPES = "examples.mcp_server.mcp_infra.adapters.docker._get_token_scopes"
REPO = "192.168.1.103:3005/gpakoh/quart-core"
DIGEST = REPO + "@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def clean_pending_actions():
    srv._confirm_store._actions.clear()
    srv._confirm_store._token_map.clear()
    yield
    srv._confirm_store._actions.clear()
    srv._confirm_store._token_map.clear()


@pytest.mark.asyncio
async def test_allowlisted_digest_requires_confirmation_and_preserves_exact_ref(monkeypatch):
    monkeypatch.setenv("MCP_DOCKER_RUN_ALLOWED_IMAGES", DIGEST)
    with patch(OWNER, return_value="owner-a"):
        pending = await srv.docker_run(
            image=DIGEST, command=["echo", "ok"], container_name="digest-smoke", timeout=19
        )
    assert pending["ok"] is True
    assert pending["result"]["status"] == "confirmation_required"

    with patch(OWNER, return_value="owner-a"), patch(SCOPES, return_value=["mcp:docker:admin"]):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            docker = AsyncMock()
            docker.run.return_value = RunResult(stdout="ok", stderr="", exit_code=0)
            docker_cls.return_value = docker
            result = await srv.confirm_operation(action_id=pending["result"]["action_id"])
    assert result["ok"] is True
    docker.run.assert_awaited_once_with(
        DIGEST, ["echo", "ok"], container_name="digest-smoke", timeout=19
    )


@pytest.mark.asyncio
async def test_exact_allowlist_and_format_are_enforced_before_confirmation(monkeypatch):
    monkeypatch.setenv("MCP_DOCKER_RUN_ALLOWED_IMAGES", DIGEST)
    other = REPO + "@sha256:" + "b" * 64
    refused = await srv.docker_run(image=other, command=["echo", "ok"])
    assert refused["ok"] is False
    assert refused["error"]["code"] == "DOCKER_RUN_IMAGE_NOT_ALLOWED"

    for malformed in (
        REPO + "@sha256:" + "a" * 63,
        REPO + "@sha256:" + "A" * 64,
        REPO + "@sha256:" + "a" * 64 + ";echo",
        REPO + "@sha512:" + "a" * 64,
        REPO + "@sha256:" + "a" * 64 + "\n",
    ):
        with monkeypatch.context() as context:
            context.setenv("MCP_DOCKER_RUN_ALLOWED_IMAGES", malformed)
            result = await srv.docker_run(image=malformed, command=["echo", "ok"])
        assert result["ok"] is False
        assert result["error"]["code"] == "DOCKER_RUN_IMAGE_INVALID"
    assert not srv._confirm_store._actions


@pytest.mark.asyncio
async def test_existing_allowlisted_tag_still_requests_confirmation(monkeypatch):
    monkeypatch.setenv("MCP_DOCKER_RUN_ALLOWED_IMAGES", "alpine:3.20")
    with patch(OWNER, return_value="owner-a"):
        result = await srv.docker_run(image="alpine:3.20", command=["echo", "ok"])
    assert result["ok"] is True
    assert result["result"]["status"] == "confirmation_required"


@pytest.mark.asyncio
async def test_docker_client_passes_exact_digest_as_one_argv_element():
    client = DockerClient()
    with patch.object(
        client,
        "_run_with_result",
        new=AsyncMock(return_value=RunResult(stdout="ok", stderr="", exit_code=0)),
    ) as run:
        await client.run(DIGEST, ["echo", "ok"], container_name="digest-smoke", timeout=19)
    argv = run.await_args.args[0]
    assert argv[-3:] == [DIGEST, "echo", "ok"]
    assert Path(argv[0]).name == "docker"
    assert argv[1:-3] == [
        "run", "--rm", "--init", "--memory", "4g", "--memory-swap", "4g",
        "--cpus", "2.0", "--pids-limit", "256", "--name", "digest-smoke",
    ]


@pytest.mark.asyncio
async def test_confirmation_still_requires_docker_admin_scope(monkeypatch):
    monkeypatch.setenv("MCP_DOCKER_RUN_ALLOWED_IMAGES", DIGEST)
    with patch(OWNER, return_value="owner-a"):
        pending = await srv.docker_run(image=DIGEST, command=["echo", "ok"])
    assert pending["ok"] is True

    with patch(OWNER, return_value="owner-a"), patch(SCOPES, return_value=["mcp:docker"]):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            result = await srv.confirm_operation(action_id=pending["result"]["action_id"])
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIRM_SCOPE_DENIED"
    docker_cls.assert_not_called()
    assert not srv._confirm_store._actions[pending["result"]["action_id"]].consumed


def test_oauth_compose_passes_operator_allowlist_without_enabling_by_default():
    import yaml

    compose_path = Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    environment = compose["services"]["mcp-oauth"]["environment"]
    assert "MCP_DOCKER_RUN_ALLOWED_IMAGES=${MCP_DOCKER_RUN_ALLOWED_IMAGES:-}" in environment


@pytest.mark.asyncio
async def test_unconfigured_allowlist_still_denies_digest(monkeypatch):
    monkeypatch.delenv("MCP_DOCKER_RUN_ALLOWED_IMAGES", raising=False)
    monkeypatch.delenv("MCP_DOCKER_RUN_ISOLATED_IMAGES", raising=False)
    result = await srv.docker_run(image=DIGEST, command=["echo", "ok"])
    assert result["ok"] is False
    assert result["error"]["code"] == "DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED"
    assert not srv._confirm_store._actions
