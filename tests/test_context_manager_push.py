"""Tests for ContextManager.push_changes() -- mirrors commit_changes()'s
existing context-not-found / git-not-initialized guards, plus the
success path recording an edit_history entry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.context_manager import ContextManager


def _make_ssh(status_stdout: str = "GIT_REPO") -> AsyncMock:
    ssh = AsyncMock()

    async def execute(session_id, command, timeout=10):
        if "test -d .git" in command:
            return {"stdout": status_stdout, "stderr": "", "exit_code": 0}
        if "git branch --show-current" in command:
            return {"stdout": "main", "stderr": "", "exit_code": 0}
        if "git status --porcelain" in command:
            return {"stdout": "", "stderr": "", "exit_code": 0}
        if "git log -1" in command:
            return {"stdout": "abc123 initial", "stderr": "", "exit_code": 0}
        if "git remote get-url origin" in command:
            return {"stdout": "git@example.com:x/y.git", "stderr": "", "exit_code": 0}
        if command.strip().startswith("cd") and "git push" in command:
            return {"stdout": "", "stderr": "", "exit_code": 0}
        return {"stdout": "", "stderr": "", "exit_code": 0}

    ssh.execute = AsyncMock(side_effect=execute)
    return ssh


@pytest.mark.asyncio
async def test_push_changes_context_not_found():
    cm = ContextManager(_make_ssh())
    result = await cm.push_changes("does-not-exist")
    assert result == {"success": False, "error": "Context not found"}


@pytest.mark.asyncio
async def test_push_changes_git_not_initialized():
    ssh = _make_ssh(status_stdout="NOT_GIT")
    cm = ContextManager(ssh)
    ctx = await cm.create_context("s1", path="/tmp/proj")

    result = await cm.push_changes(ctx.context_id)
    assert result["success"] is False
    assert "not initialized" in result["error"].lower()


@pytest.mark.asyncio
async def test_push_changes_success_records_history():
    ssh = _make_ssh()
    cm = ContextManager(ssh)
    ctx = await cm.create_context("s1", path="/tmp/proj")

    result = await cm.push_changes(ctx.context_id, remote="origin", branch="main")

    assert result["success"] is True
    assert any(e.get("type") == "push" for e in ctx.edit_history)
