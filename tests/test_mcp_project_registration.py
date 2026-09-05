"""Tests for admin-only workspace project registration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.workspace.registry import WorkspaceRegistry, resolve_runtime_registry_path
from examples.mcp_server import project_registry_control
from examples.mcp_server.mcp_infra.adapters import supervisor
from examples.mcp_server.supervisor_integration import HashMismatchError


@pytest.fixture
def registry_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_dir = tmp_path / "config-repo"
    workspace_root = tmp_path / "workspace-root"
    config_dir.mkdir()
    workspace_root.mkdir()
    (workspace_root / "existing").mkdir()
    initial = (
        "version: 1\n"
        f"registry_root: {workspace_root}\n\n"
        "projects:\n"
        "  existing:\n"
        "    root: existing\n"
        "    type: service\n"
        '    description: "existing project"\n'
        "    tags: []\n"
    )
    (config_dir / "projects.yaml").write_text(initial, encoding="utf-8")
    monkeypatch.setattr(
        supervisor, "_resolve_registry_config_dir", lambda: config_dir
    )
    monkeypatch.setenv(
        "MCP_SUPERVISOR_JOURNAL_ROOT", str(tmp_path / "journals")
    )

    def immediate_run_tool(*, tool, title, fn, success_text):
        del tool, title, success_text
        return fn()

    monkeypatch.setattr(supervisor, "run_tool", immediate_run_tool)

    class FreshRegistry:
        def project_info(self, project_id: str):
            return WorkspaceRegistry.load(config_dir / "projects.yaml").project_info(
                project_id
            )

    monkeypatch.setattr(supervisor, "_get_workspace_registry", lambda: FreshRegistry())
    return config_dir, workspace_root, initial


def test_register_uses_runtime_overlay_and_preserves_source_registry(registry_layout):
    config_dir, workspace_root, initial = registry_layout
    (workspace_root / "ECC").mkdir()

    result = supervisor.supervisor_register_project(
        "ecc-reference",
        "ECC",
        project_type="reference",
        description="Everything Claude Code reference",
        tags=["reference", "agents"],
    )

    assert result["ok"] is True
    assert result["result"]["root"] == "ECC"
    assert result["result"]["storage"] == "runtime_overlay"
    assert result["result"]["source_registry_mutated"] is False
    assert result["result"]["cache_reset"] is True
    assert str(config_dir) not in repr(result)
    assert str(workspace_root) not in repr(result)

    assert (config_dir / "projects.yaml").read_text(encoding="utf-8") == initial
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    assert runtime_path is not None
    overlay = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    assert overlay["projects"]["ecc-reference"] == {
        "root": "ECC",
        "type": "reference",
        "description": "Everything Claude Code reference",
        "tags": ["reference", "agents"],
    }
    visible = WorkspaceRegistry.load(config_dir / "projects.yaml").project_info("ecc-reference")
    assert visible["root"].endswith("ECC")


def test_register_can_persist_to_source_when_explicitly_requested(registry_layout):
    config_dir, workspace_root, initial = registry_layout
    (workspace_root / "ECC").mkdir()

    result = supervisor.supervisor_register_project(
        "ecc-reference",
        "ECC",
        project_type="reference",
        description="Everything Claude Code reference",
        tags=["reference", "agents"],
        persist_to_source=True,
    )

    assert result["ok"] is True
    assert result["result"]["storage"] == "source_registry"
    assert result["result"]["source_registry_mutated"] is True
    text = (config_dir / "projects.yaml").read_text(encoding="utf-8")
    assert text.startswith(initial.rstrip("\n"))
    loaded = yaml.safe_load(text)
    assert loaded["projects"]["ecc-reference"] == {
        "root": "ECC",
        "type": "reference",
        "description": "Everything Claude Code reference",
        "tags": ["reference", "agents"],
    }


@pytest.mark.parametrize(
    "root",
    ["/tmp/ECC", "../ECC", "a/../../ECC", ".", "./", r"a\b"],
)
def test_register_rejects_unsafe_root_syntax(registry_layout, root):
    config_dir, _workspace_root, initial = registry_layout
    result = supervisor.supervisor_register_project("bad-project", root)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert (config_dir / "projects.yaml").read_text(encoding="utf-8") == initial


def test_register_rejects_missing_and_symlink_roots(registry_layout):
    _config_dir, workspace_root, _initial = registry_layout

    missing = supervisor.supervisor_register_project("missing", "missing")
    assert missing["ok"] is False
    assert missing["error"]["code"] == "INVALID_INPUT"

    real = workspace_root / "real"
    real.mkdir()
    (workspace_root / "alias").symlink_to(real, target_is_directory=True)
    alias = supervisor.supervisor_register_project("alias-project", "alias")
    assert alias["ok"] is False
    assert alias["error"]["code"] == "POLICY_DENIED"


def test_register_rejects_duplicate_id_and_canonical_root(registry_layout):
    _config_dir, workspace_root, _initial = registry_layout
    (workspace_root / "other").mkdir()

    duplicate_id = supervisor.supervisor_register_project("existing", "other")
    assert duplicate_id["ok"] is False

    duplicate_root = supervisor.supervisor_register_project("other-id", "existing")
    assert duplicate_root["ok"] is False


def test_parent_must_exist_and_contain_child(registry_layout):
    _config_dir, workspace_root, _initial = registry_layout
    (workspace_root / "existing" / "child").mkdir()
    (workspace_root / "outside").mkdir()

    child = supervisor.supervisor_register_project(
        "child", "existing/child", parent="existing"
    )
    assert child["ok"] is True
    assert child["result"]["parent"] == "existing"

    outside = supervisor.supervisor_register_project(
        "outside-child", "outside", parent="existing"
    )
    assert outside["ok"] is False
    assert outside["error"]["code"] == "POLICY_DENIED"

    missing_parent = supervisor.supervisor_register_project(
        "missing-parent-child", "outside", parent="missing"
    )
    assert missing_parent["ok"] is False


def test_registered_child_project_is_self_verified_without_host_path_leak(
    registry_layout,
):
    _config_dir, workspace_root, _initial = registry_layout
    (workspace_root / "existing" / "child").mkdir()

    result = supervisor.supervisor_register_project(
        "child",
        "existing/child",
        project_type="supervisor-workspace",
        description="Scoped supervisor audit target",
        tags=["supervisor"],
        parent="existing",
    )

    assert result["ok"] is True
    payload = result["result"]
    assert payload["usable"] is True
    assert payload["visible_project"] == {
        "project_id": "child",
        "root": "existing/child",
        "parent": "existing",
    }
    assert str(workspace_root) not in repr(result)


def test_registration_reports_when_project_is_not_visible_after_cache_reset(
    registry_layout, monkeypatch: pytest.MonkeyPatch
):
    _config_dir, workspace_root, _initial = registry_layout
    (workspace_root / "ECC").mkdir()

    class EmptyRegistry:
        def project_info(self, project_id: str):
            raise KeyError(project_id)

    monkeypatch.setattr(supervisor, "_get_workspace_registry", lambda: EmptyRegistry())
    result = supervisor.supervisor_register_project("ecc-reference", "ECC")

    assert result["ok"] is True
    assert result["result"]["usable"] is False
    assert (
        result["result"]["visibility_error"]
        == "PROJECT_NOT_VISIBLE_AFTER_REGISTRATION"
    )


def test_registration_metadata_is_bounded(registry_layout):
    _config_dir, workspace_root, _initial = registry_layout
    (workspace_root / "ECC").mkdir()

    assert supervisor.supervisor_register_project("-bad", "ECC")["ok"] is False
    assert (
        supervisor.supervisor_register_project(
            "good", "ECC", project_type=""
        )["ok"]
        is False
    )
    assert (
        supervisor.supervisor_register_project("good", "ECC", tags=[""])["ok"]
        is False
    )
    assert (
        supervisor.supervisor_register_project(
            "good", "ECC", tags=["x" * 65]
        )["ok"]
        is False
    )


def test_registration_cas_conflict_fails_closed(registry_layout, monkeypatch):
    config_dir, workspace_root, initial = registry_layout
    (workspace_root / "ECC").mkdir()

    def conflict(*args, **kwargs):
        del args, kwargs
        raise HashMismatchError("concurrent mutation")

    monkeypatch.setattr(project_registry_control, "integrate_file", conflict)
    result = supervisor.supervisor_register_project("ecc-reference", "ECC")

    assert result["ok"] is False
    assert result["error"]["code"] == "CHECK_FAILED"
    assert (config_dir / "projects.yaml").read_text(encoding="utf-8") == initial


def test_cache_reset_failure_is_reported_without_hiding_persisted_write(
    registry_layout, monkeypatch
):
    config_dir, workspace_root, _initial = registry_layout
    (workspace_root / "ECC").mkdir()

    from examples.mcp_server.mcp_infra.adapters import workspace

    monkeypatch.setattr(
        workspace,
        "reset_workspace_registry_cache",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = supervisor.supervisor_register_project("ecc-reference", "ECC")

    assert result["ok"] is True
    assert result["result"]["cache_reset"] is False
    assert result["result"]["storage"] == "runtime_overlay"
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    assert runtime_path is not None
    loaded = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    assert "ecc-reference" in loaded["projects"]
