"""Multi-file edit service (P19.2b).

Shared per-file edit loop with result aggregation used by routers/files.py
batch_edit and bulk_edit routes. One implementation so the two paths
cannot diverge.
"""

from __future__ import annotations

from app.models import BatchEditResponse, BatchEditResult
from app.security import validate_path


async def edit_many(
    file_editor,
    session_id: str,
    files,
) -> BatchEditResponse:
    """Edit multiple files, collecting per-file results.

    Every path is checked via validate_path first — this is the only
    guardrail against a caller reaching FORBIDDEN_PATHS (/etc/shadow,
    /root/.ssh, ...) or a traversal segment on the remote target, since
    FileEditor.edit_file() does no path validation of its own and trusts
    the caller. Invalid paths are reported as per-file errors rather than
    failing the whole batch.
    """
    results = []
    files_changed = 0
    total_operations = 0

    for file_op in files:
        try:
            validate_path(file_op.path)
        except ValueError as exc:
            results.append(
                BatchEditResult(
                    path=file_op.path,
                    success=False,
                    operations_applied=0,
                    changed=False,
                    error=str(exc),
                )
            )
            continue

        try:
            result = await file_editor.edit_file(
                session_id,
                file_op.path,
                [op.model_dump() for op in file_op.operations],
            )
            results.append(
                BatchEditResult(
                    path=file_op.path,
                    success=True,
                    operations_applied=result.get("operations_applied", 0),
                    changed=result.get("changed", False),
                )
            )
            total_operations += result.get("operations_applied", 0)
            if result.get("changed", False):
                files_changed += 1
        except Exception as exc:
            results.append(
                BatchEditResult(
                    path=file_op.path,
                    success=False,
                    operations_applied=0,
                    changed=False,
                    error=str(exc),
                )
            )

    return BatchEditResponse(
        results=results,
        total_files=len(files),
        files_changed=files_changed,
        total_operations=total_operations,
    )
