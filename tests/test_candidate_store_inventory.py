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


def _resume_fixture(tmp_path, monkeypatch):
    import json

    from app.workspace import registry as registry_module
    from examples.mcp_server import candidate_clone as clone

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidates = workspace / ".mcp-candidate-clones"
    candidates.mkdir()
    for source in ("example", "example-extra"):
        (workspace / source).mkdir()
    config_dir = tmp_path / "config"
    projects = [
        "  example:\n    root: example\n    type: repo\n",
        "  example-extra:\n    root: example-extra\n    type: repo\n",
    ]

    def add(source, branch, *, override=None, registered=True):
        base = "a" * 40
        project_id = clone._project_id(source, branch, base)
        relative = f".mcp-candidate-clones/{project_id}"
        git = candidates / project_id / ".git"
        git.mkdir(parents=True)
        record = {
            "version": 1, "project_id": project_id, "source_project": source,
            "branch": branch, "base_sha": base, "head": base, "root": relative,
            "clone_origin": "PRIVATE_ORIGIN_MUST_NOT_ESCAPE",
        }
        record.update(override or {})
        (git / clone._METADATA_FILENAME).write_text(json.dumps(record))
        if registered:
            projects.append(f"  {project_id}:\n    root: {relative}\n    type: candidate-clone\n")
        return project_id, git / clone._METADATA_FILENAME

    def activate():
        _write_registry(config_dir, workspace, "".join(projects))
        monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
        monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
        registry = registry_module.WorkspaceRegistry.load(config_dir / "projects.yaml")
        monkeypatch.setattr(registry_module, "resolve_registry_root", lambda: config_dir)
        monkeypatch.setattr(gateway, "_server_workspace_registry", lambda: registry)
        monkeypatch.setattr(gateway, "_server_client", lambda: None)
        monkeypatch.setattr(gateway, "info", lambda _client, project: {
            "project": project, "is_git_repo": True,
            "workspace": {"git_state": {"head": "b" * 40, "dirty": True}},
        })

    return clone, add, activate


def test_info_candidate_resume_exact_source_and_current_head_separate(tmp_path, monkeypatch):
    _, add, activate = _resume_fixture(tmp_path, monkeypatch)
    own, _ = add("example", "fix/own")
    other, _ = add("example-extra", "fix/other")
    orphan, _ = add("example", "fix/orphan", registered=False)
    activate()

    result = gateway.gateway_info("example")["result"]
    resume = result["candidate_resume"]
    assert resume["state"] == "observed"
    assert resume["count_exact"] is True
    assert resume["count"] == 1
    assert resume["registered_candidates"] == [{
        "project_id": own, "source_project": "example",
        "recorded_branch": "fix/own", "creation_base_sha": "a" * 40,
    }]
    assert other not in str(resume) and orphan not in str(resume)
    assert "PRIVATE_ORIGIN" not in str(resume) and str(tmp_path) not in str(resume)
    assert resume["authorizes_mutation"] is False

    result = gateway.gateway_info(own)["result"]
    assert result["candidate_resume"]["lineage"] == resume["registered_candidates"][0]
    assert result["workspace"]["git_state"] == {"head": "b" * 40, "dirty": True}


@pytest.mark.parametrize("damage", ["wrong_source", "symlink", "oversize", "missing", "nested", "hardlink"])
def test_info_candidate_resume_damaged_identity_is_partial(tmp_path, monkeypatch, damage):
    _, add, activate = _resume_fixture(tmp_path, monkeypatch)
    own, _ = add("example", "fix/own")
    damaged, path = add("example", "fix/damaged",
                        override={"source_project": "example-extra"} if damage == "wrong_source" else None)
    if damage == "symlink":
        target = tmp_path / "external.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    elif damage == "oversize":
        path.write_text(" " * (64 * 1024 + 1))
    elif damage == "missing":
        path.unlink()
    elif damage == "nested":
        path.write_text("[" * 2000 + "]" * 2000)
    elif damage == "hardlink":
        import os

        os.link(path, tmp_path / "external.json")
    activate()

    resume = gateway.gateway_info("example")["result"]["candidate_resume"]
    assert resume["state"] == "partial"
    assert resume["count"] == 1 and resume["count_exact"] is False
    assert resume["registered_candidates"][0]["project_id"] == own
    assert damaged not in str(resume)
    direct = gateway.gateway_info(damaged)["result"]
    assert direct["candidate_resume"]["state"] == "unavailable"
    assert direct["candidate_resume"]["lineage"] is None
    assert direct["workspace"]["git_state"]["dirty"] is True


def test_info_candidate_resume_scan_and_display_caps(tmp_path, monkeypatch):
    clone, add, activate = _resume_fixture(tmp_path, monkeypatch)
    for i in range(4):
        add("example", f"fix/candidate-{i}")
    activate()
    monkeypatch.setattr(clone, "_RESUME_CANDIDATE_SCAN_LIMIT", 3, raising=False)
    monkeypatch.setattr(clone, "_RESUME_CANDIDATE_DISPLAY_LIMIT", 2, raising=False)
    resume = gateway.gateway_info("example")["result"]["candidate_resume"]
    assert resume["state"] == "partial"
    assert resume["count"] == 3 and resume["count_exact"] is False
    assert resume["scan_truncated"] is True
    assert resume["display_truncated"] is True
    assert len(resume["registered_candidates"]) == 2
