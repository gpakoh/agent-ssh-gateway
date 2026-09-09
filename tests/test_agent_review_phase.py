"""Regression coverage for the evidence-only OpenCode review workflow."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

import examples.mcp_server.agent_tools as agent_tools
import examples.mcp_server.opencode_tools as opencode_tools

TASK_ID = "review-task-001"


def _review_contract(**overrides: object) -> dict[str, object]:
    contract: dict[str, object] = {
        "agent": "opencode",
        "allowed_backends": ["opencode"],
        "allowed_files": ["src/**", "tests/**"],
        "forbidden_files": [],
        "required_checks": [],
        "worktree_path": "",
        "workflow_phase": "review",
        "commit_allowed": False,
        "push_allowed": False,
    }
    contract.update(overrides)
    return contract


def _reader(task_json: dict[str, object]) -> Callable[[str, str], dict[str, Any]]:
    payload = json.dumps(task_json)

    def run_cmd(_project: str, command: str) -> dict[str, Any]:
        if command.startswith("ls -ld -- "):
            return {
                "exit_code": 0,
                "stdout": "drwxr-xr-x 1 user user 0 path\n",
                "stderr": "",
            }
        if command.startswith("cat ") and "task.json" in command:
            return {"exit_code": 0, "stdout": payload, "stderr": ""}
        if command.startswith("cat ") and "current-plan.md" in command:
            return {"exit_code": 0, "stdout": "# Review\n", "stderr": ""}
        raise AssertionError(f"unexpected read command: {command}")

    return run_cmd


def _run_agent(
    run_cmd: Callable[[str, str], dict[str, Any]],
    run_script: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    return agent_tools.project_run_agent(
        run_cmd,
        project="test",
        task_id=TASK_ID,
        run_script=run_script,
    )


def _run_opencode(
    run_cmd: Callable[[str, str], dict[str, Any]],
    run_script: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    return opencode_tools.project_run_opencode(
        run_cmd,
        project="test",
        task_id=TASK_ID,
        run_script=run_script,
    )


@pytest.fixture(autouse=True)
def _no_managed_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_tools, "_resolve_project_root", lambda _project: None)
    monkeypatch.setattr(agent_tools, "managed_workspace_path", lambda _project, _task_id: None)
    monkeypatch.setattr(opencode_tools, "_resolve_project_root", lambda _project: None)
    monkeypatch.setattr(opencode_tools, "managed_workspace_path", lambda _project, _task_id: None)


@pytest.mark.parametrize("runner", [_run_agent, _run_opencode])
def test_review_runner_forces_empty_mutation_allowlist(runner) -> None:
    scripts: list[str] = []

    def run_script(_project: str, script: str) -> dict[str, Any]:
        scripts.append(script)
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    result = runner(_reader(_review_contract()), run_script)

    assert result["status"] == "needs-review"
    assert len(scripts) == 1
    script = scripts[0]
    assert 'python3 - "$td/changed-files.z" \'[]\'' in script
    assert '\'["src/**","tests/**"]\'' not in script


@pytest.mark.parametrize("runner", [_run_agent, _run_opencode])
@pytest.mark.parametrize(
    "overrides",
    [
        {"commit_allowed": True},
        {"push_allowed": True},
        {"commit_allowed": "false"},
    ],
)
def test_review_runner_rejects_mutation_intent_before_launch(runner, overrides) -> None:
    scripts: list[str] = []

    def run_script(_project: str, script: str) -> dict[str, Any]:
        scripts.append(script)
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    result = runner(_reader(_review_contract(**overrides)), run_script)

    assert result["status"] == "error"
    assert "review workflow" in result["error"]
    assert scripts == []


def test_runner_preserves_bounded_agent_findings_after_trusted_receipt() -> None:
    script = agent_tools._build_opencode_script(
        ".ai-bridge/tasks/review-task-001",
        TASK_ID,
        None,
    )

    capture = 'snapshot_worker_artifact "$td/agent-report.md" "$td/worker-report.md" 65536'
    trusted_receipt = 'cat > "$td/agent-report.md" << REOF'
    append_findings = "## Agent-provided findings (untrusted narrative)"
    assert capture in script
    assert "O_NOFOLLOW" in script
    assert "os.replace(tmp, dst)" in script
    assert '[ ! -L "$td/worker-report.md" ]' in script
    assert append_findings in script
    assert script.index(capture) < script.index(trusted_receipt) < script.index(append_findings)


def test_worker_snapshot_is_nofollow_bounded_and_replaces_destination_symlink(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("0123456789abcdef", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("do-not-touch", encoding="utf-8")
    destination = tmp_path / "snapshot.txt"
    destination.symlink_to(victim)

    script = "\n".join(
        [
            *agent_tools._worker_artifact_snapshot_script_lines(),
            f"snapshot_worker_artifact {shlex.quote(str(source))} {shlex.quote(str(destination))} 8",
        ]
    )
    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == "01234567"
    assert victim.read_text(encoding="utf-8") == "do-not-touch"

    source.unlink()
    source.symlink_to(victim)
    destination.write_text("stale", encoding="utf-8")
    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert not destination.exists()
    assert victim.read_text(encoding="utf-8") == "do-not-touch"
