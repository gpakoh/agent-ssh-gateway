"""Multi-project workspace registry.

Loads projects.yaml, builds WorkspacePolicy, provides read-only tools:
  - workspace_list_projects()
  - project_info(project_id)
  - project_tree(project_id, path, depth)

All filesystem access is validated through WorkspacePolicy.
Secrets, caches, and vendor dirs are filtered from tree output.
No write or execute capability in this module.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any

import yaml

from app.workspace.models import ProjectInfo
from app.workspace.policy import WorkspacePolicy, WorkspacePolicyError

logger = logging.getLogger(__name__)

# ── Default project registry path ─────────────────────────────────
_registry_root: Path | None = None


def set_registry_root(path: str | Path) -> None:
    global _registry_root
    _registry_root = Path(path).resolve()


def get_registry_root() -> Path:
    if _registry_root is None:
        return Path.cwd()
    return _registry_root


# ── Default hidden / vendor / cache patterns ──────────────────────

VENDOR_CACHE_PATTERNS: tuple[str, ...] = (
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".benchmarks",
    "node_modules",
    ".hg",
    ".svn",
    ".DS_Store",
    "*.pyc",
    ".terraform",
    ".serverless",
    ".next",
    ".nuxt",
)

# ── Registry loader ───────────────────────────────────────────────


def _resolve(registry_root: Path, relative_root: str) -> Path:
    """Resolve project root relative to registry_root, with traversal check."""
    resolved = (registry_root / relative_root).resolve()
    try:
        resolved.relative_to(registry_root.resolve())
    except ValueError as exc:
        raise WorkspacePolicyError(
            f"Project root {relative_root} resolves outside registry root {registry_root}"
        ) from exc
    return resolved


def load_registry(path: str | Path) -> tuple[dict[str, ProjectInfo], Path]:
    """Load project registry from a YAML file.

    Returns (projects dict, registry_root).
    Raises WorkspacePolicyError on invalid config.
    """
    path = Path(path)
    if not path.exists():
        raise WorkspacePolicyError(f"Registry file not found: {path}")
    if not path.is_file():
        raise WorkspacePolicyError(f"Registry path is not a file: {path}")

    raw = path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw)

    if not isinstance(data, dict):
        raise WorkspacePolicyError("Registry file must be a YAML mapping")

    registry_root_str = data.get("registry_root", ".")
    registry_root = Path(registry_root_str)
    if not registry_root.is_absolute():
        registry_root = (path.parent / registry_root).resolve()

    projects_raw = data.get("projects")
    if not isinstance(projects_raw, dict):
        raise WorkspacePolicyError("Registry file must contain a 'projects' mapping")

    projects: dict[str, ProjectInfo] = {}
    for pid, cfg in projects_raw.items():
        if not isinstance(cfg, dict):
            logger.warning("Skipping project %s: config must be a mapping", pid)
            continue
        relative_root = cfg.get("root", "")
        if not relative_root:
            logger.warning("Skipping project %s: missing 'root'", pid)
            continue
        root = _resolve(registry_root, relative_root)
        if not root.exists():
            logger.warning("Skipping project %s: root does not exist: %s", pid, root)
            continue

        projects[pid] = ProjectInfo(
            project_id=pid,
            root=root,
            type=str(cfg.get("type", "unknown")),
            description=str(cfg.get("description", "")),
            tags=list(cfg.get("tags", [])),
            parent=cfg.get("parent"),
        )

    return projects, registry_root


# ── WorkspaceRegistry ─────────────────────────────────────────────


class WorkspaceRegistry:
    """Read-only project registry with path validation and file tree browsing.

    Usage:
        registry = WorkspaceRegistry.load("projects.yaml")
        projects = registry.list_projects()
        info = registry.project_info("web-ssh-gateway")
        tree = registry.project_tree("web-ssh-gateway", depth=2)
    """

    def __init__(
        self,
        projects: dict[str, ProjectInfo],
        allowed_roots: list[Path],
        granted_scopes: set[str] | None = None,
    ):
        self._projects = projects
        self._allowed_roots = allowed_roots

        project_roots_raw: dict[str, Path] = {
            pid: info.root for pid, info in projects.items()
        }
        self._policy = WorkspacePolicy(
            project_roots=project_roots_raw,
            allowed_roots=allowed_roots,
            granted_scopes=granted_scopes or {"project:read", "workspace:read"},
        )

    @classmethod
    def load(
        cls,
        registry_path: str | Path,
        allowed_roots: list[Path] | None = None,
        granted_scopes: set[str] | None = None,
    ) -> WorkspaceRegistry:
        """Load registry from a YAML file.

        If allowed_roots is None, the registry_root from the YAML file is used.
        """
        projects, yaml_registry_root = load_registry(registry_path)
        if allowed_roots is None:
            allowed_roots = [yaml_registry_root]
        return cls(projects, allowed_roots, granted_scopes)

    def list_projects(self) -> list[dict[str, Any]]:
        """Return summary of all registered projects."""
        result: list[dict[str, Any]] = []
        for pid, info in sorted(self._projects.items()):
            result.append({
                "project_id": pid,
                "type": info.type,
                "description": info.description,
                "tags": info.tags,
            })
        return result

    def project_info(self, project_id: str) -> dict[str, Any]:
        """Return detailed metadata for a single project."""
        info = self._projects.get(project_id)
        if info is None:
            raise WorkspacePolicyError(f"Unknown project: {project_id}")
        result: dict[str, Any] = {
            "project_id": info.project_id,
            "root": str(info.root),
            "type": info.type,
            "description": info.description,
            "tags": info.tags,
        }
        if info.parent:
            result["parent"] = info.parent
        return result

    def project_tree(
        self,
        project_id: str,
        relative_path: str = "",
        depth: int = 3,
        max_nodes: int = 500,
    ) -> dict[str, Any]:
        """Return a file tree for a project (or subdirectory within it).

        Args:
            project_id: registered project identifier.
            relative_path: path within the project (empty = project root).
            depth: how many levels of nesting to show (0 = no children).
            max_nodes: maximum number of entries at each directory level.

        Returns:
            A dict with name, path, type, size, children, and truncated flag.

        Raises:
            WorkspacePolicyError: unknown project, traversal, symlink escape.
            HiddenPathError: path matches secret patterns.
        """
        full_path = self._policy.validate_read(project_id, relative_path or ".")

        if not full_path.exists():
            raise WorkspacePolicyError(f"Path does not exist: {full_path}")
        if not full_path.is_dir():
            raise WorkspacePolicyError(f"Path is not a directory: {full_path}")

        project_root = self._policy._resolve_project_root(project_id)
        return self._build_tree(full_path, project_root, depth, max_nodes)

    def _build_tree(
        self,
        dir_path: Path,
        project_root: Path,
        depth: int,
        max_nodes: int,
    ) -> dict[str, Any]:
        """Recursively build a file tree node, filtering secrets and vendors."""
        name = dir_path.name if dir_path != project_root else project_root.name
        node: dict[str, Any] = {
            "name": name,
            "path": str(dir_path.relative_to(project_root)) if dir_path != project_root else "",
            "type": "directory",
            "size": 0,
            "truncated": False,
        }

        if depth <= 0:
            return node

        try:
            entries = sorted(
                dir_path.iterdir(),
                key=lambda e: (0 if e.is_dir() else 1, e.name.lower()),
            )
        except PermissionError:
            logger.warning("Permission denied reading: %s", dir_path)
            node["children"] = []
            return node
        except OSError as exc:
            logger.warning("Error reading directory %s: %s", dir_path, exc)
            node["children"] = []
            return node

        children: list[dict[str, Any]] = []
        count = 0
        for entry in entries:
            if count >= max_nodes:
                node["truncated"] = True
                break

            entry_name = entry.name
            rel = str(entry.relative_to(project_root))

            if self._policy._is_hidden_or_secret(rel):
                continue

            if _is_vendor_or_cache(entry_name):
                continue

            if entry.is_dir() and not entry.is_symlink():
                subtree = self._build_tree(entry, project_root, depth - 1, max_nodes)
                children.append(subtree)
                count += 1
            elif entry.is_file() or entry.is_symlink():
                try:
                    stat = entry.stat()
                    sz = stat.st_size
                except OSError:
                    sz = 0
                child_type = "symlink" if entry.is_symlink() else "file"
                children.append({
                    "name": entry_name,
                    "path": rel,
                    "type": child_type,
                    "size": sz,
                })
                count += 1

        node["children"] = children
        return node

    def __contains__(self, project_id: str) -> bool:
        return project_id in self._projects

    def __len__(self) -> int:
        return len(self._projects)


# ── Module-level singleton (lazy) ─────────────────────────────────

_registry: WorkspaceRegistry | None = None


def get_registry(registry_path: str | Path | None = None) -> WorkspaceRegistry:
    """Get or create the module-level WorkspaceRegistry singleton.

    The first call determines registry_path. Subsequent calls return the
    existing instance. This allows tests to inject a custom registry.
    """
    global _registry
    if _registry is not None:
        return _registry
    path = registry_path or get_registry_root() / "projects.yaml"
    _registry = WorkspaceRegistry.load(path)
    return _registry


def reset_registry() -> None:
    """Reset the singleton (for testing)."""
    global _registry
    _registry = None


# ── Helpers ──────────────────────────────────────────────────────


def _is_vendor_or_cache(name: str) -> bool:
    """Check if a directory/file name matches vendor/cache patterns."""
    for pattern in VENDOR_CACHE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False
