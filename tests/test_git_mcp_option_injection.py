"""Regression tests: git_add/git_push option-injection validation."""

from __future__ import annotations

import pytest
from mcp_client_tools import git_add, git_create_branch, git_push


class _StubClient:
    def __init__(self):
        self.commands: list[str] = []

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
    git_push(client, "proj", remote="origin", branch="feature/x")
    assert client.commands == ["git push origin feature/x"]


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
