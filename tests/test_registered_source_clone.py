from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from examples.mcp_server import registered_source_clone
from examples.mcp_server.registered_source_clone import (
    RegisteredSourceCloneError,
    clone_registered_commit_via_bundle,
)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    _git("config", "user.email", "test@example.invalid", cwd=root)
    (root / "file.txt").write_text("one\n", encoding="utf-8")
    _git("add", "file.txt", cwd=root)
    _git("commit", "-qm", "initial", cwd=root)
    return root, _git("rev-parse", "HEAD", cwd=root)


def test_clone_registered_commit_never_directly_clones_registered_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source, head = _repo(tmp_path)
    source_config_before = (source / ".git" / "config").read_bytes()
    destination = tmp_path / "destination"
    real_popen = subprocess.Popen
    calls: list[list[str]] = []

    def recording_popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        command = [str(item) for item in argv]
        calls.append(command)
        if "clone" in command and str(source.resolve()) in command:
            raise AssertionError("registered production root must never be a git clone source")
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(registered_source_clone.subprocess, "Popen", recording_popen)

    clone_registered_commit_via_bundle(
        source_root=source,
        expected_sha=head,
        destination=destination,
    )

    actual = subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "--verify", f"{head}^{{commit}}"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert actual == head
    assert (source / ".git" / "config").read_bytes() == source_config_before
    clone_calls = [command for command in calls if "clone" in command]
    assert len(clone_calls) == 1
    assert any(part.endswith("source.bundle") for part in clone_calls[0])
    assert str(source.resolve()) not in clone_calls[0]
    assert all("safe.directory=*" not in " ".join(command) for command in calls)
    assert all("--global" not in command for command in calls)


def test_clone_registered_commit_accepts_precreated_empty_destination(tmp_path: Path) -> None:
    source, head = _repo(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()

    clone_registered_commit_via_bundle(
        source_root=source,
        expected_sha=head,
        destination=destination,
    )

    assert _git("rev-parse", "--verify", f"{head}^{{commit}}", cwd=destination) == head


def test_clone_registered_commit_rejects_wrong_or_missing_commit_without_direct_clone(
    tmp_path: Path,
) -> None:
    source, _head = _repo(tmp_path)
    destination = tmp_path / "destination"

    with pytest.raises(RegisteredSourceCloneError) as excinfo:
        clone_registered_commit_via_bundle(
            source_root=source,
            expected_sha="0" * 40,
            destination=destination,
        )

    assert excinfo.value.phase == "resolve_expected_commit"
    assert not destination.exists()


def test_clone_registered_commit_rejects_malformed_inherited_git_config_before_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source, head = _repo(tmp_path)
    destination = tmp_path / "destination"

    def must_not_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("Git must not run after inherited config rejection")

    monkeypatch.setattr(registered_source_clone.subprocess, "run", must_not_run)
    with pytest.raises(RegisteredSourceCloneError) as excinfo:
        clone_registered_commit_via_bundle(
            source_root=source,
            expected_sha=head,
            destination=destination,
            base_env={"GIT_CONFIG_COUNT": "not-an-int"},
        )

    assert excinfo.value.phase == "source_trust"
    assert excinfo.value.retryable is False
    assert not destination.exists()


@pytest.mark.parametrize("precreated", [False, True])
def test_failed_partial_bundle_clone_does_not_poison_destination_or_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, precreated: bool
) -> None:
    """Interrupted clone must not leave a partial .git in caller-owned target."""
    source, head = _repo(tmp_path)
    destination = tmp_path / "destination"
    if precreated:
        destination.mkdir()
        original_inode = destination.stat().st_ino
    original_run = registered_source_clone._run

    def fail_after_partial_git(argv: list[str], **kwargs: Any) -> str:
        if kwargs.get("phase") == "clone_bundle":
            clone_target = Path(argv[-1])
            clone_target.mkdir(exist_ok=True)
            (clone_target / ".git").mkdir(exist_ok=True)
            (clone_target / ".git" / "partial-object").write_text("incomplete", encoding="utf-8")
            raise RegisteredSourceCloneError(
                "synthetic clone failure",
                phase="clone_bundle",
                exit_code=128,
                failure_class="git_failure",
            )
        return original_run(argv, **kwargs)

    with monkeypatch.context() as clone_failure:
        clone_failure.setattr(registered_source_clone, "_run", fail_after_partial_git)
        with pytest.raises(RegisteredSourceCloneError) as excinfo:
            clone_registered_commit_via_bundle(
                source_root=source, expected_sha=head, destination=destination,
            )
        assert excinfo.value.phase == "clone_bundle"
        assert excinfo.value.failure_class == "git_failure"

    if precreated:
        assert destination.is_dir()
        assert destination.stat().st_ino == original_inode
        assert list(destination.iterdir()) == []
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob(".registered-source-*"))
    # A new attempt using the same exact destination is safe without manual
    # wiping of partial Git state.
    clone_registered_commit_via_bundle(
        source_root=source, expected_sha=head, destination=destination,
    )
    assert _git("rev-parse", "--verify", f"{head}^{{commit}}", cwd=destination) == head


@pytest.mark.parametrize("precreated", [False, True])
def test_bundle_clone_verified_sha_mismatch_never_publishes_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, precreated: bool
) -> None:
    """A successful clone still must pass exact SHA verification before publish."""
    source, head = _repo(tmp_path)
    destination = tmp_path / "destination"
    if precreated:
        destination.mkdir()
        original_inode = destination.stat().st_ino
    real_run = registered_source_clone._run

    def reject_staged_commit(argv: list[str], **kwargs: Any) -> str:
        result = real_run(argv, **kwargs)
        if kwargs.get("phase") == "verify_destination_commit":
            return "f" * 40
        return result

    with monkeypatch.context() as failed_verification:
        failed_verification.setattr(registered_source_clone, "_run", reject_staged_commit)
        with pytest.raises(RegisteredSourceCloneError) as excinfo:
            clone_registered_commit_via_bundle(
                source_root=source, expected_sha=head, destination=destination,
            )
        assert excinfo.value.phase == "verify_destination_commit"
    if precreated:
        assert destination.stat().st_ino == original_inode
        assert list(destination.iterdir()) == []
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob(".registered-source-*"))


def test_bundle_clone_preserves_pinned_precreated_directory_inode(tmp_path: Path) -> None:
    import os

    source, head = _repo(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir(mode=0o755)
    parent_inode = destination.stat().st_ino
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        clone_registered_commit_via_bundle(
            source_root=source, expected_sha=head, destination=destination,
        )
        assert os.fstat(fd).st_ino == parent_inode == destination.stat().st_ino
        assert _git("rev-parse", "--verify", f"{head}^{{commit}}", cwd=destination) == head
    finally:
        os.close(fd)


def test_bundle_clone_refuses_destination_modified_during_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source, head = _repo(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    real_run = registered_source_clone._run

    def interleaved_writer(argv: list[str], **kwargs: Any) -> str:
        result = real_run(argv, **kwargs)
        if kwargs.get("phase") == "verify_destination_commit":
            destination.rmdir()
            destination.mkdir()
            (destination / "foreign-work.txt").write_text("keep", encoding="utf-8")
        return result

    with monkeypatch.context() as concurrent:
        concurrent.setattr(registered_source_clone, "_run", interleaved_writer)
        with pytest.raises(RegisteredSourceCloneError) as excinfo:
            clone_registered_commit_via_bundle(
                source_root=source, expected_sha=head, destination=destination,
            )
    assert excinfo.value.phase == "validate_destination"
    assert (destination / "foreign-work.txt").read_text(encoding="utf-8") == "keep"
    assert not (destination / ".git").exists()
    assert not list(tmp_path.glob(".registered-source-*"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX Git process-group regression")
def test_git_timeout_reaps_descendant_holding_capture_pipes(tmp_path: Path) -> None:
    """A timed-out Git parent must not strand children or wait on their pipe FDs."""
    pid_marker = tmp_path / "git-worker.pid"
    # Use a native shell worker, not a new Python interpreter: CI runners
    # can be heavily loaded, but /bin/sh starts fast enough for this fixture.
    parent = 'sleep 12 & echo $! > "$1"; wait'
    worker_pid: int | None = None
    started = time.monotonic()
    try:
        with pytest.raises(RegisteredSourceCloneError) as excinfo:
            registered_source_clone._run(
                ["/bin/sh", "-c", parent, "git-like-worker", str(pid_marker)],
                cwd=tmp_path,
                env=dict(os.environ),
                phase="clone_bundle",
                timeout=2,
            )
        elapsed = time.monotonic() - started
        assert excinfo.value.phase == "clone_bundle"
        assert excinfo.value.failure_class == "git_timeout"
        assert excinfo.value.retryable is True
        # subprocess.run kills only the immediate parent; its Git-like worker
        # survives unless the whole private process group is terminated.
        assert elapsed < 7, f"timed-out Git descendants survived deadline for {elapsed:.1f}s"
        assert pid_marker.exists()
        worker_pid = int(pid_marker.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = Path(f"/proc/{worker_pid}/stat")
            try:
                state = status.read_text(encoding="utf-8").split()[2]
            except (FileNotFoundError, ProcessLookupError):
                break  # The worker exited between the two /proc operations.
            if state == "Z":
                break
            time.sleep(0.05)
        else:
            pytest.fail("Git worker survived timed-out parent")
    finally:
        if pid_marker.exists():
            worker_pid = int(pid_marker.read_text(encoding="utf-8"))
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(
    ("git_stderr", "expected_class", "expected_retryable"),
    [
        ("fatal: unable to create thread: Resource temporarily unavailable", "resource_pressure", True),
        ("fatal: Permission denied (contains secret-token)", "permission_denied", False),
        ("fatal: unrecognized bundle secret-token", "invalid_bundle", False),
        ("fatal: unknown Git failure secret-token", "git_failure", False),
    ],
)
def test_clone_bundle_failure_reports_only_sanitized_classification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    git_stderr: str,
    expected_class: str,
    expected_retryable: bool,
) -> None:
    class FailedGit:
        returncode = 128

        def __enter__(self) -> FailedGit:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def communicate(self, *, timeout: int) -> tuple[str, str]:
            assert timeout == 1
            return "", git_stderr

    monkeypatch.setattr(registered_source_clone.subprocess, "Popen", lambda *_a, **_kw: FailedGit())
    with pytest.raises(RegisteredSourceCloneError) as excinfo:
        registered_source_clone._run(
            ["git", "clone", "source.bundle", "destination"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin"},
            phase="clone_bundle",
            timeout=1,
        )
    error = excinfo.value
    assert error.phase == "clone_bundle"
    assert error.exit_code == 128
    assert error.failure_class == expected_class
    assert error.retryable is expected_retryable
    assert "secret-token" not in str(error)
