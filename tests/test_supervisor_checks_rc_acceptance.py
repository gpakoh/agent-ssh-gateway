"""Independent acceptance suite for commit 51e187f (CHECKS_RC=127 semantics).

Contract verified here:
- required-checks failure (non-127) fails the run with FINAL_RC=72
- missing check tool (CHECKS_RC=127) degrades to needs-review-warning while
  FINAL_RC stays 0
- worker / evidence / scope / parent failure domains keep precedence over
  the missing-tool warning
- the warning never lands in an accepted/success bucket downstream

Some precedence combinations cannot occur through the natural execution flow
(checks only run when every earlier domain already succeeded), so those rows
are exercised at fragment level against real generator output instead of a
fake binary run.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server.agent_tools import (
    _build_opencode_script,
    _runner_artifact_io_script_lines,
    _supervisor_postrun_script_lines,
)
from examples.mcp_server.fleet_runtime import (
    _GATEWAY_TERMINAL,
    _PRE_SUBMIT_TERMINAL,
)
from examples.mcp_server.fleet_state import TERMINAL_STATUSES
from examples.mcp_server.opencode_tools import project_run_opencode

TASK_ID = "b12345678901"
# Outer acceptance-harness budget only; production runner/watchdog timeouts are unchanged.
RUNNER_HARNESS_TIMEOUT_SECONDS = 120
FINALIZE_ANCHOR = "FINAL_RC=$RC"
STATUS_ANCHOR = 'if [ $FINAL_RC -eq 0 ] && [ "${CHECKS_WARNING:-0}" -eq 1 ]; then'
ACCEPT_SYNONYMS = frozenset({"success", "passed", "accepted", "approved", "completed"})


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _init_git_repo(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Acceptance Suite")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "base.txt")
    _git(root, "commit", "-q", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


def _finalization_fragment() -> str:
    lines = _supervisor_postrun_script_lines([], [], [])
    start = lines.index(FINALIZE_ANCHOR)
    return "\n".join(lines[start:])


def _status_block() -> str:
    lines = _build_opencode_script(".ai-bridge/tasks/acceptance", TASK_ID, None).splitlines()
    start = lines.index(STATUS_ANCHOR)
    end = start + lines[start:].index("fi")
    return "\n".join(lines[start : end + 1])


def _run_precedence_case(tmp_path: Path, **codes: int) -> tuple[int, str]:
    td = tmp_path / "artifacts"
    td.mkdir(parents=True, exist_ok=True)
    harness = "\n".join(
        [
            "set -u",
            f"td={shlex.quote(str(td))}",
            *(f"{name}={value}" for name, value in codes.items()),
            "",
            *_runner_artifact_io_script_lines(),
            _finalization_fragment(),
            "",
            _status_block(),
            "printf 'FINAL_RC=%s\\n' \"$FINAL_RC\"",
            'printf \'%s\\n\' "$(cat "$td/agent-status.md")"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    match = re.search(r"^FINAL_RC=(\d+)$", proc.stdout, re.MULTILINE)
    assert match, proc.stdout
    status_line = next(line for line in proc.stdout.splitlines() if line.startswith("Status: "))
    return int(match.group(1)), status_line.removeprefix("Status: ").strip()


@pytest.mark.parametrize(
    (
        "rc",
        "evidence_rc",
        "scope_rc",
        "checks_rc",
        "parent_rc",
        "expected_final",
        "expected_status",
    ),
    [
        pytest.param(0, 0, 0, 0, 0, 0, "needs-review", id="all-clean"),
        pytest.param(0, 0, 0, 1, 0, 72, "failed", id="checks-failure-fails-run"),
        pytest.param(0, 0, 0, 127, 0, 0, "needs-review-warning", id="missing-tool-warns-only"),
        pytest.param(7, 0, 0, 127, 0, 7, "failed", id="worker-beats-warning"),
        pytest.param(0, 1, 0, 127, 0, 70, "failed", id="evidence-beats-warning"),
        pytest.param(0, 0, 3, 127, 0, 71, "failed", id="scope-beats-warning"),
        pytest.param(0, 0, 0, 127, 1, 74, "failed", id="parent-beats-warning"),
    ],
)
def test_finalize_precedence_matrix(
    tmp_path: Path,
    rc: int,
    evidence_rc: int,
    scope_rc: int,
    checks_rc: int,
    parent_rc: int,
    expected_final: int,
    expected_status: str,
) -> None:
    final_rc, status = _run_precedence_case(
        tmp_path,
        RC=rc,
        EVIDENCE_RC=evidence_rc,
        SCOPE_RC=scope_rc,
        CHECKS_RC=checks_rc,
        PARENT_RC=parent_rc,
        PROXY_BLOCKED=0,
        RATE_LIMITED=0,
        RESOURCE_EXHAUSTED=0,
    )
    assert final_rc == expected_final
    assert status == expected_status


def _run_natural_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    opencode_exit: int,
    required_checks: list[str],
):
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    source = tmp_path / "source"
    source.mkdir()
    _init_git_repo(source)

    artifacts = tmp_path / "artifacts" / TASK_ID
    artifacts.mkdir(parents=True)
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")
    workspace = tmp_path / "workspaces" / TASK_ID
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "opencode"
    fake.write_text(f"#!/bin/sh\nexit {opencode_exit}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(
        str(artifacts),
        TASK_ID,
        None,
        project_root=str(source),
        worktree_path=str(workspace),
        required_checks=list(required_checks),
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=RUNNER_HARNESS_TIMEOUT_SECONDS,
    )
    return result, artifacts


def test_natural_flow_passing_checks_stay_needs_review(tmp_path, monkeypatch):
    result, artifacts = _run_natural_flow(
        tmp_path, monkeypatch, opencode_exit=0, required_checks=["true"]
    )
    assert result.returncode == 0, result.stderr or result.stdout
    status_file = artifacts / "agent-status.md"
    assert status_file.read_text(encoding="utf-8").strip() == "Status: needs-review"
    report = (artifacts / "agent-report.md").read_text(encoding="utf-8")
    assert "- Required-checks exit code: 0 (ran=1)" in report


def test_natural_flow_failing_check_fails_run_with_72(tmp_path, monkeypatch):
    result, artifacts = _run_natural_flow(
        tmp_path, monkeypatch, opencode_exit=0, required_checks=["false"]
    )
    assert result.returncode == 72, result.stderr or result.stdout
    status_file = artifacts / "agent-status.md"
    assert status_file.read_text(encoding="utf-8").strip() == "Status: failed"
    report = (artifacts / "agent-report.md").read_text(encoding="utf-8")
    assert "- Required-checks exit code: 1 (ran=1)" in report


def test_natural_flow_worker_failure_skips_checks(tmp_path, monkeypatch):
    """Worker failure wins and checks never execute: no warning can mask it."""
    result, artifacts = _run_natural_flow(
        tmp_path,
        monkeypatch,
        opencode_exit=7,
        required_checks=["definitely-missing-tool-acceptance --version"],
    )
    assert result.returncode == 7, result.stderr or result.stdout
    status_file = artifacts / "agent-status.md"
    assert status_file.read_text(encoding="utf-8").strip() == "Status: failed"
    report = (artifacts / "agent-report.md").read_text(encoding="utf-8")
    assert "- Worker exit code: 7" in report
    assert "- Required-checks exit code: 0 (ran=0)" in report


def test_warning_absent_from_auto_accept_buckets():
    """The warning must not be closable as any terminal state, and no fleet
    gate may equate it with a success synonym."""
    for statuses in (TERMINAL_STATUSES, _PRE_SUBMIT_TERMINAL, _GATEWAY_TERMINAL):
        assert "needs-review-warning" not in statuses
    assert not ACCEPT_SYNONYMS & {"needs-review", "needs-review-warning"}


def test_exit_zero_surfaces_as_needs_review_not_acceptance():
    result = project_run_opencode(
        lambda project, cmd: {"stdout": "# plan\n", "exit_code": 0},
        project="acceptance-proj",
        task_id=TASK_ID,
        run_script=lambda project, script: {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
        },
    )
    assert result["status"] == "needs-review"
    assert result["status"] not in ACCEPT_SYNONYMS


def test_warning_and_clean_statuses_remain_distinct_literals():
    clean = _build_opencode_script(
        ".ai-bridge/tasks/acceptance", TASK_ID, None, required_checks=["true"]
    )
    warned = _build_opencode_script(
        ".ai-bridge/tasks/acceptance", TASK_ID, None, required_checks=["true"]
    )
    assert 'runner_artifact_write_line "$td/agent-status.md" "Status: needs-review"' in clean
    assert 'runner_artifact_write_line "$td/agent-status.md" "Status: needs-review-warning"' in warned
