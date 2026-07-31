"""Context-aware file editing service (P19.2b).

Encapsulates the full edit-with-context orchestration for
PATCH /api/context/file/edit: auto-backup, path resolution, edit,
diff generation, auto-commit, validation and warnings. HTTP mapping
stays in the router.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app.diff_generator import DiffGenerator
from app.git_manager import GitStatus
from app.models import DiffLine, DiffResponse, ValidationReportResponse, ValidationStepResult

logger = logging.getLogger(__name__)

_WARNING_NOT_IN_GIT = (
    "\u26a0\ufe0f \u041f\u0440\u043e\u0435\u043a\u0442 \u043d\u0435 \u0432 Git. "
    "\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 POST /api/git/init "
    "\u0434\u043b\u044f \u0438\u043d\u0438\u0446\u0438\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u0438."
)
_WARNING_VALIDATION_FAILED = (
    "\u26a0\ufe0f \u0412\u0430\u043b\u0438\u0434\u0430\u0446\u0438\u044f \u043d\u0435 \u043f\u0440\u043e\u0439\u0434\u0435\u043d\u0430, "
    "\u043a\u043e\u043c\u043c\u0438\u0442 \u043e\u0442\u043c\u0435\u043d\u0451\u043d"
)


class ContextEditError(Exception):
    """The underlying file edit failed."""


@dataclass
class ContextEditResult:
    """Outcome of edit_file_with_context."""

    success: bool
    path: str
    operations_applied: int
    changed: bool
    diff: DiffResponse | None = None
    git_commit: str | None = None
    validation_result: ValidationReportResponse | None = None
    warning: str | None = None


def _git_initialized(ctx) -> bool:
    return bool(ctx.git_info and ctx.git_info.status.value != "not_initialized")


async def edit_file_with_context(
    ctx,
    context_manager,
    file_editor,
    manager,
    *,
    path: str,
    operations: list[dict],
    commit_message: str | None = None,
    run_validation: bool = False,
) -> ContextEditResult:
    """Edit a file with context awareness (auto-commit, validation, diff).

    `ctx` is the loaded context object; `path` is the request path
    (absolute or relative to ctx.path).
    """
    # Create Automatic Backup Before Editing (if Git Is Initialized)
    if _git_initialized(ctx):
        try:
            await context_manager.create_backup(
                ctx.context_id, f"before_edit_{path.replace('/', '_')}"
            )
        except Exception as exc:
            logger.warning("Auto-backup failed: %s", exc)

    # Perform Edit (resolve Relative Path Against Context Path)
    file_path = path if path.startswith("/") else os.path.join(ctx.path, path)

    try:
        result = await file_editor.edit_file(
            ctx.session_id,
            file_path,
            operations,
        )
        logger.info("Edit result: %s", result)
    except Exception as exc:
        logger.error("Edit failed: %s", exc)
        raise ContextEditError(str(exc)) from exc

    await context_manager.record_edit(ctx.context_id, path, "edit")
    await context_manager.add_file_to_context(ctx.context_id, path)

    out = ContextEditResult(
        success=result.get("success", True),
        path=path,
        operations_applied=result.get("operations_applied", 0),
        changed=result.get("changed", False),
    )

    # Generate Diff If File Was Changed And Git Is Initialized
    if out.changed and _git_initialized(ctx):
        try:
            # Quick Check If File Is Tracked In Git
            check_result = await manager.execute(
                ctx.session_id,
                f"cd {ctx.path} && git ls-files --error-unmatch '{path}' 2>/dev/null || echo 'NOT_TRACKED'",
                timeout=2,
            )

            if check_result["stdout"].strip() != "NOT_TRACKED":
                # Read Old Content From Git (fast, File Is Tracked)
                git_result = await manager.execute(
                    ctx.session_id,
                    f"cd {ctx.path} && git show HEAD:'{path}' 2>/dev/null || echo ''",
                    timeout=2,
                )
                old_content = git_result["stdout"]

                # Read New Content
                new_content = await file_editor.read_file(ctx.session_id, path)

                # Generate Diff
                unified_diff = DiffGenerator.generate_unified_diff(old_content, new_content, path, path)
                inline_diff = DiffGenerator.generate_inline_diff(old_content, new_content)
                changes = DiffGenerator.count_changes(unified_diff)

                out.diff = DiffResponse(
                    unified_diff=unified_diff,
                    inline_diff=[DiffLine(**line) for line in inline_diff],
                    changes=changes,
                    old_path=path,
                    new_path=path,
                )
        except Exception as exc:
            logger.warning("Diff generation failed: %s", exc)

    # Auto-commit If Enabled
    if ctx.auto_commit and out.changed:
        commit_msg = commit_message or f"Update {path}"
        commit_result = await context_manager.commit_changes(ctx.context_id, commit_msg, [path])
        if commit_result["success"]:
            out.git_commit = commit_result.get("hash")

    # Validation If Requested Or Auto_validate Enabled
    if run_validation or ctx.auto_validate:
        try:
            report = await context_manager.validate_context(ctx.context_id)
            out.validation_result = ValidationReportResponse(
                overall_status=report.overall_status.value,
                summary=report.summary,
                total_duration=report.total_duration,
                can_commit=report.can_commit,
                steps=[
                    ValidationStepResult(
                        name=step.name,
                        status=step.status.value,
                        output=step.output,
                        errors=step.errors,
                        warnings=step.warnings,
                        duration=step.duration,
                    )
                    for step in report.steps
                ],
            )

            # If Validation Failed And Auto_commit Is On, Rollback Commit
            if not report.can_commit and ctx.auto_commit:
                out.warning = _WARNING_VALIDATION_FAILED
                out.git_commit = None
        except Exception as exc:
            logger.error("Validation error: %s", exc)
            out.validation_result = ValidationReportResponse(
                overall_status="error",
                summary=f"\u041e\u0448\u0438\u0431\u043a\u0430 \u0432\u0430\u043b\u0438\u0434\u0430\u0446\u0438\u0438: {exc}",
                total_duration=0,
                can_commit=False,
                steps=[],
            )

    # Warning If Git Not Initialized
    if ctx.git_info and ctx.git_info.status == GitStatus.NOT_INITIALIZED:
        out.warning = _WARNING_NOT_IN_GIT

    return out
