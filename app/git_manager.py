"""Git operations wrapper for SSH sessions."""

import asyncio
import logging
from typing import Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class GitStatus(Enum):
    """Git repository status."""
    NOT_INITIALIZED = "not_initialized"
    CLEAN = "clean"
    HAS_CHANGES = "has_changes"
    ERROR = "error"


@dataclass
class GitInfo:
    """Git repository information."""
    status: GitStatus
    branch: Optional[str] = None
    has_changes: bool = False
    last_commit: Optional[str] = None
    remote_url: Optional[str] = None
    message: str = ""
    can_commit: bool = False


class GitManager:
    """Manages git operations via SSH."""

    def __init__(self, ssh_manager):
        self._ssh = ssh_manager

    async def check_git_status(self, session_id: str, path: str) -> GitInfo:
        """Check if directory is a git repo and get status."""
        # Check if .git exists
        result = await self._ssh.execute(
            session_id,
            f"cd {path} && test -d .git && echo 'GIT_REPO' || echo 'NOT_GIT'",
            timeout=10
        )
        
        is_git = "GIT_REPO" in result["stdout"]
        
        if not is_git:
            return GitInfo(
                status=GitStatus.NOT_INITIALIZED,
                message="⚠️ Проект не в Git. Работа продолжается, но без версионирования.",
                can_commit=False
            )

        # Get branch
        branch_result = await self._ssh.execute(
            session_id,
            f"cd {path} && git branch --show-current 2>/dev/null || echo 'HEAD'",
            timeout=10
        )
        branch = branch_result["stdout"].strip()

        # Check for changes
        status_result = await self._ssh.execute(
            session_id,
            f"cd {path} && git status --porcelain 2>/dev/null",
            timeout=10
        )
        has_changes = bool(status_result["stdout"].strip())

        # Get last commit
        last_commit_result = await self._ssh.execute(
            session_id,
            f"cd {path} && git log -1 --format='%h %s' 2>/dev/null || echo 'No commits'",
            timeout=10
        )
        last_commit = last_commit_result["stdout"].strip()

        # Get remote
        remote_result = await self._ssh.execute(
            session_id,
            f"cd {path} && git remote get-url origin 2>/dev/null || echo ''",
            timeout=10
        )
        remote_url = remote_result["stdout"].strip() or None

        status = GitStatus.HAS_CHANGES if has_changes else GitStatus.CLEAN

        return GitInfo(
            status=status,
            branch=branch,
            has_changes=has_changes,
            last_commit=last_commit,
            remote_url=remote_url,
            message=f"✅ Git активен: ветка {branch}",
            can_commit=True
        )

    async def init_repo(self, session_id: str, path: str, remote_url: Optional[str] = None) -> dict:
        """Initialize git repository."""
        commands = [
            f"cd {path} && git init",
            f"cd {path} && git config user.email 'ai@ssh-gateway.local'",
            f"cd {path} && git config user.name 'AI Gateway'",
        ]
        
        if remote_url:
            commands.append(f"cd {path} && git remote add origin {remote_url}")

        for cmd in commands:
            result = await self._ssh.execute(session_id, cmd, timeout=15)
            if result["exit_code"] != 0:
                return {
                    "success": False,
                    "error": result["stderr"] or result["stdout"]
                }

        return {
            "success": True,
            "message": "✅ Git инициализирован",
            "remote_url": remote_url
        }

    async def commit(self, session_id: str, path: str, message: str, files: Optional[list] = None) -> dict:
        """Create a git commit."""
        # Add files
        if files:
            files_str = " ".join(files)
            add_cmd = f"cd {path} && git add {files_str}"
        else:
            add_cmd = f"cd {path} && git add -A"

        result = await self._ssh.execute(session_id, add_cmd, timeout=15)
        if result["exit_code"] != 0:
            return {"success": False, "error": result["stderr"]}

        # Commit
        commit_cmd = f"cd {path} && git commit -m '{message}'"
        result = await self._ssh.execute(session_id, commit_cmd, timeout=15)
        
        if result["exit_code"] != 0:
            # Check if nothing to commit
            if "nothing to commit" in result["stdout"] or "nothing to commit" in result["stderr"]:
                return {"success": True, "message": "Нет изменений для коммита"}
            return {"success": False, "error": result["stderr"]}

        return {
            "success": True,
            "message": f"✅ Коммит создан: {message}",
            "hash": result["stdout"].strip()[:7]
        }

    async def create_backup(self, session_id: str, path: str, backup_name: str) -> dict:
        """Create a git stash as backup."""
        result = await self._ssh.execute(
            session_id,
            f"cd {path} && git stash push -m '{backup_name}'",
            timeout=15
        )
        
        if result["exit_code"] != 0 and "No local changes" not in result["stderr"]:
            return {"success": False, "error": result["stderr"]}

        return {
            "success": True,
            "message": f"💾 Бэкап создан: {backup_name}"
        }

    async def restore_backup(self, session_id: str, path: str) -> dict:
        """Restore from stash."""
        result = await self._ssh.execute(
            session_id,
            f"cd {path} && git stash pop",
            timeout=15
        )
        
        if result["exit_code"] != 0:
            return {"success": False, "error": result["stderr"]}

        return {"success": True, "message": "♻️ Бэкап восстановлен"}

    async def diff(self, session_id: str, path: str) -> str:
        """Get git diff."""
        result = await self._ssh.execute(
            session_id,
            f"cd {path} && git diff",
            timeout=15
        )
        return result["stdout"]
