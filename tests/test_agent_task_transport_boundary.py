"""Regression tests for agent-task filesystem and transport trust boundaries."""

from __future__ import annotations

import inspect
import json
import subprocess
from unittest.mock import MagicMock

import pytest

from examples.mcp_server.agent_tasks import (
    read_agent_log_tail,
    read_agent_task_file,
    write_agent_task,
)


def _shell_runner(cwd):
    def run_command(_project: str, command: str) -> dict[str, object]:
        completed = subprocess.run(
            ["sh", "-c", command],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
        }

    return run_command


def test_write_agent_task_rejects_preexisting_task_directory_symlink(
    tmp_path, monkeypatch
):
    """TEST-09: trusted writes must not follow a task-dir symlink outside state."""
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    tasks = tmp_path / ".ai-bridge" / "tasks"
    tasks.mkdir(parents=True)
    outside = tmp_path / "outside-write"
    outside.mkdir()
    (tasks / task_id).symlink_to(outside, target_is_directory=True)

    result = write_agent_task(
        _shell_runner(tmp_path),
        project="p",
        task_id=task_id,
        agent="opencode",
        task="Do not escape coordination state",
    )

    assert result["exit_code"] != 0
    assert not (outside / "task.json").exists()
    assert not (outside / "current-plan.md").exists()
    assert not (outside / "agent-status.md").exists()


def test_write_agent_task_rejects_symlinked_target_file(tmp_path, monkeypatch):
    """Trusted redirections must not follow a pre-existing task file symlink."""
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    td = tmp_path / ".ai-bridge" / "tasks" / task_id
    td.mkdir(parents=True)
    outside = tmp_path / "outside-task-json"
    outside.write_text("keep", encoding="utf-8")
    (td / "task.json").symlink_to(outside)

    result = write_agent_task(
        _shell_runner(tmp_path),
        project="p",
        task_id=task_id,
        agent="opencode",
        task="Do not follow target links",
    )

    assert result["exit_code"] == 1
    assert outside.read_text(encoding="utf-8") == "keep"
    assert "/tmp/" not in result["stderr"]


def test_list_agent_tasks_rejects_tasks_root_symlink(tmp_path, monkeypatch):
    """TEST-09: task listing must not enumerate a symlinked external root."""
    from examples.mcp_server.agent_tasks import list_agent_tasks

    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    bridge = tmp_path / ".ai-bridge"
    bridge.mkdir()
    outside = tmp_path / "outside-list"
    outside.mkdir()
    marker = "external-task-marker"
    (outside / marker).mkdir()
    (bridge / "tasks").symlink_to(outside, target_is_directory=True)

    result = list_agent_tasks(_shell_runner(tmp_path), project="p")

    assert marker not in str(result.get("stdout", ""))
    assert result["stdout"] == "(no tasks)"


def test_read_agent_task_file_rejects_task_directory_symlink(tmp_path, monkeypatch):
    """Readonly task-file access must not disclose a same-named external file."""
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    tasks = tmp_path / ".ai-bridge" / "tasks"
    tasks.mkdir(parents=True)
    outside = tmp_path / "outside-read"
    outside.mkdir()
    marker = "EXTERNAL-STATUS-MARKER"
    (outside / "agent-status.md").write_text(marker, encoding="utf-8")
    (tasks / task_id).symlink_to(outside, target_is_directory=True)

    result = read_agent_task_file(
        _shell_runner(tmp_path),
        project="p",
        task_id=task_id,
        filename="agent-status.md",
    )

    assert marker not in str(result.get("stdout", ""))
    assert result["stdout"] == "(not found)"


@pytest.mark.parametrize("entrypoint", ["agent", "opencode"])
def test_run_entrypoints_reject_symlinked_task_contract(
    tmp_path, monkeypatch, entrypoint
):
    """TEST-09: a symlinked task contract must never reach runner-script execution."""
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    tasks = tmp_path / ".ai-bridge" / "tasks"
    tasks.mkdir(parents=True)
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (outside / "task.json").write_text(
        json.dumps(
            {
                "agent": "opencode",
                "allowed_backends": ["opencode"],
                "allowed_files": [],
                "forbidden_files": [],
                "required_checks": [],
                "worktree_path": "",
                "base_ref": "",
            }
        ),
        encoding="utf-8",
    )
    (outside / "current-plan.md").write_text("# External plan\n", encoding="utf-8")
    (tasks / task_id).symlink_to(outside, target_is_directory=True)
    run_script = MagicMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})

    if entrypoint == "agent":
        from examples.mcp_server.agent_tools import project_run_agent

        monkeypatch.setattr(
            "examples.mcp_server.agent_tools._resolve_project_root", lambda _project: None
        )
        result = project_run_agent(
            _shell_runner(tmp_path),
            project="p",
            task_id=task_id,
            run_script=run_script,
        )
    else:
        from examples.mcp_server.opencode_tools import project_run_opencode

        monkeypatch.setattr(
            "examples.mcp_server.opencode_tools._resolve_project_root", lambda _project: None
        )
        result = project_run_opencode(
            _shell_runner(tmp_path),
            project="p",
            task_id=task_id,
            run_script=run_script,
        )

    assert result["status"] == "error"
    run_script.assert_not_called()


def test_adapter_transport_classification_matrix_is_explicit():
    """Executable contract: readonly routes stay generic; mutations stay trusted."""
    import examples.mcp_server.mcp_infra.adapters.agent as adapter

    for name in (
        "gateway_read_agent_status",
        "gateway_read_agent_report",
        "gateway_read_agent_diff",
        "gateway_read_agent_log",
        "gateway_list_agent_tasks",
    ):
        source = inspect.getsource(getattr(adapter, name))
        assert "run_project_command" in source, name
        assert "execute_project_script" not in source, name

    for name in ("gateway_write_agent_task", "gateway_archive_agent_task"):
        source = inspect.getsource(getattr(adapter, name))
        assert "execute_project_script" in source, name
        assert "run_project_command(" not in source, name

    run_opencode_source = inspect.getsource(adapter.gateway_run_opencode)
    assert "run_project_command" in run_opencode_source
    assert "_server_agent_client().execute_script" in run_opencode_source
    assert "_server_client().execute_project_script" not in run_opencode_source

    run_agent_source = inspect.getsource(adapter._build_agent_submit)
    assert "run_project_command" in run_agent_source
    assert "_server_agent_client().execute_script" in run_agent_source
    assert "_server_client().execute_project_script" not in run_agent_source


def test_read_agent_log_rejects_task_directory_symlink(tmp_path, monkeypatch):
    """Fixed log filename is insufficient if the containing task dir is a symlink."""
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    tasks = tmp_path / ".ai-bridge" / "tasks"
    tasks.mkdir(parents=True)
    outside = tmp_path / "outside-log"
    outside.mkdir()
    marker = "EXTERNAL-LOG-MARKER"
    (outside / "opencode-output.log").write_text(marker + "\n", encoding="utf-8")
    (tasks / task_id).symlink_to(outside, target_is_directory=True)

    result = read_agent_log_tail(
        _shell_runner(tmp_path),
        project="p",
        task_id=task_id,
        tail_lines=20,
    )

    assert marker not in str(result.get("stdout", ""))
    assert result["stdout"] == "(not found)"
