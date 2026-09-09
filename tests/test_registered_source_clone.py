from __future__ import annotations

import subprocess
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
    real_run = subprocess.run
    calls: list[list[str]] = []

    def recording_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = [str(item) for item in argv]
        calls.append(command)
        if "clone" in command and str(source.resolve()) in command:
            raise AssertionError("registered production root must never be a git clone source")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(registered_source_clone.subprocess, "run", recording_run)

    clone_registered_commit_via_bundle(
        source_root=source,
        expected_sha=head,
        destination=destination,
    )

    actual = real_run(
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
