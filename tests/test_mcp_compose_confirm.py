"""Tests for Docker Compose confirm flow."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Set up sys.path for MCP server imports
EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))


@pytest.fixture(autouse=True)
def _mcp_started():
    """Ensure _mcp_started_at is set on the server module."""
    import examples.mcp_server.server as srv

    if not hasattr(srv, "_mcp_started_at"):
        srv._mcp_started_at = time.time()
    yield


from examples.mcp_client_remote.fleet.docker_client import RunResult  # noqa: E402
from examples.mcp_server.docker_confirm import ConfirmAction  # noqa: E402
from examples.mcp_server.server import (  # noqa: E402
    _CONFIRM_HANDLERS,
    _confirm_store,
    _docker_compose_build_impl,
    _docker_compose_restart_impl,
    _docker_compose_up_impl,
    confirm_operation,
    docker_compose_build,
    docker_compose_restart,
    docker_compose_up,
)


@pytest.fixture(autouse=True)
def _sync_server_state():
    """Re-bind server globals captured at module import time.

    test_mcp_server.py's autouse reset_env fixture calls
    importlib.reload() on examples.mcp_server.server for every test.
    Reload re-executes the module and rebinds _confirm_store,
    _CONFIRM_HANDLERS and the docker_*/impl functions to fresh objects,
    so the ``from examples.mcp_server.server import ...`` names captured
    here at import time go stale whenever test_mcp_server runs first:
    confirm tests would create actions in an old store that
    confirm_operation (reading the module's current global) never sees.
    """
    import examples.mcp_server.server as srv

    globals().update(
        {
            "_CONFIRM_HANDLERS": srv._CONFIRM_HANDLERS,
            "_confirm_store": srv._confirm_store,
            "_docker_compose_build_impl": srv._docker_compose_build_impl,
            "_docker_compose_restart_impl": srv._docker_compose_restart_impl,
            "_docker_compose_up_impl": srv._docker_compose_up_impl,
            "confirm_operation": srv.confirm_operation,
            "docker_compose_build": srv.docker_compose_build,
            "docker_compose_restart": srv.docker_compose_restart,
            "docker_compose_up": srv.docker_compose_up,
        }
    )
    yield


@pytest.fixture
def clean_confirm_store():
    """Clear confirm store before and after test."""
    _confirm_store._actions.clear()
    yield
    _confirm_store._actions.clear()


class TestComposeHandlersExist:
    """Verify compose handlers are registered in _CONFIRM_HANDLERS."""

    def test_compose_up_in_handlers(self):
        assert "docker_compose_up" in _CONFIRM_HANDLERS
        assert _CONFIRM_HANDLERS["docker_compose_up"] is _docker_compose_up_impl

    def test_compose_restart_in_handlers(self):
        assert "docker_compose_restart" in _CONFIRM_HANDLERS
        assert _CONFIRM_HANDLERS["docker_compose_restart"] is _docker_compose_restart_impl

    def test_compose_build_in_handlers(self):
        assert "docker_compose_build" in _CONFIRM_HANDLERS
        assert _CONFIRM_HANDLERS["docker_compose_build"] is _docker_compose_build_impl


class TestComposeImplFunctions:
    """Verify impl functions call DockerClient correctly."""

    @pytest.mark.asyncio
    async def test_compose_up_calls_docker_client(self):
        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_instance = AsyncMock()
            mock_instance.compose_up.return_value = "started"
            mock_dc.return_value = mock_instance

            result = await _docker_compose_up_impl(
                project_dir="/app", services=["web"], detach=True, build=False, timeout=60
            )

            assert result == "started"
            mock_instance.compose_up.assert_called_once_with(
                project_dir="/app", services=["web"], detach=True, build=False, timeout=60
            )

    @pytest.mark.asyncio
    async def test_compose_restart_calls_docker_client(self):
        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_instance = AsyncMock()
            mock_instance.compose_restart.return_value = "restarted"
            mock_dc.return_value = mock_instance

            result = await _docker_compose_restart_impl(
                project_dir="/app", services=["api"], timeout=15
            )

            assert result == "restarted"
            mock_instance.compose_restart.assert_called_once_with(
                project_dir="/app", services=["api"], timeout=15
            )

    @pytest.mark.asyncio
    async def test_compose_build_calls_docker_client(self):
        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_instance = AsyncMock()
            mock_instance.compose_build.return_value = "built"
            mock_dc.return_value = mock_instance

            result = await _docker_compose_build_impl(
                project_dir="/app", services=["worker"], no_cache=True, timeout=120
            )

            assert result == "built"
            mock_instance.compose_build.assert_called_once_with(
                project_dir="/app", services=["worker"], no_cache=True, timeout=120
            )


class TestComposeConfirmFlow:
    """Test the full create → confirm → execute flow."""

    @pytest.mark.asyncio
    async def test_compose_up_confirm_flow(self, clean_confirm_store):
        """Create action via MCP tool, confirm it, verify execution."""
        # 1. Create action
        action = await docker_compose_up(
            project_dir="/app", services=["web"], detach=True, build=False, timeout=60
        )
        assert action["ok"] is True
        result = action["result"]
        assert result["status"] == "confirmation_required"
        assert "action_id" in result
        result["action_id"]

        # 2. Confirm and execute
        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_instance = AsyncMock()
            mock_instance.compose_up.return_value = "started"
            mock_dc.return_value = mock_instance

            confirm_result = await confirm_operation(token=result["confirm_token"])

            assert confirm_result["ok"] is True
            assert confirm_result["result"]["output"] == "started"
            mock_instance.compose_up.assert_called_once()

    @pytest.mark.asyncio
    async def test_compose_restart_confirm_flow(self, clean_confirm_store):
        """Create action via MCP tool, confirm it, verify execution."""
        action = await docker_compose_restart(
            project_dir="/app", services=["api"], timeout=15
        )
        result = action["result"]
        assert result["status"] == "confirmation_required"

        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_instance = AsyncMock()
            mock_instance.compose_restart.return_value = "restarted"
            mock_dc.return_value = mock_instance

            confirm_result = await confirm_operation(token=result["confirm_token"])

            assert confirm_result["ok"] is True
            assert confirm_result["result"]["output"] == "restarted"
            mock_instance.compose_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_compose_build_confirm_flow(self, clean_confirm_store):
        """Create action via MCP tool, confirm it, verify execution."""
        action = await docker_compose_build(
            project_dir="/app", services=["worker"], no_cache=True, timeout=120
        )
        result = action["result"]
        assert result["status"] == "confirmation_required"

        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_instance = AsyncMock()
            mock_instance.compose_build.return_value = "built"
            mock_dc.return_value = mock_instance

            confirm_result = await confirm_operation(token=result["confirm_token"])

            assert confirm_result["ok"] is True
            assert confirm_result["result"]["output"] == "built"
            mock_instance.compose_build.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_result_success_is_json_serializable(self, clean_confirm_store):
        action = _confirm_store.create_action(
            "docker_rm",
            {"container": "web", "force": False},
            "Remove container web",
        )
        with patch.dict(
            _CONFIRM_HANDLERS,
            {"docker_rm": AsyncMock(return_value=RunResult("removed\n", "", 0))},
        ):
            result = await confirm_operation(token=action.confirm_token)

        assert result["ok"] is True
        assert result["result"] == {
            "stdout": "removed\n",
            "stderr": "",
            "exit_code": 0,
        }
        json.dumps(result)

    @pytest.mark.asyncio
    async def test_run_result_failure_is_structured_and_json_serializable(
        self, clean_confirm_store
    ):
        action = _confirm_store.create_action(
            "docker_rm",
            {"container": "missing", "force": False},
            "Remove container missing",
        )
        with patch.dict(
            _CONFIRM_HANDLERS,
            {
                "docker_rm": AsyncMock(
                    return_value=RunResult("", "Error: no such container", 1)
                )
            },
        ):
            result = await confirm_operation(token=action.confirm_token)

        assert result["ok"] is False
        assert result["error"]["code"] == "DOCKER_COMMAND_FAILED"
        assert result["result"] == {
            "stdout": "",
            "stderr": "Error: no such container",
            "exit_code": 1,
        }
        json.dumps(result)

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, clean_confirm_store):
        """Invalid confirm token is rejected."""
        result = await confirm_operation(token="invalid-token-123")
        assert result["ok"] is False
        assert result["error"]["code"] == "CONFIRM_TOKEN_INVALID"

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, clean_confirm_store):
        """Expired confirm token is rejected."""
        # Create action
        action = await docker_compose_up(project_dir="/app")
        action_result = action["result"]

        # Manually expire it
        stored = _confirm_store._actions.get(action_result["action_id"])
        if stored:
            stored.created_at = time.monotonic() - 120  # 2 minutes ago

        result = await confirm_operation(token=action_result["confirm_token"])
        assert result["ok"] is False
        assert result["error"]["code"] == "CONFIRM_TOKEN_EXPIRED"


class TestAdminDoubleBarrier:
    """Regression: confirming an admin-only operation must re-check the
    mcp:docker:admin scope — possession of a confirm token alone must
    not complete an admin action for a caller granted only mcp:docker."""

    def _create_admin_action(self) -> ConfirmAction:
        action = _confirm_store.create_action(
            "docker_exec",
            {"container": "web", "command": ["ls", "-la"], "timeout": 30},
            "Exec in web: ls -la",
            required_scope="mcp:docker:admin",
        )
        return action

    @pytest.mark.asyncio
    async def test_admin_action_denied_without_admin_scope(self, clean_confirm_store):
        action = self._create_admin_action()
        result = await confirm_operation(token=action.confirm_token)
        assert result["ok"] is False
        assert result["error"]["code"] == "CONFIRM_SCOPE_DENIED"
        assert "mcp:docker:admin" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_admin_action_allowed_with_admin_scope(self, clean_confirm_store):
        action = self._create_admin_action()
        with patch.dict(
            "os.environ", {"MCP_TOKEN_SCOPES": "mcp:read,mcp:docker:admin"}
        ):
            with patch.dict(
                _CONFIRM_HANDLERS,
                {"docker_exec": AsyncMock(return_value={"ok": True, "output": "done"})},
            ):
                result = await confirm_operation(token=action.confirm_token)
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_non_admin_action_ok_without_scopes(self, clean_confirm_store):
        """Compose actions (required_scope=mcp:docker default) keep working
        without a token scope context, preserving existing behavior."""
        action = await docker_compose_up(project_dir="/app")
        action_result = action["result"]
        with patch("examples.mcp_server.server.DockerClient") as mock_dc:
            mock_instance = AsyncMock()
            mock_instance.compose_up.return_value = "started"
            mock_dc.return_value = mock_instance
            result = await confirm_operation(
                token=action_result["confirm_token"]
            )
        assert result["ok"] is True
