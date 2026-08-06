"""Regression tests for SnapshotManager shell-injection bugs.

snapshot_manager.py built its shell commands via naive f-string
interpolation with zero or incomplete escaping:
  - create_snapshot()'s "cd {ctx.path} && git status ..." and
    "cd {ctx.path} && git rev-parse ..." commands had NO quoting
    whatsoever around ctx.path (attacker-controlled via
    POST /api/context/create's `path` field).
  - create_snapshot()'s "cp '{ctx.path}/{file_path}' ..." used a naive
    single-quote wrap with no escaping of an embedded quote in
    file_path (sourced from `git status --short` output).
  - restore_snapshot()/delete_snapshot()'s snapshot_id (a raw request
    field / URL path segment with no format validation) was wrapped in
    single quotes without escaping embedded quotes.

Each probe drives the real SnapshotManager method (a fake self._ssh.execute
captures the command that would have been sent over SSH; a fake
context_manager.get_context returns a context whose `.path` can carry the
injection payload) and executes the captured command for real via
`sh -c`, checking for a marker file.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.snapshot_manager import SnapshotManager


def _payload(marker: Path) -> str:
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


@dataclass
class _FakeCtx:
    path: str
    session_id: str = "s1"


def _make_ctx_manager(ctx: _FakeCtx) -> AsyncMock:
    cm = AsyncMock()
    cm.get_context = AsyncMock(return_value=ctx)
    return cm


class TestCreateSnapshotShellInjection:
    @pytest.mark.asyncio
    async def test_ctx_path_escaped_in_git_status_command(self, tmp_path):
        marker = tmp_path / "pwned"
        ctx = _FakeCtx(path=_payload(marker))
        cm = _make_ctx_manager(ctx)

        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        sm = SnapshotManager(ssh, cm)

        await sm.create_snapshot("s1", "ctx1", "snap-name")

        call = next(c for c in ssh.execute.call_args_list if "git status --short" in c.args[1])
        subprocess.run(["sh", "-c", call.args[1]], check=False)
        assert not _marker_hit(marker), (
            f"create_snapshot()'s git-status command let ctx.path break out of shell quoting: {call.args[1]!r}"
        )

    @pytest.mark.asyncio
    async def test_ctx_path_escaped_in_rev_parse_command(self, tmp_path):
        marker = tmp_path / "pwned"
        ctx = _FakeCtx(path=_payload(marker))
        cm = _make_ctx_manager(ctx)

        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        sm = SnapshotManager(ssh, cm)

        await sm.create_snapshot("s1", "ctx1", "snap-name")

        call = next(c for c in ssh.execute.call_args_list if "git rev-parse HEAD" in c.args[1])
        subprocess.run(["sh", "-c", call.args[1]], check=False)
        assert not _marker_hit(marker), (
            f"create_snapshot()'s rev-parse command let ctx.path break out of shell quoting: {call.args[1]!r}"
        )

    @pytest.mark.asyncio
    async def test_modified_file_path_escaped_in_cp_command(self, tmp_path):
        marker = tmp_path / "pwned"
        payload = _payload(marker)
        ctx = _FakeCtx(path=str(tmp_path))
        cm = _make_ctx_manager(ctx)

        ssh = AsyncMock()

        async def side_effect(session_id, command, timeout=10):
            if "git status --short" in command:
                return {"stdout": payload, "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 0}

        ssh.execute = AsyncMock(side_effect=side_effect)
        sm = SnapshotManager(ssh, cm)

        await sm.create_snapshot("s1", "ctx1", "snap-name")

        call = next(c for c in ssh.execute.call_args_list if c.args[1].startswith("cp "))
        subprocess.run(["sh", "-c", call.args[1]], check=False)
        assert not _marker_hit(marker), (
            f"create_snapshot()'s cp command let file_path break out of shell quoting: {call.args[1]!r}"
        )


class TestSnapshotIdPathTraversal:
    """snapshot_id is server-generated as "snap_<timestamp>", but
    restore_snapshot()/delete_snapshot() take it back from the caller with
    no format check. shlex.quote() stops it from breaking out of its shell
    quotes (see TestSnapshotIdShellInjection above) but does nothing to
    stop "../../etc" from being a validly-quoted path segment that the
    remote shell still resolves outside SNAPSHOTS_DIR -- letting a caller
    rm -rf / read / overwrite an arbitrary path on the target host via a
    class that builds its own shell commands directly, bypassing
    command_policy entirely.
    """

    @pytest.mark.asyncio
    async def test_delete_snapshot_id_rejects_path_traversal(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "important.txt").write_text("do not delete me")
        project = tmp_path / "project"
        project.mkdir()

        ctx = _FakeCtx(path=str(project))
        cm = _make_ctx_manager(ctx)
        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        sm = SnapshotManager(ssh, cm)

        with pytest.raises(ValueError):
            await sm.delete_snapshot("s1", "ctx1", "../outside")

        ssh.execute.assert_not_awaited()
        assert (outside / "important.txt").exists()

    @pytest.mark.asyncio
    async def test_restore_snapshot_id_rejects_path_traversal(self, tmp_path):
        ctx = _FakeCtx(path=str(tmp_path))
        cm = _make_ctx_manager(ctx)
        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "exists", "stderr": "", "exit_code": 0})
        sm = SnapshotManager(ssh, cm)

        with pytest.raises(ValueError):
            await sm.restore_snapshot("s1", "ctx1", "../../etc/nginx")

        ssh.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_well_formed_snapshot_id_still_accepted(self, tmp_path):
        ctx = _FakeCtx(path=str(tmp_path))
        cm = _make_ctx_manager(ctx)
        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "not_found", "stderr": "", "exit_code": 0})
        sm = SnapshotManager(ssh, cm)

        with pytest.raises(ValueError, match="not found"):
            await sm.restore_snapshot("s1", "ctx1", "snap_1234567890")

        assert ssh.execute.await_count >= 1


class TestSnapshotIdShellInjection:
    """These predate the snap_<timestamp> format validation added for
    TestSnapshotIdPathTraversal above. The injection payload doesn't match
    that format either, so it's now rejected outright before any shell
    command is built -- a strictly stronger guarantee than "safely quoted".
    """

    @pytest.mark.asyncio
    async def test_delete_snapshot_id_escaped_in_rm_command(self, tmp_path):
        marker = tmp_path / "pwned"
        payload = _payload(marker)
        ctx = _FakeCtx(path=str(tmp_path))
        cm = _make_ctx_manager(ctx)

        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        sm = SnapshotManager(ssh, cm)

        with pytest.raises(ValueError):
            await sm.delete_snapshot("s1", "ctx1", payload)

        ssh.execute.assert_not_awaited()
        assert not _marker_hit(marker)

    @pytest.mark.asyncio
    async def test_restore_snapshot_id_escaped_in_test_dir_command(self, tmp_path):
        marker = tmp_path / "pwned"
        payload = _payload(marker)
        ctx = _FakeCtx(path=str(tmp_path))
        cm = _make_ctx_manager(ctx)

        ssh = AsyncMock()

        async def side_effect(session_id, command, timeout=10):
            if command.startswith("test -d"):
                return {"stdout": "exists", "stderr": "", "exit_code": 0}
            if command.startswith("cat "):
                return {"stdout": "{}", "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 0}

        ssh.execute = AsyncMock(side_effect=side_effect)
        sm = SnapshotManager(ssh, cm)

        with pytest.raises(ValueError):
            await sm.restore_snapshot("s1", "ctx1", payload)

        ssh.execute.assert_not_awaited()
        assert not _marker_hit(marker)
