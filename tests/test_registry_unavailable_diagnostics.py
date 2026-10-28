"""Issue #703: stale source registry entries must remain diagnosable and reserved."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.workspace.registry import WorkspacePolicyError, WorkspaceRegistry, load_registry
from examples.mcp_server.mcp_infra.adapters import gateway as gateway_adapter
from examples.mcp_server.project_registry_control import (
    ProjectRegistrationError,
    _validate_against_registry,
)


def registry_file(tmp_path: Path) -> Path:
    (tmp_path / "healthy").mkdir()
    (tmp_path / "not-a-directory").write_text("not a workspace", encoding="utf-8")
    cfg = {
        "version": 1,
        "registry_root": str(tmp_path),
        "projects": {
            "nod-shared": {
                "root": "NOD_gateway/nod-shared",
                "type": "python-library",
                "parent": "nod-gateway",
            },
            "healthy": {"root": "healthy", "type": "service"},
            "file-root": {"root": "not-a-directory", "type": "service"},
        },
    }
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def test_missing_and_non_directory_root_stay_reserved_but_not_active(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    path = registry_file(tmp_path)
    projects, root = load_registry(path)
    assert root == tmp_path
    assert set(projects) == {"healthy"}

    registry = WorkspaceRegistry.load(path)
    assert {p["project_id"] for p in registry.list_projects()} == {"healthy"}
    unavailable = {p["project_id"]: p for p in registry.list_unavailable_projects()}
    assert unavailable["nod-shared"] == {
        "project_id": "nod-shared",
        "root": "NOD_gateway/nod-shared",
        "type": "python-library",
        "source": "source_registry",
        "reason": "root_missing",
    }
    assert unavailable["file-root"]["reason"] == "root_not_directory"
    with pytest.raises(WorkspacePolicyError, match="Unknown project"):
        registry.project_info("nod-shared")


def test_project_list_exposes_stale_entry_without_authorizing_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    registry = WorkspaceRegistry.load(registry_file(tmp_path))
    monkeypatch.setattr(gateway_adapter, "_server_workspace_registry", lambda: registry)
    monkeypatch.setattr(gateway_adapter, "_candidate_store_status", lambda: {"status": "ok"})

    for filters in ({"project_id": "nod-shared"}, {"query": "nod-shared"}):
        result = gateway_adapter.gateway_project_list(**filters, compact=True)
        assert result["ok"] is True
        data = result["result"]
        assert data["count"] == 0
        assert data["projects"] == []
        assert data["unavailable_count"] == 1
        assert data["unavailable_projects"][0]["reason"] == "root_missing"
        assert data["unavailable_projects"][0]["root"] == "NOD_gateway/nod-shared"
        assert str(tmp_path) not in str(data["unavailable_projects"])

    other = gateway_adapter.gateway_project_list(project_id="healthy", compact=True)
    assert other["result"]["count"] == 1
    assert "unavailable_projects" not in other["result"]


def test_existing_id_with_missing_root_never_reports_valid_duplicate(tmp_path):
    path = registry_file(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    roots = {"default": tmp_path}
    with pytest.raises(ProjectRegistrationError) as captured:
        _validate_against_registry(
            data, roots, project_id="nod-shared", root="healthy",
            root_selector="default", parent=None,
        )
    assert captured.value.code == "WORKSPACE_CONTENDED"
    assert "unavailable registry entry" in captured.value.message
    assert not (tmp_path / "NOD_gateway").exists()


def test_healthy_existing_id_still_returns_already_exists(tmp_path):
    path = registry_file(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    with pytest.raises(ProjectRegistrationError) as captured:
        _validate_against_registry(
            data, {"default": tmp_path}, project_id="healthy", root="healthy",
            root_selector="default", parent=None,
        )
    assert captured.value.code == "ALREADY_EXISTS"


def test_unknown_new_id_still_requires_real_existing_root(tmp_path):
    path = registry_file(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    with pytest.raises(ProjectRegistrationError) as captured:
        _validate_against_registry(
            data, {"default": tmp_path}, project_id="new", root="missing",
            root_selector="default", parent=None,
        )
    assert captured.value.code == "INVALID_INPUT"
    assert "existing directory" in captured.value.message

def test_runtime_overlay_live_root_supersedes_hidden_source_entry(tmp_path, monkeypatch):
    source = registry_file(tmp_path)
    overlay = tmp_path / "runtime-projects.yaml"
    overlay.write_text(
        yaml.safe_dump({
            "version": 1,
            "projects": {"nod-shared": {"root": "healthy", "type": "python-library"}},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_RUNTIME_PROJECTS_PATH", str(overlay))
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)

    registry = WorkspaceRegistry.load(source)
    assert registry.project_info("nod-shared")["project_id"] == "nod-shared"
    assert all(
        entry["project_id"] != "nod-shared"
        for entry in registry.list_unavailable_projects()
    )
