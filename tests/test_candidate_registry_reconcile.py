from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from examples.mcp_server import candidate_registry_reconcile as reconcile
from examples.mcp_server.project_registry_control import ProjectRegistrationError
from examples.mcp_server.tool_modes import get_mcp_client_write_tools
from examples.mcp_server.tool_scopes import TOOL_SCOPES


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = workspace / ".mcp-candidate-clones"
    store.mkdir()
    (store / "candidate-live").mkdir()
    config = tmp_path / "config"
    config.mkdir()
    source = config / "projects.yaml"
    source.write_text(
        f"version: 1\nregistry_root: {workspace}\nprojects:\n"
        "  protected-source:\n    root: src\n    type: repo\n",
        encoding="utf-8",
    )
    (workspace / "src").mkdir()
    runtime = tmp_path / "runtime-projects.yaml"
    monkeypatch.setenv("MCP_RUNTIME_PROJECTS_PATH", str(runtime))
    runtime.write_text(
        "version: 1\nprojects:\n"
        "  candidate-missing-a:\n"
        "    root: .mcp-candidate-clones/candidate-missing-a\n"
        "    type: candidate-clone\n"
        "  candidate-live:\n"
        "    root: .mcp-candidate-clones/candidate-live\n"
        "    type: candidate-clone\n"
        "  candidate-missing-b:\n"
        "    root: .mcp-candidate-clones/candidate-missing-b\n"
        "    type: candidate-clone\n",
        encoding="utf-8",
    )
    return config, tmp_path / "journal", runtime, store


def _preview(config: Path, journal: Path) -> dict:
    return reconcile.reconcile_runtime_candidate_registry(config_dir=config, journal_root=journal)


def _apply(config: Path, journal: Path, preview: dict) -> dict:
    return reconcile.reconcile_runtime_candidate_registry(
        config_dir=config,
        journal_root=journal,
        apply=True,
        expected_runtime_sha256=preview["runtime_sha256"],
        expected_stale_count=preview["stale_count"],
    )


def test_preview_and_exact_cas_bulk_removal_preserves_live_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, store = _fixture(tmp_path, monkeypatch)
    before_source = (config / "projects.yaml").read_bytes()
    before_runtime = runtime.read_bytes()
    preview = _preview(config, journal)
    assert preview["applied"] is False
    assert preview["stale_count"] == 2
    assert preview["sample_ids"] == ["candidate-missing-a", "candidate-missing-b"]
    assert runtime.read_bytes() == before_runtime
    result = _apply(config, journal, preview)
    assert result["applied"] is True
    assert result["removed_count"] == 2
    assert result["workspace_bytes_mutated"] is False
    assert (config / "projects.yaml").read_bytes() == before_source
    after = runtime.read_text()
    assert "candidate-missing-a" not in after
    assert "candidate-missing-b" not in after
    assert "candidate-live:" in after
    assert (store / "candidate-live").is_dir()
    assert _preview(config, journal)["stale_count"] == 0


@pytest.mark.parametrize("kind", ["hash", "count"])
def test_stale_lease_fails_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    config, journal, runtime, _ = _fixture(tmp_path, monkeypatch)
    preview = _preview(config, journal)
    before = runtime.read_bytes()
    with pytest.raises(ProjectRegistrationError, match="since preview") as exc:
        reconcile.reconcile_runtime_candidate_registry(
            config_dir=config,
            journal_root=journal,
            apply=True,
            expected_runtime_sha256="sha256:" + "0" * 64 if kind == "hash" else preview["runtime_sha256"],
            expected_stale_count=1 if kind == "count" else 2,
        )
    assert exc.value.code == "WORKSPACE_CONTENDED"
    assert runtime.read_bytes() == before


def test_new_directory_between_scan_and_commit_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, store = _fixture(tmp_path, monkeypatch)
    preview = _preview(config, journal)
    before = runtime.read_bytes()
    original = reconcile._candidate_root_absent
    checks: dict[str, int] = {}

    def probe(path: Path, identifier: str) -> bool:
        checks[identifier] = checks.get(identifier, 0) + 1
        if identifier == "candidate-missing-a" and checks[identifier] == 2:
            (store / identifier).mkdir()
        return original(path, identifier)

    monkeypatch.setattr(reconcile, "_candidate_root_absent", probe)
    with pytest.raises(ProjectRegistrationError, match="reappeared"):
        _apply(config, journal, preview)
    assert runtime.read_bytes() == before


def test_symlink_candidate_is_never_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, store = _fixture(tmp_path, monkeypatch)
    (store / "candidate-missing-a").symlink_to(store / "candidate-live")
    preview = _preview(config, journal)
    assert preview["stale_count"] == 1
    _apply(config, journal, preview)
    assert "candidate-missing-a:" in runtime.read_text()


def test_descendant_blocks_parent_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, _ = _fixture(tmp_path, monkeypatch)
    with runtime.open("a", encoding="utf-8") as f:
        f.write(
            "  dependent:\n"
            "    root: .mcp-candidate-clones/candidate-missing-a/child\n"
            "    parent: candidate-missing-a\n"
            "    type: repo\n"
        )
    result = _preview(config, journal)
    assert result["stale_count"] == 1
    _apply(config, journal, result)
    assert "candidate-missing-a:" in runtime.read_text()


def test_source_project_with_same_candidate_root_blocks_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, _ = _fixture(tmp_path, monkeypatch)
    with (config / "projects.yaml").open("a", encoding="utf-8") as f:
        f.write(
            "  unrelated-source:\n"
            "    root: .mcp-candidate-clones/candidate-missing-a\n"
            "    type: repo\n"
        )
    preview = _preview(config, journal)
    assert preview["stale_count"] == 1
    _apply(config, journal, preview)
    assert "candidate-missing-a:" in runtime.read_text()


def test_dangling_yaml_alias_refuses_atomic_registry_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, _ = _fixture(tmp_path, monkeypatch)
    original = runtime.read_text()
    original = original.replace(
        "    type: candidate-clone\n",
        "    type: candidate-clone\n    tags: &shared-tags [must-survive]\n",
        1,
    )
    original += (
        "  unrelated:\n"
        "    root: unrelated\n"
        "    type: repo\n"
        "    tags: *shared-tags\n"
    )
    runtime.write_text(original)
    preview = _preview(config, journal)
    assert preview["stale_count"] == 2
    with pytest.raises(ProjectRegistrationError, match="invalidate YAML references") as exc:
        _apply(config, journal, preview)
    assert exc.value.code == "CHECK_FAILED"
    assert runtime.read_text() == original


def test_existing_source_id_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, _ = _fixture(tmp_path, monkeypatch)
    with (config / "projects.yaml").open("a", encoding="utf-8") as f:
        f.write(
            "  candidate-missing-a:\n"
            "    root: .mcp-candidate-clones/candidate-missing-a\n"
            "    type: candidate-clone\n"
        )
    preview = _preview(config, journal)
    assert preview["stale_count"] == 1
    _apply(config, journal, preview)
    assert "candidate-missing-a:" in runtime.read_text()


def test_missing_or_symlink_candidate_store_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, store = _fixture(tmp_path, monkeypatch)
    store.rename(tmp_path / "preserved-store")
    original = runtime.read_bytes()
    with pytest.raises(ProjectRegistrationError, match="root is absent"):
        _preview(config, journal)
    store.symlink_to(tmp_path / "preserved-store", target_is_directory=True)
    with pytest.raises(ProjectRegistrationError, match="unsafe"):
        _preview(config, journal)
    assert runtime.read_bytes() == original




def test_runtime_registry_cannot_alias_source_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, _ = _fixture(tmp_path, monkeypatch)
    source = config / "projects.yaml"
    before = source.read_bytes()
    monkeypatch.setenv("MCP_RUNTIME_PROJECTS_PATH", str(source))
    with pytest.raises(ProjectRegistrationError, match="must differ"):
        _preview(config, journal)
    assert source.read_bytes() == before
    assert runtime.exists()

def test_invalid_apply_leases_refused_without_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, _ = _fixture(tmp_path, monkeypatch)
    old = runtime.read_bytes()
    with pytest.raises(ProjectRegistrationError, match="requires preview"):
        reconcile.reconcile_runtime_candidate_registry(
            config_dir=config, journal_root=journal, apply=True
        )
    assert runtime.read_bytes() == old




def test_apply_requires_durable_audit_before_any_registry_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from examples.mcp_server.mcp_audit import AuditWriteError
    from examples.mcp_server.mcp_infra.adapters import supervisor

    config, journal, runtime, _ = _fixture(tmp_path, monkeypatch)
    preview = _preview(config, journal)
    old = runtime.read_bytes()
    monkeypatch.setattr(supervisor, "_resolve_registry_config_dir", lambda: config)
    monkeypatch.setattr(supervisor, "_journal_root_for_project", lambda *_: journal)

    class UnavailableAudit:
        def append_required(self, _event: object) -> None:
            raise AuditWriteError("audit destination unavailable")

    monkeypatch.setattr(
        supervisor, "server_attr", lambda _name: lambda: UnavailableAudit()
    )
    result = supervisor._reconcile_candidate_registry_impl(
        True, preview["runtime_sha256"], preview["stale_count"]
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert runtime.read_bytes() == old

def test_tool_is_admin_only_write_mode() -> None:
    name = "supervisor_reconcile_candidate_registry"
    assert name in get_mcp_client_write_tools()
    assert TOOL_SCOPES[name] == ["mcp:admin"]




def test_thousands_of_stale_records_reconcile_in_one_journaled_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, store = _fixture(tmp_path, monkeypatch)
    extra = 3200
    with runtime.open("a", encoding="utf-8") as handle:
        for i in range(extra):
            cid = f"candidate-legacy-{i:04d}"
            handle.write(
                f"  {cid}:\n"
                f"    root: .mcp-candidate-clones/{cid}\n"
                "    type: candidate-clone\n"
            )
    preview = _preview(config, journal)
    assert preview["stale_count"] == extra + 2
    result = _apply(config, journal, preview)
    assert result["removed_count"] == extra + 2
    assert "candidate-legacy-0000" not in runtime.read_text()
    assert "candidate-live:" in runtime.read_text()
    assert (store / "candidate-live").exists()

def test_hash_changes_only_after_successful_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, journal, runtime, _ = _fixture(tmp_path, monkeypatch)
    preview = _preview(config, journal)
    new = _apply(config, journal, preview)
    assert new["runtime_sha256"] == "sha256:" + hashlib.sha256(runtime.read_bytes()).hexdigest()
    assert new["runtime_sha256"] != preview["runtime_sha256"]
