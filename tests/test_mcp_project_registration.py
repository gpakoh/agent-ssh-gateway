"""Tests for admin-only workspace project registration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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
    return config_dir, workspace_root, initial


def test_register_uses_yaml_registry_root_and_preserves_existing_text(registry_layout):
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
    assert result["result"]["cache_reset"] is True
    assert str(config_dir) not in repr(result)
    assert str(workspace_root) not in repr(result)

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
    loaded = yaml.safe_load((config_dir / "projects.yaml").read_text(encoding="utf-8"))
    assert "ecc-reference" in loaded["projects"]
