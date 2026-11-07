"""Bounded kernel canary for verifier FD 9 across an owned shell timeout."""

from __future__ import annotations

import fcntl
import os
import shlex
import subprocess
import time

import pytest


def test_verifier_lock_stays_held_by_child_after_shell_is_terminated(tmp_path):
    """Never assume closing the SSH shell releases a child-inherited lock."""
    lock = tmp_path / ".sync.lock"
    ready = tmp_path / "ready"
    child_pid_file = tmp_path / "child.pid"
    script = "\n".join(
        [
            "set -eu",
            f"exec 9>{shlex.quote(str(lock))}",
            "flock -n 9",
            "sleep 5 &",
            f"printf '%s\\n' \"$!\" >{shlex.quote(str(child_pid_file))}",
            f"touch {shlex.quote(str(ready))}",
            "wait",
        ]
    )
    with subprocess.Popen(
        ["sh", "-c", script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as shell:
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists(), "owned shell did not acquire lock"
            child_pid = int(child_pid_file.read_text(encoding="ascii"))
            assert os.getpgid(child_pid) == shell.pid, "child escaped owned session"

            shell.terminate()
            shell.wait(timeout=2)
            with lock.open("a+") as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                # The child has a finite 5s lifetime; never kill or touch
                # unrelated processes, and never remove the lock inode.
                deadline = time.monotonic() + 7
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
                else:
                    pytest.fail("owned child did not release its inherited lock")
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        finally:
            if shell.poll() is None:
                shell.terminate()
                shell.wait(timeout=2)
