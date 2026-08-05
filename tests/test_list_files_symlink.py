"""Tests for mcp_client_tools.list_files — symlink-escape safety.

Regression: list_files() used project_dir.rglob(pattern) — which follows
symlinks when the pattern explicitly names a symlinked path segment (e.g.
pattern="some_symlink/*"), even though it doesn't descend into them for
bare "*"/"**" wildcards — and computed containment via
p.relative_to(project_dir) on the unresolved path, purely structural. It
never resolved the path to see where the symlink actually points. Same
bug class as app/workspace/search.py, app/workspace/scan_project.py, and
app/services/project_search.py fixed earlier this session. Reachable live
via the list_files MCP tool (server.py's gateway_list_files).
_safe_glob() (used by find_files, see test_find_files.py) already does
this correctly — resolves before checking containment.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
sys.path.insert(0, str(EXAMPLE_DIR))

from mcp_client_tools import list_files  # noqa: E402


def test_glob_naming_symlink_directly_does_not_leak_filenames(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret_db_backup.sql").write_text("CREATE TABLE users...")

    (project / "escape_link").symlink_to(outside)

    monkeypatch.setattr("mcp_client_tools._resolve_project", lambda name: project)
    monkeypatch.setattr("mcp_client_tools._validate_project", lambda name: name)

    result = list_files(MagicMock(), "project", "escape_link/*")
    assert result["files"] == []
    assert result["count"] == 0


def test_normal_pattern_still_works(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    sub = project / "sub"
    sub.mkdir()
    (sub / "a.py").write_text("x")

    monkeypatch.setattr("mcp_client_tools._resolve_project", lambda name: project)
    monkeypatch.setattr("mcp_client_tools._validate_project", lambda name: name)

    result = list_files(MagicMock(), "project", "sub/*")
    assert result["files"] == ["sub/a.py"]
    assert result["count"] == 1
