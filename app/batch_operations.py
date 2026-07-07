"""Batch operations for multi-file editing and refactoring."""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class BatchOperationType(Enum):
    """Types of batch operations."""

    READ = "read"
    EDIT = "edit"
    CREATE = "create"
    DELETE = "delete"
    RENAME = "rename"
    COPY = "copy"
    EXECUTE = "execute"


@dataclass
class BatchOperationResult:
    """Result of a single batch operation."""

    operation: str
    path: str
    success: bool
    output: str = ""
    error: str | None = None
    duration: float = 0.0
    lines_changed: int = 0


@dataclass
class BatchTransactionResult:
    """Result of a batch transaction."""

    transaction_id: str
    overall_success: bool
    operations: list[BatchOperationResult]
    total_duration: float
    summary: str
    git_commit: str | None = None
    validation_result: dict | None = None


class BatchOperationsManager:
    """Manages batch file operations."""

    def __init__(self, ssh_manager, file_editor, context_manager):
        self._ssh = ssh_manager
        self._file_editor = file_editor
        self._context = context_manager

    async def execute_batch(
        self,
        session_id: str,
        context_id: str,
        operations: list[dict],
        auto_commit: bool = False,
        commit_message: str = "",
        run_validation: bool = False,
        transaction_id: str = "",
    ) -> BatchTransactionResult:
        """Execute a batch of operations atomically."""
        start_time = asyncio.get_event_loop().time()
        results = []
        all_changed_files = []
        overall_success = True

        # Get context for path
        ctx = await self._context.get_context(context_id)
        base_path = ctx.path if ctx else "."

        for _, op in enumerate(operations):
            op_type = op.get("type", "")
            path = op.get("path", "")
            full_path = f"{base_path}/{path}" if not path.startswith("/") else path

            op_start = asyncio.get_event_loop().time()
            result = None

            try:
                if op_type == "read":
                    result = await self._execute_read(session_id, full_path)

                elif op_type == "edit":
                    result = await self._execute_edit(
                        session_id, full_path, op.get("operations", [])
                    )
                    if result.success:
                        all_changed_files.append(full_path)
                        await self._context.record_edit(context_id, full_path, "batch_edit")

                elif op_type == "create":
                    result = await self._execute_create(
                        session_id, full_path, op.get("content", "")
                    )
                    if result.success:
                        all_changed_files.append(full_path)

                elif op_type == "delete":
                    result = await self._execute_delete(session_id, full_path)

                elif op_type == "rename":
                    new_path = op.get("new_path", "")
                    full_new_path = (
                        f"{base_path}/{new_path}" if not new_path.startswith("/") else new_path
                    )
                    result = await self._execute_rename(session_id, full_path, full_new_path)

                elif op_type == "copy":
                    dest_path = op.get("dest_path", "")
                    full_dest_path = (
                        f"{base_path}/{dest_path}" if not dest_path.startswith("/") else dest_path
                    )
                    result = await self._execute_copy(session_id, full_path, full_dest_path)

                elif op_type == "execute":
                    result = await self._execute_command(
                        session_id, op.get("command", ""), base_path
                    )

                else:
                    result = BatchOperationResult(
                        operation=op_type,
                        path=path,
                        success=False,
                        error=f"Unknown operation type: {op_type}",
                    )

            except Exception as exc:
                result = BatchOperationResult(
                    operation=op_type, path=path, success=False, error=str(exc)
                )
                logger.error("Batch operation %s failed: %s", op_type, exc)

            if result:
                result.duration = round(asyncio.get_event_loop().time() - op_start, 2)
                results.append(result)
                if not result.success:
                    overall_success = False
                    # Stop on first error unless continue_on_error is set
                    if not op.get("continue_on_error", False):
                        break

        total_duration = round(asyncio.get_event_loop().time() - start_time, 2)

        # Git commit if enabled and operations succeeded
        git_commit = None
        if auto_commit and overall_success and all_changed_files:
            commit_result = await self._context.commit_changes(
                context_id,
                commit_message or f"Batch: {len(all_changed_files)} files changed",
                all_changed_files,
            )
            if commit_result.get("success"):
                git_commit = commit_result.get("hash")

        # Validation if requested
        validation_result = None
        if run_validation and overall_success:
            try:
                report = await self._context.validate_context(context_id)
                validation_result = {
                    "overall_status": report.overall_status.value,
                    "summary": report.summary,
                    "can_commit": report.can_commit,
                }
            except Exception as exc:
                validation_result = {
                    "overall_status": "error",
                    "summary": f"Validation error: {exc}",
                    "can_commit": False,
                }

        # Build summary
        success_count = sum(1 for r in results if r.success)
        failed_count = len(results) - success_count

        if overall_success:
            summary = f"✅ Все {len(results)} операций выполнены успешно"
        else:
            summary = f"⚠️ {success_count}/{len(results)} операций выполнено, {failed_count} ошибок"

        return BatchTransactionResult(
            transaction_id=transaction_id,
            overall_success=overall_success,
            operations=results,
            total_duration=total_duration,
            summary=summary,
            git_commit=git_commit,
            validation_result=validation_result,
        )

    async def _execute_read(self, session_id: str, path: str) -> BatchOperationResult:
        """Read file content."""
        content = await self._file_editor.read_file(session_id, path)
        return BatchOperationResult(
            operation="read",
            path=path,
            success=True,
            output=content[:1000],  # Limit output
        )

    async def _execute_edit(
        self, session_id: str, path: str, operations: list
    ) -> BatchOperationResult:
        """Edit file with operations."""
        result = await self._file_editor.edit_file(session_id, path, operations)
        return BatchOperationResult(
            operation="edit",
            path=path,
            success=result.get("success", False),
            output=f"Applied {result.get('operations_applied', 0)} operations",
            lines_changed=result.get("operations_applied", 0),
        )

    async def _execute_create(
        self, session_id: str, path: str, content: str
    ) -> BatchOperationResult:
        """Create new file."""
        # Use echo to create file
        cmd = f"cat > '{path}' << 'EOF_BATCH'\n{content}\nEOF_BATCH"

        result = await self._ssh.execute(session_id, cmd, timeout=30)

        return BatchOperationResult(
            operation="create",
            path=path,
            success=result["exit_code"] == 0,
            output=f"Created file with {len(content)} chars",
            error=result["stderr"] if result["exit_code"] != 0 else None,
        )

    async def _execute_delete(self, session_id: str, path: str) -> BatchOperationResult:
        """Delete file."""
        result = await self._ssh.execute(session_id, f"rm -f '{path}'", timeout=10)

        return BatchOperationResult(
            operation="delete",
            path=path,
            success=result["exit_code"] == 0,
            error=result["stderr"] if result["exit_code"] != 0 else None,
        )

    async def _execute_rename(
        self, session_id: str, old_path: str, new_path: str
    ) -> BatchOperationResult:
        """Rename file."""
        result = await self._ssh.execute(session_id, f"mv '{old_path}' '{new_path}'", timeout=10)

        return BatchOperationResult(
            operation="rename",
            path=f"{old_path} -> {new_path}",
            success=result["exit_code"] == 0,
            error=result["stderr"] if result["exit_code"] != 0 else None,
        )

    async def _execute_copy(
        self, session_id: str, src_path: str, dest_path: str
    ) -> BatchOperationResult:
        """Copy file."""
        result = await self._ssh.execute(session_id, f"cp '{src_path}' '{dest_path}'", timeout=10)

        return BatchOperationResult(
            operation="copy",
            path=f"{src_path} -> {dest_path}",
            success=result["exit_code"] == 0,
            error=result["stderr"] if result["exit_code"] != 0 else None,
        )

    async def _execute_command(
        self, session_id: str, command: str, cwd: str
    ) -> BatchOperationResult:
        """Execute shell command."""
        result = await self._ssh.execute(session_id, f"cd {cwd} && {command}", timeout=60)

        return BatchOperationResult(
            operation="execute",
            path=cwd,
            success=result["exit_code"] == 0,
            output=result["stdout"][:500],
            error=result["stderr"] if result["stderr"] else None,
        )
