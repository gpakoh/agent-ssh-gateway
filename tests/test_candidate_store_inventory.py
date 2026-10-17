from __future__ import annotations

from pathlib import Path

import pytest

from examples.mcp_server.candidate_clone import candidate_store_inventory
from examples.mcp_server.mcp_infra.adapters import gateway


def _write_registry(config_dir: Path, workspace: Path, projects: str = "") -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "projects.yaml").write_text(
        "version: 1\n"
        f"registry_root: {workspace}\n\n"
        "projects:\n"
        f"{projects}",
        encoding="utf-8",
    )


def test_candidate_store_inventory_reports_both_drift_directions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidates = workspace / ".mcp-candidate-clones"
    candidates.mkdir()
    (candidates / "candidate-registered").mkdir()
    (candidates / "candidate-orphan").mkdir()
    config_dir = tmp_path / "config"
    _write_registry(
        config_dir,
        workspace,
        projects=(
            "  candidate-registered:\n"
            "    root: .mcp-candidate-clones/candidate-registered\n"
            "    type: candidate-clone\n"
            "  candidate-missing:\n"
            "    root: .mcp-candidate-clones/candidate-missing\n"
            "    type: candidate-clone\n"
        ),
    )

    inventory = candidate_store_inventory(config_dir=config_dir)

    assert inventory == {
        "consistent": False,
        "physical_candidate_count": 2,
        "registered_candidate_count": 2,
        "unregistered_candidate_ids": ["candidate-orphan"],
        "missing_candidate_ids": ["candidate-missing"],
        "unsafe_candidate_entries": [],
        "invalid_candidate_entry_count": 0,
    }


def test_candidate_store_inventory_includes_runtime_overlay_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidates = workspace / ".mcp-candidate-clones"
    candidates.mkdir()
    (candidates / "candidate-runtime").mkdir()
    config_dir = tmp_path / "config"
    _write_registry(config_dir, workspace)
    runtime = tmp_path / "runtime-projects.yaml"
    runtime.write_text(
        "version: 1\n"
        "projects:\n"
        "  candidate-runtime:\n"
        "    root: .mcp-candidate-clones/candidate-runtime\n"
        "    type: candidate-clone\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_RUNTIME_PROJECTS_PATH", str(runtime))

    inventory = candidate_store_inventory(config_dir=config_dir)

    assert inventory["consistent"] is True
    assert inventory["physical_candidate_count"] == 1
    assert inventory["registered_candidate_count"] == 1
    assert inventory["unregistered_candidate_ids"] == []
    assert inventory["missing_candidate_ids"] == []


def test_candidate_store_inventory_marks_symlink_candidate_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidates = workspace / ".mcp-candidate-clones"
    candidates.mkdir()
    target = workspace / "target"
    target.mkdir()
    (candidates / "candidate-unsafe").symlink_to(target, target_is_directory=True)
    config_dir = tmp_path / "config"
    _write_registry(config_dir, workspace)

    inventory = candidate_store_inventory(config_dir=config_dir)

    assert inventory["consistent"] is False
    assert inventory["physical_candidate_count"] == 0
    assert inventory["unsafe_candidate_entries"] == ["candidate-unsafe"]


def test_project_list_keeps_registered_contract_and_adds_candidate_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Registry:
        def list_projects(self) -> list[dict[str, object]]:
            return [{"project_id": "example", "root": "example", "type": "repo", "description": "", "tags": []}]

    candidate_store = {
        "status": "ok",
        "consistent": False,
        "physical_candidate_count": 2,
        "registered_candidate_count": 1,
        "unregistered_candidate_ids": ["candidate-orphan"],
        "missing_candidate_ids": [],
        "unsafe_candidate_entries": [],
        "invalid_candidate_entry_count": 0,
    }
    monkeypatch.setattr(gateway, "_server_workspace_registry", lambda: Registry())
    monkeypatch.setattr(gateway, "_candidate_store_status", lambda: candidate_store)

    response = gateway.gateway_project_list()

    assert response["ok"] is True
    assert response["result"]["count"] == 1
    assert response["result"]["projects"][0]["project_id"] == "example"
    assert response["result"]["candidate_store"] == candidate_store
