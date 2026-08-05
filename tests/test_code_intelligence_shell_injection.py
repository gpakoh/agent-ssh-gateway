"""Regression tests for CodeIntelligence.search_code() shell-injection bug.

search_code() built its grep command with `path` interpolated with ZERO
shell quoting, and `query` wrapped in single quotes with no escaping of
embedded quotes. Both are attacker-controllable via POST /api/code/search's
CodeSearchRequest.path/.query (master-key gated, same threat model as
other fixes this session).

Each probe drives the real method (a fake self._ssh.execute captures the
command) and executes it for real via `sh -c`, checking for a marker file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.code_intelligence import CodeIntelligence


def _payload(marker: Path) -> str:
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


def _make_ssh() -> AsyncMock:
    ssh = AsyncMock()
    ssh.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 1})
    return ssh


class TestCodeIntelligenceShellInjection:
    @pytest.mark.asyncio
    async def test_search_code_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        ci = CodeIntelligence(ssh, file_editor=None)

        await ci.search_code("s1", _payload(marker), "query")

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"search_code() let path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_search_code_query_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        ci = CodeIntelligence(ssh, file_editor=None)

        await ci.search_code("s1", str(tmp_path), _payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"search_code() let query break out of shell quoting: {command!r}"
        )
