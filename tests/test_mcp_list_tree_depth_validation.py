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
