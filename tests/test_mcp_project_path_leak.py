"""Regression tests for absolute host path leaking through MCP tool responses.

list_tree(), tree(), list_files(), and info() all returned the real,
absolute host filesystem path (e.g. "/media/1TB/Python/...") in their
"root" (and info()'s "resolved_path") field, exposing internal server
layout to any MCP client. Entries within the tree/file list were already
relative — only the root/resolved_path fields leaked the real path.

Fixed by returning "." (a project-relative marker, not the real path)
for these fields; the caller already has the logical project id via the
"project" field for identifying which project a response belongs to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(EXAMPLES_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mcp_client_tools import info, list_files, list_tree, tree  # noqa: E402

import app.workspace.registry as registry_module  # noqa: E402
from app.workspace.registry import WorkspaceRegistry, reset_registry  # noqa: E402


@pytest.fixture
def real_registry(tmp_path):
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
    description: path leak test project
    tags: []
"""
    )
    reset_registry()
    registry = WorkspaceRegistry.load(yaml_path)
    registry_module._registry = registry
    yield tmp_path
    reset_registry()


class TestNoAbsolutePathLeak:
    def test_list_tree_root_is_not_absolute_path(self, real_registry):
        result = list_tree(None, "demo")
        assert result["root"] == "."
        assert str(real_registry) not in json.dumps(result)

    def test_tree_root_is_not_absolute_path(self, real_registry):
        result = tree(None, "demo")
        assert result["root"] == "."
        assert str(real_registry) not in json.dumps(result)

    def test_list_files_root_is_not_absolute_path(self, real_registry):
        result = list_files(None, "demo", "*.py")
        assert result["root"] == "."
        assert str(real_registry) not in json.dumps(result)

    def test_info_root_and_resolved_path_are_not_absolute(self, real_registry):
        result = info(None, "demo")
        assert result["root"] == "."
        assert result["resolved_path"] == "."
        assert str(real_registry) not in json.dumps(result)

    def test_list_tree_entries_still_relative_and_correct(self, real_registry):
        """The fix must not affect the (already-relative) entries themselves."""
        result = list_tree(None, "demo")
        assert "a.py" in result["entries"]
        assert "sub/b.py" in result["entries"]

    def test_nested_relative_entry_has_no_leading_slash_or_host_path(self, real_registry):
        result = list_files(None, "demo", "**/*.py")
        for f in result["files"]:
            assert not f.startswith("/")
        assert str(real_registry) not in json.dumps(result)
