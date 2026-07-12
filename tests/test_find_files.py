"""Tests for _safe_glob — the safe glob implementation."""

import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
sys.path.insert(0, str(EXAMPLE_DIR))

from chatgpt_tools import _safe_glob


def test_simple_glob(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("readme")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("main")
    result = _safe_glob(tmp_path, "*.md")
    assert result["files"] == ["README.md"]
    assert result["count"] == 1


def test_recursive_glob(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("index")
    (tmp_path / "docs" / "api.md").write_text("api")
    result = _safe_glob(tmp_path, "docs/**/*.md")
    assert result["count"] == 2


def test_dot_glob(tmp_path: Path) -> None:
    (tmp_path / "test_foo.py").write_text("")
    (tmp_path / "test_bar.py").write_text("")
    result = _safe_glob(tmp_path, "test_*.py")
    assert result["count"] == 2


def test_excludes_venv(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("main")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("lib")
    result = _safe_glob(tmp_path, "**/*.py")
    files = [f for f in result["files"] if ".venv" in f]
    assert len(files) == 0


def test_excludes_git(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "code.py").write_text("code")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("config")
    result = _safe_glob(tmp_path, "**/*.py")
    assert result["count"] == 1


def test_max_results_limit(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"file{i}.txt").write_text("x")
    result = _safe_glob(tmp_path, "*.txt", max_results=5)
    assert result["count"] == 5
    assert result["truncated"] is True


def test_traversal_blocked(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        _safe_glob(tmp_path, "../outside/*.md")


def test_absolute_pattern_blocked(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        _safe_glob(tmp_path, "/etc/*.conf")
