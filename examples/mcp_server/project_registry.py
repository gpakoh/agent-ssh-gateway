from __future__ import annotations

import os
from pathlib import Path

from examples.mcp_server.config import ALLOWED_PROJECT_ROOTS, PROJECT_MAP


class ProjectRegistry:
    """Maps project names to validated filesystem paths with symlink escape protection.

    Construction: validates that the path exists on disk.
    Resolve: validates the path is within allowed roots AFTER symlink resolution.
    """

    def __init__(self, projects: dict[str, str], allowed_roots: list[str]):
        self._projects: dict[str, Path] = {}
        for name, path_str in projects.items():
            p = Path(path_str)
            if not p.exists():
                raise ValueError(
                    f"PROJECT_NOT_FOUND: path does not exist for '{name}': {path_str}"
                )
            self._projects[name] = p
        self._allowed_roots = [Path(r).resolve() for r in allowed_roots]

    def resolve(self, name: str) -> Path:
        if name not in self._projects:
            raise ValueError(f"PROJECT_NOT_FOUND: unknown project '{name}'")
        path = self._projects[name]
        resolved = path.resolve()
        # Symlink escape check: resolved must be under an allowed root
        for root in self._allowed_roots:
            try:
                resolved.relative_to(root)
                return path
            except ValueError:
                continue
        raise ValueError(f"POLICY_DENIED: {path} is outside allowed roots")

    def list_projects(self) -> list[str]:
        return sorted(self._projects.keys())


# ── Module-level singleton (lazy) ───────────────────────────────
# Imported by chatgpt_tools and server — avoids circular imports.
# Created lazily on first access so config errors only surface at runtime.
_project_registry: ProjectRegistry | None = None


def get_project_registry() -> ProjectRegistry:
    global _project_registry  # noqa: PLW0603
    if _project_registry is None:
        _project_registry = ProjectRegistry(
            projects=PROJECT_MAP,
            allowed_roots=ALLOWED_PROJECT_ROOTS,
        )
    return _project_registry
