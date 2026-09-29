"""Tests for admin-only workspace project registration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.workspace import registry as registry_mod
from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import (
    WorkspaceRegistry,
    load_registry,
    resolve_registry_root,
    resolve_runtime_registry_path,
)
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


def _add_named_registry_root(config_dir: Path, selector: str, root: Path) -> None:
    path = config_dir / "projects.yaml"
    text = path.read_text(encoding="utf-8")
    marker = "\nprojects:\n"
    assert marker in text
    path.write_text(
        text.replace(
            marker,
            f"\nregistry_roots:\n  {selector}: {root}\n\nprojects:\n",
            1,
        ),
        encoding="utf-8",
    )


def test_registry_root_can_be_overridden_without_cwd_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKSPACE_REGISTRY_ROOT", str(tmp_path))
    monkeypatch.setattr(registry_mod, "_registry_root", None)

    assert resolve_registry_root() == tmp_path.resolve()

    registry_mod.set_registry_root(tmp_path / "explicit")
    assert registry_mod.get_registry_root() == (tmp_path / "explicit").resolve()


def test_runtime_registry_path_prefers_explicit_absolute_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "runtime-projects.yaml"
    journal = tmp_path / "journals"
    monkeypatch.setenv("MCP_RUNTIME_PROJECTS_PATH", str(explicit))
    monkeypatch.setenv("MCP_SUPERVISOR_JOURNAL_ROOT", str(journal))

    assert resolve_runtime_registry_path(tmp_path / "projects.yaml") == explicit.resolve()


@pytest.mark.parametrize("env_name", ["MCP_RUNTIME_PROJECTS_PATH", "MCP_SUPERVISOR_JOURNAL_ROOT"])
def test_runtime_registry_path_rejects_relative_overrides(
    env_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_name, "relative/path.yaml")
    if env_name == "MCP_SUPERVISOR_JOURNAL_ROOT":
        monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)

    with pytest.raises(WorkspacePolicyError, match=f"{env_name} must be absolute"):
        resolve_runtime_registry_path(Path("projects.yaml"))


@pytest.mark.parametrize(
    ("overlay_content", "message"),
    [
        ("[]\n", "Runtime registry overlay must be a YAML mapping"),
        ("projects: []\n", "Runtime registry overlay must contain a 'projects' mapping"),
    ],
)
def test_load_registry_rejects_invalid_runtime_overlay(
    registry_layout,
    overlay_content: str,
    message: str,
) -> None:
    config_dir, _workspace_root, _initial = registry_layout
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    assert runtime_path is not None
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(overlay_content, encoding="utf-8")

    with pytest.raises(WorkspacePolicyError, match=message):
        load_registry(config_dir / "projects.yaml")


def test_load_registry_rejects_runtime_overlay_directory(registry_layout) -> None:
    config_dir, _workspace_root, _initial = registry_layout
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    assert runtime_path is not None
    runtime_path.mkdir(parents=True)

    with pytest.raises(WorkspacePolicyError, match="Runtime registry overlay path is not a file"):
        load_registry(config_dir / "projects.yaml")


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
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    assert runtime_path is not None
    assert not runtime_path.exists()


@pytest.mark.parametrize(
    ("project_id", "root"),
    [
        ("runtime-entry", "source-only"),
        ("source-entry", "runtime-only"),
    ],
)
def test_source_persist_rejects_runtime_overlay_identity_conflicts(
    registry_layout,
    project_id: str,
    root: str,
):
    config_dir, workspace_root, initial = registry_layout
    (workspace_root / "runtime-only").mkdir()
    (workspace_root / "source-only").mkdir()

    runtime = supervisor.supervisor_register_project(
        "runtime-entry",
        "runtime-only",
        project_type="reference",
    )
    assert runtime["ok"] is True

    result = supervisor.supervisor_register_project(
        project_id,
        root,
        project_type="reference",
        persist_to_source=True,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "ALREADY_EXISTS"
    assert (config_dir / "projects.yaml").read_text(encoding="utf-8") == initial
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    assert runtime_path is not None
    overlay = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    assert list(overlay["projects"]) == ["runtime-entry"]


def test_register_uses_named_registry_root_in_runtime_overlay(registry_layout):
    config_dir, _workspace_root, _initial = registry_layout
    named_root = config_dir.parent / "astro-sites"
    named_root.mkdir()
    (named_root / "example").mkdir()
    _add_named_registry_root(config_dir, "astro-sites", named_root)

    result = supervisor.supervisor_register_project(
        "example",
        "example",
        project_type="astro-site",
        tags=["astro"],
        root_selector="astro-sites",
    )

    assert result["ok"] is True
    assert result["result"]["root_selector"] == "astro-sites"
    assert result["result"]["storage"] == "runtime_overlay"
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    assert runtime_path is not None
    overlay = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    assert overlay["projects"]["example"]["root"] == "example"
    assert overlay["projects"]["example"]["root_selector"] == "astro-sites"
    visible = WorkspaceRegistry.load(config_dir / "projects.yaml").project_info("example")
    assert visible["root"] == str((named_root / "example").resolve())


def test_register_allows_same_relative_path_in_distinct_registry_roots(registry_layout):
    config_dir, _workspace_root, _initial = registry_layout
    named_root = config_dir.parent / "external-root"
    named_root.mkdir()
    (named_root / "existing").mkdir()
    _add_named_registry_root(config_dir, "external", named_root)

    result = supervisor.supervisor_register_project(
        "external-existing",
        "existing",
        root_selector="external",
    )

    assert result["ok"] is True
    assert result["result"]["root_selector"] == "external"


def test_register_rejects_unknown_root_selector(registry_layout):
    _config_dir, _workspace_root, _initial = registry_layout

    result = supervisor.supervisor_register_project(
        "unknown-root",
        "existing",
        root_selector="missing-selector",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


def test_unregister_does_not_treat_other_selector_prefix_as_descendant(registry_layout):
    config_dir, workspace_root, _initial = registry_layout
    named_root = config_dir.parent / "external-root"
    named_root.mkdir()
    (workspace_root / "candidate").mkdir()
    (named_root / "candidate" / "child").mkdir(parents=True)
    _add_named_registry_root(config_dir, "external", named_root)
    journal_root = config_dir.parent / "direct-journal"

    project_registry_control.register_project(
        config_dir=config_dir,
        journal_root=journal_root,
        project_id="candidate",
        root="candidate",
        project_type="candidate-clone",
    )
    project_registry_control.register_project(
        config_dir=config_dir,
        journal_root=journal_root,
        project_id="external-child",
        root="candidate/child",
        root_selector="external",
        project_type="service",
    )

    removed = project_registry_control.unregister_project_exact(
        config_dir=config_dir,
        journal_root=journal_root,
        project_id="candidate",
        expected_root="candidate",
        expected_type="candidate-clone",
    )

    assert removed.already_absent is False
    runtime_path = resolve_runtime_registry_path(config_dir / "projects.yaml")
    assert runtime_path is not None
    overlay = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    assert "candidate" not in overlay["projects"]
    assert overlay["projects"]["external-child"]["root_selector"] == "external"


def test_unregister_requires_matching_root_selector(registry_layout):
    config_dir, _workspace_root, _initial = registry_layout
    named_root = config_dir.parent / "external-root"
    named_root.mkdir()
    (named_root / "candidate").mkdir()
    _add_named_registry_root(config_dir, "external", named_root)
    journal_root = config_dir.parent / "selector-journal"

    project_registry_control.register_project(
        config_dir=config_dir,
        journal_root=journal_root,
        project_id="external-candidate",
        root="candidate",
        root_selector="external",
        project_type="candidate-clone",
    )

    with pytest.raises(project_registry_control.ProjectRegistrationError) as exc_info:
        project_registry_control.unregister_project_exact(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id="external-candidate",
            expected_root="candidate",
            expected_type="candidate-clone",
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"


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