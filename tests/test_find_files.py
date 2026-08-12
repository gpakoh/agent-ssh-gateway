"""Tests for _safe_glob — the safe glob implementation."""

import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
sys.path.insert(0, str(EXAMPLE_DIR))

from mcp_client_tools import _safe_glob  # noqa: E402


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


def test_mcp_file_and_tree_helpers_prune_agent_runtime(monkeypatch, tmp_path):
    from examples.mcp_server import mcp_client_tools as tools

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "live.py").write_text("pass\n", encoding="utf-8")
    stale = tmp_path / ".ai-bridge" / "worktrees" / "old"
    stale.mkdir(parents=True)
    (stale / "stale.py").write_text("pass\n", encoding="utf-8")
    egg = tmp_path / "old.egg-info"
    egg.mkdir()
    (egg / "metadata.py").write_text("pass\n", encoding="utf-8")

    monkeypatch.setattr(tools, "_resolve_project", lambda project: tmp_path)

    found = tools.find_files("demo", "**/*.py")
    assert found["result"]["files"] == ["src/live.py"]
    listed = tools.list_files(None, "demo", "*.py")
    assert listed["files"] == ["src/live.py"]
    tree = tools.list_tree(None, "demo", depth=5)
    assert all(".ai-bridge" not in entry for entry in tree["entries"])
    assert all(".egg-info" not in entry for entry in tree["entries"])


def test_mcp_pruned_walk_never_descends_runtime_dirs(monkeypatch, tmp_path):
    from examples.mcp_server import mcp_client_tools as tools

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "live.py").write_text("pass\n", encoding="utf-8")
    runtime = tmp_path / ".ai-bridge" / "worktrees" / "old"
    runtime.mkdir(parents=True)
    (runtime / "stale.py").write_text("pass\n", encoding="utf-8")
    egg = tmp_path / "old.egg-info"
    egg.mkdir()
    (egg / "metadata.py").write_text("pass\n", encoding="utf-8")

    real_walk = tools.os.walk
    visited: list[Path] = []

    def tracking_walk(*args, **kwargs):
        for dirpath, dirnames, filenames in real_walk(*args, **kwargs):
            visited.append(Path(dirpath))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(tools.os, "walk", tracking_walk)
    paths = list(tools._iter_pruned_paths(tmp_path))

    assert tmp_path / "src" in paths
    assert all(".ai-bridge" not in path.parts for path in visited)
    assert all(not any(part.endswith(".egg-info") for part in path.parts) for path in visited)
