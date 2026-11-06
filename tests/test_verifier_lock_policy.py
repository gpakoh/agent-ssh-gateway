"""Redacted verifier lock diagnostics and bounded real contention tests."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path

import pytest
from verifier_lock_policy import lock_acquire_script, lock_scope_id


def _script(root: Path, *, wait_seconds: int) -> str:
    return "\n".join(
        [
            "set -eu",
            f"exec 9>{shlex.quote(str(root / '.sync.lock'))}",
            *lock_acquire_script(str(root), wait_seconds=wait_seconds),
            "flock -u 9",
        ]
    )


def test_scope_is_deterministic_opaque_and_never_written_as_path():
    identity = "/private/verifier/hidden-candidate"
    key = lock_scope_id(identity)
    assert re.fullmatch("[0-9a-f]{12}", key)
    assert key == lock_scope_id(identity)
    assert key != lock_scope_id(identity + "-other")
    script = "\n".join(lock_acquire_script(identity, wait_seconds=2))
    assert identity not in script
    assert f"scope={key}" in script
    assert "VERIFIER_LOCK_WAIT:" in script
    assert "VERIFIER_LOCK_BUSY:" in script
    assert "waited_s=$_mcp_verifier_wait" in script
    assert "flock -n 9" in script


def test_primary_and_fallback_identify_the_same_source_without_leaking_it():
    import mcp_client_tools

    identity = "/secret/candidate with spaces/and'shell;metacharacters"
    expected = f"scope={lock_scope_id(identity)}"
    primary = mcp_client_tools._build_primary_script("ruff", identity, ["src"])
    fallback = mcp_client_tools._build_readonly_fallback_script(
        "ruff", identity, ["src"]
    )
    for script in (primary, fallback):
        # The identity can appear in the underlying command/quoted source path;
        # only diagnostic echo statements must contain the pseudonymous key.
        diagnostic_lines = [
            line for line in script.splitlines()
            if "VERIFIER_LOCK_" in line and "echo" in line
        ]
        assert len(diagnostic_lines) == 2
        assert all(expected in line for line in diagnostic_lines)
        assert all(identity not in line for line in diagnostic_lines)


@pytest.mark.parametrize("wait_seconds", [True, -1, 0, 61, 1.2])
def test_wait_budget_rejects_invalid_values(wait_seconds):
    with pytest.raises(ValueError, match="wait"):
        lock_acquire_script("/safe/identity", wait_seconds=wait_seconds)


def test_one_active_holder_only_blocks_its_own_project_and_then_releases(tmp_path):
    """Actual kernel flock; neither process is killed and no source is modified."""
    import shutil

    if shutil.which("flock") is None:
        pytest.skip("flock is not installed")
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    lock_file = first / ".sync.lock"
    ready_file = first / "holder-ready"

    hold = "\n".join([
        "set -eu",
        f"exec 9>{shlex.quote(str(lock_file))}",
        "flock -n 9",
        f"touch {shlex.quote(str(ready_file))}",
        "sleep 2",
        "flock -u 9",
    ])
    with subprocess.Popen(
        ["sh", "-c", hold], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as holder:
        deadline = time.monotonic() + 3
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists(), "holder never acquired its own lock"

        started = time.monotonic()
        blocked = subprocess.run(
            ["sh", "-c", _script(first, wait_seconds=1)],
            capture_output=True, text=True, timeout=4, check=False,
        )
        assert blocked.returncode == 91
        assert time.monotonic() - started < 3.5
        assert "VERIFIER_LOCK_WAIT:" in blocked.stderr
        assert "VERIFIER_LOCK_BUSY:" in blocked.stderr
        assert f"scope={lock_scope_id(str(first))}" in blocked.stderr
        assert "waited_s=1" in blocked.stderr
        assert str(first) not in blocked.stderr
        assert str(tmp_path) not in blocked.stderr

        independent = subprocess.run(
            ["sh", "-c", _script(second, wait_seconds=1)],
            capture_output=True, text=True, timeout=4, check=False,
        )
        assert independent.returncode == 0, independent.stderr
        holder.communicate(timeout=4)
        assert holder.returncode == 0

    released = subprocess.run(
        ["sh", "-c", _script(first, wait_seconds=1)],
        capture_output=True, text=True, timeout=4, check=False,
    )
    assert released.returncode == 0, released.stderr
    assert lock_file.is_file()
