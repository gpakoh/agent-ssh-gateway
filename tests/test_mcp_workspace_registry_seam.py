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


def _forbid_local_writes(monkeypatch, root: Path) -> None:
    """Make any open()/os.makedirs() under ``root`` raise EROFS, simulating
    the real ``:ro`` bind mount. Tests run as root (this whole environment
    does), so chmod()-based read-only is a no-op -- root bypasses
    permission bits, not mount flags -- and would silently fail to catch a
    regression. Faking EROFS directly proves the code path never attempts
    a local write, root or not.
    """
    real_open = open
    real_makedirs = os.makedirs

    def _guarded_open(file, mode="r", *args, **kwargs):
        if "w" in mode or "a" in mode or "+" in mode:
            path = Path(file)
            if root in path.parents or path == root:
                raise OSError(30, "Read-only file system", str(path))
        return real_open(file, mode, *args, **kwargs)

    def _guarded_makedirs(name, *args, **kwargs):
        path = Path(name)
        if root in path.parents or path == root:
            raise OSError(30, "Read-only file system", str(path))
        return real_makedirs(name, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _guarded_open)
    monkeypatch.setattr(os, "makedirs", _guarded_makedirs)


class TestExecuteProjectScriptRealRegistrySeam:
    """Audit finding (BLOCKER): execute_project_script*() used to write a
    temp .sh file directly under the registry root before executing it --
    but in production the MCP container bind-mounts that root read-only
    (see docker-compose.yml's mcp-oauth/mcp-server ``:ro`` project mount),
    so every call failed with EROFS before the script ever ran, taking
    run_tests/run_pytest/run_mypy down entirely. Both methods now pipe the
    script as stdin instead of writing it to disk at all. These tests fake
    EROFS on any write under the registry root (see _forbid_local_writes)
    so a regression back to local file writes fails loudly here instead of
    only in production.
    """

    def test_pipes_script_via_stdin_on_read_only_root(self, real_registry, monkeypatch):
        _forbid_local_writes(monkeypatch, real_registry)
        client = _client()
        with patch.object(client, "execute_argv") as mock_execute_argv:
            mock_execute_argv.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
            result = client.execute_project_script("demo", "echo hi")

        assert result == {"exit_code": 0, "stdout": "ok", "stderr": ""}
        call_args, call_kwargs = mock_execute_argv.call_args
        assert call_args[0] == ["sh"]
        assert call_kwargs["stdin"] == "echo hi"
        assert call_kwargs["cwd"] == str(real_registry)

    def test_async_pipes_script_via_stdin_on_read_only_root(self, real_registry, monkeypatch):
        _forbid_local_writes(monkeypatch, real_registry)
        client = _client()
        with patch.object(client, "execute_raw") as mock_execute_raw:
            mock_execute_raw.return_value = {"job_id": "async-1"}
            result = client.execute_project_script_async("demo", "echo hi")

        assert result == {"job_id": "async-1"}
        call_args, call_kwargs = mock_execute_raw.call_args
        assert call_args[0] == "sh"
        assert call_kwargs["stdin"] == "echo hi"
        assert call_kwargs["redact_path_prefix"] == str(real_registry)
