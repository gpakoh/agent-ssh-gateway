"""Adversarial coverage for runner-owned agent artifact publication.

The OpenCode worker and runner currently share one OS identity, so these tests
are deliberately scoped to path/inode safety rather than claiming an OS
privilege boundary.  The runner must never follow worker-planted symlinks,
write through hardlinks, block on special files, or trust stale canonical
artifacts after the worker exits.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from examples.mcp_server.agent_tools import (
    _build_opencode_script,
    _runner_artifact_io_script_lines,
)

TASK_ID = "artifact-owner-001"
RUNNER_HARNESS_TIMEOUT_SECONDS = 60


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Artifact Ownership Tests")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "base.txt")
    _git(root, "commit", "-q", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


def _run_shell(script: str, *, cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", script],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def test_append_is_copy_on_write_for_preexisting_hardlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("victim\n", encoding="utf-8")
    artifact = tmp_path / "agent-status.md"
    os.link(victim, artifact)
    victim_inode = victim.stat().st_ino

    script = "\n".join(
        [
            *_runner_artifact_io_script_lines(),
            f"runner_artifact_append_line {shlex.quote(str(artifact))} runner-line",
        ]
    )
    result = _run_shell(script, cwd=tmp_path)

    assert result.returncode == 0, result.stderr or result.stdout
    assert victim.read_text(encoding="utf-8") == "victim\n"
    assert artifact.read_text(encoding="utf-8") == "runner-line\n"
    assert artifact.stat().st_ino != victim_inode


def test_worker_fifo_snapshot_is_nonblocking_and_rejected(tmp_path: Path) -> None:
    fifo = tmp_path / "agent-report.md"
    os.mkfifo(fifo)
    destination = tmp_path / "worker-report.md"
    destination.write_text("stale\n", encoding="utf-8")

    script = "\n".join(
        [
            *_runner_artifact_io_script_lines(),
            f"snapshot_worker_artifact {shlex.quote(str(fifo))} {shlex.quote(str(destination))} 1024",
        ]
    )
    result = _run_shell(script, cwd=tmp_path, timeout=3)

    assert result.returncode == 0, result.stderr or result.stdout
    assert not destination.exists()
    assert fifo.exists()


def test_runner_log_tail_snapshot_is_bounded_to_latest_bytes(tmp_path: Path) -> None:
    source = tmp_path / "private-output.log"
    source.write_text("0123456789abcdef", encoding="utf-8")
    destination = tmp_path / "opencode-output.log"

    script = "\n".join(
        [
            *_runner_artifact_io_script_lines(),
            f"snapshot_runner_log_tail {shlex.quote(str(source))} {shlex.quote(str(destination))} 6",
        ]
    )
    result = _run_shell(script, cwd=tmp_path)

    assert result.returncode == 0, result.stderr or result.stdout
    assert destination.read_text(encoding="utf-8") == "abcdef"
    assert destination.is_file()
    assert not destination.is_symlink()


def test_symlinked_artifact_parent_fails_closed_without_touching_target(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-task"
    real_parent.mkdir()
    victim = real_parent / "agent-status.md"
    victim.write_text("do-not-touch\n", encoding="utf-8")
    linked_parent = tmp_path / "task-link"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    script = "\n".join(
        [
            *_runner_artifact_io_script_lines(),
            f"runner_artifact_write_line {shlex.quote(str(linked_parent / 'agent-status.md'))} changed",
        ]
    )
    result = _run_shell(script, cwd=tmp_path)

    assert result.returncode == 75
    assert victim.read_text(encoding="utf-8") == "do-not-touch\n"


def test_full_runner_rejects_symlink_task_dir_before_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    real_task = tmp_path / "real-task"
    real_task.mkdir()
    (real_task / "current-plan.md").write_text("# noop\n", encoding="utf-8")
    victim_status = real_task / "agent-status.md"
    victim_status.write_text("sentinel\n", encoding="utf-8")
    linked_task = tmp_path / "task-link"
    linked_task.symlink_to(real_task, target_is_directory=True)

    script = _build_opencode_script(str(linked_task), TASK_ID, None)
    result = _run_shell(script, cwd=tmp_path)

    assert result.returncode == 75
    assert victim_status.read_text(encoding="utf-8") == "sentinel\n"
    assert 'runner_artifact_verify_dir "$td"' in script
    assert 'mkdir -p "$td"' not in script


def test_worker_symlink_poisoning_cannot_redirect_canonical_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    source = tmp_path / "source"
    _init_repo(source)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("sentinel\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "opencode"
    fake.write_text(
        "#!/bin/sh\n"
        "for target in \"$STATUS_TARGET\" \"$REPORT_TARGET\" \"$OUTPUT_TARGET\"; do\n"
        "  rm -f -- \"$target\"\n"
        "  ln -s -- \"$VICTIM_TARGET\" \"$target\"\n"
        "done\n"
        "printf 'model-output\\n'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("STATUS_TARGET", str(artifacts / "agent-status.md"))
    monkeypatch.setenv("REPORT_TARGET", str(artifacts / "agent-report.md"))
    monkeypatch.setenv("OUTPUT_TARGET", str(artifacts / "opencode-output.log"))
    monkeypatch.setenv("VICTIM_TARGET", str(victim))

    script = _build_opencode_script(str(artifacts), TASK_ID, None, project_root=str(source))
    result = _run_shell(script, cwd=source, timeout=RUNNER_HARNESS_TIMEOUT_SECONDS)

    assert result.returncode == 0, result.stderr or result.stdout
    assert victim.read_text(encoding="utf-8") == "sentinel\n"
    for name in ("agent-status.md", "agent-report.md", "opencode-output.log"):
        path = artifacts / name
        assert path.is_file()
        assert not path.is_symlink()
    assert (artifacts / "agent-status.md").read_text(encoding="utf-8").strip() == (
        "Status: needs-review"
    )
    assert "model-output" in (artifacts / "opencode-output.log").read_text(encoding="utf-8")
    report = (artifacts / "agent-report.md").read_text(encoding="utf-8")
    assert "# Agent Runner Result" in report
    assert "sentinel" not in report


def test_runner_publishes_attempt_local_log_before_opencode_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = tmp_path / "provider.txt"
    proxy_namespace = tmp_path.name.replace("_", "-")
    provider.write_text(
        f"http://127.0.0.1:19999/{proxy_namespace}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", provider.as_uri())
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "true")
    monkeypatch.setenv("OPENCODE_STARTUP_RESERVE_BYTES", "0")
    monkeypatch.setenv("OPENCODE_ADMISSION_WAIT_SECONDS", "0")
    source = tmp_path / "source"
    _init_repo(source)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")
    public_log = artifacts / "opencode-output.log"
    public_log.write_text("previous-attempt\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    release = tmp_path / "release-opencode"
    fake = fake_bin / "opencode"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '> build · big-pickle\\n'\n"
        "sleep 1\n"
        "printf '→ Read examples/mcp_server/agent_tools.py\\n'\n"
        f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.1; done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(str(artifacts), TASK_ID, None, project_root=str(source))
    proc = subprocess.Popen(
        ["sh", "-c", script],
        cwd=source,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    live_text = ""
    seen_while_running = False
    deadline = time.monotonic() + RUNNER_HARNESS_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline and proc.poll() is None:
            if public_log.is_file():
                live_text = public_log.read_text(encoding="utf-8", errors="replace")
                proxy_status_path = artifacts / "proxy-status.json"
                if proxy_status_path.is_file():
                    proxy_status = json.loads(proxy_status_path.read_text(encoding="utf-8"))
                else:
                    proxy_status = {}
                if (
                    "→ Read examples/mcp_server/agent_tools.py" in live_text
                    and proxy_status.get("final_outcome") == "running"
                ):
                    seen_while_running = True
                    break
            time.sleep(0.1)
        release.touch()
        stdout, stderr = proc.communicate(timeout=RUNNER_HARNESS_TIMEOUT_SECONDS)
    finally:
        release.touch(exist_ok=True)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert seen_while_running, live_text
    assert "previous-attempt" not in live_text
    assert proc.returncode == 0, stderr or stdout


def test_final_public_log_remains_bounded_after_opencode_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    source = tmp_path / "bounded-final-source"
    _init_repo(source)
    artifacts = tmp_path / "bounded-final-artifacts"
    artifacts.mkdir()
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")

    fake_bin = tmp_path / "bounded-final-bin"
    fake_bin.mkdir()
    fake = fake_bin / "opencode"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '> build · big-pickle\\n'\n"
        "printf '→ Read current-plan.md\\n'\n"
        "python3 - <<'PY'\n"
        "print('A' * 70000)\n"
        "print('TAIL-MARKER')\n"
        "PY\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(str(artifacts), TASK_ID, None, project_root=str(source))
    result = _run_shell(script, cwd=source, timeout=RUNNER_HARNESS_TIMEOUT_SECONDS)

    assert result.returncode == 0, result.stderr or result.stdout
    public_log = (artifacts / "opencode-output.log").read_bytes()
    assert len(public_log) <= 65536
    assert public_log.endswith(b"TAIL-MARKER\n")
    assert b"> build" not in public_log


def test_bootstrap_notices_do_not_transition_proxy_to_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = tmp_path / "provider.txt"
    proxy_namespace = tmp_path.name.replace("_", "-")
    provider.write_text(
        f"http://127.0.0.1:19999/{proxy_namespace}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", provider.as_uri())
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "true")
    monkeypatch.setenv("OPENCODE_STARTUP_RESERVE_BYTES", "0")
    monkeypatch.setenv("OPENCODE_ADMISSION_WAIT_SECONDS", "0")
    monkeypatch.setenv("OPENCODE_STARTUP_RESPONSE_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("OPENCODE_STARTUP_KILL_GRACE_SECONDS", "1")
    monkeypatch.setenv("OPENCODE_STARTUP_MAX_PROXY_ATTEMPTS", "1")
    source = tmp_path / "source"
    _init_repo(source)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "opencode"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '> build · big-pickle\\n'\n"
        "printf 'OpenCode 1.2.3\\n'\n"
        "printf 'Working directory: /tmp/example\\n'\n"
        "sleep 10\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(str(artifacts), TASK_ID, None, project_root=str(source))
    result = _run_shell(script, cwd=source, timeout=RUNNER_HARNESS_TIMEOUT_SECONDS)

    assert result.returncode == 78, result.stderr or result.stdout
    proxy_status = json.loads((artifacts / "proxy-status.json").read_text(encoding="utf-8"))
    assert proxy_status["final_outcome"] == "startup_exhausted"
    assert proxy_status["last_error_class"] == "startup_stalled"
    live_text = (artifacts / "opencode-output.log").read_text(encoding="utf-8")
    assert "OpenCode 1.2.3" in live_text
    assert "Working directory: /tmp/example" in live_text


def test_sentence_like_model_prose_transitions_proxy_to_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = tmp_path / "provider.txt"
    proxy_namespace = tmp_path.name.replace("_", "-")
    provider.write_text(
        f"http://127.0.0.1:19999/{proxy_namespace}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", provider.as_uri())
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "true")
    monkeypatch.setenv("OPENCODE_STARTUP_RESERVE_BYTES", "0")
    monkeypatch.setenv("OPENCODE_ADMISSION_WAIT_SECONDS", "0")
    # This test verifies the proxy->runtime state transition, not dependency
    # bootstrap. Keep supervisor post-run hermetic so a slow package index
    # cannot consume the communicate() budget after the fake worker exits.
    monkeypatch.setenv("UV_OFFLINE", "1")
    source = tmp_path / "source"
    _init_repo(source)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    release = tmp_path / "release-opencode-prose"
    fake = fake_bin / "opencode"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '> build · big-pickle\\n'\n"
        "printf 'Let me inspect the current plan before changing anything.\\n'\n"
        f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.1; done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(str(artifacts), TASK_ID, None, project_root=str(source))
    proc = subprocess.Popen(
        ["sh", "-c", script],
        cwd=source,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    seen_running = False
    deadline = time.monotonic() + RUNNER_HARNESS_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline and proc.poll() is None:
            proxy_status_path = artifacts / "proxy-status.json"
            if proxy_status_path.is_file():
                proxy_status = json.loads(proxy_status_path.read_text(encoding="utf-8"))
                if proxy_status.get("final_outcome") == "running":
                    seen_running = True
                    break
            time.sleep(0.1)
        release.touch()
        stdout, stderr = proc.communicate(timeout=RUNNER_HARNESS_TIMEOUT_SECONDS)
    finally:
        release.touch(exist_ok=True)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert seen_running is True
    assert proc.returncode == 0, stderr or stdout


def test_stale_supervisor_artifacts_are_reclaimed_before_postrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    source = tmp_path / "source"
    _init_repo(source)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("sentinel\n", encoding="utf-8")

    os.link(victim, artifacts / "implementation-diff.patch")
    for name in (
        "changed-files.z",
        "scope-violations.json",
        "required-checks.log",
        "supervisor-verdict.json",
        ".supervisor-index",
    ):
        (artifacts / name).symlink_to(victim)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "opencode"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(str(artifacts), TASK_ID, None, project_root=str(source))
    result = _run_shell(script, cwd=source, timeout=RUNNER_HARNESS_TIMEOUT_SECONDS)

    assert result.returncode == 0, result.stderr or result.stdout
    assert victim.read_text(encoding="utf-8") == "sentinel\n"
    for name in (
        "implementation-diff.patch",
        "changed-files.z",
        "scope-violations.json",
        "required-checks.log",
        "supervisor-verdict.json",
    ):
        path = artifacts / name
        assert path.is_file()
        assert not path.is_symlink()
        assert path.stat().st_ino != victim.stat().st_ino
    assert not (artifacts / ".supervisor-index").exists()
