"""Regression tests for GitManager shell-injection bugs.

git_manager.py had zero test coverage before this — every method built
its shell command via a hand-rolled `path.replace("'", "'\"'\"'")`
escape (correct for the escaped value alone, but several call sites never
applied ANY escaping to other interpolated values at all):
  - init_repo()'s remote_url went straight into the command with no
    quoting whatsoever.
  - create_backup()'s backup_name went straight into `-m '{backup_name}'`
    with no escaping of an embedded quote.
  - commit()'s files list used a naive f"'{f}'" wrap with no escaping of
    an embedded quote in a filename.

Each probe builds the real command via the actual GitManager method (a
fake self._ssh.execute captures what would have been sent over SSH) and
then executes that string for real via `sh -c`, checking for a marker
file — the same empirical style used elsewhere in this session for
shell-injection regressions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.git_manager import GitManager


def _payload(marker: Path) -> str:
    """Breaks out of BOTH an unquoted interpolation and a hand-wrapped
    single-quote string — same combined payload used in
    test_seam_shell_injection_matrix.py."""
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


def _make_ssh(exit_code: int = 0, stdout: str = "") -> AsyncMock:
    ssh = AsyncMock()
    ssh.execute = AsyncMock(return_value={"stdout": stdout, "stderr": "", "exit_code": exit_code})
    return ssh


class TestGitManagerShellInjection:
    @pytest.mark.asyncio
    async def test_init_repo_remote_url_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh(stdout="/usr/bin/git")  # "which git" succeeds
        gm = GitManager(ssh)

        await gm.init_repo("s1", str(tmp_path), remote_url=_payload(marker))

        # Last call is the "git remote add origin ..." command.
        command = ssh.execute.call_args_list[-1].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"init_repo() let remote_url break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_create_backup_name_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        gm = GitManager(ssh)

        await gm.create_backup("s1", str(tmp_path), _payload(marker))

        command = ssh.execute.call_args_list[-1].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"create_backup() let backup_name break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_commit_file_list_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        gm = GitManager(ssh)

        await gm.commit("s1", str(tmp_path), "a message", files=[_payload(marker)])

        # First call is "git add <files>".
        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"commit()'s file list let a filename break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_commit_message_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        gm = GitManager(ssh)

        await gm.commit("s1", str(tmp_path), _payload(marker), files=None)

        # Second call is "git commit -m <message>".
        command = ssh.execute.call_args_list[1].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"commit()'s message let it break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_check_git_status_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh(stdout="GIT_REPO")
        gm = GitManager(ssh)

        await gm.check_git_status("s1", _payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"check_git_status()'s path let it break out of shell quoting: {command!r}"
        )
