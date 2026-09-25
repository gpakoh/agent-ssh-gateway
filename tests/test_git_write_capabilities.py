"""Regression coverage for AO-004 Git metadata write-capability preflight."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from git_write_capabilities import probe_git_write_capabilities
from mcp_client_tools import git_add, git_commit, git_create_branch, git_update_branch_by_merge


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    # Keep Git's automatic maintenance synchronous in this fixture.  Some CI
    # runners detach it after commit, leaving objects/maintenance.lock alive
    # just long enough for the pre-probe snapshot to observe it and the
    # post-probe snapshot to observe its disappearance.
    _git(repo, "config", "maintenance.autoDetach", "false")
    _git(repo, "config", "gc.autoDetach", "false")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _repo_metadata_snapshot(repo: Path) -> dict[str, object]:
    git_dir = repo / ".git"
    refs = {
        str(path.relative_to(git_dir)): path.read_bytes()
        for path in sorted((git_dir / "refs").rglob("*"))
        if path.is_file()
    }
    objects = tuple(
        sorted(
            str(path.relative_to(git_dir))
            for path in (git_dir / "objects").rglob("*")
            if path.is_file()
        )
    )
    index = git_dir / "index"
    return {
        "head": (git_dir / "HEAD").read_bytes(),
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "refs": refs,
        "objects": objects,
        "status": _git(repo, "status", "--porcelain=v1"),
        "commit": _git(repo, "rev-parse", "HEAD"),
    }


class _LocalScriptClient:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def execute_project_script(self, project: str, script: str, timeout_s: int = 30) -> dict:
        assert project == "demo"
        completed = subprocess.run(
            ["sh"],
            cwd=self.repo,
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }


def _probe_stdout(
    *,
    index: bool = True,
    objects: bool = True,
    refs: bool = True,
    head: bool = True,
    detached: bool = False,
) -> str:
    return (
        "available=1\n"
        f"index={int(index)}\n"
        f"objects={int(objects)}\n"
        f"refs={int(refs)}\n"
        f"head={int(head)}\n"
        f"detached={int(detached)}\n"
    )


class _CapabilityClient:
    def __init__(self, probe_stdout: str, mutation_result: dict | None = None) -> None:
        self.probe_stdout = probe_stdout
        self.mutation_result = mutation_result or {"exit_code": 0, "stdout": "ok\n", "stderr": ""}
        self.probe_calls = 0
        self.scripts: list[str] = []
        self.commands: list[str] = []

    def execute_project_script(self, project: str, script: str, timeout_s: int = 30) -> dict:
        self.probe_calls += 1
        self.scripts.append(script)
        return {"exit_code": 0, "stdout": self.probe_stdout, "stderr": ""}

    def execute_project_command(self, project: str, command: str) -> dict:
        self.commands.append(command)
        return dict(self.mutation_result)


def test_probe_is_non_mutating_on_real_repository(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    before = _repo_metadata_snapshot(repo)

    result = probe_git_write_capabilities(_LocalScriptClient(repo), "demo")

    after = _repo_metadata_snapshot(repo)
    assert result["available"] is True
    assert result["fully_writeable"] is True
    assert result["blocked_components"] == []
    assert result["non_mutating"] is True
    assert result["execution_plane"] == "ssh_project"
    assert result["components"] == {
        "index": {"writeable": True},
        "objects": {"writeable": True},
        "refs": {"writeable": True},
        "head": {"writeable": True},
    }
    assert after == before


def test_git_create_branch_blocks_refs_before_any_git_mutation() -> None:
    client = _CapabilityClient(_probe_stdout(refs=False))

    result = git_create_branch(client, "demo", "feature/blocked")

    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_WRITE_CAPABILITY_BLOCKED"
    details = result["error"]["details"]
    assert details["blocked_components"] == ["refs"]
    assert details["mutation_occurred"] is False
    assert client.commands == []
    assert client.probe_calls == 1
    assert "target_ref=refs/heads/feature/blocked" in client.scripts[0]


def test_git_add_blocks_objects_before_index_or_object_mutation() -> None:
    client = _CapabilityClient(_probe_stdout(objects=False))

    result = git_add(client, "demo", ["file.txt"])

    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_WRITE_CAPABILITY_BLOCKED"
    assert result["error"]["details"]["blocked_components"] == ["objects"]
    assert result["error"]["details"]["mutation_occurred"] is False
    assert client.commands == []


def test_detached_commit_does_not_require_refs_when_index_objects_head_are_writeable() -> None:
    client = _CapabilityClient(_probe_stdout(refs=False, detached=True))

    result = git_commit(client, "demo", "detached commit")

    assert result["exit_code"] == 0
    assert client.probe_calls == 1
    assert len(client.commands) == 1
    assert "commit -m 'detached commit'" in client.commands[0]


def test_attached_commit_requires_refs_and_fails_before_mutation() -> None:
    client = _CapabilityClient(_probe_stdout(refs=False, detached=False))

    result = git_commit(client, "demo", "attached commit")

    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_WRITE_CAPABILITY_BLOCKED"
    assert result["error"]["details"]["required_components"] == ["index", "objects", "refs"]
    assert result["error"]["details"]["mutation_occurred"] is False
    assert client.probe_calls == 1
    assert client.commands == []


def test_preflight_transport_failure_refuses_mutation() -> None:
    class Client:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def execute_project_script(self, project: str, script: str, timeout_s: int = 30) -> dict:
            raise RuntimeError("transport unavailable")

        def execute_project_command(self, project: str, command: str) -> dict:
            self.commands.append(command)
            return {"exit_code": 0, "stdout": "unexpected", "stderr": ""}

    client = Client()
    result = git_add(client, "demo", ["file.txt"])

    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_WRITE_PREFLIGHT_UNAVAILABLE"
    assert result["error"]["retryable"] is True
    assert result["error"]["details"]["mutation_occurred"] is False
    assert client.commands == []


def test_permission_drift_after_green_preflight_is_typed_and_not_blindly_retryable() -> None:
    client = _CapabilityClient(
        _probe_stdout(),
        mutation_result={
            "exit_code": 128,
            "stdout": "",
            "stderr": "fatal: cannot lock ref 'refs/heads/feature/x': Permission denied",
        },
    )

    result = git_create_branch(client, "demo", "feature/x")

    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_WRITE_CAPABILITY_BLOCKED"
    details = result["error"]["details"]
    assert details["blocked_components"] == ["refs"]
    assert details["mutation_occurred"] == "unknown"
    assert result["error"]["retryable"] is True
    assert "Do not blindly retry" in result["error"]["hint"]
    assert client.commands == ["git switch -c feature/x"]


def test_malformed_probe_output_fails_closed() -> None:
    client = _CapabilityClient("available=1\nindex=yes\n")

    result = git_add(client, "demo", ["file.txt"])

    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_WRITE_PREFLIGHT_UNAVAILABLE"
    assert result["error"]["details"]["preflight"]["reason"] == "malformed_probe_output"
    assert client.commands == []


def test_merge_update_blocks_before_first_git_mutation_when_refs_unwriteable(
    monkeypatch, tmp_path: Path
) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "switch", "-c", "feature/update")
    feature_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")
    client = _CapabilityClient(_probe_stdout(refs=False))
    monkeypatch.setattr("mcp_client_tools._resolve_project", lambda project: repo)

    result = git_update_branch_by_merge(
        client,
        "demo",
        branch="feature/update",
        source_branch="main",
        expected_head=feature_head,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_WRITE_CAPABILITY_BLOCKED"
    assert result["error"]["details"]["blocked_components"] == ["refs"]
    assert result["error"]["details"]["mutation_occurred"] is False
    assert client.commands == []
    assert "target_ref=refs/heads/feature/update" in client.scripts[0]
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "feature/update") == feature_head
