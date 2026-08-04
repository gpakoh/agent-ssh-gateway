"""Integration tests for the seam between WorkspaceRegistry and GatewayClient.

Regression context: WorkspaceRegistry.project_info() has always returned a
plain dict (app/workspace/registry.py), but GatewayClient.execute_project_command
and execute_project_script both did `info.root` instead of `info["root"]` — an
AttributeError on every single call, taking out ~21 MCP tools. The existing
reconnect test caught none of this because it mocked project_info() with an
object exposing `.root`, matching the buggy code instead of the real dict
contract (see tests/test_gateway_client_reconnect.py).

These tests use the *real* WorkspaceRegistry — no mock of project_info() at
all — and only mock the actual network layer (_post/_run), so a future
regression on either side of this seam (registry return type, or how the
client consumes it) fails here instead of hiding behind a mock that encodes
the same wrong assumption as the code under test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(EXAMPLES_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gateway_client import GatewayClient  # noqa: E402

import app.workspace.registry as registry_module  # noqa: E402
from app.workspace.policy import WorkspacePolicyError  # noqa: E402
from app.workspace.registry import WorkspaceRegistry, reset_registry  # noqa: E402

_BASE_ENV = {
    "GATEWAY_BASE_URL": "http://gateway:8085",
    "GATEWAY_API_KEY": "test-api-key",
    "GATEWAY_SESSION_ID": "test-session-1",
    "GATEWAY_SSH_HOST": "sshd-host",
    "GATEWAY_SSH_USER": "root",
    "GATEWAY_SSH_PASSWORD": "secret",
}


def _client() -> GatewayClient:
    with patch.dict(os.environ, _BASE_ENV, clear=True):
        return GatewayClient()


@pytest.fixture
def real_registry(tmp_path):
    """A real WorkspaceRegistry, loaded from a real projects.yaml — no mocking
    of project_info() or the registry itself.
    """
    project_root = tmp_path / "demo-project"
    project_root.mkdir()
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(
        f"""
registry_root: {tmp_path}
projects:
  demo:
    root: demo-project
    type: python
    description: seam test project
    tags: []
"""
    )
    reset_registry()
    registry = WorkspaceRegistry.load(yaml_path)
    registry_module._registry = registry
    yield project_root
    reset_registry()


class TestExecuteProjectCommandRealRegistrySeam:
    def test_resolves_cwd_from_real_registry_dict(self, real_registry):
        client = _client()
        captured: dict = {}

        def _fake_post(path, payload):
            captured["path"] = path
            captured["payload"] = payload
            return {"job_id": "j1"}

        with patch.object(client, "_post", side_effect=_fake_post):
            result = client.execute_project_command("demo", "pwd")

        assert result == {"job_id": "j1"}
        assert captured["payload"]["cwd"] == str(real_registry)

    def test_unknown_project_raises_cleanly(self, real_registry):
        client = _client()
        with patch.object(client, "_post") as mock_post:
            with pytest.raises(WorkspacePolicyError, match="Unknown project"):
                client.execute_project_command("does-not-exist", "pwd")
        mock_post.assert_not_called()


class TestExecuteProjectScriptRealRegistrySeam:
    def test_writes_script_under_real_registry_root(self, real_registry):
        client = _client()

        with patch.object(client, "execute_argv") as mock_execute_argv:
            mock_execute_argv.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
            result = client.execute_project_script("demo", "echo hi")

        assert result == {"exit_code": 0, "stdout": "ok", "stderr": ""}
        call_kwargs = mock_execute_argv.call_args.kwargs
        assert call_kwargs["cwd"] == str(real_registry)
        # The temp script is cleaned up after execution.
        assert list((real_registry / ".ai-bridge" / "tmp").iterdir()) == []
