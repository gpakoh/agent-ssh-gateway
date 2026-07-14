"""Multi-project workspace security policy.

Validates paths, symlinks, scopes, and file access against a project registry.
All paths are resolved to their real target before validation (symlink-safe).
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ── Scope definitions ────────────────────────────────────────────
Scope = Literal[
    "workspace:read",
    "project:read",
    "project:write",
    "project:execute",
    "project:docker",
]

ALL_SCOPES: set[str] = {
    "workspace:read",
    "project:read",
    "project:write",
    "project:execute",
    "project:docker",
}

# Scope hierarchy: broader scope implies narrower ones
SCOPE_IMPLIES: dict[str, set[str]] = {
    "project:docker": {"project:execute", "project:write", "project:read"},
    "project:execute": {"project:write", "project:read"},
    "project:write": {"project:read"},
    "workspace:read": set(),
}

# ── Hidden / sensitive paths (relative to project root) ──────────
HIDDEN_DIR_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    ".env.local",
    ".env.production",
    ".ssh",
    ".gnupg",
    ".config",
)

SECRET_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "*.kdbx",
    "*.keychain",
)

# Paths that are always forbidden regardless of scope
SYSTEM_FORBIDDEN: frozenset[str] = frozenset({
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/root/.ssh",
    "/root/.gnupg",
    "/proc",
    "/sys",
    "/dev",
})


class WorkspacePolicyError(PermissionError):
    """Raised when a workspace policy check fails."""


class SymlinkEscapeError(WorkspacePolicyError):
    """Raised when a symlink resolves outside the allowed root."""


class HiddenPathError(WorkspacePolicyError):
    """Raised when access to a hidden/secret path is denied."""


class ScopeDeniedError(WorkspacePolicyError):
    """Raised when the required scope is not granted."""


class TraversalError(WorkspacePolicyError):
    """Raised when a path traversal (../) is detected."""


class WorkspacePolicy:
    """Validates file operations against project roots and granted scopes.

    Usage:
        policy = WorkspacePolicy(
            project_roots={"my-project": Path("/media/1TB/Python/my-project")},
            allowed_roots=[Path("/media/1TB/Python/")],
            granted_scopes={"project:read", "project:write"},
        )
        policy.validate_read("my-project", "src/main.py")
    """

    def __init__(
        self,
        project_roots: dict[str, Path],
        allowed_roots: list[Path],
        granted_scopes: set[str] | None = None,
    ):
        self._project_roots = {name: p.resolve() for name, p in project_roots.items()}
        self._allowed_roots = [r.resolve() for r in allowed_roots]
        self._granted_scopes = granted_scopes or set()

    def _expand_scopes(self, scopes: set[str]) -> set[str]:
        """Expand scopes to include implied scopes."""
        expanded = set(scopes)
        for scope in scopes:
            expanded.update(SCOPE_IMPLIES.get(scope, set()))
        return expanded

    def _require_scope(self, required: str) -> None:
        """Check that a required scope is granted."""
        expanded = self._expand_scopes(self._granted_scopes)
        if required not in expanded:
            raise ScopeDeniedError(
                f"Scope '{required}' required but not granted. "
                f"Granted: {sorted(self._granted_scopes)}"
            )

    def _resolve_project_root(self, project_id: str) -> Path:
        """Get and validate project root exists."""
        if project_id not in self._project_roots:
            raise WorkspacePolicyError(f"Unknown project: {project_id}")
        root = self._project_roots[project_id]
        if not root.exists():
            raise WorkspacePolicyError(f"Project root does not exist: {root}")
        return root

    def _check_traversal(self, relative: str) -> None:
        """Reject any path containing traversal components, absolute paths, or ~."""
        if not relative:
            raise TraversalError("Path must not be empty")
        p = Path(relative)
        if p.is_absolute():
            raise TraversalError(f"Absolute path not allowed: {relative}")
        if relative.startswith("~"):
            raise TraversalError(f"Tilde path not allowed: {relative}")
        parts = p.parts
        if ".." in parts:
            raise TraversalError(f"Path contains traversal: {relative}")

    def _check_system_forbidden(self, resolved: Path) -> None:
        """Reject absolute paths hitting system-critical locations."""
        resolved_str = str(resolved)
        for forbidden in SYSTEM_FORBIDDEN:
            if resolved_str == forbidden or resolved_str.startswith(forbidden + "/"):
                raise WorkspacePolicyError(f"Access to system path denied: {forbidden}")

    def _check_allowed_roots(self, resolved: Path) -> None:
        """Ensure resolved path is under one of the allowed roots."""
        for root in self._allowed_roots:
            try:
                resolved.relative_to(root)
                return
            except ValueError:
                continue
        raise WorkspacePolicyError(
            f"Path {resolved} is outside allowed roots: {[str(r) for r in self._allowed_roots]}"
        )

    def _check_symlink_escape(self, path: Path, project_root: Path) -> Path:
        """Resolve symlinks and verify the final target is inside project_root.

        Returns the resolved (real) path.
        """
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise WorkspacePolicyError(f"Cannot resolve path {path}: {exc}") from exc

        # The resolved path must be under the project root
        try:
            resolved.relative_to(project_root)
        except ValueError as exc:
            raise SymlinkEscapeError(
                f"Symlink escape detected: {path} resolves to {resolved} "
                f"which is outside project root {project_root}"
            ) from exc

        # Also check against global allowed roots
        self._check_allowed_roots(resolved)

        return resolved

    def _is_hidden_or_secret(self, relative: str) -> bool:
        """Check if a relative path matches hidden/secret patterns."""
        parts = Path(relative).parts
        for part in parts:
            for pattern in HIDDEN_DIR_PATTERNS:
                if fnmatch.fnmatch(part, pattern):
                    return True
        name = Path(relative).name
        for pattern in SECRET_FILE_PATTERNS:
            if fnmatch.fnmatch(name, pattern):
                return True
        return False

    def validate_read(self, project_id: str, relative_path: str) -> Path:
        """Validate a read operation. Returns the resolved absolute path.

        Checks: scope, traversal, symlinks, allowed roots, hidden/secret files.
        """
        self._require_scope("project:read")
        self._check_traversal(relative_path)

        root = self._resolve_project_root(project_id)
        full = root / relative_path

        # Check if path exists (allow non-existent for write targets)
        if full.exists():
            resolved = self._check_symlink_escape(full, root)
            self._check_system_forbidden(resolved)

            # Hidden/secret check for reads
            if self._is_hidden_or_secret(relative_path):
                raise HiddenPathError(
                    f"Access to hidden/secret path denied: {relative_path}"
                )

        return full

    def validate_write(self, project_id: str, relative_path: str) -> Path:
        """Validate a write operation. Returns the resolved absolute path.

        Checks: scope, traversal, symlinks, allowed roots.
        """
        self._require_scope("project:write")
        self._check_traversal(relative_path)

        root = self._resolve_project_root(project_id)
        full = root / relative_path

        # If the file exists, check symlinks
        if full.exists():
            resolved = self._check_symlink_escape(full, root)
            self._check_system_forbidden(resolved)

        # Block writes to hidden/secret paths
        if self._is_hidden_or_secret(relative_path):
            raise HiddenPathError(
                f"Write to hidden/secret path denied: {relative_path}"
            )

        # Ensure parent directory is inside project
        parent = full.parent
        if parent.exists():
            self._check_symlink_escape(parent, root)

        return full

    def validate_execute(self, project_id: str, relative_path: str | None = None) -> Path | None:
        """Validate an execute operation.

        Checks: scope, traversal, symlinks, allowed roots.
        If relative_path is None, validates project-level execution.
        """
        self._require_scope("project:execute")

        if relative_path is None:
            root = self._resolve_project_root(project_id)
            self._check_allowed_roots(root)
            return None

        self._check_traversal(relative_path)
        root = self._resolve_project_root(project_id)
        full = root / relative_path

        if full.exists():
            resolved = self._check_symlink_escape(full, root)
            self._check_system_forbidden(resolved)

        return full

    def validate_docker(self, project_id: str) -> Path:
        """Validate a docker operation within a project."""
        self._require_scope("project:docker")
        root = self._resolve_project_root(project_id)
        self._check_allowed_roots(root)
        return root

    def validate_search(self, project_id: str, relative_path: str) -> Path:
        """Validate a search/read-only operation.

        Allows hidden files to be found but not read.
        """
        self._require_scope("project:read")
        self._check_traversal(relative_path)

        root = self._resolve_project_root(project_id)
        full = root / relative_path

        if full.exists():
            resolved = self._check_symlink_escape(full, root)
            self._check_system_forbidden(resolved)

        return full
