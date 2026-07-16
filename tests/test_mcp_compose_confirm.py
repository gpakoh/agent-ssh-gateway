"""Tests for Docker Compose confirm flow."""

from __future__ import annotations

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
