"""Regression tests: remotes()'s meta.redacted flag must reflect reality.

Before this fix, remotes() stripped embedded credentials out of `git
remote -v` output (e.g. https://user:token@host/repo.git -> https://***:***@
host/repo.git) but had no way to tell run_tool() that it had done so --
run_tool()'s generic success path always called tool_success() with the
default redacted=False, so the reported envelope claimed nothing was
redacted even when a real credential had just been masked.
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


@pytest.fixture(autouse=True)
def _set_auth_mode():
    with patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"}, clear=False):
        yield


class TestRemotesHelperSignalsRedaction:
    def test_credential_in_url_is_redacted_and_flagged(self):
        from examples.mcp_server.mcp_client_tools import remotes

        fake_client = type(
            "FakeClient",
            (),
            {
                "execute_project_command": lambda self, project, command: {
                    "exit_code": 0,
                    "stdout": (
                        "origin\thttps://gpakoh:secrettoken@git.xloud.ru/gpakoh/repo.git (fetch)\n"
                        "origin\thttps://gpakoh:secrettoken@git.xloud.ru/gpakoh/repo.git (push)\n"
                    ),
                    "stderr": "",
                }
            },
        )()

        result = remotes(fake_client, "myproject")
        assert result["redacted"] is True
        assert "secrettoken" not in result["stdout"]
        assert "***:***@" in result["stdout"]

    def test_no_credential_present_is_not_flagged(self):
        from examples.mcp_server.mcp_client_tools import remotes

        fake_client = type(
            "FakeClient",
            (),
            {
                "execute_project_command": lambda self, project, command: {
                    "exit_code": 0,
                    "stdout": (
                        "origin\tgit@github.com:gpakoh/repo.git (fetch)\n"
                        "origin\tgit@github.com:gpakoh/repo.git (push)\n"
                    ),
                    "stderr": "",
                }
            },
        )()

        result = remotes(fake_client, "myproject")
        assert result["redacted"] is False


class TestGatewayRemotesEndToEnd:
    """Feeds the raw credentialed output through the real gateway_remotes()
    (server.py's @register_tool("remotes") wrapper, via run_tool()) to prove
    meta.redacted flips to True end-to-end, not just inside the helper."""

    def test_meta_redacted_true_when_credential_stripped(self, monkeypatch):
        from examples.mcp_server import server as mcp_server_mod

        monkeypatch.setattr(
            mcp_server_mod.client,
            "execute_project_command",
            lambda project, command: {
                "exit_code": 0,
                "stdout": "origin\thttps://gpakoh:secrettoken@git.xloud.ru/gpakoh/repo.git (fetch)\n",
                "stderr": "",
            },
        )

        result = mcp_server_mod.gateway_remotes("myproject")
        assert result["ok"] is True
        assert result["meta"]["redacted"] is True
        assert "secrettoken" not in str(result["result"])

    def test_meta_redacted_false_when_nothing_to_redact(self, monkeypatch):
        from examples.mcp_server import server as mcp_server_mod

        monkeypatch.setattr(
            mcp_server_mod.client,
            "execute_project_command",
            lambda project, command: {
                "exit_code": 0,
                "stdout": "origin\tgit@github.com:gpakoh/repo.git (fetch)\n",
                "stderr": "",
            },
        )

        result = mcp_server_mod.gateway_remotes("myproject")
        assert result["ok"] is True
        assert result["meta"]["redacted"] is False
