"""Regression tests for action-id Docker confirmation fencing."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

import examples.mcp_server.server as srv
from examples.mcp_server.docker_confirm import ConfirmStore

OWNER_HELPER = "examples.mcp_server.mcp_infra.adapters.docker._get_confirmation_owner_fingerprint"


@pytest.fixture(autouse=True)
def _clean_confirm_store():
    srv._confirm_store._actions.clear()
    srv._confirm_store._token_map.clear()
    yield
    srv._confirm_store._actions.clear()
    srv._confirm_store._token_map.clear()


async def _create_restart(
    *, owner: str | None = "owner-a", container: str = "mcp-oauth", timeout: int = 10
) -> dict:
    with patch(OWNER_HELPER, return_value=owner):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            docker_cls.return_value._validate_container_name.return_value = None
            result = await srv.docker_restart(container=container, timeout=timeout)
    assert result["ok"] is True
    assert result["result"]["status"] == "confirmation_required"
    return result["result"]


@pytest.mark.asyncio
async def test_action_id_confirmation_executes_exact_pending_restart():
    pending = await _create_restart(container="mcp-oauth", timeout=17)

    with patch(OWNER_HELPER, return_value="owner-a"):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            docker = AsyncMock()
            docker.restart.return_value = "restarted"
            docker_cls.return_value = docker
            result = await srv.confirm_operation(action_id=pending["action_id"])

    assert result["ok"] is True
    assert result["result"]["output"] == "restarted"
    docker.restart.assert_awaited_once_with("mcp-oauth", timeout=17)


@pytest.mark.asyncio
async def test_action_id_replay_is_rejected():
    pending = await _create_restart()
    with patch(OWNER_HELPER, return_value="owner-a"):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            docker = AsyncMock()
            docker.restart.return_value = "restarted"
            docker_cls.return_value = docker
            first = await srv.confirm_operation(action_id=pending["action_id"])
            replay = await srv.confirm_operation(action_id=pending["action_id"])

    assert first["ok"] is True
    assert replay["ok"] is False
    assert replay["error"]["code"] == "CONFIRM_TOKEN_CONSUMED"
    assert docker.restart.await_count == 1


@pytest.mark.asyncio
async def test_action_id_expired_is_rejected_without_execution():
    pending = await _create_restart()
    srv._confirm_store._actions[pending["action_id"]].created_at = time.monotonic() - 120

    with patch(OWNER_HELPER, return_value="owner-a"):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            docker = AsyncMock()
            docker_cls.return_value = docker
            result = await srv.confirm_operation(action_id=pending["action_id"])

    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIRM_TOKEN_EXPIRED"
    docker.restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_random_action_id_is_rejected():
    with patch(OWNER_HELPER, return_value="owner-a"):
        result = await srv.confirm_operation(action_id="0" * 32)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_action_id_wrong_owner_is_rejected_and_not_consumed():
    pending = await _create_restart(owner="owner-a")

    with patch(OWNER_HELPER, return_value="owner-b"):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            docker = AsyncMock()
            docker_cls.return_value = docker
            result = await srv.confirm_operation(action_id=pending["action_id"])

    assert result["ok"] is False
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert srv._confirm_store._actions[pending["action_id"]].consumed is False
    docker.restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_id_missing_owner_fails_closed():
    pending = await _create_restart(owner=None)
    with patch(OWNER_HELPER, return_value=None):
        result = await srv.confirm_operation(action_id=pending["action_id"])
    assert result["ok"] is False
    assert result["error"]["code"] == "AUTH_ERROR"
    assert srv._confirm_store._actions[pending["action_id"]].consumed is False


@pytest.mark.asyncio
async def test_confirmation_requires_exactly_one_selector():
    pending = await _create_restart()
    none = await srv.confirm_operation()
    both = await srv.confirm_operation(
        token=pending["confirm_token"], action_id=pending["action_id"]
    )
    assert none["ok"] is False
    assert none["error"]["code"] == "INVALID_INPUT"
    assert both["ok"] is False
    assert both["error"]["code"] == "INVALID_INPUT"
    assert srv._confirm_store._actions[pending["action_id"]].consumed is False


@pytest.mark.asyncio
async def test_confirmation_of_action_a_cannot_execute_action_b_or_other_args():
    action_a = await _create_restart(owner="owner-a", container="alpha", timeout=7)
    action_b = await _create_restart(owner="owner-b", container="beta", timeout=23)

    with patch(OWNER_HELPER, return_value="owner-a"):
        with patch("examples.mcp_server.server.DockerClient") as docker_cls:
            docker = AsyncMock()
            docker.restart.return_value = "restarted"
            docker_cls.return_value = docker
            wrong = await srv.confirm_operation(action_id=action_b["action_id"])
            right = await srv.confirm_operation(action_id=action_a["action_id"])

    assert wrong["ok"] is False
    assert wrong["error"]["code"] == "PERMISSION_DENIED"
    assert right["ok"] is True
    docker.restart.assert_awaited_once_with("alpha", timeout=7)
    assert srv._confirm_store._actions[action_b["action_id"]].consumed is False


def test_confirm_store_snapshots_mutable_kwargs_at_creation():
    store = ConfirmStore()
    services = ["api"]
    kwargs = {"project_dir": "/app", "services": services, "timeout": 30}
    action = store.create_action("docker_compose_restart", kwargs, "restart api")

    services.append("worker")
    kwargs["timeout"] = 99

    assert action.kwargs == {
        "project_dir": "/app",
        "services": ["api"],
        "timeout": 30,
    }


def test_action_id_lookup_is_bound_to_exact_stored_action_and_args():
    store = ConfirmStore()
    first = store.create_action(
        "docker_restart", {"container": "alpha", "timeout": 7}, "restart alpha"
    )
    second = store.create_action(
        "docker_restart", {"container": "beta", "timeout": 23}, "restart beta"
    )

    resolved, status = store.peek_action_id(second.action_id)

    assert status.value == "ok"
    assert resolved is second
    assert resolved is not first
    assert resolved.kwargs == {"container": "beta", "timeout": 23}


@pytest.mark.asyncio
async def test_pending_list_does_not_expose_owner_fingerprint():
    pending = await _create_restart(owner="owner-a")
    result = await srv.docker_pending_actions()
    item = next(row for row in result["result"]["items"] if row["action_id"] == pending["action_id"])
    assert "owner_fingerprint" not in item


@pytest.mark.asyncio
async def test_action_id_admin_scope_is_rechecked_before_consume():
    action = srv._confirm_store.create_action(
        "docker_exec",
        {"container": "web", "command": ["ls"], "timeout": 30},
        "Exec in web: ls",
        required_scope="mcp:docker:admin",
        owner_fingerprint="owner-a",
    )
    with patch(OWNER_HELPER, return_value="owner-a"):
        with patch(
            "examples.mcp_server.mcp_infra.adapters.docker._get_token_scopes",
            return_value=["mcp:docker"],
        ):
            result = await srv.confirm_operation(action_id=action.action_id)

    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIRM_SCOPE_DENIED"
    assert action.consumed is False


@pytest.mark.asyncio
async def test_legacy_token_confirmation_still_works_without_owner_context():
    action = srv._confirm_store.create_action(
        "docker_restart",
        {"container": "legacy", "timeout": 5},
        "Restart container legacy",
    )
    with patch("examples.mcp_server.server.DockerClient") as docker_cls:
        docker = AsyncMock()
        docker.restart.return_value = "restarted"
        docker_cls.return_value = docker
        result = await srv.confirm_operation(token=action.confirm_token)

    assert result["ok"] is True
    docker.restart.assert_awaited_once_with("legacy", timeout=5)
