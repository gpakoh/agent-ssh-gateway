"""Regression tests for list_tree()/tree() silently clamping an invalid depth.

list_tree(depth=0) and list_tree(depth=-1) used to return a *successful*
result with depth silently substituted to 1 (`min(max(depth, 1), 5)`),
giving the caller no signal that their input was out of range. Same bug
in tree(). Fixed by validating depth explicitly and raising ValueError
for anything outside [DEPTH_MIN, DEPTH_MAX] or non-integer input, instead
of silently clamping.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(EXAMPLES_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mcp_client_tools import DEPTH_MAX, DEPTH_MIN, list_tree, tree  # noqa: E402

import app.workspace.registry as registry_module  # noqa: E402
from app.workspace.registry import WorkspaceRegistry, reset_registry  # noqa: E402


@pytest.fixture
def real_registry(tmp_path):
    """A real WorkspaceRegistry with one real project directory and a few files."""
    project_root = tmp_path / "demo-project"
    project_root.mkdir()
    (project_root / "a.py").write_text("x = 1\n")
    sub = project_root / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("y = 2\n")

    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(
        f"""
registry_root: {tmp_path}
projects:
  demo:
    root: demo-project
    type: python
    description: depth validation test project
    tags: []
"""
    )
    reset_registry()
    registry = WorkspaceRegistry.load(yaml_path)
    registry_module._registry = registry
    yield project_root
    reset_registry()


class TestListTreeDepthValidation:
    def test_zero_depth_raises(self, real_registry):
        with pytest.raises(ValueError, match="depth"):
            list_tree(None, "demo", depth=0)

    def test_negative_depth_raises(self, real_registry):
        with pytest.raises(ValueError, match="depth"):
            list_tree(None, "demo", depth=-1)

    def test_above_max_depth_raises(self, real_registry):
        with pytest.raises(ValueError, match="depth"):
            list_tree(None, "demo", depth=DEPTH_MAX + 1)

    def test_non_integer_depth_raises(self, real_registry):
        with pytest.raises(ValueError, match="depth"):
            list_tree(None, "demo", depth=2.5)  # type: ignore[arg-type]

    def test_valid_min_depth_passes_through_unchanged(self, real_registry):
        result = list_tree(None, "demo", depth=DEPTH_MIN)
        assert result["depth"] == DEPTH_MIN

    def test_valid_max_depth_passes_through_unchanged(self, real_registry):
        result = list_tree(None, "demo", depth=DEPTH_MAX)
        assert result["depth"] == DEPTH_MAX

    def test_default_depth_still_works(self, real_registry):
        result = list_tree(None, "demo")
        assert result["depth"] == 2
        assert "a.py" in result["entries"]


class TestTreeDepthValidation:
    def test_zero_depth_raises(self, real_registry):
        with pytest.raises(ValueError, match="depth"):
            tree(None, "demo", depth=0)

    def test_negative_depth_raises(self, real_registry):
        with pytest.raises(ValueError, match="depth"):
            tree(None, "demo", depth=-1)

    def test_valid_depth_passes_through_unchanged(self, real_registry):
        result = tree(None, "demo", depth=DEPTH_MIN)
        assert result["depth"] == DEPTH_MIN


class TestWorkingDirectoryValidation:
    def test_existing_project_returns_relative_root(self, real_registry):
        from mcp_client_tools import working_directory

        result = working_directory(None, "demo")
        assert result["outcome"] == "passed"
        assert result["stdout"] == "."

    def test_missing_project_raises_project_not_found(self, real_registry):
        from gateway_client import GatewayClientError
        from mcp_client_tools import working_directory

        with pytest.raises(GatewayClientError) as exc:
            working_directory(None, "no-such-project")
        assert exc.value.status_code == 404


class TestTreeGlobValidation:
    def test_glob_wildcard_accepts_py(self, real_registry):
        result = tree(None, "demo", glob="*.py")
        assert result["count"] == 2
        assert "a.py" in result["entries"]
        assert "sub/b.py" in result["entries"]

    def test_glob_subdir_pattern_filters(self, real_registry):
        result = tree(None, "demo", glob="sub/*.py")
        assert result["count"] == 1
        assert "sub/b.py" in result["entries"]
        assert "a.py" not in result["entries"]

    def test_glob_question_mark(self, real_registry):
        result = tree(None, "demo", glob="?.py")
        assert "a.py" in result["entries"]
        assert "sub/b.py" in result["entries"]

    def test_glob_no_match(self, real_registry):
        result = tree(None, "demo", glob="*.txt")
        assert result["count"] == 0
        assert result["entries"] == []

    def test_glob_invalid_characters_rejected(self, real_registry):
        with pytest.raises(ValueError, match="glob"):
            tree(None, "demo", glob="*.py;rm -rf /")

    def test_glob_absolute_rejected(self, real_registry):
        with pytest.raises(ValueError, match="glob"):
            tree(None, "demo", glob="/etc/*.py")

    def test_glob_traversal_rejected(self, real_registry):
        with pytest.raises(ValueError, match="glob"):
            tree(None, "demo", glob="../*.py")

    def test_no_glob_still_works(self, real_registry):
        result = tree(None, "demo")
        assert result["count"] == 3


class TestListFilesTruncation:
    def test_many_files_marks_truncated(self, real_registry) -> None:
        from mcp_client_tools import list_files

        for i in range(210):
            (real_registry / f"bulk{i}.txt").write_text("x")

        result = list_files(None, "demo", "bulk*.txt")
        assert len(result["files"]) == 200
        assert result["truncated"] is True
        assert result["count"] == 200

    def test_few_files_not_truncated(self, real_registry) -> None:
        from mcp_client_tools import list_files

        result = list_files(None, "demo", "*.py")
        assert result["truncated"] is False
        assert result["count"] == len(result["files"])


class TestListTreeTruncation:
    def test_many_entries_marks_truncated(self, real_registry) -> None:
        from mcp_client_tools import list_tree

        for i in range(210):
            (real_registry / f"bulk{i}.txt").write_text("x")

        result = list_tree(None, "demo", depth=1)
        assert len(result["entries"]) == 200
        assert result["truncated"] is True
        assert result["count"] == 200

    def test_few_entries_not_truncated(self, real_registry) -> None:
        result = list_tree(None, "demo", depth=1)
        assert result["truncated"] is False
        assert result["count"] == len(result["entries"])

    def test_custom_max_results_slices(self, real_registry) -> None:
        result = list_tree(None, "demo", depth=1, max_results=1)
        assert len(result["entries"]) == 1
        assert result["truncated"] is True
        assert result["count"] == 1


class TestTreeTruncation:
    def test_many_entries_marks_truncated(self, real_registry) -> None:
        for i in range(210):
            (real_registry / f"bulk{i}.txt").write_text("x")

        result = tree(None, "demo", depth=1)
        assert len(result["entries"]) == 200
        assert result["truncated"] is True
        assert result["count"] == 200

    def test_few_entries_not_truncated(self, real_registry) -> None:
        result = tree(None, "demo", depth=1)
        assert result["truncated"] is False
        assert result["count"] == len(result["entries"])

    def test_custom_max_results_slices(self, real_registry) -> None:
        result = tree(None, "demo", depth=1, max_results=1)
        assert len(result["entries"]) == 1
        assert result["truncated"] is True
        assert result["count"] == 1


class TestSafeGlobTimeoutTruncation:
    def test_timeout_marks_truncated(self, real_registry, monkeypatch) -> None:
        from mcp_client_tools import _safe_glob

        calls = {"n": 0}

        def fake_monotonic() -> float:
            calls["n"] += 1
            return 0.0 if calls["n"] == 1 else 100.0

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        result = _safe_glob(real_registry, "*.py")
        assert result["truncated"] is True
        assert result["count"] == 0

    def test_no_timeout_not_truncated(self, real_registry) -> None:
        from mcp_client_tools import _safe_glob

        result = _safe_glob(real_registry, "*.py")
        assert result["truncated"] is False
        assert result["count"] == 1
