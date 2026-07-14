"""Tests for app.workspace.search — literal text search in workspace projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry
from app.workspace.search import SearchError, project_search_text

# ── Helpers ───────────────────────────────────────────────────────


def _make_registry(tmp: Path, projects: dict[str, Path] | None = None) -> WorkspaceRegistry:
    """Build a WorkspaceRegistry pointing at a temp directory."""
    if projects is None:
        projects = {"test-project": tmp}
    return WorkspaceRegistry(
        projects={
            pid: __import__("app.workspace.models", fromlist=["ProjectInfo"]).ProjectInfo(
                project_id=pid,
                root=root,
                type="test",
                description="",
                tags=[],
            )
            for pid, root in projects.items()
        },
        allowed_roots=[tmp],
        granted_scopes={"project:read", "workspace:read"},
    )


# ── Tests ─────────────────────────────────────────────────────────


class TestSearchTextLiteralMatch:
    def test_search_text_literal_match(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello world\nfoo bar\nhello again\n")
        (tmp_path / "b.py").write_text("# nothing here\n")
        reg = _make_registry(tmp_path)

        result = project_search_text("test-project", "hello", registry=reg)

        assert result["project_id"] == "test-project"
        assert result["query"] == "hello"
        assert result["truncated"] is False
        assert len(result["matches"]) == 2
        paths = {m["path"] for m in result["matches"]}
        assert "a.txt" in paths

    def test_search_text_case_insensitive(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("Foo BAR\n")
        reg = _make_registry(tmp_path)

        result = project_search_text("test-project", "foo", case_sensitive=False, registry=reg)
        assert len(result["matches"]) == 1
        assert result["matches"][0]["preview"] == "Foo BAR"

    def test_search_text_case_sensitive(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("Foo BAR\n")
        reg = _make_registry(tmp_path)

        result = project_search_text("test-project", "foo", case_sensitive=True, registry=reg)
        assert len(result["matches"]) == 0


class TestSearchTextContextLines:
    def test_search_text_context_lines(self, tmp_path: Path) -> None:
        lines = [f"line {i}" for i in range(10)]
        (tmp_path / "doc.txt").write_text("\n".join(lines) + "\n")
        reg = _make_registry(tmp_path)

        result = project_search_text(
            "test-project", "line 5", context_lines=2, registry=reg
        )
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert m["line"] == 6
        assert m["column"] == 1
        assert m["preview"] == "line 5"
        assert m["before"] == ["line 3", "line 4"]
        assert m["after"] == ["line 6", "line 7"]

    def test_search_text_context_at_boundary(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("first\nsecond\n")
        reg = _make_registry(tmp_path)

        result = project_search_text(
            "test-project", "first", context_lines=2, registry=reg
        )
        m = result["matches"][0]
        assert m["before"] == []
        assert m["after"] == ["second"]


class TestSearchTextSkipsBinaryAndSecrets:
    def test_search_text_skips_binary_and_secrets(self, tmp_path: Path) -> None:
        # Binary file with null byte
        (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02secret\x00")
        # .env file (hidden)
        (tmp_path / ".env").write_text("API_KEY=abc\n")
        # .env.production file (hidden)
        (tmp_path / ".env.production").write_text("SECRET=prod\n")
        # Normal file (no "secret" in it)
        (tmp_path / "clean.txt").write_text("nothing sensitive here\n")

        reg = _make_registry(tmp_path)
        result = project_search_text("test-project", "secret", registry=reg)

        # Should have 0 matches: binary skipped, .env files skipped
        assert len(result["matches"]) == 0

    def test_search_text_skips_vendor_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "dep.js").write_text("vendor code\n")
        (tmp_path / "src.js").write_text("real code\n")

        reg = _make_registry(tmp_path)
        result = project_search_text("test-project", "vendor code", registry=reg)
        assert len(result["matches"]) == 0

    def test_search_text_skips_pycache(self, tmp_path: Path) -> None:
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "mod.pyc").write_bytes(b"\x00\x00")
        (tmp_path / "mod.py").write_text("clean code\n")

        reg = _make_registry(tmp_path)
        result = project_search_text("test-project", "clean", registry=reg)
        assert len(result["matches"]) == 1


class TestSearchTextTruncated:
    def test_search_text_truncated(self, tmp_path: Path) -> None:
        # Create a file with many matches
        content = "\n".join(["match here" for _ in range(50)])
        (tmp_path / "big.txt").write_text(content + "\n")
        reg = _make_registry(tmp_path)

        result = project_search_text(
            "test-project", "match here", max_matches=5, registry=reg
        )
        assert result["truncated"] is True
        assert len(result["matches"]) == 5

    def test_search_text_max_matches_exact(self, tmp_path: Path) -> None:
        content = "\n".join(["target" for _ in range(3)])
        (tmp_path / "small.txt").write_text(content + "\n")
        reg = _make_registry(tmp_path)

        result = project_search_text(
            "test-project", "target", max_matches=10, registry=reg
        )
        assert result["truncated"] is False
        assert len(result["matches"]) == 3


class TestSearchTextEmptyQuery:
    def test_search_text_empty_query_rejected(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        with pytest.raises(SearchError, match="must not be empty"):
            project_search_text("test-project", "", registry=reg)

    def test_search_text_unknown_project(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        with pytest.raises(WorkspacePolicyError):
            project_search_text("nonexistent", "query", registry=reg)


class TestSearchTextGlob:
    def test_search_text_file_glob(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("target in py\n")
        (tmp_path / "b.txt").write_text("target in txt\n")
        reg = _make_registry(tmp_path)

        result = project_search_text(
            "test-project", "target", file_glob="*.py", registry=reg
        )
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "a.py"


class TestSearchTextRelativePath:
    def test_search_text_relative_path(self, tmp_path: Path) -> None:
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "a.py").write_text("deep match\n")
        (tmp_path / "a.py").write_text("root match\n")
        reg = _make_registry(tmp_path)

        result = project_search_text(
            "test-project", "match", relative_path="src", registry=reg
        )
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "src/a.py"

    def test_search_text_relative_path_not_dir(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("content\n")
        reg = _make_registry(tmp_path)
        with pytest.raises(SearchError, match="not a directory"):
            project_search_text(
                "test-project", "content", relative_path="file.txt", registry=reg
            )
