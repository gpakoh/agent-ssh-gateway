"""Server-controlled workspace project registration transaction.

This module owns validation and CAS persistence for one new ``projects.yaml``
entry.  MCP transport, scopes, and response envelopes remain in the supervisor
adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from examples.mcp_server.supervisor_integration import integrate_file

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ProjectRegistrationError(ValueError):
    """Fail-closed validation/configuration error for project registration."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProjectRegistrationResult:
    project_id: str
    root: str
    project_type: str
    description: str
    tags: list[str]
    parent: str | None
    registry_hash: str


def _error(code: str, message: str) -> ProjectRegistrationError:
    return ProjectRegistrationError(code, message)


def _normalize_metadata(
    project_id: str,
    root: str,
    project_type: str,
    description: str,
    tags: list[str] | None,
    parent: str | None,
) -> tuple[str, str, str, str, list[str], str | None]:
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise _error("INVALID_INPUT", "project_id has an invalid format.")

    if not isinstance(root, str):
        raise _error("INVALID_INPUT", "root must be a safe relative path.")
    root = root.strip()
    if (
        not root
        or os.path.isabs(root)
        or "\\" in root
        or ".." in Path(root).parts
        or root in {".", "./"}
    ):
        raise _error("INVALID_INPUT", "root must be a safe relative path.")

    if (
        not isinstance(project_type, str)
        or not project_type.strip()
        or len(project_type.strip()) > 64
        or "\n" in project_type
        or "\r" in project_type
    ):
        raise _error("INVALID_INPUT", "project_type is invalid.")
    project_type = project_type.strip()

    if not isinstance(description, str) or len(description) > 2000:
        raise _error("INVALID_INPUT", "description is invalid.")

    if tags is None:
        normalized_tags: list[str] = []
    elif not isinstance(tags, list) or len(tags) > 32:
        raise _error("INVALID_INPUT", "tags must be a list of strings.")
    else:
        normalized_tags = []
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 64:
                raise _error("INVALID_INPUT", "tags must contain short strings.")
            normalized_tags.append(tag.strip())

    if parent is not None:
        if not isinstance(parent, str) or not _PROJECT_ID_RE.fullmatch(parent.strip()):
            raise _error("INVALID_INPUT", "parent has an invalid format.")
        parent = parent.strip()

    return project_id, root, project_type, description, normalized_tags, parent


def _load_registry(
    config_dir: Path,
) -> tuple[bytes, dict[str, Any], Path]:
    registry_path = config_dir / "projects.yaml"
    try:
        original = registry_path.read_bytes()
        data = yaml.safe_load(original)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _error("TOOL_EXECUTION_FAILED", "Workspace registry cannot be read.") from exc

    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        raise _error("TOOL_EXECUTION_FAILED", "Workspace registry is malformed.")

    raw_workspace_root = data.get("registry_root", ".")
    if not isinstance(raw_workspace_root, str) or not raw_workspace_root.strip():
        raise _error("TOOL_EXECUTION_FAILED", "Workspace registry root is malformed.")

    workspace_root = Path(raw_workspace_root.strip())
    if not workspace_root.is_absolute():
        workspace_root = config_dir / workspace_root
    try:
        workspace_root = workspace_root.resolve(strict=True)
    except OSError as exc:
        raise _error("TOOL_EXECUTION_FAILED", "Workspace registry root is unavailable.") from exc
    if not workspace_root.is_dir():
        raise _error("TOOL_EXECUTION_FAILED", "Workspace registry root is unavailable.")

    return original, data, workspace_root


def _resolve_candidate(workspace_root: Path, relative_root: str) -> Path:
    candidate = workspace_root
    for part in Path(relative_root).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise _error("POLICY_DENIED", "Project roots may not traverse symlinks.")

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _error("INVALID_INPUT", "Project root must be an existing directory.") from exc

    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise _error("POLICY_DENIED", "Project root resolves outside registry_root.") from exc

    if resolved == workspace_root or not resolved.is_dir():
        raise _error("INVALID_INPUT", "Project root must be an existing directory.")
    return resolved


def _resolve_existing_root(
    workspace_root: Path,
    relative_root: str,
    *,
    require_existing: bool,
) -> Path:
    candidate = workspace_root / relative_root
    try:
        resolved = candidate.resolve(strict=require_existing)
        resolved.relative_to(workspace_root)
    except (OSError, ValueError) as exc:
        raise _error("TOOL_EXECUTION_FAILED", "Existing project root is malformed.") from exc
    if require_existing and not resolved.is_dir():
        raise _error("TOOL_EXECUTION_FAILED", "Existing project root is unavailable.")
    return resolved


def _validate_against_registry(
    data: dict[str, Any],
    workspace_root: Path,
    *,
    project_id: str,
    root: str,
    parent: str | None,
) -> None:
    projects = data["projects"]
    if project_id in projects:
        raise _error("ALREADY_EXISTS", "project_id is already registered.")

    candidate = _resolve_candidate(workspace_root, root)
    for existing_cfg in projects.values():
        if not isinstance(existing_cfg, dict):
            continue
        existing_root = existing_cfg.get("root")
        if not isinstance(existing_root, str) or not existing_root.strip():
            continue
        try:
            resolved = _resolve_existing_root(
                workspace_root,
                existing_root.strip(),
                require_existing=False,
            )
        except ProjectRegistrationError:
            continue
        if resolved == candidate:
            raise _error("ALREADY_EXISTS", "Project root is already registered.")

    if parent is None:
        return

    parent_cfg = projects.get(parent)
    if not isinstance(parent_cfg, dict):
        raise _error("INVALID_INPUT", "parent is not a registered project.")
    parent_root = parent_cfg.get("root")
    if not isinstance(parent_root, str) or not parent_root.strip():
        raise _error("TOOL_EXECUTION_FAILED", "Parent registry entry is malformed.")

    parent_resolved = _resolve_existing_root(
        workspace_root,
        parent_root.strip(),
        require_existing=True,
    )
    try:
        candidate.relative_to(parent_resolved)
    except ValueError as exc:
        raise _error("POLICY_DENIED", "Project root must be below its declared parent.") from exc
    if candidate == parent_resolved:
        raise _error("POLICY_DENIED", "Project root must be below its declared parent.")


def _append_entry(
    original: bytes,
    *,
    project_id: str,
    root: str,
    project_type: str,
    description: str,
    tags: list[str],
    parent: str | None,
) -> bytes:
    # JSON scalars/arrays are valid YAML. Appending avoids reserializing and
    # reordering the hand-curated registry.
    lines = [
        f"  {project_id}:",
        f"    root: {json.dumps(root, ensure_ascii=False)}",
    ]
    if parent is not None:
        lines.append(f"    parent: {parent}")
    lines.extend(
        [
            f"    type: {json.dumps(project_type, ensure_ascii=False)}",
            f"    description: {json.dumps(description, ensure_ascii=False)}",
            f"    tags: {json.dumps(tags, ensure_ascii=False)}",
        ]
    )
    text = original.decode("utf-8")
    return (text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n").encode("utf-8")


def register_project(
    *,
    config_dir: Path,
    journal_root: Path,
    project_id: str,
    root: str,
    project_type: str = "unknown",
    description: str = "",
    tags: list[str] | None = None,
    parent: str | None = None,
) -> ProjectRegistrationResult:
    """Validate and atomically append one project registry entry."""

    config_dir = config_dir.resolve()
    (
        project_id,
        root,
        project_type,
        description,
        normalized_tags,
        parent,
    ) = _normalize_metadata(
        project_id,
        root,
        project_type,
        description,
        tags,
        parent,
    )

    original, data, workspace_root = _load_registry(config_dir)
    _validate_against_registry(
        data,
        workspace_root,
        project_id=project_id,
        root=root,
        parent=parent,
    )
    updated = _append_entry(
        original,
        project_id=project_id,
        root=root,
        project_type=project_type,
        description=description,
        tags=normalized_tags,
        parent=parent,
    )
    expected_hash = "sha256:" + hashlib.sha256(original).hexdigest()
    persisted = integrate_file(
        config_dir,
        "projects.yaml",
        expected_hash,
        updated,
        journal_root,
    )
    return ProjectRegistrationResult(
        project_id=project_id,
        root=root,
        project_type=project_type,
        description=description,
        tags=normalized_tags,
        parent=parent,
        registry_hash=persisted.new_hash,
    )
