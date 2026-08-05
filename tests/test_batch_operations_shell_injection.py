"""Regression tests for BatchOperationsManager shell-injection bugs.

batch_operations.py built shell commands via naive single-quote wraps
with NO escaping of embedded quotes (_execute_delete/_execute_rename/
_execute_copy/_execute_create's path), and _execute_command's
"cd {cwd} && ..." had NO quoting at all around cwd (cwd == base_path ==
ctx.path, attacker-controllable via POST /api/context/create). All of
path/new_path/dest_path are likewise raw, unconstrained request fields
via POST /api/batch/execute's BatchOperationItem.

_execute_create additionally embedded raw, attacker-controlled file
content directly inside a heredoc with no encoding: content containing
a line that happened to match the heredoc terminator ('EOF_BATCH')
would terminate the heredoc early and let the REMAINDER of content
execute as literal shell commands -- a more severe bug than simple
quote-escaping, fixed by switching to a base64-encoded heredoc (same
approach as FileEditor.write_file()).

Each probe drives the real method (a fake self._ssh.execute captures
the command) and executes it for real via `sh -c`, checking for a
marker file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.batch_operations import BatchOperationsManager


def _payload(marker: Path) -> str:
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


def _make_ssh(exit_code: int = 0, stdout: str = "") -> AsyncMock:
    ssh = AsyncMock()
    ssh.execute = AsyncMock(return_value={"stdout": stdout, "stderr": "", "exit_code": exit_code})
    return ssh


def _make_manager(ssh: AsyncMock) -> BatchOperationsManager:
    return BatchOperationsManager(ssh, file_editor=None, context_manager=None)


class TestBatchOperationsShellInjection:
    @pytest.mark.asyncio
    async def test_execute_delete_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        mgr = _make_manager(ssh)

        await mgr._execute_delete("s1", _payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_execute_delete() let path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_rename_old_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        mgr = _make_manager(ssh)

        await mgr._execute_rename("s1", _payload(marker), "new_name")

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_execute_rename() let old_path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_copy_src_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        mgr = _make_manager(ssh)

        await mgr._execute_copy("s1", _payload(marker), "dest")

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_execute_copy() let src_path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_command_cwd_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        mgr = _make_manager(ssh)

        await mgr._execute_command("s1", "echo hi", _payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_execute_command() let cwd break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_create_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        mgr = _make_manager(ssh)

        await mgr._execute_create("s1", _payload(marker), "some content")

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_execute_create() let path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_create_content_cannot_break_heredoc(self, tmp_path):
        """Regression: a raw (non-base64) heredoc body let file content
        containing a literal 'EOF_BATCH' line terminate the heredoc early,
        running the rest of content as shell commands."""
        marker = tmp_path / "pwned"
        target = tmp_path / "out.txt"
        content = f"line one\nEOF_BATCH\ntouch {marker}\nline after\n"
        ssh = _make_ssh()
        mgr = _make_manager(ssh)

        await mgr._execute_create("s1", str(target), content)

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not marker.exists(), (
            f"_execute_create()'s heredoc let content break out via an embedded "
            f"terminator line: {command!r}"
        )
        assert target.read_text() == content, "file content should be written verbatim"
