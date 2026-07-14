"""Tests for multi-project workspace security policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace_policy import (
    ALL_SCOPES,
    HiddenPathError,
    ScopeDeniedError,
    SymlinkEscapeError,
    TraversalError,
    WorkspacePolicy,
    WorkspacePolicyError,
)


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace with project structure."""
    # Project A: normal project
    project_a = tmp_path / "project-a"
    project_a.mkdir()
    (project_a / "src").mkdir()
    (project_a / "src" / "main.py").write_text("print('hello')")
    (project_a / "README.md").write_text("# Project A")
    (project_a / ".env").write_text("SECRET=abc123")
    (project_a / ".env.production").write_text("SECRET=prod")
    (project_a / "id_rsa").write_text("private-key-data")

    # Hidden dir
    hidden_dir = project_a / ".ssh"
    hidden_dir.mkdir()
    (hidden_dir / "authorized_keys").write_text("ssh-rsa AAAA...")

    # Project B: separate project
    project_b = tmp_path / "project-b"
    project_b.mkdir()
    (project_b / "lib").mkdir()
    (project_b / "lib" / "utils.py").write_text("# utils")

    # Symlink that escapes
    escape_link = project_a / "escape_link"
    escape_link.symlink_to(tmp_path / "project-b")

    # Symlink inside project (valid)
    safe_link = project_a / "safe_link"
    safe_link.symlink_to(project_a / "src")

    # Symlink to system path
    system_link = project_a / "system_link"
    system_link.symlink_to(Path("/etc/passwd"))

    allowed_roots = [tmp_path]
    project_roots = {
        "project-a": project_a,
        "project-b": project_b,
    }

    return {
        "tmp_path": tmp_path,
        "project_a": project_a,
        "project_b": project_b,
        "project_roots": project_roots,
        "allowed_roots": allowed_roots,
    }


@pytest.fixture
def full_access_policy(workspace):
    """Policy with all scopes."""
    return WorkspacePolicy(
        project_roots=workspace["project_roots"],
        allowed_roots=workspace["allowed_roots"],
        granted_scopes=ALL_SCOPES,
    )


@pytest.fixture
def read_only_policy(workspace):
    """Policy with only read scope."""
    return WorkspacePolicy(
        project_roots=workspace["project_roots"],
        allowed_roots=workspace["allowed_roots"],
        granted_scopes={"project:read"},
    )


@pytest.fixture
def write_only_policy(workspace):
    """Policy with only write scope (implies read)."""
    return WorkspacePolicy(
        project_roots=workspace["project_roots"],
        allowed_roots=workspace["allowed_roots"],
        granted_scopes={"project:write"},
    )


# ── Traversal Tests ──────────────────────────────────────────────


class TestTraversal:
    def test_reject_dotdot_prefix(self, full_access_policy):
        with pytest.raises(TraversalError):
            full_access_policy.validate_read("project-a", "../etc/passwd")

    def test_reject_dotdot_middle(self, full_access_policy):
        with pytest.raises(TraversalError):
            full_access_policy.validate_read("project-a", "src/../../etc/shadow")

    def test_reject_dotdot_suffix(self, full_access_policy):
        with pytest.raises(TraversalError):
            full_access_policy.validate_write("project-a", "src/..")

    def test_accept_normal_path(self, full_access_policy):
        path = full_access_policy.validate_read("project-a", "src/main.py")
        assert path.name == "main.py"

    def test_reject_write_traversal(self, full_access_policy):
        with pytest.raises(TraversalError):
            full_access_policy.validate_write("project-a", "../other/file.py")


# ── Symlink Tests ────────────────────────────────────────────────


class TestSymlinkEscape:
    def test_reject_symlink_outside_project(self, full_access_policy):
        with pytest.raises(SymlinkEscapeError):
            full_access_policy.validate_read("project-a", "escape_link")

    def test_reject_symlink_to_system_path(self, full_access_policy):
        with pytest.raises(SymlinkEscapeError, match="outside project root"):
            full_access_policy.validate_read("project-a", "system_link")

    def test_accept_symlink_inside_project(self, full_access_policy):
        path = full_access_policy.validate_read("project-a", "safe_link/main.py")
        assert path.exists()

    def test_reject_write_through_symlink_escape(self, full_access_policy):
        with pytest.raises(SymlinkEscapeError):
            full_access_policy.validate_write("project-a", "escape_link/secret.txt")


# ── Hidden / Secret Path Tests ───────────────────────────────────


class TestHiddenPaths:
    def test_reject_read_env_file(self, full_access_policy):
        with pytest.raises(HiddenPathError):
            full_access_policy.validate_read("project-a", ".env")

    def test_reject_read_env_production(self, full_access_policy):
        with pytest.raises(HiddenPathError):
            full_access_policy.validate_read("project-a", ".env.production")

    def test_reject_read_private_key(self, full_access_policy):
        with pytest.raises(HiddenPathError):
            full_access_policy.validate_read("project-a", "id_rsa")

    def test_reject_write_env_file(self, full_access_policy):
        with pytest.raises(HiddenPathError):
            full_access_policy.validate_write("project-a", ".env")

    def test_reject_write_to_hidden_dir(self, full_access_policy):
        with pytest.raises(HiddenPathError):
            full_access_policy.validate_write("project-a", ".ssh/new_key")

    def test_accept_normal_file(self, full_access_policy):
        path = full_access_policy.validate_read("project-a", "README.md")
        assert path.exists()


# ── Scope Tests ──────────────────────────────────────────────────


class TestScopes:
    def test_read_only_can_read(self, read_only_policy):
        path = read_only_policy.validate_read("project-a", "README.md")
        assert path.exists()

    def test_read_only_cannot_write(self, read_only_policy):
        with pytest.raises(ScopeDeniedError, match="project:write"):
            read_only_policy.validate_write("project-a", "new_file.py")

    def test_read_only_cannot_execute(self, read_only_policy):
        with pytest.raises(ScopeDeniedError, match="project:execute"):
            read_only_policy.validate_execute("project-a")

    def test_read_only_cannot_docker(self, read_only_policy):
        with pytest.raises(ScopeDeniedError, match="project:docker"):
            read_only_policy.validate_docker("project-a")

    def test_write_implies_read(self, write_only_policy):
        path = write_only_policy.validate_read("project-a", "README.md")
        assert path.exists()

    def test_write_can_write(self, write_only_policy):
        path = write_only_policy.validate_write("project-a", "new_file.py")
        assert "new_file.py" in str(path)

    def test_write_cannot_execute(self, write_only_policy):
        with pytest.raises(ScopeDeniedError, match="project:execute"):
            write_only_policy.validate_execute("project-a")

    def test_execute_implies_write_and_read(self, workspace):
        policy = WorkspacePolicy(
            project_roots=workspace["project_roots"],
            allowed_roots=workspace["allowed_roots"],
            granted_scopes={"project:execute"},
        )
        # Can read
        path = policy.validate_read("project-a", "README.md")
        assert path.exists()

        # Can write
        path = policy.validate_write("project-a", "execute_written.py")
        assert "execute_written.py" in str(path)

    def test_docker_implies_all_project_scopes(self, workspace):
        policy = WorkspacePolicy(
            project_roots=workspace["project_roots"],
            allowed_roots=workspace["allowed_roots"],
            granted_scopes={"project:docker"},
        )
        # Can read, write, execute
        policy.validate_read("project-a", "README.md")
        policy.validate_write("project-a", "docker_written.py")
        policy.validate_execute("project-a")


# ── Project Root Validation ──────────────────────────────────────


class TestProjectRoot:
    def test_reject_unknown_project(self, full_access_policy):
        with pytest.raises(WorkspacePolicyError, match="Unknown project"):
            full_access_policy.validate_read("nonexistent", "file.py")

    def test_validate_project_exists(self, full_access_policy):
        path = full_access_policy.validate_read("project-a", "README.md")
        assert path.parent.name == "project-a"


# ── Allowed Roots Tests ──────────────────────────────────────────


class TestAllowedRoots:
    def test_reject_path_outside_allowed_roots(self, workspace):
        # Create a file outside allowed roots
        outside = Path("/tmp/outside_test_file.txt")
        try:
            outside.write_text("test")
            policy = WorkspacePolicy(
                project_roots=workspace["project_roots"],
                allowed_roots=workspace["allowed_roots"],
                granted_scopes=ALL_SCOPES,
            )
            # Symlink to outside path
            link = workspace["project_a"] / "outside_link"
            link.symlink_to(outside)
            with pytest.raises(SymlinkEscapeError):
                policy.validate_read("project-a", "outside_link")
        finally:
            outside.unlink(missing_ok=True)


# ── Scope Hierarchy Tests ────────────────────────────────────────


class TestScopeHierarchy:
    def test_project_docker_implies_execute(self, workspace):
        policy = WorkspacePolicy(
            project_roots=workspace["project_roots"],
            allowed_roots=workspace["allowed_roots"],
            granted_scopes={"project:docker"},
        )
        # Should not raise
        policy.validate_execute("project-a")

    def test_project_execute_implies_write(self, workspace):
        policy = WorkspacePolicy(
            project_roots=workspace["project_roots"],
            allowed_roots=workspace["allowed_roots"],
            granted_scopes={"project:execute"},
        )
        # Should not raise
        policy.validate_write("project-a", "new.py")

    def test_project_write_implies_read(self, workspace):
        policy = WorkspacePolicy(
            project_roots=workspace["project_roots"],
            allowed_roots=workspace["allowed_roots"],
            granted_scopes={"project:write"},
        )
        # Should not raise
        policy.validate_read("project-a", "README.md")


# ── Edge Cases ───────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_relative_path(self, full_access_policy):
        with pytest.raises(TraversalError):
            full_access_policy.validate_read("project-a", "")

    def test_absolute_path_rejected(self, full_access_policy):
        with pytest.raises(TraversalError, match="Absolute path not allowed"):
            full_access_policy.validate_read("project-a", "/etc/passwd")

    def test_absolute_nonexisting_path_rejected(self, full_access_policy):
        with pytest.raises(TraversalError, match="Absolute path not allowed"):
            full_access_policy.validate_read("project-a", "/tmp/nonexistent_policy_probe")

    def test_tilde_rejected(self, full_access_policy):
        with pytest.raises(TraversalError, match="Tilde path not allowed"):
            full_access_policy.validate_read("project-a", "~/secret")

    def test_tilde_user_rejected(self, full_access_policy):
        with pytest.raises(TraversalError, match="Tilde path not allowed"):
            full_access_policy.validate_read("project-a", "~root/.ssh/id_rsa")

    def test_validate_execute_with_path(self, full_access_policy):
        path = full_access_policy.validate_execute("project-a", "src/main.py")
        assert path.name == "main.py"

    def test_validate_execute_without_path(self, full_access_policy):
        path = full_access_policy.validate_execute("project-a")
        assert path is None

    def test_validate_docker_returns_project_root(self, full_access_policy, workspace):
        path = full_access_policy.validate_docker("project-a")
        assert path == workspace["project_a"].resolve()

    def test_no_scopes_deny_all(self, workspace):
        policy = WorkspacePolicy(
            project_roots=workspace["project_roots"],
            allowed_roots=workspace["allowed_roots"],
            granted_scopes=set(),
        )
        with pytest.raises(ScopeDeniedError):
            policy.validate_read("project-a", "README.md")
