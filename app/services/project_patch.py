"""Project patch application service (P19.2b).

Encapsulates the transactional multi-file patch flow for
POST /api/projects/*/apply-patch: parse, validate, dry-run preview,
backup -> atomic write -> rollback on failure. HTTP mapping stays in the
router; this module raises PatchValidationError / HashMismatchError /
RollbackFailedError / ProjectNotFoundError.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from app.models import ProjectPatchApplyResponse, ProjectPatchFileResult
from app.patch_apply import (
    PatchApplier,
    PatchValidationError,
    RollbackFailedError,
)

logger = logging.getLogger(__name__)


class ProjectNotFoundError(Exception):
    """Requested project is not registered in the workspace registry."""


def _resolve_within_root(project_root: Path, rel_path: str) -> Path:
    """Resolve rel_path against project_root and reject any escape.

    f["path"] comes straight from the unified diff's source-file header —
    fully attacker-controlled via the raw patch text, with no traversal
    filtering anywhere in patch_apply.py. A path like "../../../etc/passwd"
    (or even a plain absolute path — `Path(root) / "/etc/passwd"` silently
    discards `root` entirely, a well-known pathlib pitfall) would resolve
    outside project_root, and the caller only needs the "project:patch"
    scope, not the master key, to reach this. Resolve first, then check
    containment on the resolved path — this catches both cases regardless
    of how the escape was constructed.
    """
    full_path = (project_root / rel_path).resolve()
    resolved_root = project_root.resolve()
    if full_path != resolved_root and resolved_root not in full_path.parents:
        raise PatchValidationError(f"Path escapes project root: {rel_path!r}")
    return full_path


async def apply_project_patch(
    project: str,
    *,
    patch: str,
    strip: int = 0,
    expected_hashes: dict[str, str] | None = None,
    dry_run: bool = False,
) -> ProjectPatchApplyResponse:
    """Apply a unified diff patch to project files with hash verification and rollback."""
    applier = PatchApplier()
    rid = uuid.uuid4().hex[:12]

    # Validate patch size
    applier._validate_patch_size(len(patch.encode("utf-8")))

    # Parse patch
    files = applier._parse_patch(patch, strip=strip)

    # Validate limits
    applier._validate_file_count(len(files))
    total_hunks = sum(f["hunk_count"] for f in files)
    applier._validate_hunk_count(total_hunks)

    # Validate forbidden ops
    applier._validate_no_forbidden_ops(files)

    # Resolve project path via workspace registry
    from app.workspace.registry import get_registry

    try:
        registry = get_registry()
        project_root = registry._policy._resolve_project_root(project)
    except Exception as exc:
        raise ProjectNotFoundError(f"Project not found: {project}") from exc

    file_results: list[ProjectPatchFileResult] = []

    # Dry run: apply in memory only
    if dry_run:
        preview_parts = []
        for f in files:
            full_path = _resolve_within_root(project_root, f["path"])
            if full_path.exists():
                original = full_path.read_text(encoding="utf-8", errors="replace")
            else:
                original = ""
            new_content = applier._apply_in_memory(original, f)
            if original != new_content:
                preview_parts.append(f"--- {f['path']}\n+++ {f['path']}\n{new_content}")
            file_results.append(
                ProjectPatchFileResult(
                    path=f["path"],
                    status="dry_run",
                    hunks_applied=f["hunk_count"],
                )
            )
        return ProjectPatchApplyResponse(
            success=True,
            files_applied=len(files),
            files_failed=0,
            hunks_applied=total_hunks,
            preview="\n".join(preview_parts),
            files=file_results,
        )

    # Apply: read, verify hashes, apply in memory
    prepared: list[tuple[object, str, str, int]] = []

    for f in files:
        full_path = _resolve_within_root(project_root, f["path"])

        # Check file size
        if full_path.exists():
            file_size = full_path.stat().st_size
            if file_size > applier.MAX_FILE_SIZE:
                raise PatchValidationError(
                    f"File '{f['path']}' is {file_size} bytes, exceeds {applier.MAX_FILE_SIZE} limit"
                )
            original = full_path.read_text(encoding="utf-8", errors="replace")
        else:
            original = ""

        # Verify hash for existing files
        if full_path.exists() and expected_hashes and f["path"] in expected_hashes:
            applier._check_hash(f["path"], original, expected_hashes[f["path"]])

        new_content = applier._apply_in_memory(original, f)
        prepared.append((full_path, original, new_content, f["hunk_count"]))

    # Transactional write with rollback
    completed: list[tuple[object, object]] = []

    for full_path, _original, new_content, _hunk_count in prepared:
        backup = full_path.parent / f".{full_path.name}.mcp-patch-{rid}.bak"
        tmp = full_path.parent / f".{full_path.name}.mcp-patch-{rid}.tmp"

        try:
            # Backup
            if full_path.exists():
                shutil.copy2(str(full_path), str(backup))
            else:
                backup.write_text("", encoding="utf-8")

            # Write temp file
            tmp.write_text(new_content, encoding="utf-8")

            # fsync
            fd = os.open(str(tmp), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

            # Atomic rename
            os.rename(str(tmp), str(full_path))

            completed.append((full_path, backup))

        except Exception as exc:
            logger.error("Patch write failed for %s: %s", full_path, exc)
            # Rollback completed files
            rollback_errors = []
            for rb_path, rb_backup in completed:
                try:
                    os.rename(str(rb_backup), str(rb_path))
                except Exception as rb_exc:
                    rollback_errors.append(f"{rb_path}: {rb_exc}")
                    logger.error("Rollback failed for %s: %s", rb_path, rb_exc)

            # Cleanup temp files
            for rb_path, _ in completed:
                tmp_rb = rb_path.parent / f".{rb_path.name}.mcp-patch-{rid}.tmp"
                try:
                    tmp_rb.unlink(missing_ok=True)
                except Exception:
                    pass

            if rollback_errors:
                raise RollbackFailedError(
                    f"Write failed for {full_path} and rollback also failed: "
                    + "; ".join(rollback_errors)
                ) from exc

            file_results.append(
                ProjectPatchFileResult(
                    path=str(full_path.relative_to(project_root)),
                    status="failed",
                    error=str(exc),
                )
            )
            return ProjectPatchApplyResponse(
                success=False,
                files_applied=0,
                files_failed=1,
                hunks_applied=0,
                errors=file_results,
                files=file_results,
            )

    # Cleanup backups on success
    for rb_path, rb_backup in completed:
        try:
            rb_backup.unlink(missing_ok=True)
        except Exception:
            pass
        tmp = rb_path.parent / f".{rb_path.name}.mcp-patch-{rid}.tmp"
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    file_results = [
        ProjectPatchFileResult(
            path=str(p.relative_to(project_root)),
            status="applied",
            hunks_applied=h,
        )
        for p, _, _, h in prepared
    ]

    return ProjectPatchApplyResponse(
        success=True,
        files_applied=len(prepared),
        files_failed=0,
        hunks_applied=sum(h for _, _, _, h in prepared),
        files=file_results,
    )
