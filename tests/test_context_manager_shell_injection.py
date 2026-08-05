"""Regression tests for ContextManager._checkout_branch shell-injection bug.

_checkout_branch() built its "git branch --list", "git checkout", and
"git checkout -b" commands by interpolating `path` and `branch` directly
into an f-string with zero shell escaping — unlike every other SSH-command
builder in this codebase (see git_manager.py), which consistently uses
shlex.quote(). Both `path` and `branch` are attacker-controllable via
POST /api/context/create's ContextCreateRequest.path/.branch (master-key
gated, but still a real injection).

Each probe calls the real ContextManager method (a fake self._ssh.execute
captures what would have been sent over SSH) and then executes that
command string for real via `sh -c`, checking for a marker file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.context_manager import ContextManager


def _payload(marker: Path) -> str:
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


def _make_ssh(branch_list_stdout: str = "") -> AsyncMock:
    ssh = AsyncMock()
    ssh.execute = AsyncMock(
        return_value={"stdout": branch_list_stdout, "stderr": "", "exit_code": 0}
    )
    return ssh


class TestCheckoutBranchShellInjection:
    @pytest.mark.asyncio
    async def test_branch_list_command_escapes_path_and_branch(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        cm = ContextManager(ssh)

        await cm._checkout_branch("s1", str(tmp_path), _payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_checkout_branch()'s branch --list let branch break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_checkout_existing_branch_command_escapes_branch(self, tmp_path):
        marker = tmp_path / "pwned"
        payload = _payload(marker)
        # Non-empty stdout => branch "exists" => takes the plain checkout path.
        ssh = _make_ssh(branch_list_stdout=f"  {payload}\n")
        cm = ContextManager(ssh)

        await cm._checkout_branch("s1", str(tmp_path), payload)

        command = ssh.execute.call_args_list[1].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_checkout_branch()'s checkout let branch break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_checkout_new_branch_command_escapes_path(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh(branch_list_stdout="")  # branch doesn't exist => -b create path
        cm = ContextManager(ssh)

        await cm._checkout_branch("s1", _payload(marker), "feature/x")

        command = ssh.execute.call_args_list[1].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_checkout_branch()'s checkout -b let path break out of shell quoting: {command!r}"
        )
