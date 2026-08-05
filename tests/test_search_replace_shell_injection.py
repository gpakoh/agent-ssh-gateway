"""Regression tests for GlobalSearchReplace shell-injection bugs.

search() built its grep/find commands with:
  - `path` interpolated with ZERO shell quoting in "cd {path} && ...".
  - `file_pattern` wrapped in single quotes with no escaping of embedded
    quotes in "find . -type f -name '{file_pattern}' ...".
  - `files_arg` (filenames returned by a prior `find` call) joined and
    interpolated completely unquoted into the grep command.

path and file_pattern are attacker-controllable via POST /api/search/global's
GlobalSearchRequest.path/.file_pattern (master-key gated, same threat model
as other fixes this session); files_arg is attacker-influenceable via
filenames present on the target filesystem.

Each probe drives the real search() method (a fake self._ssh.execute
captures the command(s)) and executes the relevant command for real via
`sh -c`, checking for a marker file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.search_replace import GlobalSearchReplace


def _payload(marker: Path) -> str:
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


class TestSearchReplaceShellInjection:
    @pytest.mark.asyncio
    async def test_search_path_is_escaped_default_pattern(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 1})
        gsr = GlobalSearchReplace(ssh, file_editor=None)

        await gsr.search("s1", _payload(marker), "query")

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"search() let path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_search_file_pattern_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        gsr = GlobalSearchReplace(ssh, file_editor=None)

        await gsr.search("s1", str(tmp_path), "query", file_pattern=_payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"search()'s find command let file_pattern break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_search_files_arg_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        payload = _payload(marker)
        ssh = AsyncMock()

        async def side_effect(session_id, command, timeout=10):
            if command.strip().startswith("cd") and "find ." in command:
                return {"stdout": payload, "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        ssh.execute = AsyncMock(side_effect=side_effect)
        gsr = GlobalSearchReplace(ssh, file_editor=None)

        await gsr.search("s1", str(tmp_path), "query", file_pattern="*.py")

        command = ssh.execute.call_args_list[1].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"search()'s grep command let a found filename break out of shell quoting: {command!r}"
        )
