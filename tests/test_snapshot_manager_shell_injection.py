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


class TestSnapshotIdShellInjection:
    @pytest.mark.asyncio
    async def test_delete_snapshot_id_escaped_in_rm_command(self, tmp_path):
        marker = tmp_path / "pwned"
        payload = _payload(marker)
        ctx = _FakeCtx(path=str(tmp_path))
        cm = _make_ctx_manager(ctx)

        ssh = AsyncMock()
        ssh.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        sm = SnapshotManager(ssh, cm)

        await sm.delete_snapshot("s1", "ctx1", payload)

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"delete_snapshot() let snapshot_id break out of shell quoting: {command!r}"
        )

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

        await sm.restore_snapshot("s1", "ctx1", payload)

        call = next(c for c in ssh.execute.call_args_list if c.args[1].startswith("test -d"))
        subprocess.run(["sh", "-c", call.args[1]], check=False)
        assert not _marker_hit(marker), (
            f"restore_snapshot()'s existence check let snapshot_id break out of shell quoting: {call.args[1]!r}"
        )
