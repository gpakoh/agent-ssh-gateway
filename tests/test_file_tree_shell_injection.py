"""Regression tests for FileTreeExplorer shell-injection bug.

get_tree() and _get_directory_children() built their `ls -la '{path}'`
command via a naive single-quote wrap with NO escaping of embedded
quotes in `path` -- attacker-controllable via POST /api/tree's
FileTreeRequest.path (master-key gated, but a real injection).

Each probe calls the real method (a fake self._ssh.execute captures the
command) and executes it for real via `sh -c`, checking for a marker file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.file_tree import FileTreeExplorer


def _payload(marker: Path) -> str:
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


class TestFileTreeShellInjection:
    @pytest.mark.asyncio
    async def test_get_tree_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "total 0", "stderr": "", "exit_code": 0})
        fte = FileTreeExplorer(ssh)

        await fte.get_tree("s1", _payload(marker), depth=0)

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"get_tree()'s ls command let path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_get_directory_children_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "total 0", "stderr": "", "exit_code": 0})
        fte = FileTreeExplorer(ssh)

        await fte._get_directory_children("s1", _payload(marker), depth=0, show_hidden=False)

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_get_directory_children()'s ls command let path break out of shell quoting: {command!r}"
        )
