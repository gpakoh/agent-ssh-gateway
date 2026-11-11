"""Owned verifier subprocess deadline, PID-group fence and inherited FD9 canaries."""

from __future__ import annotations

import fcntl
import os
import shlex
import signal
import subprocess
import time

import pytest
from verifier_child_policy import owned_uv_guard_lines


def _script(lock, *, seconds: int, command: str) -> str:
    return "\n".join(
        [
            "set -eu",
            f"exec 9>{shlex.quote(str(lock))}",
            "flock -n 9",
            *owned_uv_guard_lines(timeout_seconds=seconds),
            f"verifier_uv {command}",
            "flock -u 9",
        ]
    )


@pytest.mark.parametrize("bad", [True, False, 0, -1, 3601, "5", None])
def test_guard_rejects_non_bounded_deadlines(bad):
    with pytest.raises(ValueError, match="timeout"):
        owned_uv_guard_lines(timeout_seconds=bad)


def test_owned_verifier_propagates_success_and_tool_failure_without_leak(tmp_path):
    lock = tmp_path / ".sync.lock"
    for rc in (0, 7):
        script = _script(
            lock,
            seconds=3,
            command=f"python3 -c {shlex.quote(f'raise SystemExit({rc})')}",
        )
        result = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
        )
        assert result.returncode == rc, result.stderr
        assert "VERIFIER_CHILD_TIMEOUT" not in result.stderr
        with lock.open("a+") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)


def test_bounded_owned_group_kills_fd9_holding_grandchild_before_lock_reuse(tmp_path):
    """No production PIDs: the entire group is started by this test and bounded."""
    lock = tmp_path / ".sync.lock"
    # Nested Python deliberately passes FD 9 into the third process. If the
    # guard kills only its direct child, this owned grandchild keeps the lock
    # and the kernel lock assertion fails. No unknown PID or lock is deleted.
    nested = (
        'import subprocess; subprocess.run('
        '["python3", "-c", "import time; time.sleep(10)"], '
        'pass_fds=(9,), check=True)'
    )
    script = _script(
        lock,
        seconds=1,
        command=f"python3 -c {shlex.quote(nested)}",
    )
    started = time.monotonic()
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=7,
        check=False,
    )
    assert result.returncode == 124, result.stderr
    assert time.monotonic() - started < 6
    assert result.stderr.count("VERIFIER_CHILD_TIMEOUT:") == 1
    assert str(tmp_path) not in result.stderr
    with lock.open("a+") as probe:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)


def test_signal_to_owned_shell_group_cancels_its_separate_uv_group(tmp_path):
    """Cancellation targets only this test's owned session, never unknown PIDs."""
    lock = tmp_path / ".sync.lock"
    ready = tmp_path / "ready"
    script = "\n".join(
        [
            "set -eu",
            f"exec 9>{shlex.quote(str(lock))}",
            "flock -n 9",
            *owned_uv_guard_lines(timeout_seconds=5),
            f"verifier_uv python3 -c {shlex.quote(f'from pathlib import Path; Path({str(ready)!r}).touch(); import time; time.sleep(10)')}",
        ]
    )
    with subprocess.Popen(
        ["sh", "-c", script], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) as shell:
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists(), "owned verifier child never started"
            assert os.getpgid(shell.pid) == shell.pid, "test shell is not its own group"
            with lock.open("a+") as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.killpg(shell.pid, signal.SIGTERM)
                shell.wait(timeout=4)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.02)
                else:
                    pytest.fail("owned verifier process group retained FD 9 after SIGTERM")
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        finally:
            if shell.poll() is None and os.getpgid(shell.pid) == shell.pid:
                os.killpg(shell.pid, signal.SIGTERM)
                shell.wait(timeout=4)


def test_external_shell_sigkill_keeps_fd9_until_owned_child_finishes(tmp_path):
    """An uncatchable SSH-shell kill must never unlock a still-writing child.

    Both processes belong to this test. The child waits for a private marker
    with its own deadline, and no shared runner or unknown PID is signalled.
    """
    lock = tmp_path / ".sync.lock"
    ready = tmp_path / "child-ready"
    release = tmp_path / "release-child"
    done = tmp_path / "child-done"
    child_code = (
        "from pathlib import Path; import time; "
        f"ready=Path({str(ready)!r}); release=Path({str(release)!r}); "
        f"done=Path({str(done)!r}); ready.touch(); "
        "deadline=time.monotonic()+18; "
        "\nwhile not release.exists() and time.monotonic()<deadline: "
        "time.sleep(0.02)\n"
        "if not release.exists(): raise SystemExit(12)\n"
        "done.touch()"
    )
    script = _script(
        lock,
        seconds=24,
        command=f"python3 -c {shlex.quote(child_code)}",
    )
    with subprocess.Popen(
        ["sh", "-c", script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as shell:
        try:
            deadline = time.monotonic() + 12
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists(), "owned child never began work"
            assert os.getpgid(shell.pid) == shell.pid
            with lock.open("a+") as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.kill(shell.pid, signal.SIGKILL)
                assert shell.wait(timeout=4) == -signal.SIGKILL
                # The inner supervisor/worker owns inherited FD9 after its
                # outer shell disappears. Releasing early risks env corruption.
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                release.touch()
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.02)
                else:
                    pytest.fail("owned child did not release FD9 after finishing")
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            assert done.exists()
        finally:
            release.touch()
            if shell.poll() is None and os.getpgid(shell.pid) == shell.pid:
                os.kill(shell.pid, signal.SIGTERM)
                shell.wait(timeout=5)


def test_multiple_uv_calls_share_one_verifier_deadline(tmp_path):
    """All invocations use one deadline, even on a heavily contended CI runner.

    Avoid a one-second startup race: a short Python subprocess may not even
    launch in that window when unit shards run concurrently. The first command
    is a quick external executable, while the second must time out and leave
    no marker or inherited lock.
    """
    lock = tmp_path / ".sync.lock"
    completed = tmp_path / "completed"
    late = tmp_path / "late"
    guard_lines = owned_uv_guard_lines(timeout_seconds=8)
    assert sum("_MCP_VERIFIER_DEADLINE=" in line for line in guard_lines) == 1
    assert '"$_MCP_VERIFIER_DEADLINE" "$@"' in guard_lines[-2]
    script = "\n".join(
        [
            "set -eu",
            f"exec 9>{shlex.quote(str(lock))}",
            "flock -n 9",
            *guard_lines,
            "verifier_uv true",
            f"touch {shlex.quote(str(completed))}",
            "verifier_uv sleep 30",
            f"touch {shlex.quote(str(late))}",
        ]
    )
    started = time.monotonic()
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 124, result.stderr
    assert completed.exists(), "first short command did not finish"
    assert not late.exists()
    assert time.monotonic() - started < 18
    assert "VERIFIER_CHILD_TIMEOUT:" in result.stderr
    with lock.open("a+") as probe:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)


def test_both_verifier_paths_guard_all_uv_commands():
    from mcp_client_tools import _build_primary_script, _build_readonly_fallback_script

    for tool in ("pytest", "ruff", "mypy"):
        for builder in (_build_primary_script, _build_readonly_fallback_script):
            script = builder(tool, "/safe/project", ["tests"])
            assert 'exec 9>"$LOCK"' in script
            assert "flock -n 9" in script
            assert "verifier_uv() {" in script
            commands = [
                line.strip() for line in script.splitlines()
                if "uv " in line and not line.strip().startswith("#")
                and ("uv run " in line or "uv sync " in line or "uv pip " in line)
            ]
            assert commands
            assert all(
                line.startswith("verifier_uv uv ") or line.startswith("verifier_uv uv")
                for line in commands
            ), commands
    primary_compileall = _build_primary_script("compileall", "/safe/project", ["tests"])
    assert "verifier_uv uv run --no-project" in primary_compileall
    fallback = _build_readonly_fallback_script("pytest", "/safe/project", ["tests"])
    assert "verifier_uv uv sync --all-extras --frozen" in fallback
    assert "verifier_uv uv sync --all-extras --no-install-project" in fallback
    assert 'verifier_uv uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python3"' in fallback
    assert fallback.index("flock -n 9") < fallback.index("verifier_uv uv sync")


def test_real_verifier_sync_job_has_bounded_child_command_and_wait_budgets(monkeypatch):
    """No 110s mypy false timeout, and the server command outlives its child."""
    from pathlib import Path

    import mcp_client_tools as tools
    from verifier_child_policy import (
        _VERIFIER_CHILD_SECONDS,
        VERIFIER_SYNC_COMMAND_TIMEOUT_S,
        VERIFIER_SYNC_WAIT_TIMEOUT_S,
    )

    assert 110 < _VERIFIER_CHILD_SECONDS < VERIFIER_SYNC_COMMAND_TIMEOUT_S
    assert VERIFIER_SYNC_COMMAND_TIMEOUT_S < VERIFIER_SYNC_WAIT_TIMEOUT_S <= 300
    monkeypatch.setattr(tools, "_resolve_project", lambda _: Path("/work/proj"))

    class Client:
        def __init__(self):
            self.shell_kwargs = None
            self.wait_kwargs = None

        def execute_raw(self, command, **kwargs):
            if command == "command -v uv":
                return {"job_id": "uv-check"}
            assert command == "sh"
            self.shell_kwargs = kwargs
            return {"job_id": "mypy-check"}

        def wait_job(self, job_id, **kwargs):
            if job_id == "uv-check":
                return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
            self.wait_kwargs = kwargs
            return {"job_id": job_id, "exit_code": 0, "stdout": "Success", "stderr": ""}

    client = Client()
    result = tools._run_uv_tool(client, "project", "mypy", "run_mypy", ["app/security.py"])
    assert result["ok"]
    assert client.shell_kwargs["timeout_s"] == VERIFIER_SYNC_COMMAND_TIMEOUT_S
    assert f"time.monotonic() + {_VERIFIER_CHILD_SECONDS}" in client.shell_kwargs["stdin"]
    assert client.wait_kwargs["timeout_sec"] == VERIFIER_SYNC_WAIT_TIMEOUT_S


def test_async_verifier_uses_own_long_job_budget_not_sync_cap(monkeypatch):
    """Long-running async pytest must not silently inherit the sync 240s cutoff."""
    from pathlib import Path

    import mcp_client_tools as tools

    monkeypatch.setattr(tools, "_resolve_project", lambda _: Path("/work/proj"))
    monkeypatch.setattr(tools, "_project_has_uv_lock", lambda _: True)
    monkeypatch.setattr(tools, "_venv_usable_on_target", lambda *args: True)

    class Client:
        async_job_timeout = 3600

        def __init__(self):
            self.kwargs = None
            self.fallback = None

        def execute_raw(self, command, **kwargs):
            if command == "command -v uv":
                return {"job_id": "uv-check"}
            self.kwargs = kwargs
            return {"job_id": "queued-suite"}

        def wait_job(self, job_id, **kwargs):
            assert job_id == "uv-check"
            return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}

        def execute_project_script_async(self, project, script):
            self.fallback = script
            return {"job_id": "queued-fallback"}

    client = Client()
    result = tools._run_uv_tool(
        client, "project", "pytest", "run_tests", ["tests"], async_submit=True
    )
    assert result["ok"]
    assert client.kwargs["timeout_s"] == 3600
    assert "time.monotonic() + 3570" in client.kwargs["stdin"]
    monkeypatch.setattr(tools, "_project_has_uv_lock", lambda _: False)
    result = tools._run_uv_tool(
        client, "project", "pytest", "run_tests", ["tests"], async_submit=True
    )
    assert result["ok"]
    assert result["result"]["job_id"] == "queued-fallback"
    assert "time.monotonic() + 3570" in client.fallback
