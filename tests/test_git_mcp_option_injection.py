"""Regression tests: git_add/create-branch validation and MCP git_push boundary."""

from __future__ import annotations

import pytest
from mcp_client_tools import (
    git_add,
    git_create_branch,
    git_fetch_ref,
    git_push,
    git_refresh_branch_to_head,
    git_update_branch_by_merge,
)


class _LocalGitClient:
    def __init__(self, root):
        self.root = root
        self.commands: list[str] = []

    def execute_project_script(self, project: str, script: str, timeout_s: int = 30) -> dict:
        return {
            "exit_code": 0,
            "stdout": "available=1\nindex=1\nobjects=1\nrefs=1\nhead=1\ndetached=0\n",
            "stderr": "",
        }

    def execute_project_command(self, project: str, command: str) -> dict:
        import subprocess

        self.commands.append(command)
        completed = subprocess.run(
            command,
            shell=True,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }


def _git(root, *args: str) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_merge_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "switch", "-c", "feature/update")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    feature_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "master")
    (repo / "master.txt").write_text("master\n", encoding="utf-8")
    _git(repo, "add", "master.txt")
    _git(repo, "commit", "-m", "master")
    return repo, feature_head


class _StubClient:
    def __init__(self):
        self.commands: list[str] = []

    def execute_project_script(self, project: str, script: str, timeout_s: int = 30) -> dict:
        return {
            "exit_code": 0,
            "stdout": "available=1\nindex=1\nobjects=1\nrefs=1\nhead=1\ndetached=0\n",
            "stderr": "",
        }

    def execute_project_command(self, project: str, command: str) -> dict:
        self.commands.append(command)
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}


def test_git_push_rejects_option_injection_remote():
    client = _StubClient()
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        git_push(client, "proj", remote="--mirror")


def test_git_push_rejects_option_injection_branch():
    client = _StubClient()
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        git_push(client, "proj", remote="origin", branch="--delete main")


def test_git_push_rejects_refspec_colon():
    client = _StubClient()
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        git_push(client, "proj", remote="origin:main")


def test_git_push_well_formed():
    client = _StubClient()
    from unittest.mock import patch

    with patch("mcp_client_tools.git_push_control_plane", return_value={"ok": True}) as push:
        git_push(client, "proj", remote="origin", branch="feature/x")
    push.assert_called_once_with(project="proj", remote="origin", branch="feature/x")
    assert client.commands == []


def test_git_fetch_ref_rejects_option_or_refspec_injection():
    client = _StubClient()
    for value in ("--all", "origin:main", "has space"):
        with pytest.raises(ValueError, match="INVALID_INPUT"):
            git_fetch_ref(client, "proj", remote=value)
    assert client.commands == []


def test_git_refresh_branch_to_head_fast_forwards_clean_current_branch(tmp_path):
    repo, feature_head = _init_merge_repo(tmp_path)
    _git(repo, "switch", "feature/update")
    (repo / "next.txt").write_text("next\n", encoding="utf-8")
    _git(repo, "add", "next.txt")
    _git(repo, "commit", "-m", "next")
    target_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--hard", feature_head)
    client = _LocalGitClient(repo)
    from unittest.mock import patch

    with patch("mcp_client_tools._resolve_project", return_value=repo):
        result = git_refresh_branch_to_head(
            client,
            "proj",
            branch="feature/update",
            expected_current_head=feature_head,
            target_head=target_head,
        )

    assert result["exit_code"] == 0
    assert result["previous_head"] == feature_head
    assert result["new_head"] == target_head
    assert result["clean"] is True
    assert _git(repo, "rev-parse", "HEAD") == target_head
    assert client.commands == [f"git reset --hard {target_head}"]


def test_git_refresh_branch_to_head_rejects_non_fast_forward(tmp_path):
    repo, feature_head = _init_merge_repo(tmp_path)
    _git(repo, "switch", "feature/update")
    master_head = _git(repo, "rev-parse", "master")
    client = _LocalGitClient(repo)
    from unittest.mock import patch

    with patch("mcp_client_tools._resolve_project", return_value=repo):
        result = git_refresh_branch_to_head(
            client,
            "proj",
            branch="feature/update",
            expected_current_head=feature_head,
            target_head=master_head,
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_NON_FAST_FORWARD"
    assert client.commands == []
    assert _git(repo, "rev-parse", "HEAD") == feature_head


def test_git_refresh_branch_to_head_rejects_stale_expected_head(tmp_path):
    repo, feature_head = _init_merge_repo(tmp_path)
    _git(repo, "switch", "feature/update")
    client = _LocalGitClient(repo)
    from unittest.mock import patch

    with patch("mcp_client_tools._resolve_project", return_value=repo):
        result = git_refresh_branch_to_head(
            client,
            "proj",
            branch="feature/update",
            expected_current_head="0" * 40,
            target_head=feature_head,
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert client.commands == []


def test_git_create_branch_well_formed():
    client = _StubClient()
    git_create_branch(client, "proj", branch="ai/fleet-hardening")
    assert client.commands == ["git switch -c ai/fleet-hardening"]


def test_git_create_branch_rejects_protected_names():
    client = _StubClient()
    for branch in ("main", "master"):
        with pytest.raises(ValueError, match="POLICY_DENIED"):
            git_create_branch(client, "proj", branch=branch)
    assert client.commands == []


def test_git_create_branch_rejects_option_or_refspec_injection():
    client = _StubClient()
    for branch in ("--orphan", "HEAD:feature"):
        with pytest.raises(ValueError, match="INVALID_INPUT"):
            git_create_branch(client, "proj", branch=branch)
    assert client.commands == []


def test_git_add_rejects_option_paths():
    client = _StubClient()
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        git_add(client, "proj", paths=["-A"])
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        git_add(client, "proj", paths=["--patch"])


def test_git_add_uses_separator():
    client = _StubClient()
    git_add(client, "proj", paths=["app/foo.py", "tests/"])
    assert client.commands == ["git add -- app/foo.py tests/"]


def test_git_add_empty_paths_rejected():
    client = _StubClient()
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        git_add(client, "proj", paths=[])


def test_git_update_branch_by_merge_merges_source_into_existing_branch(tmp_path):
    repo, feature_head = _init_merge_repo(tmp_path)
    client = _LocalGitClient(repo)
    from unittest.mock import patch

    with patch("mcp_client_tools._resolve_project", return_value=repo):
        result = git_update_branch_by_merge(
            client,
            "proj",
            branch="feature/update",
            source_branch="master",
            expected_head=feature_head,
        )

    assert result["exit_code"] == 0
    assert result["branch"] == "feature/update"
    assert result["source_branch"] == "master"
    assert result["previous_head"] == feature_head
    assert _git(repo, "branch", "--show-current") == "feature/update"
    assert _git(repo, "merge-base", "--is-ancestor", "master", "HEAD") == ""
    assert client.commands == [
        "git switch feature/update",
        "git merge --no-ff --no-edit master",
        "git rev-parse HEAD",
    ]


def test_git_update_branch_by_merge_rejects_protected_target_before_commands():
    client = _StubClient()
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        git_update_branch_by_merge(client, "proj", branch="master", source_branch="feature/x")
    assert client.commands == []


def test_git_update_branch_by_merge_rejects_option_or_refspec_injection():
    client = _StubClient()
    for value in ("--merge", "origin:master", "has space"):
        with pytest.raises(ValueError, match="INVALID_INPUT"):
            git_update_branch_by_merge(client, "proj", branch="feature/x", source_branch=value)
    assert client.commands == []


def test_git_update_branch_by_merge_rejects_expected_head_mismatch(tmp_path):
    repo, feature_head = _init_merge_repo(tmp_path)
    client = _LocalGitClient(repo)
    wrong_head = "0" * 40
    from unittest.mock import patch

    with patch("mcp_client_tools._resolve_project", return_value=repo):
        result = git_update_branch_by_merge(
            client,
            "proj",
            branch="feature/update",
            source_branch="master",
            expected_head=wrong_head,
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert result["error"]["details"]["actual_head"] == feature_head
    assert client.commands == []


def test_git_update_branch_by_merge_rejects_dirty_worktree(tmp_path):
    repo, _feature_head = _init_merge_repo(tmp_path)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    client = _LocalGitClient(repo)
    from unittest.mock import patch

    with patch("mcp_client_tools._resolve_project", return_value=repo):
        result = git_update_branch_by_merge(
            client,
            "proj",
            branch="feature/update",
            source_branch="master",
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert client.commands == []


def _init_conflict_repo(tmp_path, start_branch: str):
    """Build a repo where feature/update and master modified the same file.

    Returns (repo, feature_head, master_head) with the repo left on
    ``start_branch`` so the caller's original branch is predictable.
    """
    repo = tmp_path / "conflict-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "same.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "same.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "switch", "-c", "feature/update")
    (repo / "same.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "same.txt")
    _git(repo, "commit", "-m", "feature change")
    feature_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "master")
    (repo / "same.txt").write_text("master\n", encoding="utf-8")
    _git(repo, "add", "same.txt")
    _git(repo, "commit", "-m", "master change")
    master_head = _git(repo, "rev-parse", "HEAD")
    if start_branch == "feature/update":
        _git(repo, "switch", start_branch)
    else:
        _git(repo, "switch", "-c", start_branch)
    return repo, feature_head, master_head


def test_git_update_branch_by_merge_conflict_recovers_to_target_branch(tmp_path):
    repo, feature_head, master_head = _init_conflict_repo(tmp_path, "feature/update")
    client = _LocalGitClient(repo)
    from unittest.mock import patch

    with patch("mcp_client_tools._resolve_project", return_value=repo):
        result = git_update_branch_by_merge(
            client,
            "proj",
            branch="feature/update",
            source_branch="master",
            expected_head=feature_head,
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "MERGE_CONFLICT"
    assert result["error"]["retryable"] is True
    details = result["error"]["details"]
    assert details["branch"] == "feature/update"
    assert details["source_branch"] == "master"
    assert details["previous_head"] == feature_head
    assert details["source_head"] == master_head
    assert details["original_branch"] == "feature/update"
    assert details["original_head"] == feature_head
    assert details["conflicted_paths"] == ["same.txt"]
    recovery = details["recovery"]
    assert recovery["merge_aborted"] is True
    assert recovery["restored_original_ref"] is True
    assert recovery["final_branch"] == "feature/update"
    assert recovery["final_head"] == feature_head
    assert recovery["final_status_entries"] == 0

    assert _git(repo, "branch", "--show-current") == "feature/update"
    assert _git(repo, "rev-parse", "HEAD") == feature_head
    assert _git(repo, "status", "--porcelain=v1") == ""
    assert _git(repo, "rev-parse", "master") == master_head


def test_git_update_branch_by_merge_conflict_recovers_to_original_branch(tmp_path):
    repo, feature_head, master_head = _init_conflict_repo(tmp_path, "staging")
    client = _LocalGitClient(repo)
    from unittest.mock import patch

    with patch("mcp_client_tools._resolve_project", return_value=repo):
        result = git_update_branch_by_merge(
            client,
            "proj",
            branch="feature/update",
            source_branch="master",
            expected_head=feature_head,
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "MERGE_CONFLICT"
    assert result["error"]["retryable"] is True
    details = result["error"]["details"]
    assert details["branch"] == "feature/update"
    assert details["source_branch"] == "master"
    assert details["previous_head"] == feature_head
    assert details["source_head"] == master_head
    assert details["original_branch"] == "staging"
    assert details["original_ref"] == "refs/heads/staging"
    assert details["original_head"] == master_head
    assert details["conflicted_paths"] == ["same.txt"]
    recovery = details["recovery"]
    assert recovery["merge_aborted"] is True
    assert recovery["restored_original_ref"] is True
    assert recovery["final_branch"] == "staging"
    assert recovery["final_head"] == master_head
    assert recovery["final_status_entries"] == 0

    assert _git(repo, "branch", "--show-current") == "staging"
    assert _git(repo, "rev-parse", "HEAD") == master_head
    assert _git(repo, "status", "--porcelain=v1") == ""
    assert _git(repo, "rev-parse", "feature/update") == feature_head
