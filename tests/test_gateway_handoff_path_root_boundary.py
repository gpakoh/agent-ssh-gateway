"""Root containment for MCP handoff file paths uses path components, not prefixes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_SERVER_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
if str(MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER_DIR))

from gateway_client import GatewayClientError, resolve_file_path  # noqa: E402


@pytest.mark.parametrize(
    "path",
    [
        "/srv/project-evil/secrets",
        "/srv/project.backup/file",
        "/srv/projects/secret",
        "/srv/project1/file",
        "/srv/projection",
        "/srv/project_2026/file",
    ],
)
def test_sibling_with_matching_string_prefix_is_outside_root(
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    monkeypatch.setenv("MCP_GATEWAY_PROJECT_ROOT", "/srv/project")
    with pytest.raises(GatewayClientError, match="outside allowed"):
        resolve_file_path(path)


@pytest.mark.parametrize(
    ("path", "resolved"),
    [
        ("/srv/project", "/srv/project"),
        ("/srv/project/src/file.py", "/srv/project/src/file.py"),
        ("src/file.py", "/srv/project/src/file.py"),
        ("README.md", "/srv/project/README.md"),
        (".ai-bridge/current-plan.md", "/srv/project/.ai-bridge/current-plan.md"),
    ],
)
def test_exact_root_descendants_and_relative_paths_keep_contract(
    monkeypatch: pytest.MonkeyPatch, path: str, resolved: str,
) -> None:
    monkeypatch.setenv("MCP_GATEWAY_PROJECT_ROOT", "/srv/project///")
    assert resolve_file_path(path) == resolved


@pytest.mark.parametrize("path", ["/srv/project2/private", "../secret", "/srv/project/../secret"])
def test_invalid_path_error_never_echoes_user_path_or_root(
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    monkeypatch.setenv("MCP_GATEWAY_PROJECT_ROOT", "/srv/project")
    with pytest.raises(GatewayClientError) as error:
        resolve_file_path(path)
    assert path not in str(error.value)
    assert "/srv/project" not in str(error.value)


@pytest.mark.parametrize("path", [None, 123, {"name": "secret"}, ["file"]])
def test_wrong_type_is_rejected_before_string_methods(
    monkeypatch: pytest.MonkeyPatch, path: object,
) -> None:
    monkeypatch.setenv("MCP_GATEWAY_PROJECT_ROOT", "/srv/project")
    with pytest.raises(GatewayClientError, match="path must be a non-empty string"):
        resolve_file_path(path)  # type: ignore[arg-type]


def test_empty_root_keeps_existing_unconfined_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_GATEWAY_PROJECT_ROOT", raising=False)
    assert resolve_file_path("README.md") == "README.md"
    assert resolve_file_path("/srv/project-evil/file") == "/srv/project-evil/file"


def test_root_slash_allows_any_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_GATEWAY_PROJECT_ROOT", "/")
    assert resolve_file_path("/tmp/file") == "/tmp/file"
    assert resolve_file_path("README.md") == "/README.md"