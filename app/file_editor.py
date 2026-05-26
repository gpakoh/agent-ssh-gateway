"""File editing utilities for remote SSH servers."""

import logging
import re
from dataclasses import dataclass
from typing import Literal, Optional

from app.security import validate_path
from app.ssh_manager import SSHSessionManager, SessionNotFoundError, ExecutionError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Edit Operations
# ---------------------------------------------------------------------------

@dataclass
class EditOperation:
    """Single file edit operation."""

    type: Literal["replace", "insert_after", "insert_before", "delete", "append"]
    old: Optional[str] = None
    new: Optional[str] = None
    after: Optional[str] = None
    before: Optional[str] = None
    text: Optional[str] = None
    count: int = 0  # 0 = all occurrences


# ---------------------------------------------------------------------------
# File Editor
# ---------------------------------------------------------------------------

class FileEditor:
    """Edit files on remote SSH servers."""

    def __init__(self, ssh_manager: SSHSessionManager) -> None:
        self._ssh = ssh_manager

    async def read_file(self, session_id: str, path: str) -> str:
        """Read a remote file."""
        validated = validate_path(path)
        # Check If Path Is A Directory
        check = await self._ssh.execute(
            session_id, f"test -d '{self._escape(validated)}' && echo 'DIR' || echo 'FILE'", timeout=10
        )
        if check["stdout"].strip() == "DIR":
            raise ExecutionError(f"Cannot read {validated}: it is a directory, not a file")

        result = await self._ssh.execute(session_id, f"cat '{self._escape(validated)}'", timeout=30)
        if result["exit_code"] != 0:
            raise ExecutionError(f"Cannot read {validated}: {result['stderr']}")
        return result["stdout"]

    async def write_file(self, session_id: str, path: str, content: str) -> None:
        """Write content to a remote file using base64 encoding via heredoc.
        
        Automatically creates parent directories if they don't exist.
        """
        import base64
        import os

        validated = validate_path(path)
        # Create Parent Directories
        parent_dir = os.path.dirname(validated)
        if parent_dir:
            mkdir_result = await self._ssh.execute(
                session_id, f"mkdir -p '{self._escape(parent_dir)}'", timeout=10
            )
            if mkdir_result["exit_code"] != 0:
                raise ExecutionError(f"Cannot create directory {parent_dir}: {mkdir_result['stderr']}")

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        # Use Heredoc To Avoid Command Line Length Limits With Echo
        cmd = f"base64 -d << 'EOF_BASE64' > '{self._escape(validated)}'\n{encoded}\nEOF_BASE64"
        result = await self._ssh.execute(session_id, cmd, timeout=30)
        if result["exit_code"] != 0:
            raise ExecutionError(f"Cannot write {validated}: {result['stderr']}")

    async def edit_file(
        self,
        session_id: str,
        path: str,
        operations: list[dict],
    ) -> dict:
        """Apply a series of edit operations to a remote file.

        Returns:
            {"success": True, "path": str, "operations_applied": int}
        """
        validated = validate_path(path)
        # Check If First Operation Is Create (creates New File)
        is_create = operations and operations[0].get("type") == "create"
        
        if is_create:
            # Start With Empty Content For Create Operation
            content = ""
            original = ""
        else:
            # Read Current Content
            content = await self.read_file(session_id, validated)
            original = content

        applied = 0
        errors = []

        for op in operations:
            try:
                content = self._apply_operation(content, op)
                applied += 1
            except Exception as exc:
                errors.append(f"Operation {op.get('type', '?')}: {exc}")

        if errors:
            raise ExecutionError("; ".join(errors))

        # Write Back Only If Changed Or Create
        if content != original or is_create:
            await self.write_file(session_id, validated, content)

        return {
            "success": True,
            "path": path,
            "operations_applied": applied,
            "changed": content != original or is_create,
        }

    def _apply_operation(self, content: str, op: dict) -> str:
        """Apply a single operation to content."""
        op_type = op.get("type")

        if op_type == "replace":
            return self._op_replace(content, op)
        elif op_type == "insert_after":
            return self._op_insert_after(content, op)
        elif op_type == "insert_before":
            return self._op_insert_before(content, op)
        elif op_type == "delete":
            return self._op_delete(content, op)
        elif op_type == "append":
            return self._op_append(content, op)
        elif op_type == "create":
            return self._op_create(content, op)
        else:
            raise ValueError(f"Unknown operation type: {op_type}")

    def _op_replace(self, content: str, op: dict) -> str:
        old = op.get("old")
        new = op.get("new", "")
        count = op.get("count", 0)

        if old is None:
            raise ValueError("replace requires 'old'")

        if count == 0:
            return content.replace(old, new)
        else:
            return content.replace(old, new, count)

    def _op_insert_after(self, content: str, op: dict) -> str:
        after = op.get("after")
        text = op.get("text", "")

        if after is None:
            raise ValueError("insert_after requires 'after'")

        if after not in content:
            raise ValueError(f"Text not found: {after[:50]}...")

        return content.replace(after, after + text, 1)

    def _op_insert_before(self, content: str, op: dict) -> str:
        before = op.get("before")
        text = op.get("text", "")

        if before is None:
            raise ValueError("insert_before requires 'before'")

        if before not in content:
            raise ValueError(f"Text not found: {before[:50]}...")

        return content.replace(before, text + before, 1)

    def _op_delete(self, content: str, op: dict) -> str:
        old = op.get("old")
        count = op.get("count", 0)

        if old is None:
            raise ValueError("delete requires 'old'")

        if count == 0:
            return content.replace(old, "")
        else:
            return content.replace(old, "", count)

    def _op_append(self, content: str, op: dict) -> str:
        text = op.get("text", "")
        if content and not content.endswith("\n"):
            content += "\n"
        return content + text + "\n"

    def _op_create(self, content: str, op: dict) -> str:
        text = op.get("text", "")
        return text

    def _escape(self, path: str) -> str:
        """Escape single quotes in path."""
        return path.replace("'", "'\"'\"'")

    # ------------------------------------------------------------------
    # Diff / Patch Utilities
    # ------------------------------------------------------------------

    async def diff_files(
        self,
        session_id: str,
        path1: str,
        path2: str,
    ) -> str:
        """Run diff between two files."""
        v1 = validate_path(path1)
        v2 = validate_path(path2)
        cmd = f"diff -u '{self._escape(v1)}' '{self._escape(v2)}' 2>&1 || true"
        result = await self._ssh.execute(session_id, cmd, timeout=30)
        return result["stdout"]

    async def apply_patch(
        self,
        session_id: str,
        patch_content: str,
        strip: int = 0,
    ) -> dict:
        """Apply a unified diff patch."""
        import base64

        encoded = base64.b64encode(patch_content.encode("utf-8")).decode("ascii")
        cmd = (
            f"echo '{encoded}' | base64 -d | patch -p{strip} 2>&1"
        )
        result = await self._ssh.execute(session_id, cmd, timeout=30)

        if result["exit_code"] != 0 and "succeeded" not in result["stdout"]:
            raise ExecutionError(f"Patch failed: {result['stderr'] or result['stdout']}")

        return {
            "success": True,
            "output": result["stdout"],
        }
