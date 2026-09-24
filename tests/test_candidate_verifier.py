from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from typing import Any

import pytest

from examples.mcp_server import candidate_verifier as candidate_verifier_module
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


def _run_id_from_script(script: str) -> str:
    match = re.search(r"(?m)^RUN_ID=([0-9a-f]+)$", script)
    assert match is not None, "verifier script must embed RUN_ID"
    return match.group(1)


def _image_ref() -> str:
    return os.environ["MCP_VERIFIER_IMAGE"]


def _script_checks(script: str) -> list[str]:
    positions = [match.start() for match in re.finditer(r"(?m)^CHECK=", script)]
    positions.append(len(script))
    checks: list[str] = []
    for index in range(len(positions) - 1):
        start = positions[index] + len("CHECK=")
        block = script[start : positions[index + 1]]
        lexer = shlex.shlex(block, posix=True)
        lexer.whitespace_split = True
        checks.append(next(iter(lexer)))
    return checks


def _primary_tool_for(command: str) -> dict[str, Any]:
    try:
        first = shlex.split(command)[0]
    except (ValueError, IndexError):
        first = ""
    builtins = {"true", "false", "echo", "cd", "printf", "set", "test"}
    if first in builtins:
        return {
            "kind": "builtin",
            "name": first,
            "class": "builtin",
            "shell_path": "/bin/sh",
            "shell_sha256": "0" * 64,
            "shell_identity": "verifier-sh",
        }
    return {
        "kind": "external",
        "name": first or "tool",
        "basename": first or "tool",
        "path": f"/usr/bin/{first or 'tool'}",
        "sha256": "1" * 64,
        "version": "1.0",
    }


def _receipt_line(
    run_id: str,
    image: str,
    index: int,
    command: str,
    *,
    exit_code: int = 0,
    duration_ms: int = 7,
    stdout_tail: str = "",
    stderr_tail: str = "",
    primary_tool: dict[str, Any] | None = None,
    before_sha: str = "b" * 64,
    after_sha: str | None = None,
    before_bytes: int = 32,
    after_bytes: int = 32,
    before_entries: int = 1,
    after_entries: int = 1,
    cwd: str = ".",
    extra: dict[str, Any] | None = None,
) -> str:
    resolved_after_sha = before_sha if after_sha is None else after_sha
    payload: dict[str, Any] = {
        "v": 1,
        "run_id": run_id,
        "verifier_image": image,
        "check_index": index,
        "command": command,
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "cwd": cwd,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "primary_tool": primary_tool if primary_tool is not None else _primary_tool_for(command),
        "before_status_sha256": before_sha,
        "before_status_bytes": before_bytes,
        "before_status_entries": before_entries,
        "after_status_sha256": resolved_after_sha,
        "after_status_bytes": after_bytes,
        "after_status_entries": after_entries,
        "mutation_changed": before_sha != resolved_after_sha,
    }
    if extra:
        payload.update(extra)
    return candidate_verifier_module._encode_receipt_frame(payload)


def _stdout_from_script(
    script: str,
    *,
    commands: list[str] | None = None,
) -> str:
    run_id = _run_id_from_script(script)
    image = _image_ref()
    resolved_commands = commands if commands is not None else _script_checks(script)
    lines = [
        _receipt_line(run_id, image, index, command)
        for index, command in enumerate(resolved_commands)
    ]
    return "".join(line + "\n" for line in lines)


def _success_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if len(argv) > 1 and argv[1] == "run":
        stdout = _stdout_from_script(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, stdout, "")
    return subprocess.CompletedProcess(argv, 0, "", "")


def _run_verifier_script_locally(
    tmp_path: Path,
    required_checks: list[str],
    *,
    run_id: str,
    verifier_image: str,
) -> tuple[int, str, str]:
    repo = tmp_path / "repo"
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
    script = build_candidate_verifier_script(
        staging_root=repo,
        expected_sha=expected_sha,
        required_checks=required_checks,
        run_id=run_id,
        verifier_image=verifier_image,
    )
    script = script.replace("SOURCE=/candidate-src", f"SOURCE={shlex.quote(str(repo))}", 1)
    result = subprocess.run(
        ["sh"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    return result.returncode, result.stdout, expected_sha


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
    assert "MCP_CHECK_RECEIPT_V1:" in script
    assert "MCP_FRAME_BUILDER_EOF" in script


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
    assert argv[:3] == ["docker", "run", "-i"]
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


def test_each_verification_cleans_only_its_execution_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), dict(kwargs)))
        if len(argv) > 1 and argv[1] == "run":
            return subprocess.CompletedProcess(
                argv, 0, _stdout_from_script(kwargs["input"]), ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    for _ in range(2):
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="3" * 40,
            required_checks=["pytest -q"],
            runner=runner,
        )

    assert [call[0][1] for call in calls] == ["run", "rm", "run", "rm"]
    run_calls = [call for call in calls if call[0][1] == "run"]
    cleanup_calls = [call for call in calls if call[0][1] == "rm"]
    assert len(run_calls) == 2
    run_names = [call[0][call[0].index("--name") + 1] for call in run_calls]
    cleanup_names = [call[0][-1] for call in cleanup_calls]
    assert len(set(run_names)) == 2
    assert cleanup_names == run_names
    assert all(name.startswith("mcp-candidate-verifier-") for name in run_names)
    assert all(call[1]["input"].endswith("exit 0") for call in run_calls)


def test_concurrent_verifications_do_not_reuse_or_cross_clean_container_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    overlap = Barrier(2)
    lock = Lock()
    run_names: list[str] = []
    cleanup_names: list[str] = []

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1] == "run":
            name = argv[argv.index("--name") + 1]
            with lock:
                run_names.append(name)
            overlap.wait(timeout=5)
        elif argv[1] == "rm":
            with lock:
                cleanup_names.append(argv[-1])
        if _kwargs.get("input"):
            return subprocess.CompletedProcess(argv, 0, _stdout_from_script(_kwargs["input"]), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verify_once(expected_sha: str) -> None:
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha=expected_sha,
            required_checks=["pytest -q"],
            runner=runner,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(verify_once, "a" * 40), pool.submit(verify_once, "b" * 40)]
        for future in futures:
            future.result(timeout=10)

    assert len(run_names) == 2
    assert len(set(run_names)) == 2
    assert sorted(cleanup_names) == sorted(run_names)


def test_cancellation_cleans_only_started_execution_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[1] == "run":
            raise asyncio.CancelledError
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(asyncio.CancelledError):
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="c" * 40,
            required_checks=["pytest -q"],
            runner=runner,
        )

    assert [argv[1] for argv in calls] == ["run", "rm"]
    run_name = calls[0][calls[0].index("--name") + 1]
    assert calls[1][-1] == run_name


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
    assert [argv[1] for argv in calls] == ["run", "rm"]
    run_name = calls[0][calls[0].index("--name") + 1]
    assert calls[1][-1] == run_name


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


def test_verifier_exit_codes_have_operator_actionable_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    expectations = {
        80: ("VERIFIER_BOOTSTRAP_FAILED", "verifier_bootstrap", True),
        81: ("CANDIDATE_SOURCE_UNAVAILABLE", "candidate_checkout", False),
        82: ("VERIFIER_ENV_UNAVAILABLE", "verifier_env", True),
        83: ("CANDIDATE_CHECK_FAILED", "candidate_check", False),
    }

    for exit_code, (expected_code, expected_phase, expected_retryable) in expectations.items():
        def runner(
            argv: list[str],
            exit_code: int = exit_code,
            **_kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            rc = exit_code if len(argv) > 1 and argv[1] == "run" else 0
            return subprocess.CompletedProcess(
                argv,
                rc,
                stdout="MCP_VERIFY_EXIT=7\nMCP_VERIFY_CHECK=0\n",
                stderr="",
            )

        with pytest.raises(CandidateVerificationError) as exc_info:
            verify_candidate_via_docker(
                staging_root=staging,
                expected_sha="5" * 40,
                required_checks=["pytest -q"],
                runner=runner,
            )

        err = exc_info.value
        assert err.code == expected_code
        assert err.phase == expected_phase
        assert err.retryable is expected_retryable
        assert err.details["phase"] == expected_phase
        assert err.details["mutation_occurred"] is False


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
    assert err.code == "CANDIDATE_CHECK_FAILED"
    assert err.phase == "candidate_check"
    assert err.retryable is False
    assert err.details["failed_check"] == "ruff check ."
    assert err.details["check_index"] == 1
    assert err.details["phase"] == "candidate_check"
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
    with pytest.raises(CandidateVerificationError, match="escapes candidate root") as exc_info:
        verify_candidate_via_docker(
            staging_root=outside,
            expected_sha="6" * 40,
            required_checks=[],
            runner=lambda *_args, **_kwargs: None,
        )
    err = exc_info.value
    assert err.code == "CANDIDATE_VOLUME_SUBPATH_INVALID"
    assert err.phase == "source_resolution"
    assert err.details["mutation_occurred"] is False


def test_workspace_materializer_routes_registered_source_through_bundle_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
    (source / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=source, check=True)
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    original_clone = candidate_verifier_module.clone_registered_commit_via_bundle
    captured: list[dict[str, Any]] = []

    def recording_clone(**kwargs: Any) -> None:
        captured.append(dict(kwargs))
        original_clone(**kwargs)

    monkeypatch.setattr(
        candidate_verifier_module,
        "clone_registered_commit_via_bundle",
        recording_clone,
    )

    staging = candidate_verifier_module._materialize_workspace_source(source, expected_sha)
    try:
        assert len(captured) == 1
        call = captured[0]
        assert call["source_root"] == source.resolve()
        assert call["expected_sha"] == expected_sha
        assert call["destination"] == staging
        env = call["base_env"]
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        actual = subprocess.run(
            ["git", "-C", str(staging), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert actual == expected_sha
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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
        if len(argv) > 1 and argv[1] == "run":
            return subprocess.CompletedProcess(
                argv, 0, _stdout_from_script(kwargs["input"]), ""
            )
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


def test_two_check_success_returns_structured_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    receipt = verify_candidate_via_docker(
        staging_root=staging,
        expected_sha="9" * 40,
        required_checks=["true", "git status"],
        runner=_success_runner,
    )
    assert receipt["expected_sha"] == "9" * 40
    assert receipt["verifier_image"] == _image_ref()
    assert receipt["check_count"] == 2
    checks = receipt["checks"]
    assert len(checks) == 2
    assert [check["check_index"] for check in checks] == [0, 1]
    assert checks[0]["command"] == "true"
    assert checks[1]["command"] == "git status"
    assert checks[0]["cwd"] == "."
    assert checks[0]["exit_code"] == 0
    assert checks[0]["duration_ms"] >= 0
    assert re.fullmatch(r"[0-9a-f]{64}", checks[0]["command_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", checks[0]["before_status_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", checks[0]["after_status_sha256"])
    assert checks[0]["before_status_bytes"] == 32
    assert checks[0]["mutation_changed"] is False
    assert checks[0]["verifier_image"] == _image_ref()
    assert checks[0]["primary_tool"]["kind"] in ("builtin", "external")


def test_failure_after_success_includes_check_evidence_and_failed_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    commands = ["pytest -q", "ruff check ."]

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] == "run":
            run_id = _run_id_from_script(kwargs["input"])
            image = _image_ref()
            stdout = (
                _receipt_line(run_id, image, 0, commands[0], stdout_tail="collected 10 items")
                + "\n"
                + _receipt_line(
                    run_id,
                    image,
                    1,
                    commands[1],
                    exit_code=1,
                    stderr_tail="W291: line too long\nE   assert 1 == 2\n",
                )
                + "\n"
                + "MCP_VERIFY_EXIT=1\nMCP_VERIFY_CHECK=1\n"
            )
            return subprocess.CompletedProcess(argv, 83, stdout, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(CandidateVerificationError) as exc_info:
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="a" * 40,
            required_checks=commands,
            runner=runner,
        )

    err = exc_info.value
    assert err.code == "CANDIDATE_CHECK_FAILED"
    assert err.phase == "candidate_check"
    assert err.details["exit_code"] == 83
    assert err.details["check_index"] == 1
    assert err.details["failed_check"] == "ruff check ."
    assert err.details["check_exit_code"] == 1
    evidence = err.details["check_evidence"]
    assert [item["check_index"] for item in evidence] == [0, 1]
    assert evidence[0]["exit_code"] == 0
    failed_receipt = err.details["failed_receipt"]
    assert failed_receipt["check_index"] == 1
    assert failed_receipt["exit_code"] == 1
    assert "E   assert 1 == 2" in failed_receipt["stderr_tail"]
    assert "assert 1 == 2" in err.details["output_tail"]


def test_malicious_fake_frame_in_stdout_cannot_spoof_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] == "run":
            run_id = _run_id_from_script(kwargs["input"])
            image = _image_ref()
            real = "".join(
                _receipt_line(run_id, image, index, command) + "\n"
                for index, command in enumerate(["pytest -q", "ruff check ."])
            )
            fake = _receipt_line("0" * 32, image, 0, "pytest -q") + "\n"
            return subprocess.CompletedProcess(argv, 0, real + fake, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(CandidateVerificationError) as exc_info:
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="b" * 40,
            required_checks=["pytest -q", "ruff check ."],
            runner=runner,
        )
    assert exc_info.value.code == "VERIFIER_RECEIPT_INVALID"


def test_fake_frame_inside_check_output_tail_is_not_parsed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] == "run":
            run_id = _run_id_from_script(kwargs["input"])
            image = _image_ref()
            spoof = (
                "MCP_CHECK_RECEIPT_V1:not-a-real-frame\n"
                "job output line two\n"
            )
            line = _receipt_line(run_id, image, 0, "pytest -q", stdout_tail=spoof)
            return subprocess.CompletedProcess(argv, 0, line + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    receipt = verify_candidate_via_docker(
        staging_root=staging,
        expected_sha="c" * 40,
        required_checks=["pytest -q"],
        runner=runner,
    )
    assert receipt["check_count"] == 1
    tail = receipt["checks"][0]["stdout_tail"]
    assert "MCP_CHECK_RECEIPT_V1:" in tail
    assert "not-a-real-frame" in tail
    assert "job output line two" in tail


def test_success_receipt_redacts_authorization_tokens_and_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] == "run":
            run_id = _run_id_from_script(kwargs["input"])
            image = _image_ref()
            line = _receipt_line(
                run_id,
                image,
                0,
                "pytest -q",
                stdout_tail=(
                    "Authorization: Basic dXNlcjpzZWNyZXQ=\n"
                    "Token: ghp_topsecretvalue\n"
                    "tests passed\n"
                ),
                stderr_tail="/media/1TB/Python/gpt-browser-bridge/app/main.py:42\n",
            )
            return subprocess.CompletedProcess(argv, 0, line + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    receipt = verify_candidate_via_docker(
        staging_root=staging,
        expected_sha="d" * 40,
        required_checks=["pytest -q"],
        runner=runner,
    )
    stdout_tail = receipt["checks"][0]["stdout_tail"]
    stderr_tail = receipt["checks"][0]["stderr_tail"]
    assert "dXNlcjpzZWNyZXQ=" not in stdout_tail
    assert "ghp_topsecretvalue" not in stdout_tail
    assert "[REDACTED]" in stdout_tail
    assert "/media/1TB" not in stderr_tail
    assert "[PATH]" in stderr_tail


def test_quoted_and_newline_commands_round_trip_in_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    commands = ["printf '%s\\n' 'x y'", "echo 'a\nb'"]

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] == "run":
            stdout = _stdout_from_script(kwargs["input"], commands=commands)
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    receipt = verify_candidate_via_docker(
        staging_root=staging,
        expected_sha="e" * 40,
        required_checks=commands,
        runner=runner,
    )
    assert receipt["check_count"] == len(commands)
    for index, command in enumerate(commands):
        assert receipt["checks"][index]["command"] == command
    assert "a\nb" in receipt["checks"][1]["command"]


def test_provenance_accepts_python_git_uv_and_shell_builtin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    commands = ["python3 -m pytest", "git status", "uv run pytest -q", "true"]
    tools = {
        "python3 -m pytest": {
            "kind": "external",
            "name": "python3",
            "basename": "python3",
            "path": "/usr/bin/python3",
            "sha256": "a" * 64,
            "version": "3.12.5",
        },
        "git status": {
            "kind": "external",
            "name": "git",
            "basename": "git",
            "path": "/usr/bin/git",
            "sha256": "b" * 64,
            "version": "2.43.0",
        },
        "uv run pytest -q": {
            "kind": "external",
            "name": "uv",
            "basename": "uv",
            "path": "/usr/local/bin/uv",
            "sha256": "c" * 64,
            "version": "0.5.11",
        },
        "true": {
            "kind": "builtin",
            "name": "true",
            "class": "builtin",
            "shell_path": "/bin/sh",
            "shell_sha256": "d" * 64,
            "shell_identity": "verifier-sh",
        },
    }

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] == "run":
            run_id = _run_id_from_script(kwargs["input"])
            image = _image_ref()
            lines = [
                _receipt_line(run_id, image, index, command, primary_tool=tools[command])
                for index, command in enumerate(commands)
            ]
            return subprocess.CompletedProcess(
                argv, 0, "".join(line + "\n" for line in lines), ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    receipt = verify_candidate_via_docker(
        staging_root=staging,
        expected_sha="f" * 40,
        required_checks=commands,
        runner=runner,
    )
    for index, command in enumerate(commands):
        assert receipt["checks"][index]["primary_tool"] == tools[command]
        assert receipt["checks"][index]["exit_code"] == 0


def test_unresolved_provenance_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] == "run":
            run_id = _run_id_from_script(kwargs["input"])
            image = _image_ref()
            unresolved = {"kind": "unresolved", "reason": "not_found_on_trusted_path"}
            line = _receipt_line(run_id, image, 0, "mystery-tool -q", primary_tool=unresolved)
            return subprocess.CompletedProcess(argv, 0, line + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(CandidateVerificationError) as exc_info:
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="a0" * 20,
            required_checks=["mystery-tool -q"],
            runner=runner,
        )
    assert exc_info.value.code == "VERIFIER_PROVENANCE_UNRESOLVED"
    assert exc_info.value.phase == "receipt_validation"


@pytest.mark.parametrize(
    "variant", ["missing", "duplicate", "out_of_order", "wrong_command"]
)
def test_parser_rejects_missing_duplicate_out_of_order_wrong_command_frames(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variant: str,
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    commands = ["true", "pytest -q"]

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] == "run":
            run_id = _run_id_from_script(kwargs["input"])
            image = _image_ref()
            if variant == "missing":
                stdout = ""
            elif variant == "duplicate":
                stdout = (
                    _receipt_line(run_id, image, 0, commands[0])
                    + "\n"
                    + _receipt_line(run_id, image, 0, commands[0])
                    + "\n"
                )
            elif variant == "out_of_order":
                stdout = (
                    _receipt_line(run_id, image, 1, commands[1])
                    + "\n"
                    + _receipt_line(run_id, image, 0, commands[0])
                    + "\n"
                )
            else:  # wrong_command
                stdout = _receipt_line(run_id, image, 0, "false") + "\n"
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(CandidateVerificationError) as exc_info:
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="1f" * 20,
            required_checks=commands,
            runner=runner,
        )
    assert exc_info.value.code == "VERIFIER_RECEIPT_INVALID"


def test_mutation_digest_changes_when_check_creates_file(tmp_path: Path) -> None:
    run_id = "cafe" * 8
    image = "registry.invalid/verifier:test"
    returncode, stdout, _expected_sha = _run_verifier_script_locally(
        tmp_path,
        ["touch created.txt"],
        run_id=run_id,
        verifier_image=image,
    )
    assert returncode == 0
    checks = candidate_verifier_module._parse_check_receipts(
        stdout,
        run_id=run_id,
        verifier_image=image,
        required_checks=["touch created.txt"],
    )
    assert len(checks) == 1
    check = checks[0]
    assert check["command"] == "touch created.txt"
    assert check["exit_code"] == 0
    assert check["mutation_changed"] is True
    assert check["before_status_sha256"] != check["after_status_sha256"]
    assert check["after_status_entries"] == 1
    assert check["primary_tool"]["kind"] == "external"


def test_verifier_script_emits_real_builtin_provenance(tmp_path: Path) -> None:
    run_id = "beef" * 8
    image = "registry.invalid/verifier:test"
    returncode, stdout, _expected_sha = _run_verifier_script_locally(
        tmp_path,
        ["true"],
        run_id=run_id,
        verifier_image=image,
    )
    assert returncode == 0
    checks = candidate_verifier_module._parse_check_receipts(
        stdout,
        run_id=run_id,
        verifier_image=image,
        required_checks=["true"],
    )
    primary = checks[0]["primary_tool"]
    assert primary["kind"] == "builtin"
    assert primary["name"] == "true"
    assert re.fullmatch(r"[0-9a-f]{64}", primary["shell_sha256"])
    assert primary["shell_identity"] == "verifier-sh"


def test_total_evidence_is_bounded(tmp_path: Path) -> None:
    run_id = "dead" * 8
    image = "registry.invalid/verifier:test"
    noisy = (
        'i=0; while [ "$i" -lt 500 ]; do '
        'printf "line-%04d-%0400d\\n" "$i" "$i"; '
        "i=$((i + 1)); done"
    )
    returncode, stdout, _expected_sha = _run_verifier_script_locally(
        tmp_path,
        [noisy],
        run_id=run_id,
        verifier_image=image,
    )
    assert returncode == 0
    checks = candidate_verifier_module._parse_check_receipts(
        stdout,
        run_id=run_id,
        verifier_image=image,
        required_checks=[noisy],
    )
    frame_lines = [line for line in stdout.splitlines() if line.startswith("MCP_CHECK_RECEIPT_V1:")]
    assert len(frame_lines) == 1
    assert sum(len(line) for line in frame_lines) <= candidate_verifier_module._max_total_receipt_chars(1)
    assert len(checks[0]["stdout_tail"]) <= candidate_verifier_module._RECEIPT_TAIL_CHARS
    assert len(checks[0]["stderr_tail"]) <= candidate_verifier_module._RECEIPT_TAIL_CHARS


def test_oversized_fake_frame_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] == "run":
            run_id = _run_id_from_script(kwargs["input"])
            image = _image_ref()
            huge = "x" * 20_000
            line = _receipt_line(run_id, image, 0, "pytest -q", stdout_tail=huge)
            return subprocess.CompletedProcess(argv, 0, line + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(CandidateVerificationError) as exc_info:
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha="ab" * 20,
            required_checks=["pytest -q"],
            runner=runner,
        )
    assert exc_info.value.code == "VERIFIER_RECEIPT_INVALID"


def test_workspace_verifier_forwards_receipt_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = _configure(monkeypatch, tmp_path)
    receipt = verify_workspace_via_docker(
        workspace_root=staging,
        expected_sha="1b" * 20,
        required_checks=["true"],
        runner=_success_runner,
    )
    assert receipt["expected_sha"] == "1b" * 20
    assert receipt["verifier_image"] == _image_ref()
    assert receipt["check_count"] == 1
    assert receipt["checks"][0]["command"] == "true"