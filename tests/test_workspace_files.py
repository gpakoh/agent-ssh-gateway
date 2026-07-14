"""Unit tests for workspace file read and find — all using temp directories."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace.models import ProjectInfo
from app.workspace.policy import (
    HiddenPathError,
    TraversalError,
    WorkspacePolicyError,
)
from app.workspace.registry import WorkspaceRegistry


@pytest.fixture()
def tmp_project(tmp_path: Path):
    """Create a temporary project tree with files for testing."""
    # Create directories
    src = tmp_path / "src"
    src.mkdir()
    sub = src / "sub"
    sub.mkdir()

    # Create text files
    (tmp_path / "README.md").write_text("# Hello\nWorld\n")
    (src / "main.py").write_text("import os\nprint('hello')\n")
    (src / "sub" / "helper.py").write_text("def helper():\n    pass\n")

    # Create a binary file
    (src / "data.bin").write_bytes(b"\x00\x01\x02\x03" * 100)

    # Create hidden/secret files
    (tmp_path / ".env").write_text("SECRET=y\n")
    (tmp_path / "id_rsa").write_text("fake-key\n")
    (tmp_path / "server.pem").write_text("fake-cert\n")

    # Create vendor/cache dirs
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("module.exports={}\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00" * 10)

    # Create a large file
    (src / "large.py").write_text("\n".join(f"line {i}" for i in range(1, 1001)) + "\n")

    return tmp_project_inner(tmp_path, src)


class tmp_project_inner:
    """Wrapper that stores paths."""
    def __init__(self, root: Path, src: Path):
        self.root = root
        self.src = src


def _make_registry(project_root: Path, project_id: str = "test-project") -> WorkspaceRegistry:
    """Create a WorkspaceRegistry pointing at a temp directory."""
    info = ProjectInfo(
        project_id=project_id,
        root=project_root,
        type="test",
        description="test project",
        tags=["test"],
    )
    return WorkspaceRegistry(
        projects={project_id: info},
        allowed_roots=[project_root.parent],
        granted_scopes={"project:read", "workspace:read"},
    )


class TestProjectFileRead:
    def test_file_read_valid_text(self, tmp_project):
        registry = _make_registry(tmp_project.root)

        from app.workspace.files import project_file_read

        out = project_file_read("test-project", "README.md", registry=registry)
        assert out["project_id"] == "test-project"
        assert out["path"] == "README.md"
        assert out["type"] == "file"
        assert out["encoding"] == "utf-8"
        assert "# Hello" in out["content"]
        assert out["truncated"] is False
        assert out["start_line"] == 1
        assert out["end_line"] == 2

    def test_file_read_line_slice(self, tmp_project):
        from app.workspace.files import project_file_read

        registry = _make_registry(tmp_project.root)
        out = project_file_read(
            "test-project", "src/large.py",
            start_line=10, max_lines=5,
            registry=registry,
        )
        lines = out["content"].splitlines()
        assert len(lines) == 5
        assert lines[0] == "line 10"
        assert lines[4] == "line 14"
        assert out["start_line"] == 10
        assert out["end_line"] == 14

    def test_file_read_hidden_secret_rejected(self, tmp_project):
        from app.workspace.files import project_file_read

        registry = _make_registry(tmp_project.root)
        with pytest.raises(HiddenPathError):
            project_file_read("test-project", ".env", registry=registry)

        with pytest.raises(HiddenPathError):
            project_file_read("test-project", "id_rsa", registry=registry)

        with pytest.raises(HiddenPathError):
            project_file_read("test-project", "server.pem", registry=registry)

    def test_file_read_absolute_tilde_traversal_rejected(self, tmp_project):
        from app.workspace.files import project_file_read

        registry = _make_registry(tmp_project.root)
        with pytest.raises(TraversalError):
            project_file_read("test-project", "/etc/passwd", registry=registry)

        with pytest.raises(TraversalError):
            project_file_read("test-project", "~/secret", registry=registry)

        with pytest.raises(TraversalError):
            project_file_read("test-project", "../outside", registry=registry)

    def test_file_read_binary_rejected(self, tmp_project):
        from app.workspace.files import project_file_read

        registry = _make_registry(tmp_project.root)
        with pytest.raises(WorkspacePolicyError, match="Binary content"):
            project_file_read("test-project", "src/data.bin", registry=registry)

    def test_file_read_not_found(self, tmp_project):
        from app.workspace.files import project_file_read

        registry = _make_registry(tmp_project.root)
        with pytest.raises(WorkspacePolicyError, match="File not found"):
            project_file_read("test-project", "nonexistent.py", registry=registry)

    def test_file_read_directory_rejected(self, tmp_project):
        from app.workspace.files import project_file_read

        registry = _make_registry(tmp_project.root)
        with pytest.raises(WorkspacePolicyError, match="directory"):
            project_file_read("test-project", "src", registry=registry)


class TestProjectFindFiles:
    def test_find_files_basic(self, tmp_project):
        from app.workspace.files import project_find_files

        registry = _make_registry(tmp_project.root)
        out = project_find_files("test-project", pattern="*.py", registry=registry)
        paths = [r["path"] for r in out["results"]]
        assert "src/main.py" in paths
        assert "src/sub/helper.py" in paths
        # README.md should not be in results
        assert not any("README" in p for p in paths)

    def test_find_files_skips_secrets_and_ignores(self, tmp_project):
        from app.workspace.files import project_find_files

        registry = _make_registry(tmp_project.root)
        out = project_find_files("test-project", pattern="*", registry=registry)
        paths = [r["path"] for r in out["results"]]

        # Hidden/secret paths should be skipped
        assert not any("id_rsa" in p for p in paths)
        assert not any("server.pem" in p for p in paths)
        assert not any(".env" in p for p in paths)

        # Vendor/cache should be skipped
        assert not any("node_modules" in p for p in paths)
        assert not any("__pycache__" in p for p in paths)

    def test_find_files_truncated(self, tmp_project):
        from app.workspace.files import project_find_files

        registry = _make_registry(tmp_project.root)
        out = project_find_files("test-project", pattern="*", max_results=2, registry=registry)
        assert out["truncated"] is True
        assert len(out["results"]) == 2

    def test_find_files_subdirectory(self, tmp_project):
        from app.workspace.files import project_find_files

        registry = _make_registry(tmp_project.root)
        out = project_find_files("test-project", pattern="*", relative_path="src", registry=registry)
        paths = [r["path"] for r in out["results"]]
        assert all(p.startswith("src/") for p in paths)

    def test_find_files_root_default(self, tmp_project):
        from app.workspace.files import project_find_files

        registry = _make_registry(tmp_project.root)
        out = project_find_files("test-project", pattern="README*", registry=registry)
        assert len(out["results"]) >= 1
        assert out["results"][0]["path"] == "README.md"

    def test_find_files_symlink_escape_skipped(self, tmp_project):
        from app.workspace.files import project_find_files

        # Create a symlink that escapes the project
        link = tmp_project.root / "escape_link"
        link.symlink_to("/tmp")
        registry = _make_registry(tmp_project.root)
        out = project_find_files("test-project", pattern="*", registry=registry)
        paths = [r["path"] for r in out["results"]]
        assert not any("escape_link" in p for p in paths)
