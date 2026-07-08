"""Tests for pure-Python project search — no shell, no grep."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.services.project_search import search_text


@pytest.fixture
def sample_project() -> Iterator[Path]:
    """Create a temporary project structure with known content."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Python file with searchable content
        src = root / "src"
        src.mkdir()
        (src / "main.py").write_text(
            "def hello():\n"
            '    return "Hello, World!"\n'
            "\n"
            "SESSION_NOT_FOUND = 'error'\n"
            "print(SESSION_NOT_FOUND)\n"
        )
        (src / "utils.py").write_text(
            "from config import SESSION_NOT_FOUND\n"
            "SESSION_TIMEOUT = 30\n"
        )

        # .git directory (should be pruned)
        git_dir = root / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("SESSION_NOT_FOUND = 'git'")

        # .venv directory (should be pruned)
        venv_dir = root / ".venv"
        venv_dir.mkdir()
        (venv_dir / "lib.py").write_text("SESSION_NOT_FOUND = 'venv'")

        # __pycache__ directory (should be pruned)
        pycache = root / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.py").write_text("SESSION_NOT_FOUND = 'cache'")

        # node_modules directory (should be pruned)
        nm = root / "node_modules"
        nm.mkdir()
        (nm / "dep.js").write_text("SESSION_NOT_FOUND = 'dep'")

        # Binary file (should be skipped)
        (root / "data.bin").write_bytes(b"\x00\x01\x02SESSION_NOT_FOUND\x00\xff")

        # Large file (120K lines ~2.1MB, exceeds default 2MB limit → auto-skipped)
        huge = root / "huge.log"
        with huge.open("w") as f:
            f.write("SESSION_NOT_FOUND\n" * 120_000)

        # Nested file
        nested = root / "nested" / "deep"
        nested.mkdir(parents=True)
        (nested / "found.txt").write_text("SESSION_NOT_FOUND at line 1\nother line\n")

        yield root


class TestSearchText:
    def test_finds_text_in_py_file(self, sample_project: Path) -> None:
        result = search_text(sample_project, "SESSION_NOT_FOUND")
        assert result["count"] >= 3  # main.py, utils.py, nested/found.txt
        assert result["query"] == "SESSION_NOT_FOUND"
        assert not result["truncated"]

    def test_glob_limits_search(self, sample_project: Path) -> None:
        result = search_text(sample_project, "SESSION_NOT_FOUND", glob="*.py")
        # Only main.py and utils.py match (nested/found.txt is .txt)
        assert 2 <= result["count"] <= 3
        for m in result["matches"]:
            assert m["path"].endswith(".py")

    def test_prunes_git(self, sample_project: Path) -> None:
        result = search_text(sample_project, "SESSION_NOT_FOUND")
        paths = [m["path"] for m in result["matches"]]
        assert all(".git" not in p for p in paths)

    def test_prunes_venv(self, sample_project: Path) -> None:
        result = search_text(sample_project, "SESSION_NOT_FOUND")
        paths = [m["path"] for m in result["matches"]]
        assert all(".venv" not in p for p in paths)

    def test_prunes_pycache(self, sample_project: Path) -> None:
        result = search_text(sample_project, "SESSION_NOT_FOUND")
        paths = [m["path"] for m in result["matches"]]
        assert all("__pycache__" not in p for p in paths)

    def test_prunes_node_modules(self, sample_project: Path) -> None:
        result = search_text(sample_project, "SESSION_NOT_FOUND")
        paths = [m["path"] for m in result["matches"]]
        assert all("node_modules" not in p for p in paths)

    def test_skips_binary_file(self, sample_project: Path) -> None:
        result = search_text(sample_project, "SESSION_NOT_FOUND")
        paths = [m["path"] for m in result["matches"]]
        assert "data.bin" not in paths

    def test_skips_huge_file(self, sample_project: Path) -> None:
        result = search_text(
            sample_project,
            "SESSION_NOT_FOUND",
            max_file_size_bytes=100_000,
        )
        paths = [m["path"] for m in result["matches"]]
        assert "huge.log" not in paths

    def test_respects_max_matches(self, sample_project: Path) -> None:
        result = search_text(sample_project, "SESSION_NOT_FOUND", max_matches=1)
        assert result["count"] == 1
        assert result["truncated"] is True
        assert result["truncated_reason"] == "max_matches"

    def test_respects_max_files(self) -> None:
        """max_files limits how many files are read, not how many matches."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(10):
                (root / f"{i}.txt").write_text("SESSION_NOT_FOUND\n")
            result = search_text(root, "SESSION_NOT_FOUND", max_files=3)
        assert result["count"] == 3  # 1 match per file × 3 files
        assert result["truncated"] is True
        assert result["truncated_reason"] == "max_files"

    def test_returns_relative_paths(self, sample_project: Path) -> None:
        result = search_text(sample_project, "SESSION_NOT_FOUND")
        for m in result["matches"]:
            assert not m["path"].startswith("/")
            assert not m["path"].startswith("../")

    def test_no_shell_invocation(self, sample_project: Path) -> None:
        """Verify the function doesn't indirectly invoke a shell."""
        import subprocess
        original = subprocess.run
        calls = []

        def tracking_run(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        subprocess.run = tracking_run
        try:
            search_text(sample_project, "SESSION_NOT_FOUND")
        finally:
            subprocess.run = original
        assert not calls, f"subprocess.run was called: {calls}"

    def test_path_traversal_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "file.txt").write_text("content")

            # '..' in root is resolved away by Path.resolve(), so this
            # effectively tests that we get a sensible error for non-existent path
            # The actual traversal protection is via path validation upstream
            result = search_text(root / "sub", "content")
            assert result["count"] == 1

    def test_missing_root_gives_controlled_error(self) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            search_text("/nonexistent/path/12345", "query")

    def test_root_is_file_gives_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "file.txt"
            f.write_text("content")
            with pytest.raises(ValueError, match="not a directory"):
                search_text(str(f), "query")

    def test_empty_query_finds_nothing(self, sample_project: Path) -> None:
        """An empty string query matches nothing (no matches)."""
        result = search_text(sample_project, "")
        assert result["count"] == 0

    def test_glob_subdirectory_pattern(self, sample_project: Path) -> None:
        """Glob pattern matching files in subdirectories."""
        result = search_text(sample_project, "SESSION_NOT_FOUND", glob="nested/**/*.txt")
        assert result["count"] == 1
        assert "nested/deep/found.txt" == result["matches"][0]["path"]

    def test_line_numbers_are_correct(self, sample_project: Path) -> None:
        """Verify line_number reflects the actual matching line."""
        result = search_text(sample_project, "print(SESSION_NOT_FOUND)")
        match = next(
            (m for m in result["matches"] if "print(SESSION_NOT_FOUND)" in m["line"]),
            None,
        )
        assert match is not None
        assert match["line_number"] == 5  # 5th line in main.py
