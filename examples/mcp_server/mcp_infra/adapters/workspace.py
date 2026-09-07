"""Workspace write tools (Phase C1) adapter.

_get_workspace_registry is resolved through the server module at call
time: tests patch examples.mcp_server.server._get_workspace_registry and
expect the patched function here. The registry factory itself lives in
this module and is re-exported by server.py (identity: tests patch the
server-module name, the adapter resolves the same object through it).

Tools are registered explicitly via register_all() (called by server.py
after runtime.set_mcp) instead of import-time decorator side effects.
"""

from __future__ import annotations

import errno
import os
from typing import Any

from tool_results import tool_error, tool_success

from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.tool_registry import instrumented, register_tool


def _server_workspace_registry():
    return server_attr("_get_workspace_registry")()


# ── Workspace write tools (Phase C1) ─────────────────────────────

_workspace_registry_cache = None


def _get_workspace_registry():
    """Get or create the workspace registry, resolving projects.yaml path.

    Uses a lazy cache to avoid re-parsing YAML on every call. The root is
    resolved by the shared resolve_registry_root() — the exact same
    deterministic resolution the REST app pins at startup — so MCP and
    REST can never drift apart again.
    """
    global _workspace_registry_cache
    if _workspace_registry_cache is not None:
        return _workspace_registry_cache

    from app.workspace.policy import ALL_SCOPES
    from app.workspace.registry import WorkspaceRegistry, resolve_registry_root, set_registry_root

    root = resolve_registry_root()
    if not (root / "projects.yaml").exists():
        raise RuntimeError(
            "Cannot find projects.yaml. Set WORKSPACE_REGISTRY_ROOT or "
            "ensure projects.yaml exists in the repo root."
        )
    set_registry_root(root)
    _workspace_registry_cache = WorkspaceRegistry.load(
        root / "projects.yaml", granted_scopes=ALL_SCOPES
    )
    return _workspace_registry_cache

def _workspace_candidate_required(
    tool: str, project_id: str, registry: Any
) -> dict[str, Any] | None:
    """Fail fast when a known project root cannot accept source mutations."""
    try:
        info = registry.project_info(project_id)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    root = info.get("root")
    if not isinstance(root, str) or not root:
        return None
    if not os.path.isdir(root):
        return None
    if os.access(root, os.W_OK):
        return None
    return tool_error(
        tool=tool,
        code="CANDIDATE_REQUIRED",
        message=f"Project {project_id!r} workspace root is not writeable.",
        retryable=False,
        hint="Use a registered writeable candidate clone before retrying.",
        source="gateway",
    )


def _workspace_mutation_error(tool: str, exc: Exception) -> dict[str, Any]:
    """Classify workspace write failures without hiding the operator action."""
    if isinstance(exc, OSError) and exc.errno == errno.EROFS:
        return tool_error(
            tool=tool,
            code="WORKSPACE_READONLY",
            message="Workspace project root is mounted read-only",
            retryable=False,
            hint="Use a writeable candidate clone or change the workspace mount before retrying.",
            source="gateway",
        )
    if isinstance(exc, PermissionError):
        return tool_error(
            tool=tool,
            code="PERMISSION_DENIED",
            message=str(exc),
            retryable=False,
            source="gateway",
        )
    return tool_error(
        tool=tool, code="TOOL_EXECUTION_FAILED", message=str(exc), source="gateway"
    )


def gateway_workspace_file_write(
    project_id: str,
    relative_path: str,
    content: str,
    max_bytes: int = 1_000_000,
    safe: bool = False,
) -> dict[str, Any]:
    """Write (create or overwrite) a UTF-8 text file inside a project.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        content: UTF-8 text content to write.
        max_bytes: Maximum content size in bytes (default 1MB).
        safe: If True, include change receipt in response for rollback.

    Returns:
        Contract v1 dict with project_id, path, size, encoding.
        If safe=True, includes nested receipt dict.
    """
    from app.config import settings as _settings

    if _settings.workspace_readonly:
        return tool_error(
            tool="workspace_file_write",
            code="WORKSPACE_READONLY",
            message="Workspace is in read-only mode",
        )
    try:
        from app.workspace.edit import project_file_write

        registry = _server_workspace_registry()
        candidate_required = _workspace_candidate_required(
            "workspace_file_write", project_id, registry
        )
        if candidate_required is not None:
            return candidate_required
        result = project_file_write(
            project_id=project_id,
            relative_path=relative_path,
            content=content,
            max_bytes=max_bytes,
            registry=registry,
            safe=safe,
        )
        return tool_success(tool="workspace_file_write", result=result)
    except Exception as exc:
        return _workspace_mutation_error("workspace_file_write", exc)


def gateway_workspace_file_edit(
    project_id: str,
    relative_path: str,
    old_string: str,
    new_string: str,
    max_bytes: int = 1_000_000,
    safe: bool = False,
) -> dict[str, Any]:
    """Edit a file by replacing the first occurrence of old_string with new_string.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        old_string: Literal string to find and replace (must not be empty).
        new_string: Replacement string.
        max_bytes: Maximum file size in bytes (default 1MB).
        safe: If True, include change receipt in response for rollback.

    Returns:
        Contract v1 dict with project_id, path, size, diff, replaced.
        If safe=True, includes nested receipt dict.
    """
    from app.config import settings as _settings

    if _settings.workspace_readonly:
        return tool_error(
            tool="workspace_file_edit",
            code="WORKSPACE_READONLY",
            message="Workspace is in read-only mode",
        )
    try:
        from app.workspace.edit import project_file_edit

        registry = _server_workspace_registry()
        candidate_required = _workspace_candidate_required(
            "workspace_file_edit", project_id, registry
        )
        if candidate_required is not None:
            return candidate_required
        result = project_file_edit(
            project_id=project_id,
            relative_path=relative_path,
            old_string=old_string,
            new_string=new_string,
            max_bytes=max_bytes,
            registry=registry,
            safe=safe,
        )
        return tool_success(tool="workspace_file_edit", result=result)
    except Exception as exc:
        return _workspace_mutation_error("workspace_file_edit", exc)


def gateway_workspace_apply_patch(
    project_id: str,
    relative_path: str,
    patch: str,
    max_bytes: int = 1_000_000,
    safe: bool = False,
) -> dict[str, Any]:
    """Apply a unified diff patch to a file inside a project.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        patch: Unified diff text (single file).
        max_bytes: Maximum file size in bytes (default 1MB).
        safe: If True, include change receipt in response for rollback.

    Returns:
        Dict with project_id, path, size, applied, backup_hash (patch stripped).
        If safe=True, includes nested receipt dict.
    """
    from app.config import settings as _settings

    if _settings.workspace_readonly:
        return tool_error(
            tool="workspace_apply_patch",
            code="WORKSPACE_READONLY",
            message="Workspace is in read-only mode",
        )
    try:
        from app.workspace.edit import project_apply_patch

        registry = _server_workspace_registry()
        candidate_required = _workspace_candidate_required(
            "workspace_apply_patch", project_id, registry
        )
        if candidate_required is not None:
            return candidate_required
        result = project_apply_patch(
            project_id=project_id,
            relative_path=relative_path,
            patch=patch,
            max_bytes=max_bytes,
            registry=registry,
            safe=safe,
        )
        # Strip patch content from response to avoid leaking input
        result.pop("patch", None)
        return tool_success(tool="workspace_apply_patch", result=result)
    except Exception as exc:
        return _workspace_mutation_error("workspace_apply_patch", exc)


def gateway_workspace_preview_write(
    project_id: str,
    relative_path: str,
    content: str,
    max_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Preview a file write without writing to disk.

    Returns diff, hashes, and size changes. No disk mutation.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        content: UTF-8 text content to write.
        max_bytes: Maximum content size in bytes (default 1MB).

    Returns:
        Contract v1 dict with before_hash, after_hash, size_before,
        size_after, diff, changed, file_exists_before, encoding.
    """
    try:
        from app.workspace.preview import project_file_preview_write

        registry = _server_workspace_registry()
        result = project_file_preview_write(
            project_id=project_id,
            relative_path=relative_path,
            content=content,
            max_bytes=max_bytes,
            registry=registry,
        )
        return tool_success(tool="workspace_preview_write", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_preview_write",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


def gateway_workspace_preview_edit(
    project_id: str,
    relative_path: str,
    old_string: str,
    new_string: str,
    max_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Preview a file edit without writing to disk.

    Returns diff, hashes, and size changes. No disk mutation.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        old_string: Literal string to find and replace (must not be empty).
        new_string: Replacement string.
        max_bytes: Maximum file size in bytes (default 1MB).

    Returns:
        Contract v1 dict with before_hash, after_hash, size_before,
        size_after, diff, changed, replaced, encoding.
    """
    try:
        from app.workspace.preview import project_file_preview_edit

        registry = _server_workspace_registry()
        result = project_file_preview_edit(
            project_id=project_id,
            relative_path=relative_path,
            old_string=old_string,
            new_string=new_string,
            max_bytes=max_bytes,
            registry=registry,
        )
        return tool_success(tool="workspace_preview_edit", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_preview_edit",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


def gateway_workspace_preview_patch(
    project_id: str,
    relative_path: str,
    patch: str,
    max_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Preview a patch application without writing to disk.

    Returns diff, hashes, and size changes. No disk mutation.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        patch: Unified diff text (single file).
        max_bytes: Maximum file size in bytes (default 1MB).

    Returns:
        Contract v1 dict with before_hash, after_hash, size_before,
        size_after, diff, changed, applied, encoding.
    """
    try:
        from app.workspace.preview import project_file_preview_patch

        registry = _server_workspace_registry()
        result = project_file_preview_patch(
            project_id=project_id,
            relative_path=relative_path,
            patch=patch,
            max_bytes=max_bytes,
            registry=registry,
        )
        return tool_success(tool="workspace_preview_patch", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_preview_patch",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


def gateway_workspace_verify(
    project_id: str,
    relative_path: str,
    expected_hash: str,
) -> dict[str, Any]:
    """Verify a file's current SHA-256 hash matches expected hash.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        expected_hash: Expected SHA-256 hash (e.g. "sha256:abc...").

    Returns:
        Contract v1 dict with project_id, path, matches, current_hash,
        file_exists.
    """
    try:
        from app.workspace.preview import project_file_verify

        registry = _server_workspace_registry()
        result = project_file_verify(
            project_id=project_id,
            relative_path=relative_path,
            expected_hash=expected_hash,
            registry=registry,
        )
        return tool_success(tool="workspace_verify", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_verify",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )

def gateway_todo_backlog_upsert(
    project_id: str,
    title: str,
    severity: str,
    observed_behavior: str,
    reproduction_steps: str,
    expected_behavior: str,
    impact: str,
    acceptance_criteria: str,
    related_evidence: str = "",
    section_title: str = "Runtime/tooling intake",
    failure_mode: str = "",
    entry_date: str | None = None,
    update_existing: bool = False,
    safe: bool = False,
) -> dict[str, Any]:
    """Create/update one structured TODO.md backlog entry with dedupe.

    Args:
        project_id: Registered project/candidate identifier to mutate.
        title: Human-readable finding title.
        severity: One of P0, P1, P2, P3.
        observed_behavior: What actually happened.
        reproduction_steps: How to reproduce or observe it again.
        expected_behavior: Desired behavior.
        impact: Why the finding matters.
        acceptance_criteria: Concrete closure checks.
        related_evidence: Optional commits, runs, PRs, task ids or log refs.
        section_title: Section prefix for newly-created entries.
        failure_mode: Optional stable dedupe key; title is used when omitted.
        entry_date: Optional YYYY-MM-DD; defaults to current date.
        update_existing: Append dated evidence to a matched item instead of
            refusing the duplicate.
        safe: Include an edit receipt.

    Returns:
        Contract v1 dict describing created/updated action and post-write proof.
    """
    from app.config import settings as _settings

    if _settings.workspace_readonly:
        return tool_error(
            tool="todo_backlog_upsert",
            code="WORKSPACE_READONLY",
            message="Workspace is in read-only mode",
        )
    try:
        from app.workspace.todo_backlog import TodoBacklogError, upsert_todo_backlog_entry

        registry = _server_workspace_registry()
        candidate_required = _workspace_candidate_required(
            "todo_backlog_upsert", project_id, registry
        )
        if candidate_required is not None:
            return candidate_required
        result = upsert_todo_backlog_entry(
            project_id=project_id,
            title=title,
            severity=severity,
            observed_behavior=observed_behavior,
            reproduction_steps=reproduction_steps,
            expected_behavior=expected_behavior,
            impact=impact,
            acceptance_criteria=acceptance_criteria,
            related_evidence=related_evidence,
            section_title=section_title,
            failure_mode=failure_mode,
            entry_date=entry_date,
            update_existing=update_existing,
            registry=registry,
            safe=safe,
        )
        return tool_success(tool="todo_backlog_upsert", result=result)
    except TodoBacklogError as exc:
        return tool_error(
            tool="todo_backlog_upsert",
            code=exc.code,
            message=str(exc),
            details=exc.details or None,
            source="gateway",
        )
    except Exception as exc:
        return _workspace_mutation_error("todo_backlog_upsert", exc)


def reset_workspace_registry_cache() -> None:
    """Drop the MCP workspace-registry cache after a registry mutation."""
    global _workspace_registry_cache
    _workspace_registry_cache = None


def register_all() -> None:
    register_tool("workspace_file_write")(instrumented("workspace_file_write")(gateway_workspace_file_write))
    register_tool("workspace_file_edit")(instrumented("workspace_file_edit")(gateway_workspace_file_edit))
    register_tool("workspace_apply_patch")(instrumented("workspace_apply_patch")(gateway_workspace_apply_patch))
    register_tool("workspace_preview_write")(instrumented("workspace_preview_write")(gateway_workspace_preview_write))
    register_tool("workspace_preview_edit")(instrumented("workspace_preview_edit")(gateway_workspace_preview_edit))
    register_tool("workspace_preview_patch")(instrumented("workspace_preview_patch")(gateway_workspace_preview_patch))
    register_tool("workspace_verify")(instrumented("workspace_verify")(gateway_workspace_verify))
    register_tool("todo_backlog_upsert")(
        instrumented("todo_backlog_upsert")(gateway_todo_backlog_upsert)
    )
