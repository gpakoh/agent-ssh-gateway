"""Regression tests for ProjectAnalytics shell-injection bug.

Every command builder in project_analytics.py (_get_file_stats,
_get_code_stats, _get_git_stats, _get_test_stats, _get_dependency_stats)
built its "cd '{path}' && ..." command via a naive single-quote wrap with
NO escaping of embedded quotes. path is attacker-controllable via
POST /api/analytics's ProjectAnalyticsRequest.path (master-key gated,
same threat model as other fixes this session).

Each probe drives the real method (a fake self._ssh.execute captures the
command) and executes the first generated command for real via `sh -c`,
checking for a marker file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.project_analytics import ProjectAnalytics


def _payload(marker: Path) -> str:
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


def _make_ssh() -> AsyncMock:
    ssh = AsyncMock()
    ssh.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
    return ssh


async def _assert_first_command_safe(coro_factory, marker: Path):
    ssh = _make_ssh()
    analytics = ProjectAnalytics(ssh)
    await coro_factory(analytics, ssh)
    command = ssh.execute.call_args_list[0].args[1]
    # Some pre-fix commands, once the injection payload lets them fall
    # through to the real trailing command (e.g. "pip list --outdated"),
    # can reach out to the network and hang. The marker touch always runs
    # first in the chain, so a short timeout is enough to observe it
    # without waiting on an unrelated network call.
    try:
        subprocess.run(["sh", "-c", command], check=False, timeout=10)
    except subprocess.TimeoutExpired:
        pass
    assert not _marker_hit(marker), f"let path break out of shell quoting: {command!r}"


class TestProjectAnalyticsShellInjection:
    @pytest.mark.asyncio
    async def test_get_file_stats_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"

        async def run(analytics, ssh):
            await analytics._get_file_stats("s1", _payload(marker))

        await _assert_first_command_safe(run, marker)

    @pytest.mark.asyncio
    async def test_get_code_stats_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"

        async def run(analytics, ssh):
            await analytics._get_code_stats("s1", _payload(marker))

        await _assert_first_command_safe(run, marker)

    @pytest.mark.asyncio
    async def test_get_git_stats_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"

        async def run(analytics, ssh):
            await analytics._get_git_stats("s1", _payload(marker))

        await _assert_first_command_safe(run, marker)

    @pytest.mark.asyncio
    async def test_get_test_stats_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"

        async def run(analytics, ssh):
            await analytics._get_test_stats("s1", _payload(marker))

        await _assert_first_command_safe(run, marker)

    @pytest.mark.asyncio
    async def test_get_dependency_stats_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"

        async def run(analytics, ssh):
            await analytics._get_dependency_stats("s1", _payload(marker))

        await _assert_first_command_safe(run, marker)
