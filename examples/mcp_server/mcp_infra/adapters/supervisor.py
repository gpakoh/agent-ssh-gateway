"""Admin-only supervisor integration MCP adapter.

The underlying persistence primitive lives in ``supervisor_integration``.
This adapter deliberately does *not* expose project_root or journal_root to
callers: the project root comes from the workspace registry and journals live
under a server-controlled persistent directory.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from tool_results import tool_error, tool_success

from examples.mcp_server.candidate_clone import (
    CandidateCloneError,
)
from examples.mcp_server.candidate_clone import (
    prepare_candidate_clone as _prepare_candidate_clone,
)
from examples.mcp_server.mcp_infra.tool_registry import (
    _validate_project,
    instrumented,
    register_tool,
    run_tool,
)
from examples.mcp_server.project_registry_control import (
    ProjectRegistrationError,
    register_project,
)
from examples.mcp_server.supervisor_integration import (
    HashMismatchError,
    JournalPendingError,
    PathEscapeError,
    RollbackFailedError,
    SupervisorIntegrationError,
    SymlinkRejectedError,
    TargetMissingError,
    UnsupportedTargetError,
    integrate_file,
    recover_pending,
)

logger = logging.getLogger(__name__)

_DEFAULT_JOURNAL_ROOT = Path("/app/data/supervisor-integration")


class _ProjectResolutionError(Exception):
    pass


def _get_workspace_registry():
    # Import lazily so tests can replace this helper without importing the
    # whole MCP server and so the same cached registry implementation as the
    # workspace adapter is used in production.
    from examples.mcp_server.mcp_infra.adapters import workspace

    return workspace._get_workspace_registry()


def _resolve_project(project: str) -> tuple[str, Path]:
    project = _validate_project(project)
    try:
        info = _get_workspace_registry().project_info(project)
        raw_root = info.get("root")
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ValueError("registry project root missing")
        root = Path(raw_root).resolve()
        if not root.is_dir():
            raise ValueError("registry project root is not a directory")
    except Exception as exc:
        raise _ProjectResolutionError(project) from exc
    return project, root


def _journal_base() -> Path:
    configured = os.environ.get("MCP_SUPERVISOR_JOURNAL_ROOT", "").strip()
    raw = Path(configured) if configured else _DEFAULT_JOURNAL_ROOT
    if not raw.is_absolute():
        raise ValueError("MCP_SUPERVISOR_JOURNAL_ROOT must be an absolute path")
    return raw.resolve()


def _journal_root_for_project(project: str, project_root: Path) -> Path:
    base = _journal_base()
    project_root = project_root.resolve()
    # A server misconfiguration must not put crash-recovery secrets/backups
    # inside the checkout being modified.
    try:
        base.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise ValueError("Supervisor journal root must be outside the project checkout")

    digest = hashlib.sha256(f"{project}\0{project_root}".encode()).hexdigest()
    return base / digest


def _supervisor_io_error(tool: str, exc: BaseException, message: str) -> dict[str, Any]:
    """Classify supervisor adapter I/O failures without leaking host paths."""
    if isinstance(exc, PermissionError):
        return tool_error(
            tool=tool,
            code="PERMISSION_DENIED",
            message=message,
            retryable=False,
            hint="Use a writeable candidate clone or fix the server-side file permissions before retrying.",
        )
    if isinstance(exc, OSError) and exc.errno == errno.EROFS:
        return tool_error(
            tool=tool,
            code="WORKSPACE_READONLY",
            message=message,
            retryable=False,
            hint="Use a writeable candidate clone or change the workspace mount before retrying.",
        )
    logger.warning("Supervisor adapter I/O failure", exc_info=exc)
    return tool_error(
        tool=tool,
        code="TOOL_EXECUTION_FAILED",
        message=message,
        retryable=False,
    )


def _integration_error(tool: str, exc: SupervisorIntegrationError) -> dict[str, Any]:
    if isinstance(exc, TargetMissingError):
        return tool_error(
            tool=tool,
            code="FILE_NOT_FOUND",
            message="Supervisor integration target does not exist.",
            retryable=False,
        )
    if isinstance(exc, HashMismatchError):
        return tool_error(
            tool=tool,
            code="CHECK_FAILED",
            message="Supervisor integration precondition hash did not match.",
            retryable=False,
        )
    if isinstance(exc, JournalPendingError):
        return tool_error(
            tool=tool,
            code="CHECK_FAILED",
            message="A pending supervisor recovery journal must be reconciled first.",
            retryable=False,
        )
    if isinstance(exc, PathEscapeError | SymlinkRejectedError | UnsupportedTargetError):
        return tool_error(
            tool=tool,
            code="POLICY_DENIED",
            message="Supervisor integration rejected the target as unsafe.",
            retryable=False,
        )
    if isinstance(exc, RollbackFailedError):
        logger.error("Supervisor integration rollback failed", exc_info=exc)
        return tool_error(
            tool=tool,
            code="INTERNAL_ERROR",
            message="Supervisor integration rollback failed; manual recovery is required.",
            retryable=False,
        )
    logger.warning("Supervisor integration failed", exc_info=exc)
    return tool_error(
        tool=tool,
        code="TOOL_EXECUTION_FAILED",
        message="Supervisor integration failed.",
        retryable=False,
    )


def _resolve_registry_config_dir() -> Path:
    """Resolve the server-owned directory containing projects.yaml."""
    from app.workspace.registry import resolve_registry_root

    config_dir = resolve_registry_root().resolve()
    if not (config_dir / "projects.yaml").is_file():
        raise ValueError("workspace registry is unavailable")
    return config_dir


def _reset_project_registry_caches() -> bool:
    """Reset both registry singletons; report partial failure explicitly."""
    ok = True
    try:
        from app.workspace.registry import reset_registry

        reset_registry()
    except Exception:
        logger.exception("App workspace registry cache reset failed")
        ok = False
    try:
        from examples.mcp_server.mcp_infra.adapters.workspace import (
            reset_workspace_registry_cache,
        )

        reset_workspace_registry_cache()
    except Exception:
        logger.exception("MCP workspace registry cache reset failed")
        ok = False
    return ok


def _registration_visibility(
    result_project_id: str,
    result_root: str,
) -> dict[str, Any]:
    """Check whether the newly persisted project is visible to live tools."""
    try:
        info = _get_workspace_registry().project_info(result_project_id)
    except Exception:
        return {
            "usable": False,
            "visibility_error": "PROJECT_NOT_VISIBLE_AFTER_REGISTRATION",
        }

    visible: dict[str, Any] = {
        "project_id": result_project_id,
        # Use the registry-relative root already validated/persisted by the
        # control plane.  The workspace registry returns an absolute host path;
        # do not echo that through ChatGPT-facing tooling.
        "root": result_root,
    }
    if isinstance(info.get("parent"), str) and info["parent"]:
        visible["parent"] = info["parent"]
    return {"usable": True, "visible_project": visible}


def _register_project_impl(
    project_id: str,
    root: str,
    project_type: str,
    description: str,
    tags: list[str] | None,
    parent: str | None,
    persist_to_source: bool,
) -> dict[str, Any]:
    tool = "supervisor_register_project"
    try:
        config_dir = _resolve_registry_config_dir()
        journal_root = _journal_root_for_project("workspace-registry", config_dir)
        result = register_project(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id=project_id,
            root=root,
            project_type=project_type,
            description=description,
            tags=tags,
            parent=parent,
            persist_to_source=persist_to_source,
        )
    except ProjectRegistrationError as exc:
        return tool_error(
            tool=tool,
            code=exc.code,
            message=exc.message,
            retryable=False,
        )
    except SupervisorIntegrationError as exc:
        return _integration_error(tool, exc)
    except ValueError:
        return tool_error(
            tool=tool,
            code="INVALID_INPUT",
            message="Workspace registry control plane is misconfigured.",
            retryable=False,
        )
    except (OSError, UnicodeError) as exc:
        return _supervisor_io_error(
            tool,
            exc,
            "Project registration could not be persisted.",
        )

    cache_reset = _reset_project_registry_caches()
    payload: dict[str, Any] = {
        "project_id": result.project_id,
        "root": result.root,
        "type": result.project_type,
        "description": result.description,
        "tags": result.tags,
        "parent": result.parent,
        "registry_hash": result.registry_hash,
        "storage": result.storage,
        "source_registry_mutated": result.storage == "source_registry",
        "cache_reset": cache_reset,
    }
    payload.update(_registration_visibility(result.project_id, result.root))
    return tool_success(tool=tool, result=payload)


def supervisor_register_project(
    project_id: str,
    root: str,
    project_type: str = "unknown",
    description: str = "",
    tags: list[str] | None = None,
    parent: str | None = None,
    persist_to_source: bool = False,
) -> dict[str, Any]:
    """Register one existing directory without dirtying source by default."""
    return run_tool(
        tool="supervisor_register_project",
        title="Supervisor register project",
        fn=lambda: _register_project_impl(
            project_id,
            root,
            project_type,
            description,
            tags,
            parent,
            persist_to_source,
        ),
        success_text="Project registration completed.",
    )


def prepare_candidate_clone(
    project: str,
    branch: str,
    base_ref: str | None = None,
) -> dict[str, Any]:
    """Create/recover a durable writeable candidate clone and register it."""

    def _fn() -> dict[str, Any]:
        config_dir = _resolve_registry_config_dir()
        journal_root = _journal_root_for_project("workspace-registry", config_dir)
        try:
            receipt = _prepare_candidate_clone(
                project,
                branch,
                base_ref,
                config_dir=config_dir,
                journal_root=journal_root,
            )
        except CandidateCloneError as exc:
            return tool_error(
                tool="prepare_candidate_clone",
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                details=exc.details,
            )
        cache_reset = _reset_project_registry_caches()
        result = receipt.as_dict()
        result["cache_reset"] = cache_reset
        return result

    return run_tool(
        tool="prepare_candidate_clone",
        title="Prepare candidate clone",
        fn=_fn,
        success_text="Prepared candidate clone.",
    )


def _integrate_impl(
    project: str,
    relative_path: str,
    expected_sha256: str,
    new_content: str,
) -> dict[str, Any]:
    tool = "supervisor_integrate_file"
    if not isinstance(new_content, str):
        return tool_error(
            tool=tool,
            code="INVALID_INPUT",
            message="new_content must be UTF-8 text.",
            retryable=False,
        )
    try:
        project, project_root = _resolve_project(project)
    except _ProjectResolutionError:
        return tool_error(
            tool=tool,
            code="PROJECT_NOT_FOUND",
            message="Project is not registered or its root is unavailable.",
            retryable=False,
        )
    try:
        journal_root = _journal_root_for_project(project, project_root)
    except ValueError:
        return tool_error(
            tool=tool,
            code="INVALID_INPUT",
            message="Supervisor journal storage is misconfigured.",
            retryable=False,
        )

    try:
        result = integrate_file(
            project_root,
            relative_path,
            expected_sha256,
            new_content,
            journal_root,
        )
    except SupervisorIntegrationError as exc:
        return _integration_error(tool, exc)
    except (OSError, UnicodeError) as exc:
        return _supervisor_io_error(
            tool,
            exc,
            "Supervisor integration could not persist the requested change.",
        )

    # Do not expose result.target_path: it is an absolute host path.
    return tool_success(
        tool=tool,
        result={
            "project": project,
            "path": result.relative_path,
            "original_hash": result.original_hash,
            "new_hash": result.new_hash,
        },
    )


def supervisor_integrate_file(
    project: str,
    relative_path: str,
    expected_sha256: str,
    new_content: str,
) -> dict[str, Any]:
    """Safely replace one existing project file using an expected SHA-256.

    This tool is registered only in ``mcp_client_write`` and requires
    ``mcp:admin``. It cannot create/delete files or choose storage roots.
    """
    return run_tool(
        tool="supervisor_integrate_file",
        title="Supervisor integrate file",
        fn=lambda: _integrate_impl(project, relative_path, expected_sha256, new_content),
        success_text="Supervisor file integration completed.",
    )


def _recover_impl(project: str) -> dict[str, Any]:
    tool = "supervisor_recover_integrations"
    try:
        project, project_root = _resolve_project(project)
    except _ProjectResolutionError:
        return tool_error(
            tool=tool,
            code="PROJECT_NOT_FOUND",
            message="Project is not registered or its root is unavailable.",
            retryable=False,
        )
    try:
        journal_root = _journal_root_for_project(project, project_root)
    except ValueError:
        return tool_error(
            tool=tool,
            code="INVALID_INPUT",
            message="Supervisor journal storage is misconfigured.",
            retryable=False,
        )

    try:
        recovered = recover_pending(project_root, journal_root)
    except SupervisorIntegrationError as exc:
        logger.warning("Supervisor recovery sweep failed", exc_info=exc)
        return tool_error(
            tool=tool,
            code="TOOL_EXECUTION_FAILED",
            message="Supervisor recovery sweep failed.",
            retryable=False,
        )
    except OSError as exc:
        return _supervisor_io_error(tool, exc, "Supervisor recovery sweep failed.")

    rows: list[dict[str, Any]] = []
    for item in recovered:
        row: dict[str, Any] = {
            "path": item.relative_path,
            "status": item.status,
            "journal_retained": item.journal_retained,
        }
        # Raw item.error may contain absolute project/journal paths. Keep the
        # external contract deliberately generic while preserving status.
        if item.error:
            row["error"] = "Manual intervention is required for this recovery entry."
        rows.append(row)

    return tool_success(
        tool=tool,
        result={
            "project": project,
            "count": len(rows),
            "recoveries": rows,
        },
    )


def supervisor_recover_integrations(project: str) -> dict[str, Any]:
    """Reconcile pending supervisor integration journals for one project."""
    return run_tool(
        tool="supervisor_recover_integrations",
        title="Supervisor recover integrations",
        fn=lambda: _recover_impl(project),
        success_text="Supervisor integration recovery sweep completed.",
    )


def register_all() -> None:
    register_tool("supervisor_integrate_file")(
        instrumented("supervisor_integrate_file")(supervisor_integrate_file)
    )
    register_tool("supervisor_recover_integrations")(
        instrumented("supervisor_recover_integrations")(supervisor_recover_integrations)
    )
    register_tool("supervisor_register_project")(
        instrumented("supervisor_register_project")(supervisor_register_project)
    )
    register_tool("prepare_candidate_clone")(
        instrumented("prepare_candidate_clone")(prepare_candidate_clone)
    )


__all__ = [
    "register_all",
    "supervisor_integrate_file",
    "supervisor_recover_integrations",
    "supervisor_register_project",
    "prepare_candidate_clone",
]
