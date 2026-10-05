"""Contract tests for examples.mcp_server.config project-root allowlist.

The allowlist is a security boundary: every project path must resolve under
one of these roots. The roots are deployment configuration, so there is no
built-in default -- a missing or blank MCP_ALLOWED_PROJECT_ROOTS must fail
closed instead of silently falling back to a host-specific layout.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
CONFIG_PATH = EXAMPLES_DIR / "mcp_server" / "config.py"
ENV_NAME = "MCP_ALLOWED_PROJECT_ROOTS"


def _reload_config(monkeypatch, value):
    """Reload the config module with a controlled environment."""
    if value is None:
        monkeypatch.delenv(ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(ENV_NAME, value)
    sys_modules = importlib.import_module("examples.mcp_server.config")
    return importlib.reload(sys_modules)


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch):
    """Leave the imported module as-is for the rest of the suite."""
    yield
    monkeypatch.setenv(ENV_NAME, "/var/www/")
    module = importlib.import_module("examples.mcp_server.config")
    importlib.reload(module)


@pytest.mark.parametrize("value", [None, "", "   ", ",", " , , "])
def test_project_roots_fail_closed_without_configuration(monkeypatch, value):
    with pytest.raises(RuntimeError, match=ENV_NAME):
        _reload_config(monkeypatch, value)


def test_project_roots_parse_comma_separated_list(monkeypatch):
    module = _reload_config(monkeypatch, "/srv/projects/ , /var/www/,, /opt/app ")
    assert module.ALLOWED_PROJECT_ROOTS == ["/srv/projects/", "/var/www/", "/opt/app"]


def test_project_roots_contain_no_built_in_host_layout():
    """No host-specific mount may reappear as a default."""
    source = CONFIG_PATH.read_text(encoding="utf-8")
    assert "/media/1TB" not in source, "host mount layout must not be committed"
    assert "_ALLOWED_PROJECT_ROOTS_DEFAULT" not in source, "there must be no default list"