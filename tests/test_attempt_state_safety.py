"""BLOCKER B + C: attempt-state fail-closed read and atomic replacement.

BLOCKER B -- ``read_agent_attempt_state`` must distinguish a definitely
ABSENT record (a first attempt is allowed) from an existing-but-untrustworthy
record (symlink-unsafe path, permission error, malformed/truncated JSON,
missing required fields, invalid types).  The latter fails CLOSED with
``AttemptStateError`` so the durable sync path surfaces
``kind=durable-state-error`` and never creates a fresh attempt that would
duplicate an accepted execution.

BLOCKER C -- ``write_agent_attempt_state`` must replace the canonical
attempt-state file atomically: a full write to a same-directory temp file,
then an atomic rename.  The canonical path is only ever the destination of a
rename, so it is always a complete record (old or new) and never partial JSON.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server.agent_tools import project_run_agent


def _shell_runner(cwd):
    def run_script(_project: str, script: str) -> dict:
        result = subprocess.run(
            ["sh", "-c", script],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }

    return run_script


def _task_dir(tmp_path, task_id):
    return tmp_path / ".ai-bridge" / "tasks" / task_id


class TestReadAttemptStateFailClosed:
    """BLOCKER B: absent is ''None'', everything untrustworthy raises."""

    def test_absent_record_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import read_agent_attempt_state

        assert (
            read_agent_attempt_state(
                _shell_runner(tmp_path), project="p", task_id="d00000000101"
            )
            is None
        )

    def test_valid_record_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import read_agent_attempt_state

        td = _task_dir(tmp_path, "d00000000102")
        td.mkdir(parents=True)
        expected = {"attempt_id": "abc", "fingerprint": "aa", "job_id": "job-1"}
        (td / "attempt-state.json").write_text(json.dumps(expected), encoding="utf-8")

        assert (
            read_agent_attempt_state(
                _shell_runner(tmp_path), project="p", task_id="d00000000102"
            )
            == expected
        )

    def _write_corrupt(self, tmp_path, task_id, content):
        td = _task_dir(tmp_path, task_id)
        td.mkdir(parents=True)
        (td / "attempt-state.json").write_text(content, encoding="utf-8")

    def test_truncated_json_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import AttemptStateError, read_agent_attempt_state

        self._write_corrupt(tmp_path, "d00000000103", '{"attempt_id": "abc"')
        with pytest.raises(AttemptStateError, match="JSON"):
            read_agent_attempt_state(
                _shell_runner(tmp_path), project="p", task_id="d00000000103"
            )

    def test_empty_file_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import AttemptStateError, read_agent_attempt_state

        self._write_corrupt(tmp_path, "d00000000104", "")
        with pytest.raises(AttemptStateError, match="JSON"):
            read_agent_attempt_state(
                _shell_runner(tmp_path), project="p", task_id="d00000000104"
            )

    def test_non_object_json_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import AttemptStateError, read_agent_attempt_state

        self._write_corrupt(tmp_path, "d00000000105", "[1, 2, 3]")
        with pytest.raises(AttemptStateError, match="object"):
            read_agent_attempt_state(
                _shell_runner(tmp_path), project="p", task_id="d00000000105"
            )

    def test_missing_required_fields_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import AttemptStateError, read_agent_attempt_state

        self._write_corrupt(
            tmp_path,
            "d00000000106",
            json.dumps({"fingerprint": "aa", "job_id": None}),
        )
        with pytest.raises(AttemptStateError, match="attempt_id"):
            read_agent_attempt_state(
                _shell_runner(tmp_path), project="p", task_id="d00000000106"
            )

    def test_invalid_attempt_id_type_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import AttemptStateError, read_agent_attempt_state

        self._write_corrupt(
            tmp_path,
            "d00000000107",
            json.dumps({"attempt_id": 42, "fingerprint": "aa", "job_id": None}),
        )
        with pytest.raises(AttemptStateError, match="attempt_id"):
            read_agent_attempt_state(
                _shell_runner(tmp_path), project="p", task_id="d00000000107"
            )

    def test_invalid_job_id_type_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import AttemptStateError, read_agent_attempt_state

        self._write_corrupt(
            tmp_path,
            "d00000000108",
            json.dumps({"attempt_id": "abc", "fingerprint": "aa", "job_id": 7}),
        )
        with pytest.raises(AttemptStateError, match="job_id"):
            read_agent_attempt_state(
                _shell_runner(tmp_path), project="p", task_id="d00000000108"
            )

    def test_unreadable_file_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import AttemptStateError, read_agent_attempt_state

        def runner(project, command):
            if command.startswith("ls -ld -- "):
                return {"exit_code": 0, "stdout": "drwxr-xr-x 1 u u 0 .\n", "stderr": ""}
            if command.startswith("cat "):
                return {"exit_code": 1, "stdout": "", "stderr": "cat: Permission denied"}
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        with pytest.raises(AttemptStateError, match="cannot be read"):
            read_agent_attempt_state(runner, project="p", task_id="d00000000109")

    def test_unverifiable_path_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import AttemptStateError, read_agent_attempt_state

        def runner(project, command):
            return {
                "exit_code": 2,
                "stdout": "",
                "stderr": "ls: cannot open directory '.ai-bridge': Permission denied",
            }

        with pytest.raises(AttemptStateError, match="cannot verify"):
            read_agent_attempt_state(runner, project="p", task_id="d00000000110")

    def test_symlink_task_dir_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import AttemptStateError, read_agent_attempt_state

        td = _task_dir(tmp_path, "d00000000111")
        td.mkdir(parents=True)
        outside = tmp_path / "outside-attempt"
        outside.mkdir()
        (outside / "attempt-state.json").write_text('{"attempt_id": "x"}', encoding="utf-8")
        td.rmdir()
        td.symlink_to(outside, target_is_directory=True)

        with pytest.raises(AttemptStateError, match="symlink"):
            read_agent_attempt_state(
                _shell_runner(tmp_path), project="p", task_id="d00000000111"
            )

    def test_corrupt_state_blocks_submit_and_never_calls_submit(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import (
            claim_agent_attempt_state,
            read_agent_attempt_state,
            write_agent_attempt_state,
        )

        task_id = "d00000000112"
        td = _task_dir(tmp_path, task_id)
        td.mkdir(parents=True)
        (td / "task.json").write_text(
            json.dumps({"agent": "auto", "allowed_backends": ["opencode"]}),
            encoding="utf-8",
        )
        (td / "current-plan.md").write_text("# Plan\n\nExecute the task.\n", encoding="utf-8")
        (td / "attempt-state.json").write_text('{"attempt_id": "truncated')

        rc = _shell_runner(tmp_path)
        submitted: list[str] = []

        def submit(project, script, submission_key):
            submitted.append(submission_key)
            return {"job_id": "job-must-not-exist"}

        result = project_run_agent(
            rc,
            project="p",
            task_id=task_id,
            run_script_async=submit,
            run_script_wait=lambda jid: {"status": "completed", "exit_code": 0},
            read_attempt_state=lambda p, t: read_agent_attempt_state(rc, project=p, task_id=t),
            claim_attempt_state=lambda p, t, rec: claim_agent_attempt_state(
                rc, project=p, task_id=t, record=rec
            ),
            write_attempt_state=lambda p, t, rec: write_agent_attempt_state(
                rc, project=p, task_id=t, record=rec
            ),
            job_status=lambda jid: {"status": "running"},
        )

        assert result["status"] == "error"
        assert result["kind"] == "durable-state-error"
        assert submitted == []

class TestWriteAttemptStateAtomic:
    """BLOCKER C + new round: canonical is only a rename target and the temp
    is created exclusively.

    Default temp acquisition uses ``mktemp "$base.XXXXXX"`` (same directory,
    O_EXCL, unpredictable): a planted symlink or stale partial at a plausible
    temp name is never followed or reused -- the fresh exclusive temp
    supersedes it. A pinned temp path (test-only) colliding with an existing
    file OR symlink fails closed (exit 51) before anything is written.
    """

    def _target(self, tmp_path):
        return str(tmp_path / "attempt-state.json")

    def test_successful_attempt_state_write_is_leftover_free(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        from examples.mcp_server.agent_tasks import (
            read_agent_attempt_state,
            write_agent_attempt_state,
        )

        task_id = "e00000000101"
        td = _task_dir(tmp_path, task_id)
        td.mkdir(parents=True)
        rc = _shell_runner(tmp_path)
        new = {"attempt_id": "abc", "fingerprint": "aa", "job_id": "job-1"}

        write_agent_attempt_state(rc, project="p", task_id=task_id, record=new)

        assert read_agent_attempt_state(rc, project="p", task_id=task_id) == new
        assert [entry.name for entry in td.iterdir()] == ["attempt-state.json"]

    def test_colliding_pinned_temp_fails_closed_never_touches_canonical(self, tmp_path):
        from examples.mcp_server.agent_tasks import _atomic_encoded_write

        target = self._target(tmp_path)
        Path(target).write_text('{"attempt_id": "old"}', encoding="utf-8")
        # The pinned temp path is occupied (a directory): the collision guard
        # fires BEFORE any write/rename, so the canonical stays the old record.
        (tmp_path / "state.tmp").mkdir()

        lines = "\n".join(
            _atomic_encoded_write(
                target, '{"attempt_id": "new"}', tmp_path=str(tmp_path / "state.tmp")
            )
        )
        result = _shell_runner(tmp_path)("p", lines)

        assert result["exit_code"] == 51
        assert (tmp_path / "state.tmp").is_dir()
        assert json.loads(Path(target).read_text(encoding="utf-8")) == {"attempt_id": "old"}

    def test_stale_partial_temp_never_reaches_canonical(self, tmp_path):
        from examples.mcp_server.agent_tasks import _atomic_encoded_write

        target = self._target(tmp_path)
        Path(target).write_text('{"attempt_id": "old"}', encoding="utf-8")
        # A prior crash left a partial write behind at a plausible temp name.
        # The default path acquires an EXCLUSIVE fresh mktemp name instead of
        # reusing it, so the partial file is never renamed over the canonical
        # and the new complete record wins.
        (tmp_path / "attempt-state.json.tmp").write_text('{"attempt_id": "partial', encoding="utf-8")

        lines = "\n".join(_atomic_encoded_write(target, '{"attempt_id": "new"}'))
        result = _shell_runner(tmp_path)("p", lines)

        assert result["exit_code"] == 0
        assert json.loads(Path(target).read_text(encoding="utf-8")) == {"attempt_id": "new"}
        assert (tmp_path / "attempt-state.json.tmp").read_text(encoding="utf-8") == (
            '{"attempt_id": "partial'
        ), "the exclusive fresh temp supersedes the planted partial; it is left untouched"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["attempt-state.json", "attempt-state.json.tmp"]

    def test_planted_symlink_at_pinned_temp_fails_closed_outside_untouched(self, tmp_path):
        from examples.mcp_server.agent_tasks import _atomic_encoded_write

        target = self._target(tmp_path)
        Path(target).write_text('{"attempt_id": "old"}', encoding="utf-8")
        outside = tmp_path / "outside-target.json"
        outside.write_text("attacker", encoding="utf-8")
        # An attacker pins the temp to a symlink pointing outside: the guard
        # rejects it on NAME COLLISION before any dereference happens.
        (tmp_path / "state.tmp").symlink_to(outside)

        lines = "\n".join(
            _atomic_encoded_write(
                target, '{"attempt_id": "new"}', tmp_path=str(tmp_path / "state.tmp")
            )
        )
        result = _shell_runner(tmp_path)("p", lines)

        assert result["exit_code"] == 51
        assert json.loads(Path(target).read_text(encoding="utf-8")) == {"attempt_id": "old"}
        assert outside.read_text(encoding="utf-8") == "attacker"
        assert (tmp_path / "state.tmp").is_symlink()

    def test_planted_symlink_at_plausible_default_temp_is_bypassed_outside_untouched(self, tmp_path):
        from examples.mcp_server.agent_tasks import _atomic_encoded_write

        target = self._target(tmp_path)
        Path(target).write_text('{"attempt_id": "old"}', encoding="utf-8")
        outside = tmp_path / "outside-target.json"
        outside.write_text("attacker", encoding="utf-8")
        # A pre-planted symlink at the plausible temp name is IGNORED: mktemp
        # picks an exclusive fresh name and the write proceeds via THAT file.
        (tmp_path / "attempt-state.json.tmp").symlink_to(outside)

        lines = "\n".join(_atomic_encoded_write(target, '{"attempt_id": "new"}'))
        result = _shell_runner(tmp_path)("p", lines)

        assert result["exit_code"] == 0
        assert json.loads(Path(target).read_text(encoding="utf-8")) == {"attempt_id": "new"}
        assert (tmp_path / "attempt-state.json.tmp").is_symlink(), "planted symlink stays untouched"
        assert outside.read_text(encoding="utf-8") == "attacker"

    def test_generated_script_publishes_canonical_only_via_rename(self):
        from examples.mcp_server.agent_tasks import _atomic_encoded_write

        lines = _atomic_encoded_write(".ai-bridge/tasks/t/attempt-state.json", "{}")
        script = "\n".join(lines)
        # The canonical path is the destination of a same-dir atomic rename and
        # never the target of a shell redirect (no partial overwrite possible).
        assert '> ".ai-bridge/tasks/t/attempt-state.json"' not in script
        assert 'tmp=$(mktemp "$base.XXXXXX") || exit 50' in script
        assert 'mv -f -- "$tmp" .ai-bridge/tasks/t/attempt-state.json' in script
        assert 'base64 -d > "$tmp"' in script
