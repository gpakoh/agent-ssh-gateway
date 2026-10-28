"""Issue #703: explicit Gitea-only coordination without a false source checkout."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry
from examples.mcp_server.mcp_infra.adapters import coordination_adapter as adapter
from examples.mcp_server.mcp_infra.adapters import gateway as gateway_adapter
from examples.mcp_server.project_registry_control import (
    ProjectRegistrationError,
    _validate_against_registry,
)


@pytest.fixture
def gitea_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "gateway").mkdir()
    source = tmp_path / "projects.yaml"
    source.write_text(
        yaml.safe_dump({
            "version": 1,
            "registry_root": str(tmp_path),
            "projects": {
                "agent-ssh-gateway": {"root": "gateway", "type": "fastapi"},
                "nod-shared": {
                    "type": "coordination-only",
                    "gitea_repo": "gpakoh/nod-shared",
                    "description": "Independent canonical Gitea repo",
                    "tags": ["nod", "coordination"],
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    return source, WorkspaceRegistry.load(source)


def test_gitea_only_is_visible_in_registry_without_root(gitea_registry, monkeypatch):
    _, registry = gitea_registry
    names = {p["project_id"]: p for p in registry.list_projects()}
    assert names["nod-shared"]["type"] == "coordination-only"
    assert names["nod-shared"]["gitea_repo"] == "gpakoh/nod-shared"
    assert "root" not in names["nod-shared"]
    assert registry.project_info("nod-shared") == names["nod-shared"]
    assert registry.list_unavailable_projects() == []

    monkeypatch.setattr(gateway_adapter, "_server_workspace_registry", lambda: registry)
    monkeypatch.setattr(gateway_adapter, "_candidate_store_status", lambda: {"status": "ok"})
    listed = gateway_adapter.gateway_project_list(project_id="nod-shared", compact=False)
    assert listed["ok"] is True
    assert listed["result"]["count"] == 1
    assert listed["result"]["projects"][0]["gitea_repo"] == "gpakoh/nod-shared"


def test_gitea_only_is_never_filesystem_authority(gitea_registry, monkeypatch):
    _, registry = gitea_registry
    with pytest.raises(WorkspacePolicyError):
        registry.project_tree("nod-shared")

    import app.workspace.registry as registry_module
    from examples.mcp_server.mcp_client_tools import _resolve_project

    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    with pytest.raises(Exception) as exc:
        _resolve_project("nod-shared")
    assert "Gitea-only" in str(exc.value)


def test_coord_binding_exact_owner_and_repo(gitea_registry, monkeypatch):
    _, registry = gitea_registry
    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "gpakoh")
    monkeypatch.setattr(adapter, "server_attr", lambda _: lambda: registry)
    assert adapter._registered("nod-shared") == "nod-shared"
    assert adapter._registered("agent-ssh-gateway") == "agent-ssh-gateway"

    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "different-owner")
    with pytest.raises(ValueError, match="not verified"):
        adapter._registered("nod-shared")


@pytest.mark.asyncio
async def test_coord_create_uses_only_exact_canonical_gitea_repo(gitea_registry, monkeypatch):
    _, registry = gitea_registry
    monkeypatch.setenv("GITEA_TOKEN", "test-token")
    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "gpakoh")
    monkeypatch.setattr(adapter, "_principal", lambda: "authenticated-caller")
    client = type("Client", (), {})()
    client.__class__.__aenter__ = AsyncMock(return_value=client)
    client.__class__.__aexit__ = AsyncMock(return_value=None)
    client.get_repo = AsyncMock(return_value={
        "full_name": "gpakoh/nod-shared", "permissions": {"push": True},
    })
    create = AsyncMock(return_value={"number": 42, "created": True})
    monkeypatch.setattr(adapter, "create_thread", create)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda: registry) if name == "_get_workspace_registry"
        else (lambda _: client) if name == "GiteaClient" else None,
    )
    result = await adapter.coord_create(
        "agent-ssh-gateway", "nod-shared",
        "Typed contracts", "Please implement", "nod-shared-e2e-verification",
    )
    assert result["ok"] is True
    assert result["result"]["target_project"] == "nod-shared"
    client.get_repo.assert_awaited_once_with("gpakoh", "nod-shared")
    assert create.await_args.args[:3] == (client, "gpakoh", "nod-shared")


@pytest.mark.asyncio
async def test_coord_create_denies_repo_without_write_permission(gitea_registry, monkeypatch):
    _, registry = gitea_registry
    monkeypatch.setenv("GITEA_TOKEN", "test-token")
    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "gpakoh")
    monkeypatch.setattr(adapter, "_principal", lambda: "authenticated-caller")
    client = type("Client", (), {})()
    client.__class__.__aenter__ = AsyncMock(return_value=client)
    client.__class__.__aexit__ = AsyncMock(return_value=None)
    client.get_repo = AsyncMock(return_value={"permissions": {"pull": True}})
    create = AsyncMock()
    monkeypatch.setattr(adapter, "create_thread", create)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda: registry) if name == "_get_workspace_registry"
        else (lambda _: client) if name == "GiteaClient" else None,
    )
    response = await adapter.coord_create(
        "agent-ssh-gateway", "nod-shared", "Task", "Body", "no-write-test",
    )
    assert response["ok"] is False
    create.assert_not_awaited()


@pytest.mark.parametrize("repo,other", [
    ("other-owner/nod-shared", False),
    ("gpakoh/other-repo", False),
    ("gpakoh/nod-shared", True),
    ("", False),
])
def test_source_mapping_validation_fails_closed(tmp_path, monkeypatch, repo, other):
    monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    source = tmp_path / "projects.yaml"
    source.write_text(yaml.safe_dump({
        "version": 1,
        "registry_root": str(tmp_path),
        "projects": {"nod-shared": {
            "type": "coordination-only", "gitea_repo": repo,
        }},
    }), encoding="utf-8")
    registry = WorkspaceRegistry.load(source)
    if other:
        assert registry.project_info("nod-shared")["gitea_repo"] == repo
    else:
        with pytest.raises(WorkspacePolicyError):
            registry.project_info("nod-shared")
        assert registry.list_unavailable_projects()[0]["reason"] == "invalid_coordination_mapping"


def test_runtime_overlay_cannot_shadow_source_mapping(gitea_registry, monkeypatch, tmp_path):
    source, _ = gitea_registry
    overlay = tmp_path / "overlay.yaml"
    (tmp_path / "fake").mkdir()
    overlay.write_text(yaml.safe_dump({
        "version": 1,
        "projects": {"nod-shared": {"root": "fake", "type": "service"}},
    }), encoding="utf-8")
    monkeypatch.setenv("MCP_RUNTIME_PROJECTS_PATH", str(overlay))
    registry = WorkspaceRegistry.load(source)
    assert registry.project_info("nod-shared")["type"] == "coordination-only"
    assert "root" not in registry.project_info("nod-shared")
    with pytest.raises(WorkspacePolicyError):
        registry.project_tree("nod-shared")


def test_duplicate_registered_gitea_only_id_is_not_free(gitea_registry):
    source, _ = gitea_registry
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    with pytest.raises(ProjectRegistrationError) as exc:
        _validate_against_registry(
            data, {"default": source.parent}, project_id="nod-shared",
            root="gateway", root_selector="default", parent=None,
        )
    assert exc.value.code == "ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_gitea_repo_redirect_or_identity_mismatch_denies_any_write(gitea_registry, monkeypatch):
    _, registry = gitea_registry
    monkeypatch.setenv("GITEA_TOKEN", "test-token")
    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "gpakoh")
    monkeypatch.setattr(adapter, "_principal", lambda: "authenticated-caller")
    client = type("Client", (), {})()
    client.__class__.__aenter__ = AsyncMock(return_value=client)
    client.__class__.__aexit__ = AsyncMock(return_value=None)
    client.get_repo = AsyncMock(return_value={
        "full_name": "gpakoh/renamed-or-unexpected-repo",
        "permissions": {"push": True},
    })
    create = AsyncMock()
    mutate = AsyncMock()
    monkeypatch.setattr(adapter, "create_thread", create)
    monkeypatch.setattr(adapter, "write_event", mutate)
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda: registry) if name == "_get_workspace_registry"
        else (lambda _: client) if name == "GiteaClient" else None,
    )
    result = await adapter.coord_create(
        "agent-ssh-gateway", "nod-shared", "Task", "Body", "identity-mismatch",
    )
    assert result["ok"] is False
    create.assert_not_awaited()
    response = await adapter.coord_reply("nod-shared", 1, "Body", "no-redirect-write")
    assert response["ok"] is False
    mutate.assert_not_awaited()


def test_gateway_info_is_explicit_without_a_fake_checkout(gitea_registry, monkeypatch):
    _, registry = gitea_registry
    monkeypatch.setattr(gateway_adapter, "_server_workspace_registry", lambda: registry)

    def no_ssh_client():
        raise AssertionError("Gitea-only info must not resolve SSH workspace")

    monkeypatch.setattr(gateway_adapter, "_server_client", no_ssh_client)
    response = gateway_adapter.gateway_info("nod-shared")
    assert response["ok"] is True
    assert response["result"]["type"] == "coordination-only"
    assert response["result"]["gitea_repo"] == "gpakoh/nod-shared"
    assert response["result"]["workspace_available"] is False
    assert "root" not in response["result"]


@pytest.mark.asyncio
async def test_gitea_only_board_is_read_only(gitea_registry, monkeypatch):
    _, registry = gitea_registry
    monkeypatch.setenv("GITEA_TOKEN", "test-token")
    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "gpakoh")
    client = type("Client", (), {})()
    client.__class__.__aenter__ = AsyncMock(return_value=client)
    client.__class__.__aexit__ = AsyncMock(return_value=None)
    client.get_repo = AsyncMock()
    monkeypatch.setattr(adapter, "list_threads_preview", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda name: (lambda: registry) if name == "_get_workspace_registry"
        else (lambda _: client) if name == "GiteaClient" else None,
    )
    result = await adapter.coord_board("nod-shared")
    assert result["ok"] is True
    assert result["result"]["project"] == "nod-shared"
    assert result["result"]["count"] == 0
    client.get_repo.assert_not_awaited()


def test_gitea_only_must_not_declare_a_local_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    (tmp_path / "real_directory").mkdir()
    source = tmp_path / "projects.yaml"
    source.write_text(yaml.safe_dump({
        "version": 1,
        "registry_root": str(tmp_path),
        "projects": {"nod-shared": {
            "type": "coordination-only",
            "gitea_repo": "gpakoh/nod-shared",
            "root": "real_directory",
        }},
    }), encoding="utf-8")
    registry = WorkspaceRegistry.load(source)
    with pytest.raises(WorkspacePolicyError):
        registry.project_info("nod-shared")
    assert registry.list_unavailable_projects()[0]["reason"] == "invalid_coordination_mapping"


def test_invalid_source_gitea_mapping_cannot_be_replaced_by_runtime_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "gpakoh")
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)
    (tmp_path / "fake_checkout").mkdir()
    source = tmp_path / "projects.yaml"
    source.write_text(yaml.safe_dump({
        "version": 1, "registry_root": str(tmp_path),
        "projects": {"nod-shared": {
            "type": "coordination-only",
            "gitea_repo": "wrong-owner/nod-shared",
        }},
    }), encoding="utf-8")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump({
        "version": 1,
        "projects": {"nod-shared": {"type": "service", "root": "fake_checkout"}},
    }), encoding="utf-8")
    monkeypatch.setenv("MCP_RUNTIME_PROJECTS_PATH", str(overlay))
    registry = WorkspaceRegistry.load(source)
    assert "nod-shared" not in {x["project_id"] for x in registry.list_projects()}
    assert registry.list_unavailable_projects()[0]["reason"] == "invalid_coordination_mapping"
    with pytest.raises(WorkspacePolicyError):
        registry.project_info("nod-shared")


def test_invalid_owner_source_reservation_does_not_claim_valid_registered_id(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("MCP_COORD_GITEA_OWNER", "gpakoh")
    data = {
        "version": 1, "registry_root": str(tmp_path),
        "projects": {"nod-shared": {
            "type": "coordination-only",
            "gitea_repo": "wrong-owner/nod-shared",
        }},
    }
    with pytest.raises(ProjectRegistrationError) as exc:
        _validate_against_registry(
            data, {"default": tmp_path},
            project_id="nod-shared", root=".", root_selector="default", parent=None,
        )
    assert exc.value.code == "WORKSPACE_CONTENDED"


def test_invalid_gitea_only_owner_is_reserved_but_not_registered(gitea_registry):
    source, _ = gitea_registry
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    data["projects"]["nod-shared"]["gitea_repo"] = "unexpected-owner/nod-shared"
    with pytest.raises(ProjectRegistrationError) as exc:
        _validate_against_registry(
            data, {"default": source.parent}, project_id="nod-shared",
            root="gateway", root_selector="default", parent=None,
        )
    assert exc.value.code == "WORKSPACE_CONTENDED"
