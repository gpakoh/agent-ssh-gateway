"""Context management for AI development sessions."""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from app.git_manager import GitManager, GitInfo, GitStatus
from app.validation_pipeline import ValidationPipeline, ValidationReport
from app.smart_context import SmartContextState

logger = logging.getLogger(__name__)


class ContextStatus(Enum):
    """Context status."""
    ACTIVE = "active"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass
class Context:
    """Development context."""
    context_id: str
    session_id: str
    name: str
    path: str
    branch: Optional[str] = None
    git_info: Optional[GitInfo] = None
    files_opened: list[str] = field(default_factory=list)
    edit_history: list[dict] = field(default_factory=list)
    auto_commit: bool = False
    auto_validate: bool = False
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    status: ContextStatus = ContextStatus.ACTIVE
    error_message: Optional[str] = None
    smart_state: SmartContextState = field(default_factory=SmartContextState)

    def touch(self) -> None:
        """Update last used timestamp."""
        self.last_used = time.time()

    @property
    def idle_time(self) -> float:
        """Seconds since last used."""
        return time.time() - self.last_used


class ContextManager:
    """Manages development contexts with git awareness."""

    def __init__(self, ssh_manager, context_timeout: int = 1800) -> None:
        self._contexts: dict[str, Context] = {}
        self._session_contexts: dict[str, str] = {}  # session_id -> context_id
        self._lock = asyncio.Lock()
        self._ssh = ssh_manager
        self._git = GitManager(ssh_manager)
        self._validation = ValidationPipeline(ssh_manager)
        self._context_timeout = context_timeout
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start_cleanup_task(self) -> None:
        """Start cleanup coroutine."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Context cleanup task started")

    async def stop_cleanup_task(self) -> None:
        """Stop cleanup coroutine."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("Context cleanup task stopped")

    async def _cleanup_loop(self) -> None:
        """Periodically remove expired contexts."""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self.cleanup_expired_contexts()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Context cleanup error: %s", exc)

    async def cleanup_expired_contexts(self) -> int:
        """Remove expired contexts."""
        expired: list[str] = []
        now = time.time()

        async with self._lock:
            for ctx_id, ctx in list(self._contexts.items()):
                if now - ctx.last_used > self._context_timeout:
                    expired.append(ctx_id)

        for ctx_id in expired:
            await self.delete_context(ctx_id)

        return len(expired)

    async def create_context(
        self,
        session_id: str,
        name: Optional[str] = None,
        path: str = ".",
        branch: Optional[str] = None,
        auto_commit: bool = False,
        auto_validate: bool = False,
    ) -> Context:
        """Create a new development context."""
        context_id = str(uuid.uuid4())
        
        # Auto-generate name if not provided
        if not name:
            import os
            base_name = os.path.basename(os.path.normpath(path)) or "context"
            name = f"{base_name}_{context_id[:8]}"

        # Check git status
        git_info = await self._git.check_git_status(session_id, path)

        # If branch specified and git initialized, checkout branch
        if branch and git_info.status != GitStatus.NOT_INITIALIZED:
            await self._checkout_branch(session_id, path, branch)
            git_info = await self._git.check_git_status(session_id, path)

        context = Context(
            context_id=context_id,
            session_id=session_id,
            name=name,
            path=path,
            branch=branch or git_info.branch,
            git_info=git_info,
            auto_commit=auto_commit,
            auto_validate=auto_validate,
        )

        async with self._lock:
            self._contexts[context_id] = context
            self._session_contexts[session_id] = context_id

        logger.info("Context %s created: %s@%s", context_id, name, path)
        return context

    async def get_context(self, context_id: str) -> Optional[Context]:
        """Get context by ID."""
        async with self._lock:
            ctx = self._contexts.get(context_id)
            if ctx:
                ctx.touch()
            return ctx

    async def get_context_by_session(self, session_id: str) -> Optional[Context]:
        """Get context by session ID."""
        async with self._lock:
            ctx_id = self._session_contexts.get(session_id)
            if ctx_id:
                ctx = self._contexts.get(ctx_id)
                if ctx:
                    ctx.touch()
                return ctx
            return None

    async def delete_context(self, context_id: str) -> bool:
        """Delete a context."""
        async with self._lock:
            ctx = self._contexts.pop(context_id, None)
            if ctx:
                self._session_contexts.pop(ctx.session_id, None)
                logger.info("Context %s deleted", context_id)
                return True
        return False

    async def update_git_status(self, context_id: str) -> GitInfo:
        """Refresh git status for context."""
        ctx = await self.get_context(context_id)
        if not ctx:
            raise ValueError(f"Context {context_id} not found")

        git_info = await self._git.check_git_status(ctx.session_id, ctx.path)
        ctx.git_info = git_info
        return git_info

    async def init_git(self, context_id: str, remote_url: Optional[str] = None) -> dict:
        """Initialize git for context."""
        ctx = await self.get_context(context_id)
        if not ctx:
            return {"success": False, "error": "Context not found"}

        result = await self._git.init_repo(ctx.session_id, ctx.path, remote_url)
        if result["success"]:
            ctx.git_info = await self._git.check_git_status(ctx.session_id, ctx.path)
        return result

    async def commit_changes(
        self,
        context_id: str,
        message: str,
        files: Optional[list] = None
    ) -> dict:
        """Commit changes in context."""
        ctx = await self.get_context(context_id)
        if not ctx:
            return {"success": False, "error": "Context not found"}

        if not ctx.git_info or ctx.git_info.status == GitStatus.NOT_INITIALIZED:
            return {
                "success": False,
                "error": "Git not initialized. Use /api/git/init first."
            }

        # Pre-commit hooks: run validation if auto_validate is enabled
        if ctx.auto_validate:
            logger.info("Running pre-commit validation for context %s", context_id)
            validation_report = await self._validation.validate(ctx.session_id, ctx.path)
            
            if not validation_report.can_commit:
                logger.warning("Pre-commit validation failed for context %s", context_id)
                return {
                    "success": False,
                    "error": f"Pre-commit validation failed: {validation_report.summary}",
                    "validation_report": {
                        "overall_status": validation_report.overall_status.value,
                        "summary": validation_report.summary,
                        "can_commit": validation_report.can_commit,
                    }
                }
            
            logger.info("Pre-commit validation passed for context %s", context_id)

        # Prepend context name to commit message
        full_message = f"[{ctx.name}] {message}"
        result = await self._git.commit(ctx.session_id, ctx.path, full_message, files)
        
        if result["success"]:
            ctx.git_info = await self._git.check_git_status(ctx.session_id, ctx.path)
            ctx.edit_history.append({
                "type": "commit",
                "message": full_message,
                "timestamp": time.time()
            })

        return result

    async def create_backup(self, context_id: str, backup_name: str) -> dict:
        """Create backup stash."""
        ctx = await self.get_context(context_id)
        if not ctx:
            return {"success": False, "error": "Context not found"}

        return await self._git.create_backup(ctx.session_id, ctx.path, backup_name)

    async def restore_backup(self, context_id: str) -> dict:
        """Restore from stash."""
        ctx = await self.get_context(context_id)
        if not ctx:
            return {"success": False, "error": "Context not found"}

        return await self._git.restore_backup(ctx.session_id, ctx.path)

    async def add_file_to_context(self, context_id: str, file_path: str) -> None:
        """Add file to context's open files and smart state."""
        ctx = await self.get_context(context_id)
        if ctx:
            if file_path not in ctx.files_opened:
                ctx.files_opened.append(file_path)
            # Open in smart context
            ctx.smart_state.open_file(file_path)

    async def record_edit(self, context_id: str, file_path: str, operation: str) -> None:
        """Record an edit in context history."""
        ctx = await self.get_context(context_id)
        if ctx:
            ctx.edit_history.append({
                "type": "edit",
                "file": file_path,
                "operation": operation,
                "timestamp": time.time()
            })
            ctx.smart_state.last_edited_file = file_path

    async def close_file(self, context_id: str, file_path: str) -> bool:
        """Close file in smart context."""
        ctx = await self.get_context(context_id)
        if ctx:
            return ctx.smart_state.close_file(file_path)
        return False

    async def update_cursor(self, context_id: str, file_path: str, line: int, column: int = 1) -> None:
        """Update cursor position."""
        ctx = await self.get_context(context_id)
        if ctx:
            ctx.smart_state.update_cursor(file_path, line, column)

    async def add_command(self, context_id: str, command: str, directory: str = "") -> dict:
        """Add command to history."""
        ctx = await self.get_context(context_id)
        if ctx:
            cmd = ctx.smart_state.add_command(command, directory)
            return cmd.to_dict()
        return {}

    async def add_search(self, context_id: str, query: str, path: str = "", replace_with: str = "") -> dict:
        """Add search query to history."""
        ctx = await self.get_context(context_id)
        if ctx:
            search = ctx.smart_state.add_search(query, path, replace_with)
            return search.to_dict()
        return {}

    async def add_bookmark(self, context_id: str, file_path: str, line: int, note: str = "") -> dict:
        """Add bookmark."""
        ctx = await self.get_context(context_id)
        if ctx:
            bookmark = ctx.smart_state.add_bookmark(file_path, line, note)
            return bookmark.to_dict()
        return {}

    async def remove_bookmark(self, context_id: str, file_path: str, line: int) -> bool:
        """Remove bookmark."""
        ctx = await self.get_context(context_id)
        if ctx:
            return ctx.smart_state.remove_bookmark(file_path, line)
        return False

    async def get_smart_state(self, context_id: str) -> dict:
        """Get smart context state."""
        ctx = await self.get_context(context_id)
        if ctx:
            return ctx.smart_state.to_dict()
        return {}

    async def validate_context(
        self,
        context_id: str,
        run_mypy: bool = True,
        run_tests: bool = True,
    ) -> ValidationReport:
        """Run validation pipeline for context."""
        ctx = await self.get_context(context_id)
        if not ctx:
            raise ValueError(f"Context {context_id} not found")

        report = await self._validation.validate(
            session_id=ctx.session_id,
            path=ctx.path,
            run_mypy=run_mypy,
            run_tests=run_tests,
        )

        # Record validation in history
        ctx.edit_history.append({
            "type": "validation",
            "status": report.overall_status.value,
            "duration": report.total_duration,
            "timestamp": time.time()
        })

        return report

    async def _checkout_branch(self, session_id: str, path: str, branch: str) -> None:
        """Checkout or create branch."""
        # Check if branch exists
        result = await self._ssh.execute(
            session_id,
            f"cd {path} && git branch --list {branch}",
            timeout=10
        )
        
        if result["stdout"].strip():
            # Branch exists, checkout
            await self._ssh.execute(
                session_id,
                f"cd {path} && git checkout {branch}",
                timeout=10
            )
        else:
            # Create new branch
            await self._ssh.execute(
                session_id,
                f"cd {path} && git checkout -b {branch}",
                timeout=10
            )
