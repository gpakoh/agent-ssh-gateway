"""Regression tests for agent-task filesystem and transport trust boundaries."""

from __future__ import annotations

import inspect
import json
import subprocess
from unittest.mock import MagicMock

import pytest

from examples.mcp_server.agent_paths import project_state_key
from examples.mcp_server.agent_tasks import (
    archive_agent_task,
    list_agent_tasks,
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


def _write_external_contract(td, *, marker: str = "EXTERNAL-CONTRACT") -> None:
    td.mkdir(parents=True, exist_ok=True)
    (td / "task.json").write_text(
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
    (td / "current-plan.md").write_text(f"# {marker}\n", encoding="utf-8")
    (td / "agent-status.md").write_text(marker, encoding="utf-8")
    (td / "agent-report.md").write_text(marker, encoding="utf-8")
    (td / "implementation-diff.patch").write_text(marker, encoding="utf-8")
    (td / "opencode-output.log").write_text(marker + "\n", encoding="utf-8")


def _legacy_bridge_symlink(tmp_path, task_id: str):
    external = tmp_path / "external-state"
    td = external / "tasks" / task_id
    _write_external_contract(td)
    (tmp_path / ".ai-bridge").symlink_to(external, target_is_directory=True)
    return external, td


def test_legacy_bridge_ancestor_symlink_blocks_write(tmp_path, monkeypatch):
    """RED: .ai-bridge itself is part of the trust boundary, not only tasks/."""
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    external, td = _legacy_bridge_symlink(tmp_path, task_id)
    before = (td / "task.json").read_text(encoding="utf-8")

    result = write_agent_task(
        _shell_runner(tmp_path),
        project="p",
        task_id=task_id,
        agent="opencode",
        task="must stay inside trusted coordination state",
    )

    assert result["exit_code"] != 0
    assert (td / "task.json").read_text(encoding="utf-8") == before
    assert (external / "tasks" / task_id).is_dir()


@pytest.mark.parametrize(
    "filename",
    ["agent-status.md", "agent-report.md", "implementation-diff.patch"],
)
def test_legacy_bridge_ancestor_symlink_blocks_task_reads(
    tmp_path, monkeypatch, filename
):
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    _legacy_bridge_symlink(tmp_path, task_id)

    result = read_agent_task_file(
        _shell_runner(tmp_path),
        project="p",
        task_id=task_id,
        filename=filename,
    )

    assert "EXTERNAL-CONTRACT" not in str(result.get("stdout", ""))
    assert result["stdout"] == "(not found)"


def test_legacy_bridge_ancestor_symlink_blocks_log_and_list(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    _legacy_bridge_symlink(tmp_path, task_id)

    log_result = read_agent_log_tail(
        _shell_runner(tmp_path), project="p", task_id=task_id, tail_lines=20
    )
    list_result = list_agent_tasks(_shell_runner(tmp_path), project="p")

    assert "EXTERNAL-CONTRACT" not in str(log_result.get("stdout", ""))
    assert log_result["stdout"] == "(not found)"
    assert task_id not in str(list_result.get("stdout", ""))
    assert list_result["stdout"] == "(no tasks)"


@pytest.mark.parametrize("entrypoint", ["agent", "opencode"])
def test_legacy_bridge_ancestor_symlink_blocks_run_contract(
    tmp_path, monkeypatch, entrypoint
):
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    _legacy_bridge_symlink(tmp_path, task_id)
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


def test_legacy_bridge_ancestor_symlink_blocks_archive(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
    task_id = "a12345678901"
    external, td = _legacy_bridge_symlink(tmp_path, task_id)

    result = archive_agent_task(
        _shell_runner(tmp_path), project="p", task_id=task_id
    )

    assert result["exit_code"] != 0
    assert td.is_dir()
    assert not (external / "archive" / task_id).exists()


def test_configured_project_key_ancestor_symlink_fails_closed(tmp_path, monkeypatch):
    project = "p"
    task_id = "a12345678901"
    state_root = tmp_path / "state-root"
    state_root.mkdir()
    external = tmp_path / "external-project-state"
    external.mkdir()
    key = project_state_key(project)
    (state_root / key).symlink_to(external, target_is_directory=True)
    td = external / "tasks" / task_id
    _write_external_contract(td)
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(state_root))
    before = (td / "task.json").read_text(encoding="utf-8")

    read_result = read_agent_task_file(
        _shell_runner(tmp_path),
        project=project,
        task_id=task_id,
        filename="agent-status.md",
    )
    write_result = write_agent_task(
        _shell_runner(tmp_path),
        project=project,
        task_id=task_id,
        agent="opencode",
        task="must reject project-key symlink",
    )

    assert read_result["stdout"] == "(not found)"
    assert write_result["exit_code"] != 0
    assert (td / "task.json").read_text(encoding="utf-8") == before


def test_configured_state_root_symlink_fails_closed(tmp_path, monkeypatch):
    """Configured state root is a trust anchor and must itself be non-symlink."""
    project = "p"
    task_id = "a12345678901"
    external = tmp_path / "external-root"
    key = project_state_key(project)
    td = external / key / "tasks" / task_id
    _write_external_contract(td)
    state_root = tmp_path / "state-root-link"
    state_root.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(state_root))
    before = (td / "task.json").read_text(encoding="utf-8")

    read_result = read_agent_task_file(
        _shell_runner(tmp_path),
        project=project,
        task_id=task_id,
        filename="agent-status.md",
    )
    write_result = write_agent_task(
        _shell_runner(tmp_path),
        project=project,
        task_id=task_id,
        agent="opencode",
        task="must reject configured root symlink",
    )

    assert read_result["stdout"] == "(not found)"
    assert write_result["exit_code"] != 0
    assert (td / "task.json").read_text(encoding="utf-8") == before


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
