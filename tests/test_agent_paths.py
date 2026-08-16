from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server.agent_paths import (
    managed_source_bundle_path,
    managed_workspace_path,
    project_state_key,
    task_archive_path,
    task_dir,
)
from examples.mcp_server.agent_tasks import write_agent_task

TASK_ID = "external-state-task-001"


def test_project_state_key_is_stable_path_safe_and_collision_resistant():
    key = project_state_key("KOJO / production")
    assert key == project_state_key("KOJO / production")
    assert "/" not in key and " " not in key
    assert key != project_state_key("KOJO---production")


@pytest.mark.parametrize("value", ["relative/path", "/"])
def test_state_root_rejects_unsafe_operator_configuration(monkeypatch, value):
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", value)
    with pytest.raises(ValueError):
        task_dir("kojo", TASK_ID)


def test_legacy_layout_remains_when_state_root_unset(monkeypatch):
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    assert task_dir("kojo", TASK_ID) == f".ai-bridge/tasks/{TASK_ID}"
    assert task_archive_path("kojo", TASK_ID) == f".ai-bridge/archive/{TASK_ID}"


def test_managed_workspace_path_is_operator_derived(monkeypatch):
    monkeypatch.setenv("MCP_AGENT_WORKSPACE_ROOT", "/var/lib/mcp-agent/workspaces")
    worktree = managed_workspace_path("kojo-bot-service", TASK_ID)
    key = project_state_key("kojo-bot-service")
    assert worktree == f"/var/lib/mcp-agent/workspaces/{key}/{TASK_ID}"


def test_managed_source_bundle_path_is_exact_sha_and_operator_derived(monkeypatch):
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", "/var/lib/mcp-agent/sources")
    sha = "A" * 40
    key = project_state_key("kojo-bot-service")
    assert managed_source_bundle_path("kojo-bot-service", sha) == (
        f"/var/lib/mcp-agent/sources/{key}/{sha.lower()}.bundle"
    )


def test_managed_source_bundle_requires_full_commit_id(monkeypatch):
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", "/var/lib/mcp-agent/sources")
    with pytest.raises(ValueError, match="full commit id"):
        managed_source_bundle_path("kojo-bot-service", "master")


def test_managed_source_bundle_is_disabled_without_source_root(monkeypatch):
    monkeypatch.delenv("MCP_AGENT_SOURCE_ROOT", raising=False)
    assert managed_source_bundle_path("kojo-bot-service", "a" * 40) is None


def test_write_agent_task_uses_external_state_without_touching_source(tmp_path, monkeypatch):
    source = tmp_path / "read only source"
    source.mkdir()
    (source / "sentinel.txt").write_text("source\n", encoding="utf-8")
    state_root = tmp_path / "agent state"
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(state_root))

    def run_cmd(project: str, script: str) -> dict:
        completed = subprocess.run(
            ["sh", "-c", script],
            cwd=source,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    hostile = "before\nPEOF\nprintf PWNED >/tmp/not-executed\nafter"
    result = write_agent_task(
        run_cmd,
        project="kojo-bot-service",
        task_id=TASK_ID,
        agent="opencode",
        task="Audit KOJO",
        allowed_files=["kojo/**"],
        constraints=hostile,
    )
    assert result["exit_code"] == 0, result["stderr"]

    td = Path(task_dir("kojo-bot-service", TASK_ID))
    assert td.is_dir()
    contract = json.loads((td / "task.json").read_text(encoding="utf-8"))
    assert contract["task_id"] == TASK_ID
    plan = (td / "current-plan.md").read_text(encoding="utf-8")
    assert hostile in plan
    assert f"{td}/agent-status.md" in plan
    assert (td / "agent-status.md").read_text(encoding="utf-8").startswith("Status: created")

    assert not (source / ".ai-bridge").exists()
    assert (source / "sentinel.txt").read_text(encoding="utf-8") == "source\n"
