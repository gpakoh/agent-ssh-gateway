"""Server-controlled workspace project registration transaction.

This module owns validation and CAS persistence for one new ``projects.yaml``
entry.  MCP transport, scopes, and response envelopes remain in the supervisor
adapter.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.workspace.registry import load_registry_roots, resolve_runtime_registry_path
from examples.mcp_server.supervisor_integration import integrate_file

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ROOT_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REGISTRY_LOCK_FILENAME = ".project-registry.lock"
_REGISTRY_LOCK_TIMEOUT_S = 30.0
_REGISTRY_LOCK_POLL_S = 0.05
_REGISTRY_PROCESS_LOCK = threading.RLock()
_REGISTRY_LOCK_STATE = threading.local()


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
    root_selector: str
    project_type: str
    description: str
    tags: list[str]
    parent: str | None
    registry_hash: str
    storage: str


@dataclass(frozen=True)
class ProjectUnregistrationResult:
    project_id: str
    root: str
    project_type: str
    registry_hash: str | None
    storage: str | None
    already_absent: bool


def _error(code: str, message: str) -> ProjectRegistrationError:
    return ProjectRegistrationError(code, message)


@contextlib.contextmanager
def project_registry_mutation_lock(journal_root: Path) -> Iterator[None]:
    """Serialize registry mutations across threads and processes."""
    if not _REGISTRY_PROCESS_LOCK.acquire(timeout=_REGISTRY_LOCK_TIMEOUT_S):
        raise _error("WORKSPACE_CONTENDED", "Registry mutation lock is busy.")
    try:
        depth = int(getattr(_REGISTRY_LOCK_STATE, "depth", 0))
        if depth > 0:
            _REGISTRY_LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _REGISTRY_LOCK_STATE.depth = depth
            return

        try:
            journal_root.mkdir(parents=True, exist_ok=True)
            root_stat = journal_root.lstat()
        except OSError as exc:
            raise _error("TOOL_EXECUTION_FAILED", "Registry mutation lock root is unavailable.") from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise _error("POLICY_DENIED", "Registry mutation lock root is unsafe.")

        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise _error("POLICY_DENIED", "Secure registry mutation locking is unavailable.")
        flags = os.O_RDWR | os.O_CREAT | nofollow | getattr(os, "O_CLOEXEC", 0)
        lock_path = journal_root / _REGISTRY_LOCK_FILENAME
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise _error("TOOL_EXECUTION_FAILED", "Registry mutation lock cannot be opened.") from exc

        locked = False
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise _error("POLICY_DENIED", "Registry mutation lock file is unsafe.")
            os.fchmod(fd, 0o600)

            deadline = time.monotonic() + _REGISTRY_LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise _error(
                            "WORKSPACE_CONTENDED",
                            "Registry mutation lock is busy.",
                        ) from exc
                    time.sleep(_REGISTRY_LOCK_POLL_S)

            _REGISTRY_LOCK_STATE.depth = 1
            try:
                yield
            finally:
                _REGISTRY_LOCK_STATE.depth = 0
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    finally:
        _REGISTRY_PROCESS_LOCK.release()


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
) -> tuple[bytes, dict[str, Any], dict[str, Path]]:
    registry_path = config_dir / "projects.yaml"
    try:
        original = registry_path.read_bytes()
        data = yaml.safe_load(original)
        workspace_roots = load_registry_roots(registry_path)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise _error("TOOL_EXECUTION_FAILED", "Workspace registry cannot be read.") from exc

    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        raise _error("TOOL_EXECUTION_FAILED", "Workspace registry is malformed.")

    default_root = workspace_roots.get("default")
    if default_root is None or not default_root.is_dir():
        raise _error("TOOL_EXECUTION_FAILED", "Workspace registry root is unavailable.")

    return original, data, workspace_roots


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
    workspace_roots: dict[str, Path],
    relative_root: str,
    *,
    root_selector: str,
    require_existing: bool,
) -> Path:
    workspace_root = workspace_roots.get(root_selector)
    if workspace_root is None:
        raise _error(
            "TOOL_EXECUTION_FAILED",
            "Existing project root selector is malformed.",
        )
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
    workspace_roots: dict[str, Path],
    *,
    project_id: str,
    root: str,
    root_selector: str,
    parent: str | None,
) -> None:
    projects = data["projects"]
    if project_id in projects:
        raise _error("ALREADY_EXISTS", "project_id is already registered.")

    workspace_root = workspace_roots.get(root_selector)
    if workspace_root is None:
        raise _error("INVALID_INPUT", "root_selector is not configured.")
    if not workspace_root.is_dir():
        raise _error("INVALID_INPUT", "Selected project root is unavailable.")

    candidate = _resolve_candidate(workspace_root, root)
    for existing_cfg in projects.values():
        if not isinstance(existing_cfg, dict):
            continue
        existing_root = existing_cfg.get("root")
        if not isinstance(existing_root, str) or not existing_root.strip():
            continue
        existing_selector = existing_cfg.get("root_selector", "default")
        if not isinstance(existing_selector, str):
            continue
        try:
            resolved = _resolve_existing_root(
                workspace_roots,
                existing_root.strip(),
                root_selector=existing_selector,
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
    parent_selector = parent_cfg.get("root_selector", "default")
    if (
        not isinstance(parent_root, str)
        or not parent_root.strip()
        or not isinstance(parent_selector, str)
    ):
        raise _error("TOOL_EXECUTION_FAILED", "Parent registry entry is malformed.")

    parent_resolved = _resolve_existing_root(
        workspace_roots,
        parent_root.strip(),
        root_selector=parent_selector,
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
    root_selector: str,
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
    if root_selector != "default":
        lines.append(
            f"    root_selector: {json.dumps(root_selector, ensure_ascii=False)}"
        )
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


def _runtime_registry_seed() -> bytes:
    return b"version: 1\nprojects:\n"


def _remove_entry(original: bytes, *, project_id: str) -> bytes:
    """Remove one control-plane project block without reserializing the registry."""
    marker = f"  {project_id}:"
    lines = original.decode("utf-8").splitlines(keepends=True)
    starts = [idx for idx, line in enumerate(lines) if line.rstrip("\r\n") == marker]
    if len(starts) != 1:
        raise _error("TOOL_EXECUTION_FAILED", "Project registry entry cannot be removed safely.")
    start = starts[0]
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.startswith("    "):
            break
        end += 1
    del lines[start:end]
    while (
        start > 0
        and start < len(lines)
        and not lines[start - 1].strip()
        and not lines[start].strip()
    ):
        del lines[start]
    return "".join(lines).encode("utf-8")


def _read_or_create_runtime_registry(path: Path) -> bytes:
    if path.exists() and not path.is_file():
        raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay is not a file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists():
        return path.read_bytes()

    seed = _runtime_registry_seed()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    try:
        os.write(fd, seed)
        os.fsync(fd)
    finally:
        os.close(fd)
    return seed


def _load_existing_runtime_registry_data(config_dir: Path) -> dict[str, Any]:
    """Read the runtime overlay for cross-store validation without creating it."""
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    if runtime_path is None or not runtime_path.exists():
        return {"version": 1, "projects": {}}
    if not runtime_path.is_file():
        raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay is not a file.")
    try:
        original = runtime_path.read_bytes()
        data = yaml.safe_load(original) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay cannot be read.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
        raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay is malformed.")
    return data


def _load_runtime_registry(config_dir: Path) -> tuple[bytes, dict[str, Any], Path]:
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    if runtime_path is None:
        raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay is not configured.")
    try:
        original = _read_or_create_runtime_registry(runtime_path)
        data = yaml.safe_load(original) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay cannot be read.") from exc
    if not isinstance(data, dict):
        raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay is malformed.")
    projects = data.get("projects")
    if projects is None:
        data["projects"] = {}
    elif not isinstance(projects, dict):
        raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay is malformed.")
    return original, data, runtime_path


def _merged_registry_data(
    source_data: dict[str, Any],
    runtime_data: dict[str, Any],
) -> dict[str, Any]:
    source_projects = source_data.get("projects", {})
    runtime_projects = runtime_data.get("projects", {})
    merged = dict(source_data)
    merged["projects"] = {
        **(source_projects if isinstance(source_projects, dict) else {}),
        **(runtime_projects if isinstance(runtime_projects, dict) else {}),
    }
    return merged


def _register_project_unlocked(
    *,
    config_dir: Path,
    journal_root: Path,
    project_id: str,
    root: str,
    root_selector: str = "default",
    project_type: str = "unknown",
    description: str = "",
    tags: list[str] | None = None,
    parent: str | None = None,
    persist_to_source: bool = False,
) -> ProjectRegistrationResult:
    """Validate and append one project entry to the selected registry store."""

    config_dir = config_dir.resolve()
    if not isinstance(root_selector, str) or not _ROOT_SELECTOR_RE.fullmatch(
        root_selector.strip()
    ):
        raise _error("INVALID_INPUT", "root_selector has an invalid format.")
    root_selector = root_selector.strip()
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

    original, data, workspace_roots = _load_registry(config_dir)
    storage = "source_registry" if persist_to_source else "runtime_overlay"
    if persist_to_source:
        runtime_data = _load_existing_runtime_registry_data(config_dir)
        validation_data = _merged_registry_data(data, runtime_data)
        target_root = config_dir
        target_relative = "projects.yaml"
        target_original = original
    else:
        runtime_original, runtime_data, runtime_path = _load_runtime_registry(config_dir)
        validation_data = _merged_registry_data(data, runtime_data)
        target_root = runtime_path.parent
        target_relative = runtime_path.name
        target_original = runtime_original

    _validate_against_registry(
        validation_data,
        workspace_roots,
        project_id=project_id,
        root=root,
        root_selector=root_selector,
        parent=parent,
    )
    updated = _append_entry(
        target_original,
        project_id=project_id,
        root=root,
        root_selector=root_selector,
        project_type=project_type,
        description=description,
        tags=normalized_tags,
        parent=parent,
    )
    expected_hash = "sha256:" + hashlib.sha256(target_original).hexdigest()
    persisted = integrate_file(
        target_root,
        target_relative,
        expected_hash,
        updated,
        journal_root,
    )
    return ProjectRegistrationResult(
        project_id=project_id,
        root=root,
        root_selector=root_selector,
        project_type=project_type,
        description=description,
        tags=normalized_tags,
        parent=parent,
        registry_hash=persisted.new_hash,
        storage=storage,
    )


def register_project(
    *,
    config_dir: Path,
    journal_root: Path,
    project_id: str,
    root: str,
    root_selector: str = "default",
    project_type: str = "unknown",
    description: str = "",
    tags: list[str] | None = None,
    parent: str | None = None,
    persist_to_source: bool = False,
) -> ProjectRegistrationResult:
    """Serialize and register one validated project entry."""
    with project_registry_mutation_lock(journal_root):
        return _register_project_unlocked(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id=project_id,
            root=root,
            root_selector=root_selector,
            project_type=project_type,
            description=description,
            tags=tags,
            parent=parent,
            persist_to_source=persist_to_source,
        )


def _unregister_project_exact_unlocked(
    *,
    config_dir: Path,
    journal_root: Path,
    project_id: str,
    expected_root: str,
    expected_type: str,
    expected_root_selector: str = "default",
) -> ProjectUnregistrationResult:
    """CAS-remove one exact project entry after identity and descendant checks."""
    config_dir = config_dir.resolve()
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise _error("INVALID_INPUT", "project_id has an invalid format.")
    if (
        not isinstance(expected_root, str)
        or not expected_root.strip()
        or os.path.isabs(expected_root)
        or "\\" in expected_root
        or ".." in Path(expected_root).parts
    ):
        raise _error("INVALID_INPUT", "expected_root must be a safe relative path.")
    expected_root = expected_root.strip().rstrip("/")
    if not isinstance(expected_type, str) or not expected_type.strip():
        raise _error("INVALID_INPUT", "expected_type is invalid.")
    expected_type = expected_type.strip()
    if (
        not isinstance(expected_root_selector, str)
        or not _ROOT_SELECTOR_RE.fullmatch(expected_root_selector.strip())
    ):
        raise _error("INVALID_INPUT", "expected_root_selector has an invalid format.")
    expected_root_selector = expected_root_selector.strip()

    source_original, source_data, workspace_roots = _load_registry(config_dir)
    source_projects = source_data.get("projects", {})
    if not isinstance(source_projects, dict):
        raise _error("TOOL_EXECUTION_FAILED", "Workspace registry is malformed.")

    expected_workspace_root = workspace_roots.get(expected_root_selector)
    if expected_workspace_root is None:
        raise _error(
            "WORKSPACE_CONTENDED",
            "Project root selector does not match cleanup expectations.",
        )

    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    runtime_original: bytes | None = None
    runtime_data: dict[str, Any] = {"version": 1, "projects": {}}
    if runtime_path is not None and runtime_path.exists():
        try:
            runtime_original = runtime_path.read_bytes()
            loaded = yaml.safe_load(runtime_original) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay cannot be read.") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("projects", {}), dict):
            raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay is malformed.")
        runtime_data = loaded
    runtime_projects = runtime_data.get("projects", {})
    assert isinstance(runtime_projects, dict)

    source_entry = source_projects.get(project_id)
    runtime_entry = runtime_projects.get(project_id)
    if source_entry is not None and runtime_entry is not None:
        raise _error("WORKSPACE_CONTENDED", "Project is registered in multiple registry stores.")
    entry = runtime_entry if runtime_entry is not None else source_entry
    if entry is None:
        return ProjectUnregistrationResult(
            project_id=project_id,
            root=expected_root,
            project_type=expected_type,
            registry_hash=None,
            storage=None,
            already_absent=True,
        )
    if not isinstance(entry, dict):
        raise _error("TOOL_EXECUTION_FAILED", "Project registry entry is malformed.")
    entry_selector = entry.get("root_selector", "default")
    if not isinstance(entry_selector, str):
        raise _error("TOOL_EXECUTION_FAILED", "Project registry entry is malformed.")
    if (
        entry.get("root") != expected_root
        or entry.get("type") != expected_type
        or entry_selector != expected_root_selector
    ):
        raise _error(
            "WORKSPACE_CONTENDED",
            "Project registry identity does not match cleanup expectations.",
        )

    merged_projects = {**source_projects, **runtime_projects}
    descendant_prefix = expected_root + "/"
    for other_id, other_entry in merged_projects.items():
        if other_id == project_id:
            continue
        if not isinstance(other_entry, dict):
            raise _error("TOOL_EXECUTION_FAILED", "Project registry entry is malformed.")
        if other_entry.get("parent") == project_id:
            raise _error("WORKSPACE_CONTENDED", "Candidate project still has registered descendants.")
        other_root = other_entry.get("root")
        other_selector = other_entry.get("root_selector", "default")
        if not isinstance(other_selector, str):
            raise _error("TOOL_EXECUTION_FAILED", "Project registry entry is malformed.")
        other_workspace_root = workspace_roots.get(other_selector)
        if other_workspace_root is None:
            raise _error("TOOL_EXECUTION_FAILED", "Project registry entry is malformed.")
        if (
            other_workspace_root == expected_workspace_root
            and isinstance(other_root, str)
            and other_root.startswith(descendant_prefix)
        ):
            raise _error(
                "WORKSPACE_CONTENDED",
                "Candidate project still has registered descendants.",
            )

    if runtime_entry is not None:
        if runtime_path is None or runtime_original is None:
            raise _error("TOOL_EXECUTION_FAILED", "Runtime project registry overlay is unavailable.")
        target_root = runtime_path.parent
        target_relative = runtime_path.name
        target_original = runtime_original
        storage = "runtime_overlay"
    else:
        target_root = config_dir
        target_relative = "projects.yaml"
        target_original = source_original
        storage = "source_registry"

    updated = _remove_entry(target_original, project_id=project_id)
    expected_hash = "sha256:" + hashlib.sha256(target_original).hexdigest()
    persisted = integrate_file(
        target_root,
        target_relative,
        expected_hash,
        updated,
        journal_root,
    )
    return ProjectUnregistrationResult(
        project_id=project_id,
        root=expected_root,
        project_type=expected_type,
        registry_hash=persisted.new_hash,
        storage=storage,
        already_absent=False,
    )

def unregister_project_exact(
    *,
    config_dir: Path,
    journal_root: Path,
    project_id: str,
    expected_root: str,
    expected_type: str,
    expected_root_selector: str = "default",
) -> ProjectUnregistrationResult:
    """Serialize and CAS-remove one exact project entry."""
    with project_registry_mutation_lock(journal_root):
        return _unregister_project_exact_unlocked(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id=project_id,
            expected_root=expected_root,
            expected_type=expected_type,
            expected_root_selector=expected_root_selector,
        )
