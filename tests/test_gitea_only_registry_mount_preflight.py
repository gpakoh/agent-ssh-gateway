"""Deploy preflight regression: Gitea-only registry cannot silently bind stale host YAML."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
import yaml

from scripts.check_gitea_only_registry_mount import (
    RegistryDrift,
    _projects,
    compare_gitea_only_bindings,
    main,
)


def registry(*, repo: str = "gpakoh/nod-shared", **extra: object) -> bytes:
    projects = {
        "nod-shared": {"type": "coordination-only", "gitea_repo": repo, **extra},
        "agent-ssh-gateway": {"type": "fastapi", "root": "web_ssh/web-ssh-gateway"},
    }
    return yaml.safe_dump({"version": 1, "projects": projects}).encode()


def test_deployed_mapping_matches_reviewed_source():
    assert compare_gitea_only_bindings(registry(), registry()) == []


def test_case_sensitive_nod_source_repo_pin_is_required_by_deploy():
    source = yaml.safe_load(registry())
    source["projects"]["nod-gateway"] = {
        "type": "monorepo", "root": "NOD_gateway",
        "gitea_repo": "gpakoh/NOD_gateway",
    }
    mounted = yaml.safe_load(yaml.safe_dump(source))
    mounted["projects"]["nod-gateway"].pop("gitea_repo")
    expect = yaml.safe_dump(source).encode()
    assert compare_gitea_only_bindings(expect, yaml.safe_dump(mounted).encode()) == [
        "nod-gateway"
    ]
    mounted["projects"]["nod-gateway"]["gitea_repo"] = "gpakoh/nod-gateway"
    assert compare_gitea_only_bindings(expect, yaml.safe_dump(mounted).encode()) == [
        "nod-gateway"
    ]
    mounted["projects"]["nod-gateway"]["gitea_repo"] = "gpakoh/NOD_gateway"
    mounted["projects"]["nod-gateway"]["description"] = "host-owned note"
    assert compare_gitea_only_bindings(expect, yaml.safe_dump(mounted).encode()) == []


def test_extra_or_cross_owner_source_repo_pin_fails_closed():
    source = yaml.safe_load(registry())
    mounted = yaml.safe_load(registry())
    mounted["projects"]["agent-ssh-gateway"]["gitea_repo"] = "gpakoh/other"
    assert compare_gitea_only_bindings(
        yaml.safe_dump(source).encode(), yaml.safe_dump(mounted).encode(),
    ) == ["agent-ssh-gateway"]
    source["projects"]["agent-ssh-gateway"]["gitea_repo"] = "other/agent-ssh-gateway"
    assert compare_gitea_only_bindings(
        yaml.safe_dump(source).encode(), yaml.safe_dump(source).encode(),
    ) == ["agent-ssh-gateway"]


def test_stale_legacy_nod_shared_mapping_fails_before_deploy():
    old = yaml.safe_dump({
        "version": 1,
        "projects": {
            "nod-shared": {
                "root": "NOD_gateway/nod-shared",
                "parent": "nod-gateway",
                "type": "python-library",
            },
        },
    }).encode()
    assert compare_gitea_only_bindings(registry(), old) == ["nod-shared"]


@pytest.mark.parametrize("overrides", [
    {"type": "python-library", "gitea_repo": "gpakoh/nod-shared"},
    {"type": "coordination-only", "gitea_repo": "other/nod-shared"},
    {"type": "coordination-only", "gitea_repo": "gpakoh/nod-shared", "root": "vendored/nod_shared"},
    {"type": "coordination-only", "gitea_repo": "gpakoh/nod-shared", "parent": "nod-gateway"},
    {"type": "coordination-only", "gitea_repo": "gpakoh/nod-shared", "root": None},
    {"type": "coordination-only", "gitea_repo": "gpakoh/nod-shared", "parent": None},
])
def test_changed_mounted_entry_fails(overrides):
    mounted = yaml.safe_dump({"projects": {"nod-shared": overrides}}).encode()
    assert compare_gitea_only_bindings(registry(), mounted) == ["nod-shared"]


def test_no_missing_mapping_can_report_success():
    mounted = yaml.safe_dump({"projects": {"unrelated": {"root": "x"}}}).encode()
    assert compare_gitea_only_bindings(registry(), mounted) == ["nod-shared"]


def test_extra_unreviewed_gitea_only_mapping_is_rejected():
    cfg = yaml.safe_load(registry())
    cfg["projects"]["extra"] = {"type": "coordination-only", "gitea_repo": "gpakoh/extra"}
    assert compare_gitea_only_bindings(registry(), yaml.safe_dump(cfg).encode()) == ["extra"]


def test_unrelated_host_registry_changes_are_not_overwritten_or_blocked():
    doc = yaml.safe_load(registry())
    doc["projects"]["agent-ssh-gateway"]["description"] = "locally configured deployment"
    doc["projects"]["operator-owned"] = {"root": "site-audit-platform", "type": "service"}
    assert compare_gitea_only_bindings(registry(), yaml.safe_dump(doc).encode()) == []


@pytest.mark.parametrize("bad", [
    b"not: [valid",
    b"just a scalar",
    b"projects:\n  nod-shared:\n    type: coordination-only\n  nod-shared:\n    type: service\n",
    b"projects:\n  nod-shared:\n    type: coordination-only\n    type: service\n",
    b"projects:\n  nod-shared: []\n",
])
def test_ambiguous_or_malformed_yaml_fails_closed(bad):
    with pytest.raises(RegistryDrift):
        compare_gitea_only_bindings(registry(), bad)


def test_source_contract_cannot_give_a_root_or_change_repo_identity():
    assert compare_gitea_only_bindings(registry(root=None), registry(root=None)) == ["nod-shared"]
    invalid = registry(repo="gpakoh/not-nod-shared")
    assert compare_gitea_only_bindings(invalid, invalid) == ["nod-shared"]


def test_source_yaml_is_size_bounded():
    with pytest.raises(RegistryDrift, match="size limit"):
        _projects(b"x" * 1_000_001)


def test_cli_stale_mount_failure_is_safe_and_no_secret_leak(tmp_path, monkeypatch, capsys):
    mounted = tmp_path / "runtime.yaml"
    mounted.write_text("projects:\n  nod-shared:\n    type: python-library\n    private_key: SUPERSECRET\n")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(registry()), encoding="utf-8"))
    assert main(["--mounted", str(mounted)]) == 3
    out = capsys.readouterr()
    assert "REGISTRY_CONFIG_DRIFT" in out.err
    assert "nod-shared" in out.err
    assert "SUPERSECRET" not in out.err
    assert str(tmp_path) not in out.err


def test_cli_matches_reconciled_mapping(tmp_path, monkeypatch, capsys):
    mounted = tmp_path / "runtime.yaml"
    mounted.write_bytes(registry())
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(registry()), encoding="utf-8"))
    assert main(["--mounted", str(mounted)]) == 0
    assert "contract: OK" in capsys.readouterr().out


def test_deploy_preflight_is_read_only_and_precedes_container_recreation():
    repo = Path(__file__).resolve().parents[1]
    shell = (repo / "scripts/deploy-from-registry.sh").read_text(encoding="utf-8")
    preflight = shell.index("scripts/check_gitea_only_registry_mount.py")
    assert shell.index('docker pull "$MCP_REPO:$DEPLOY_TAG"') < preflight
    assert preflight < shell.index("if ! deploy_services")
    for guard in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "target=/app/projects.yaml,readonly",
        '< "$ROOT/projects.yaml"',
        "REGISTRY_CONFIG_DRIFT",
    ):
        assert guard in shell
    assert "--apply" not in shell[preflight - 600:preflight + 400]
