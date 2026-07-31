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
    *,
    validate: bool = False,
) -> BatchEditResponse:
    """Edit multiple files, collecting per-file results.

    With validate=True each path is checked via validate_path first and
    invalid paths are reported as per-file errors (batch_edit behavior);
    bulk_edit passes validate=False.
    """
    results = []
    files_changed = 0
    total_operations = 0

    for file_op in files:
        if validate:
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
