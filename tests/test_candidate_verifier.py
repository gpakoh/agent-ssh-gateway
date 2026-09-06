from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from examples.mcp_server.candidate_verifier import (
    CandidateVerificationError,
    build_candidate_verifier_script,
    build_ephemeral_verifier_argv,
    verify_candidate_via_docker,
    verify_workspace_via_docker,
)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "candidates"
    staging = root / "project" / "task" / "candidate-staging" / "repo"
    staging.mkdir(parents=True)
    monkeypatch.setenv("MCP_TASK_CANDIDATE_ROOT", str(root))
    monkeypatch.setenv("MCP_TASK_CANDIDATE_VOLUME_NAME", "ssh-gateway-mcp-candidates")
    monkeypatch.setenv("MCP_VERIFIER_IMAGE", "registry.invalid/ssh-gateway-sshd:deadbeef")
    monkeypatch.setenv("MCP_CANDIDATE_VERIFY_TIMEOUT_SECONDS", "120")
    return staging


def test_verifier_script_clones_read_only_source_into_disposable_tmp() -> None:
    script = build_candidate_verifier_script(
        staging_root=Path("/var/lib/mcp-candidates/project/task/attempt/candidate-staging/repo"),
        expected_sha="1" * 40,
        required_checks=["pytest -q"],
    )
    assert 'mktemp -d /tmp/mcp-candidate-verify.' in script
    assert 'git clone --no-hardlinks --no-checkout "$SOURCE" "$VERIFY_ROOT/repo"' in script
    assert '(cd "$VERIFY_ROOT/repo"' in script
    assert 'cd "$SOURCE"' not in script
    assert 'git -C "$SOURCE"' not in script
    assert "SOURCE=/candidate-src" in script
    assert "/var/lib/mcp-candidates/project/task/attempt" not in script
    assert 'pytest -q' in script


def test_verifier_command_is_shell_quoted_as_data() -> None:
    command = "printf '%s\n' 'a; exit 99'"
    script = build_candidate_verifier_script(
        staging_root=Path("/var/lib/mcp-candidates/repo"),
        expected_sha="2" * 40,
        required_checks=[command],
    )
    assert "CHECK=" in script
    assert 'sh -c "$CHECK"' in script


def test_ephemeral_docker_argv_has_narrow_security_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    argv = build_ephemeral_verifier_argv(
        staging_root=staging,
        volume_name="ssh-gateway-mcp-candidates",
        image="registry.invalid/ssh-gateway-sshd:deadbeef",
        timeout_seconds=120,
    )
    joined = " ".join(argv)
    for token in (
        "--rm",
        "--init",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "--pids-limit 256",
    ):
        assert token in joined
    assert "--network bridge" in joined
    assert "--entrypoint /usr/bin/timeout" in joined
    assert " KILL 120 /bin/sh -s" in joined
    assert "type=volume,src=ssh-gateway-mcp-candidates" in joined
    assert "dst=/candidate-src" in joined
    assert "volume-subpath=project/task/candidate-staging/repo" in joined
    assert "dst=" + str(tmp_path / "candidates") not in joined
    assert ",readonly" in joined
    assert "/var/run/docker.sock" not in joined
    assert "agent_runtime" not in joined
    assert "internal_net" not in joined
    assert "verifier_net" not in joined


def test_each_verification_force_cleans_fixed_container_before_and_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), dict(kwargs)))
        return subprocess.CompletedProcess(argv, 0, "", "")

    for _ in range(2):
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="3" * 40,
            required_checks=["pytest -q"],
            runner=runner,
        )

    commands = [call[0][1:3] for call in calls]
    assert commands == [
        ["rm", "-f"], ["run", "--rm"], ["rm", "-f"],
        ["rm", "-f"], ["run", "--rm"], ["rm", "-f"],
    ]
    run_calls = [call for call in calls if call[0][1] == "run"]
    assert len(run_calls) == 2
    assert all(call[1]["input"].endswith("exit 0") for call in run_calls)


def test_timeout_forces_container_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if len(argv) > 1 and argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(CandidateVerificationError, match="timed out"):
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="4" * 40,
            required_checks=["pytest -q"],
            runner=runner,
        )
    assert [argv[1] for argv in calls] == ["rm", "run", "rm"]


def test_verifier_nonzero_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        rc = 83 if len(argv) > 1 and argv[1] == "run" else 0
        return subprocess.CompletedProcess(argv, rc, "", "")

    with pytest.raises(CandidateVerificationError, match="exit code 83"):
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="5" * 40,
            required_checks=["false"],
            runner=runner,
        )


def test_required_check_failure_exposes_check_name_and_sanitized_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        rc = 83 if len(argv) > 1 and argv[1] == "run" else 0
        return subprocess.CompletedProcess(
            argv,
            rc,
            stdout=(
                "MCP_VERIFY_EXIT=1\n"
                "MCP_VERIFY_CHECK=1\n"
                "tests/test_a.py:12: in test_thing\n"
                "E   assert 1 == 2\n"
                "fatal: /tmp/secret-key.pem permission denied\n"
            ),
            stderr="",
        )

    with pytest.raises(CandidateVerificationError) as exc_info:
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="5" * 40,
            required_checks=["pytest -q", "ruff check ."],
            runner=runner,
        )

    err = exc_info.value
    assert err.code == "REQUIRED_CHECK_FAILED"
    assert err.phase == "required_checks"
    assert err.retryable is False
    assert err.details["failed_check"] == "ruff check ."
    assert err.details["check_index"] == 1
    assert err.details["exit_code"] == 83
    assert err.details["check_exit_code"] == 1
    assert err.details["mutation_occurred"] is False
    tail = err.details["output_tail"]
    assert "/tmp/secret-key.pem" not in tail
    assert "[PATH]" in tail
    assert "assert 1 == 2" in tail


def test_verifier_output_tail_redacts_paths_and_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        rc = 83 if len(argv) > 1 and argv[1] == "run" else 0
        return subprocess.CompletedProcess(
            argv,
            rc,
            stdout=(
                "MCP_VERIFY_EXIT=1\n"
                "MCP_VERIFY_CHECK=0\n"
                "Authorization: Basic dXNlcjpzZWNyZXQ=\n"
                "Token: ghp_topsecretvalue\n"
                "/media/1TB/Python/gpt-browser-bridge/app/main.py:42\n"
            ),
            stderr="",
        )

    with pytest.raises(CandidateVerificationError) as exc_info:
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="5" * 40,
            required_checks=["pytest -q", "ruff check ."],
            runner=runner,
        )

    tail = exc_info.value.details["output_tail"]
    assert "dXNlcjpzZWNyZXQ=" not in tail
    assert "ghp_topsecretvalue" not in tail
    assert "[REDACTED]" in tail
    assert "/media/1TB" not in tail
    assert "[PATH]" in tail


def test_verifier_source_must_stay_under_candidate_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(CandidateVerificationError, match="escapes candidate root"):
        verify_candidate_via_docker(
            staging_root=outside,
            expected_sha="6" * 40,
            required_checks=[],
            runner=lambda *_args, **_kwargs: None,
        )


def test_workspace_verifier_materializes_external_workspace_under_candidate_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    candidate_root = staging.parents[3]
    repo = tmp_path / "outside-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), dict(kwargs)))
        return subprocess.CompletedProcess(argv, 0, "", "")

    verify_workspace_via_docker(
        workspace_root=repo,
        expected_sha=expected_sha,
        required_checks=["true"],
        runner=runner,
    )

    run_calls = [call for call in calls if len(call[0]) > 1 and call[0][1] == "run"]
    assert len(run_calls) == 1
    argv, kwargs = run_calls[0]
    joined = " ".join(argv)
    assert "volume-subpath=verified-workspaces/verified-workspace-" in joined
    assert str(repo) not in joined
    assert str(repo) not in kwargs["input"]
    assert list((candidate_root / "verified-workspaces").iterdir()) == []


def test_empty_check_contract_skips_dependency_bootstrap() -> None:
    script = build_candidate_verifier_script(
        staging_root=Path("/var/lib/mcp-candidates/repo"),
        expected_sha="7" * 40,
        required_checks=[],
    )
    assert "CHECKS_PRESENT=0" in script
    assert 'if [ "$CHECKS_PRESENT" = "1" ]' in script


def test_nonempty_check_contract_enables_dependency_bootstrap() -> None:
    script = build_candidate_verifier_script(
        staging_root=Path("/var/lib/mcp-candidates/repo"),
        expected_sha="8" * 40,
        required_checks=["pytest -q"],
    )
    assert "CHECKS_PRESENT=1" in script


def test_signal_terminates_verifier_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    script = build_candidate_verifier_script(
        staging_root=repo,
        expected_sha=sha,
        required_checks=['kill -TERM "$PPID"; exit 0'],
    )
    script = script.replace("SOURCE=/candidate-src", f"SOURCE={shlex.quote(str(repo))}", 1)
    result = subprocess.run(
        ["sh"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 85
