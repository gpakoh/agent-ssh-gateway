"""Fail-closed tests for branch switch + merge worktree permission boundaries (#875)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import mcp_client_tools as tools
import pytest


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "master")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "maintenance.autoDetach", "false")
    (root / "nested").mkdir()
    (root / "nested" / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")

    _git(root, "switch", "-c", "feature/merge")
    (root / "nested" / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "feature")
    feature = _git(root, "rev-parse", "HEAD")

    _git(root, "switch", "master")
    (root / "nested" / "master.txt").write_text("master\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "master")
    master = _git(root, "rev-parse", "HEAD")
    return root, master, feature, _git(root, "rev-parse", "feature/merge")


class _Client:
    def __init__(self, root: Path) -> None:
        self.root = root

    def execute_project_script(
        self, project: str, script: str, timeout_s: int = 30
    ) -> dict[str, Any]:
        assert project == "demo"
        done = subprocess.run(
            ["sh"], input=script, cwd=self.root, capture_output=True,
            text=True, check=False, timeout=timeout_s,
        )
        return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


def _install_runner(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[str]:
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    commands: list[str] = []

    def run(_client: Any, project: str, command: str) -> dict[str, Any]:
        assert project == "demo"
        commands.append(command)
        done = subprocess.run(
            ["sh", "-c", command], cwd=root, capture_output=True, text=True, check=False
        )
        return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}

    monkeypatch.setattr(tools, "run_project_command", run)
    return commands


def _update(root: Path) -> dict[str, Any]:
    return tools.git_update_branch_by_merge(
        _Client(root),
        "demo",
        branch="feature/merge",
        source_branch="master",
        expected_head=_git(root, "rev-parse", "feature/merge"),
    )


def test_merge_checks_both_worktree_diffs_before_successful_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, master, feature, _ = _repo(tmp_path)
    commands = _install_runner(monkeypatch, root)
    result = _update(root)
    assert result["outcome"] == "passed", result
    assert _git(root, "branch", "--show-current") == "feature/merge"
    assert _git(root, "status", "--porcelain=v1") == ""
    assert _git(root, "merge-base", "--is-ancestor", master, "HEAD") == ""
    assert _git(root, "merge-base", "--is-ancestor", feature, "HEAD") == ""
    assert len(commands) == 3


@pytest.mark.parametrize("blocked_phase", ["switch", "merge"])
def test_blocked_worktree_path_refuses_switch_and_merge_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_phase: str
) -> None:
    root, master, feature, _ = _repo(tmp_path)
    commands = _install_runner(monkeypatch, root)
    checked: list[tuple[str, str]] = []

    def probe(_client: Any, _project: str, *, current_head: str, target_head: str) -> dict:
        checked.append((current_head, target_head))
        blocked = (len(checked) == 1) if blocked_phase == "switch" else (len(checked) == 2)
        return {
            "available": True, "checked_count": 2, "blocked_count": int(blocked),
            "blocked_paths": ([{"path": "nested/feature.txt", "reason": "parent_not_writeable"}]
                              if blocked else []),
        }

    monkeypatch.setattr(tools, "probe_git_refresh_worktree_paths", probe)
    result = _update(root)
    assert result["error"]["code"] == "GIT_WRITE_CAPABILITY_BLOCKED"
    assert result["error"]["details"]["phase"] == blocked_phase
    assert result["error"]["details"]["mutation_occurred"] is False
    assert result["error"]["details"]["blocked_paths"][0]["path"] == "nested/feature.txt"
    assert checked == ([(master, feature)] if blocked_phase == "switch"
                       else [(master, feature), (feature, master)])
    assert commands == []
    assert _git(root, "rev-parse", "HEAD") == master
    assert _git(root, "status", "--porcelain=v1") == ""


def test_unavailable_worktree_probe_fails_before_git_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, master, _feature, _ = _repo(tmp_path)
    commands = _install_runner(monkeypatch, root)
    monkeypatch.setattr(
        tools, "probe_git_refresh_worktree_paths",
        lambda *_a, **_kw: {"available": False, "reason": "probe_transport_failed"},
    )
    result = _update(root)
    assert result["error"]["code"] == "GIT_WRITE_PREFLIGHT_UNAVAILABLE"
    assert result["error"]["details"]["mutation_occurred"] is False
    assert commands == []
    assert _git(root, "rev-parse", "HEAD") == master


def test_switch_exit_zero_without_checkout_does_not_run_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, master, _feature, _ = _repo(tmp_path)
    commands = _install_runner(monkeypatch, root)
    actual = tools.run_project_command

    def lie(client: Any, project: str, command: str) -> dict[str, Any]:
        if command.startswith("git switch "):
            commands.append(command)
            return {"exit_code": 0, "stdout": "", "stderr": "permission denied"}
        return actual(client, project, command)

    monkeypatch.setattr(tools, "run_project_command", lie)
    result = _update(root)
    assert result["error"]["code"] == "WORKSPACE_VERIFICATION_FAILED"
    assert result["error"]["details"]["phase"] == "post_switch"
    assert result["error"]["details"]["mutation_occurred"] == "unknown"
    assert len(commands) == 1
    assert _git(root, "rev-parse", "HEAD") == master
    assert _git(root, "status", "--porcelain=v1") == ""


def test_merge_exit_zero_without_actual_merge_does_not_claim_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _master, feature, _ = _repo(tmp_path)
    commands = _install_runner(monkeypatch, root)
    actual = tools.run_project_command

    def lie(client: Any, project: str, command: str) -> dict[str, Any]:
        if command.startswith("git merge "):
            commands.append(command)
            return {"exit_code": 0, "stdout": "", "stderr": "silent unmerged state"}
        return actual(client, project, command)

    monkeypatch.setattr(tools, "run_project_command", lie)
    result = _update(root)
    assert result["error"]["code"] == "WORKSPACE_VERIFICATION_FAILED"
    assert result["error"]["details"]["phase"] == "post_merge"
    assert result["error"]["details"]["mutation_occurred"] == "unknown"
    assert _git(root, "rev-parse", "HEAD") == feature
    assert _git(root, "status", "--porcelain=v1") == ""
    assert len(commands) == 3
