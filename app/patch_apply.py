"""Unified diff patch apply with validation, hash checks, and transactional writes."""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

import unidiff

logger = logging.getLogger(__name__)


class PatchValidationError(ValueError):
    """Raised when patch validation fails."""


class HashMismatchError(ValueError):
    """Raised when file hash doesn't match expected."""


class RollbackFailedError(RuntimeError):
    """Raised when rollback after failed write also fails."""


@dataclass
class FileApplyResult:
    """Result of applying patch to a single file."""

    path: str
    status: str  # "applied", "skipped", "dry_run", "failed"
    hunks_applied: int = 0
    error: str | None = None


@dataclass
class PatchResult:
    """Result of applying a patch."""

    success: bool
    files_applied: int
    files_failed: int
    hunks_applied: int
    preview: str | None = None
    errors: list[FileApplyResult] = field(default_factory=list)
    files: list[FileApplyResult] = field(default_factory=list)


class PatchApplier:
    """Apply unified diff patches with validation and transactional writes."""

    MAX_FILES = 20
    MAX_HUNKS = 100
    MAX_PATCH_SIZE = 1_048_576  # 1 MiB
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MiB

    def _parse_patch(self, patch_text: str, strip: int = 1) -> list[dict]:
        """Parse unified diff into per-file dicts with path and hunks."""
        patch_set = unidiff.PatchSet(patch_text)
        result = []
        for patched_file in patch_set:
            source_file = patched_file.source_file
            # Apply strip to path
            parts = source_file.split("/")
            if strip > 0 and len(parts) > strip:
                path = "/".join(parts[strip:])
            elif source_file.startswith("a/"):
                path = source_file[2:]
            else:
                path = source_file

            hunks = []
            for hunk in patched_file:
                hunks.append({
                    "source_start": hunk.source_start,
                    "source_length": hunk.source_length,
                    "target_start": hunk.target_start,
                    "target_length": hunk.target_length,
                    "lines": [str(line) for line in hunk],
                })

            result.append({
                "path": path,
                "hunks": hunks,
                "hunk_count": len(hunks),
                "is_rename": patched_file.is_rename,
                "is_copy": getattr(patched_file, "is_copy", False),
                "is_device_file": getattr(patched_file, "is_device_file", False),
                "added": patched_file.added,
                "removed": patched_file.removed,
            })
        return result

    def _validate_file_count(self, count: int) -> None:
        if count > self.MAX_FILES:
            raise PatchValidationError(
                f"Patch contains {count} files, exceeds limit of {self.MAX_FILES} files"
            )

    def _validate_hunk_count(self, count: int) -> None:
        if count > self.MAX_HUNKS:
            raise PatchValidationError(
                f"Patch contains {count} hunks, exceeds limit of {self.MAX_HUNKS} hunks"
            )

    def _validate_patch_size(self, size: int) -> None:
        if size > self.MAX_PATCH_SIZE:
            raise PatchValidationError(
                f"Patch size {size} bytes exceeds 1 MiB limit"
            )

    def _validate_no_forbidden_ops(self, files: list[dict]) -> None:
        for f in files:
            if f.get("is_rename"):
                raise PatchValidationError(
                    "v1: rename/copy operations are not supported"
                )
            if f.get("is_copy"):
                raise PatchValidationError(
                    "v1: rename/copy operations are not supported"
                )
            if f.get("is_device_file"):
                raise PatchValidationError(
                    "v1: /dev/null paths are not supported"
                )
            if f["path"] == "/dev/null" or f["path"].endswith("/dev/null"):
                raise PatchValidationError(
                    "v1: /dev/null paths are not supported"
                )

    def _compute_sha256(self, content: str) -> str:
        """Compute sha256 hash of content with 'sha256:' prefix."""
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _check_hash(self, path: str, content: str, expected: str) -> None:
        """Verify file content matches expected hash."""
        actual = self._compute_sha256(content)
        if actual != expected:
            raise HashMismatchError(
                f"Hash mismatch for '{path}': expected {expected}, got {actual}"
            )

    def _apply_in_memory(self, original: str, file_info: dict) -> str:
        """Apply hunks to original content in memory, return new content."""
        lines = original.splitlines(keepends=True)
        # Rebuild PatchSet for this specific file
        patch_text = self._rebuild_patch_for_file(file_info)
        patch_set = unidiff.PatchSet(patch_text)

        if not patch_set:
            return original

        patched_file = patch_set[0]
        result_lines = []
        source_idx = 0

        for hunk in patched_file:
            # Add context before hunk
            while source_idx < hunk.source_start - 1 and source_idx < len(lines):
                result_lines.append(lines[source_idx])
                source_idx += 1

            # Process hunk lines
            for line in hunk:
                if line.is_added:
                    result_lines.append(line.value)
                elif line.is_removed:
                    source_idx += 1
                elif line.is_context:
                    result_lines.append(line.value)
                    source_idx += 1

        # Add remaining lines
        while source_idx < len(lines):
            result_lines.append(lines[source_idx])
            source_idx += 1

        return "".join(result_lines)

    def _rebuild_patch_for_file(self, file_info: dict) -> str:
        """Rebuild a minimal unified diff string for a single file."""
        lines = [f"--- a/{file_info['path']}", f"+++ b/{file_info['path']}"]
        for hunk in file_info["hunks"]:
            lines.append(
                f"@@ -{hunk['source_start']},{hunk['source_length']} "
                f"+{hunk['target_start']},{hunk['target_length']} @@"
            )
            lines.extend(hunk["lines"])
        return "\n".join(lines) + "\n"
