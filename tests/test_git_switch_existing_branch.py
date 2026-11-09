"""Real Git regression coverage for exact-lease existing-branch switching."""

from __future__ import annotations

import subprocess
from pathlib import Path

import mcp_client_tools as tools
import pytest


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "git-project"
    root.mkdir()
    _git(root, "init", "-b", "master")
    _git(root, "config", "user.email", "tester@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "master")
    master = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "-c", "feature/review")
    (root / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "feature")
    feature = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "master")
    return root, master, feature


class Client:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.scripts: list[str] = []
        self.before_switch = None
        self.fail_switch = False

    def execute_project_script(
        self, project: str, script: str, timeout_s: int = 30
    ) -> dict:
        assert project == "demo"
        self.scripts.append(script)
        if "TARGET_BRANCH=" in script and "flock -n 9" in script:
            if self.before_switch is not None:
                self.before_switch()
            if self.fail_switch:
                return {"exit_code": 128, "stdout": "", "stderr": "fatal: rejected"}
        result = subprocess.run(
            ["sh"], input=script, cwd=self.root, capture_output=True,
            text=True, check=False, timeout=timeout_s
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


def _switch(client: Client, root: Path, *, branch="feature/review", **overrides):
    state = tools._project_git_state(root)
    values = {
        "project": "demo",
        "branch": branch,
        "expected_current_branch": state["branch"],
        "expected_current_head": state["head"],
        "expected_target_head": _git(root, "rev-parse", "refs/heads/feature/review"),
        "expected_status_sha256": state["status_sha256"],
        "expected_index_sha256": state["index_sha256"],
    }
    values.update(overrides)
    return tools.git_switch_existing_branch(client, **values)


def test_existing_branch_switch_checks_exact_state_and_preserves_history(
    tmp_path: Path, monkeypatch
) -> None:
    root, master, feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    client = Client(root)
    result = _switch(client, root)
    assert result["outcome"] == "passed", result
    assert result["previous_head"] == master
    assert result["new_head"] == feature
    assert result["clean"] and result["mutation_occurred"] is True
    assert _git(root, "branch", "--show-current") == "feature/review"
    assert _git(root, "rev-parse", "refs/heads/master") == master
    assert _git(root, "rev-parse", "HEAD") == feature
    assert _git(root, "status", "--porcelain=v1") == ""
    assert "flock -n 9" in client.scripts[-1]


def test_switch_current_branch_is_noop_without_script(tmp_path: Path, monkeypatch):
    root, master, _feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    client = Client(root)
    result = _switch(client, root, branch="master", expected_target_head=master)
    assert result["outcome"] == "passed" and result["already_current"]
    assert client.scripts == []
    assert _git(root, "rev-parse", "HEAD") == master


@pytest.mark.parametrize(
    "bad_branch", ["--force", "master;touch", "refs/heads/master", "a..b", "x//y", "feature/x.lock"]
)
def test_rejects_unsafe_names_without_mutation(tmp_path: Path, monkeypatch, bad_branch):
    root, master, _feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        _switch(Client(root), root, branch=bad_branch)
    assert _git(root, "rev-parse", "HEAD") == master


def test_missing_or_changed_target_is_fail_closed(tmp_path: Path, monkeypatch):
    root, master, feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    client = Client(root)
    for branch, expected in [("feature/missing", feature), ("feature/review", master)]:
        result = _switch(client, root, branch=branch, expected_target_head=expected)
        assert result["error"]["code"] == "GIT_LOCAL_REF_MISSING"
        assert result["error"]["details"]["mutation_occurred"] is False
    assert not client.scripts
    assert _git(root, "rev-parse", "HEAD") == master


def test_failed_git_switch_command_has_declared_typed_failure(
    tmp_path: Path, monkeypatch
) -> None:
    root, master, _feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    client = Client(root)
    client.fail_switch = True
    result = _switch(client, root)
    assert result["error"]["code"] == "GIT_OPERATION_FAILED"
    assert result["error"]["details"]["mutation_occurred"] == "unknown"
    assert result["error"]["retryable"] is False
    assert _git(root, "rev-parse", "HEAD") == master


def test_dirty_workspace_refused_without_discard(tmp_path: Path, monkeypatch):
    root, master, _feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    (root / "app.py").write_text("keep my work\n", encoding="utf-8")
    result = _switch(Client(root), root)
    assert result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert result["error"]["details"]["mutation_occurred"] is False
    assert (root / "app.py").read_text(encoding="utf-8") == "keep my work\n"
    assert _git(root, "rev-parse", "HEAD") == master


def test_stale_snapshot_rejected_before_runner_invocation(tmp_path: Path, monkeypatch):
    root, master, _feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    client = Client(root)
    result = _switch(
        client, root,
        expected_current_head="f" * 40,
        expected_index_sha256="a" * 64
    )
    assert result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert not client.scripts
    assert _git(root, "rev-parse", "HEAD") == master


def test_concurrent_file_edit_before_switch_is_retained(tmp_path: Path, monkeypatch):
    root, master, _feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    client = Client(root)
    client.before_switch = lambda: (root / "app.py").write_text(
        "concurrent edit\n", encoding="utf-8"
    )
    result = _switch(client, root)
    assert result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert result["error"]["details"]["mutation_occurred"] is False
    assert (root / "app.py").read_text(encoding="utf-8") == "concurrent edit\n"
    assert _git(root, "rev-parse", "HEAD") == master


def test_race_moved_target_ref_does_not_select_wrong_commit(tmp_path: Path, monkeypatch):
    root, master, _feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    client = Client(root)
    client.before_switch = lambda: _git(
        root, "update-ref", "refs/heads/feature/review", master
    )
    result = _switch(client, root)
    assert result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert result["error"]["details"]["mutation_occurred"] is False
    assert _git(root, "rev-parse", "HEAD") == master


def test_invalid_hash_rejected_locally(tmp_path: Path, monkeypatch):
    root, master, _feature = _repo(tmp_path)
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)
    client = Client(root)
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        _switch(client, root, expected_index_sha256="nope")
    assert not client.scripts
    assert _git(root, "rev-parse", "HEAD") == master
