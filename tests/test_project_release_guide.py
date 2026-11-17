"""Release guidance is read-only and distinguishes evidence from assumptions."""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock

import pytest

from examples.mcp_server.mcp_infra.adapters import release_guide as guide
from examples.mcp_server.tool_modes import TOOL_NAMES_BY_MODE
from examples.mcp_server.tool_scopes import get_required_scopes


def catalog():
    return {
        "schema_version": 1,
        "projects": {
            "quart-core": {
                "repository": "quart-core",
                "title": "Core",
                "compose_project": "infra-quart",
                "services": [
                    {"service": "quart-core", "container": "quart-core", "purpose": "API"},
                    {"service": "quart-core-worker", "container": "quart-core-worker", "purpose": "worker"},
                ],
                "workflow": {
                    "repository": "quart-core",
                    "branch": "main",
                    "path": ".github/workflows/ci-cd.yml",
                    "trigger": "manual_exact_sha",
                    "audited_commit_sha": "c" * 40,
                    "audited_blob_sha": "d" * 40,
                    "sha_input": "release_sha",
                    "description": "Manual",
                },
                "steps": ["API then worker"],
                "conditions": [{"id": "rollback", "description": "Check LKG", "hint": "Use preflight"}],
                "source_files": ["scripts/deploy-quart-core.sh"],
                "notes": [],
            }
        },
    }


def remote_file(value=None):
    return {
        "ok": True,
        "result": {
            "encoding": "base64",
            "content": base64.b64encode(json.dumps(value or catalog()).encode()).decode(),
            "sha": "b" * 40,
            "resolved_commit_sha": "a" * 40,
        },
    }


def inspection(name):
    service = name
    return {
        "ok": True,
        "meta": {"truncated": False},
        "result": [{
            "Config": {
                "Image": "example/repo@sha256:" + "a" * 64,
                "Labels": {
                    "com.docker.compose.project": "infra-quart",
                    "com.docker.compose.service": service,
                },
            },
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "RestartCount": 0,
        }],
    }


def configure(monkeypatch):
    monkeypatch.setattr(guide.coordination_adapter, "_owner", lambda: "gpakoh")
    monkeypatch.setattr(guide.coordination_adapter, "_registered", lambda name: name)
    async def read_file(owner, repo, path, **kwargs):
        if repo == "infra-quart" and path == guide._CATALOG_PATH:
            return remote_file()
        return {"ok": True, "result": {"sha": "d" * 40, "resolved_commit_sha": "e" * 40}}

    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(side_effect=read_file))
    monkeypatch.setattr(guide.docker, "docker_inspect", AsyncMock(side_effect=inspection))


def test_tool_visible_with_explicit_read_scopes():
    assert "project_release_guide" in TOOL_NAMES_BY_MODE["mcp_client"]
    assert "project_release_guide" in TOOL_NAMES_BY_MODE["mcp_client_write"]
    assert get_required_scopes("project_release_guide") == ["mcp:repo", "mcp:docker"]


@pytest.mark.asyncio
async def test_core_guide_has_order_provenance_live_health_and_unknown_release_conditions(monkeypatch):
    configure(monkeypatch)
    result = await guide.project_release_guide("quart-core")
    assert result["ok"]
    plan = result["result"]
    assert plan["configured"] is True
    assert plan["source"]["commit_sha"] == "a" * 40
    assert plan["workflow"]["observation"]["status"] == "unchanged"
    assert plan["workflow"]["observation"]["current_commit_sha"] == "e" * 40
    assert plan["steps"] == ["API then worker"]
    assert [service["observation"]["health"] for service in plan["services"]] == ["healthy", "healthy"]
    assert plan["conditions"][0]["status"] == "unknown"
    assert plan["ready_for_release"] == "unknown"
    assert guide.docker.docker_inspect.await_count == 2


@pytest.mark.asyncio
async def test_missing_project_profile_is_not_invented(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(
        guide.remote,
        "gitea_list_workflows",
        AsyncMock(return_value={"ok": True, "result": {"workflows": [
            {"name": "CI", "path": ".gitea/workflows/ci.yml", "secret": "not returned"}
        ]}}),
    )
    result = await guide.project_release_guide("other-service")
    assert result["ok"]
    assert result["result"]["configured"] is False
    assert result["result"]["workflow_candidates"] == [
        {"name": "CI", "path": ".gitea/workflows/ci.yml"}
    ]
    assert result["result"]["ready_for_release"] == "unknown"
    guide.docker.docker_inspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_docker_never_implies_healthy_or_release_ready(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(guide.docker, "docker_inspect", AsyncMock(side_effect=RuntimeError("private")))
    result = await guide.project_release_guide("quart-core")
    assert result["ok"]
    assert all(service["observation"]["status"] == "unknown" for service in result["result"]["services"])
    assert result["result"]["ready_for_release"] == "unknown"
    assert "private" not in repr(result)


@pytest.mark.asyncio
async def test_wrong_compose_identity_is_unknown(monkeypatch):
    configure(monkeypatch)
    bad = inspection("quart-core")
    bad["result"][0]["Config"]["Labels"]["com.docker.compose.project"] = "other"
    monkeypatch.setattr(guide.docker, "docker_inspect", AsyncMock(return_value=bad))
    result = await guide.project_release_guide("quart-core")
    assert all(service["observation"]["status"] == "unknown" for service in result["result"]["services"])


@pytest.mark.asyncio
async def test_corrupt_or_truncated_catalog_fails_closed(monkeypatch):
    configure(monkeypatch)
    payload = remote_file()
    payload["result"]["truncated"] = True
    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(return_value=payload))
    result = await guide.project_release_guide("quart-core")
    assert result["ok"] is False
    assert result["error"]["code"] == "REMOTE_SCHEMA_INVALID"
    guide.docker.docker_inspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_or_unregistered_project_never_reads_catalog(monkeypatch):
    configure(monkeypatch)
    result = await guide.project_release_guide("../secrets")
    assert result["error"]["code"] == "INVALID_INPUT"
    guide.remote.gitea_get_file.assert_not_awaited()
    def registered(name):
        if name == "missing":
            raise ValueError("not registered")
        return name

    monkeypatch.setattr(guide.coordination_adapter, "_registered", registered)
    monkeypatch.setattr(guide, "_unavailable_workspace", lambda _: None)
    result = await guide.project_release_guide("missing")
    assert result["error"]["code"] == "PROJECT_NOT_FOUND"
    guide.remote.gitea_get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_mapped_to_different_repository_is_rejected(monkeypatch):
    configure(monkeypatch)
    bad = catalog()
    bad["projects"]["quart-core"]["repository"] = "other"
    bad["projects"]["quart-core"]["workflow"]["repository"] = "other"
    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(return_value=remote_file(bad)))
    result = await guide.project_release_guide("quart-core")
    assert result["ok"] is False
    assert result["error"]["code"] == "SOURCE_HASH_MISMATCH"

@pytest.mark.asyncio
async def test_changed_workflow_is_exposed_without_claiming_release_is_ready(monkeypatch):
    configure(monkeypatch)

    async def read_file(owner, repo, path, **kwargs):
        if repo == "infra-quart":
            return remote_file()
        return {"ok": True, "result": {"sha": "f" * 40, "resolved_commit_sha": "e" * 40}}

    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(side_effect=read_file))
    result = await guide.project_release_guide("quart-core")
    assert result["ok"]
    assert result["result"]["workflow"]["observation"]["status"] == "changed"
    assert result["result"]["ready_for_release"] == "unknown"


@pytest.mark.asyncio
async def test_workflow_lookup_failure_is_unknown_and_does_not_leak_error(monkeypatch):
    configure(monkeypatch)

    async def read_file(owner, repo, path, **kwargs):
        if repo == "infra-quart":
            return remote_file()
        raise RuntimeError("private-error")

    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(side_effect=read_file))
    result = await guide.project_release_guide("quart-core")
    assert result["ok"]
    assert result["result"]["workflow"]["observation"]["status"] == "unknown"
    assert "private-error" not in repr(result)

def test_catalog_supports_many_project_profiles_with_bounded_input():
    value = catalog()
    example = value["projects"]["quart-core"]
    for index in range(100):
        value["projects"][f"service-{index}"] = example
    size = len(json.dumps(value).encode())
    assert 65536 < size < guide._CATALOG_LIMIT
    parsed, _, _ = guide._catalog(remote_file(value)["result"])
    assert len(parsed.projects) == 101


@pytest.mark.asyncio
async def test_registered_project_without_workspace_returns_unverified_workflow_candidates(monkeypatch):
    configure(monkeypatch)

    def registered(name):
        if name == "proxy-parser":
            raise ValueError("workspace absent")
        return name

    class Registry:
        def list_unavailable_projects(self):
            return [{"project_id": "proxy-parser", "source": "source_registry", "reason": "root_missing"}]

    monkeypatch.setattr(guide.coordination_adapter, "_registered", registered)
    monkeypatch.setattr(guide.coordination_adapter, "server_attr", lambda _: lambda: Registry())
    monkeypatch.setattr(
        guide.remote, "gitea_get_repo",
        AsyncMock(return_value={"ok": True, "result": {"full_name": "gpakoh/proxy-parser"}}),
    )
    monkeypatch.setattr(
        guide.remote, "gitea_list_workflows",
        AsyncMock(return_value={"ok": True, "result": {"workflows": [{"name": "CI", "path": "ci.yml"}]}}),
    )
    result = await guide.project_release_guide("proxy-parser")
    assert result["ok"] is True
    assert result["result"]["configured"] is False
    assert result["result"]["workspace"] == {"status": "unavailable", "reason": "root_missing"}
    assert result["result"]["workflow_candidates"] == [{"name": "CI", "path": "ci.yml"}]
    assert result["result"]["ready_for_release"] == "unknown"


@pytest.mark.asyncio
async def test_missing_workspace_cannot_assume_gitea_identity(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(
        guide.coordination_adapter, "_registered",
        lambda name: (_ for _ in ()).throw(ValueError()) if name == "proxy-parser" else name,
    )
    monkeypatch.setattr(guide, "_unavailable_workspace", lambda _: "root_missing")
    monkeypatch.setattr(
        guide.remote, "gitea_get_repo",
        AsyncMock(return_value={"ok": True, "result": {"full_name": "other/proxy-parser"}}),
    )
    result = await guide.project_release_guide("proxy-parser")
    assert result["ok"] is False
    assert result["error"]["code"] == "SOURCE_UNAVAILABLE"
    guide.docker.docker_inspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_can_describe_project_without_containers_or_workflow(monkeypatch):
    configure(monkeypatch)
    data = catalog()
    profile = data["projects"]["quart-core"]
    profile.update({"compose_project": None, "services": [], "workflow": None, "source_files": []})
    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(return_value=remote_file(data)))
    result = await guide.project_release_guide("quart-core")
    assert result["ok"] is True
    assert result["result"]["workflow"] is None
    assert result["result"]["services"] == []
    assert result["result"]["source_freshness"] == "unknown"
    guide.docker.docker_inspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_deploy_source_is_reported_separately_from_workflow(monkeypatch):
    configure(monkeypatch)
    data = catalog()
    data["projects"]["quart-core"]["source_evidence"] = [{
        "repository": "infra-quart", "path": "scripts/deploy-quart-core.sh",
        "ref": "main", "audited_blob_sha": "d" * 40,
    }]

    async def read_file(owner, repo, path, **kwargs):
        if path == guide._CATALOG_PATH:
            return remote_file(data)
        blob = "f" * 40 if path == "scripts/deploy-quart-core.sh" else "d" * 40
        return {"ok": True, "result": {"sha": blob, "resolved_commit_sha": "e" * 40}}

    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(side_effect=read_file))
    result = await guide.project_release_guide("quart-core")
    assert result["ok"] is True
    assert result["result"]["workflow"]["observation"]["status"] == "unchanged"
    assert result["result"]["source_freshness"] == "changed"
    assert result["result"]["ready_for_release"] == "unknown"


@pytest.mark.asyncio
async def test_unchecked_source_never_appears_fresh(monkeypatch):
    configure(monkeypatch)
    data = catalog()
    data["projects"]["quart-core"]["source_evidence"] = [{
        "repository": "infra-quart", "path": "scripts/other.sh",
        "ref": "main", "audited_blob_sha": "d" * 40,
    }]

    async def read_file(owner, repo, path, **kwargs):
        if path == guide._CATALOG_PATH:
            return remote_file(data)
        return {"ok": True, "result": {"sha": "d" * 40, "resolved_commit_sha": "e" * 40}}

    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(side_effect=read_file))
    result = await guide.project_release_guide("quart-core")
    assert result["ok"] is True
    assert result["result"]["source_freshness"] == "unknown"


@pytest.mark.asyncio
async def test_all_reviewed_source_blobs_must_be_available_and_unchanged(monkeypatch):
    configure(monkeypatch)
    data = catalog()
    data["projects"]["quart-core"]["source_evidence"] = [{
        "repository": "infra-quart", "path": "scripts/deploy-quart-core.sh",
        "ref": "main", "audited_blob_sha": "d" * 40,
    }]

    async def read_file(owner, repo, path, **kwargs):
        if path == guide._CATALOG_PATH:
            return remote_file(data)
        return {"ok": True, "result": {"sha": "d" * 40, "resolved_commit_sha": "e" * 40}}

    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(side_effect=read_file))
    result = await guide.project_release_guide("quart-core")
    assert result["result"]["source_freshness"] == "unchanged"

    async def unreadable_source(owner, repo, path, **kwargs):
        if path == "scripts/deploy-quart-core.sh":
            raise RuntimeError("sensitive detail")
        return await read_file(owner, repo, path, **kwargs)

    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(side_effect=unreadable_source))
    result = await guide.project_release_guide("quart-core")
    assert result["result"]["source_freshness"] == "unknown"
    assert "sensitive detail" not in repr(result)


@pytest.mark.asyncio
async def test_unavailable_workspace_uses_reviewed_catalog_repo_mapping(monkeypatch):
    configure(monkeypatch)
    data = catalog()
    data["projects"]["proxy-parser"] = data["projects"].pop("quart-core")
    data["projects"]["proxy-parser"]["repository"] = "proxy-parser"
    data["projects"]["proxy-parser"]["workflow"]["repository"] = "proxy-parser"
    monkeypatch.setattr(
        guide.coordination_adapter, "_registered",
        lambda name: (_ for _ in ()).throw(ValueError()) if name == "proxy-parser" else name,
    )
    monkeypatch.setattr(guide, "_unavailable_workspace", lambda _: "root_missing")
    monkeypatch.setattr(
        guide.remote, "gitea_get_repo",
        AsyncMock(return_value={"ok": True, "result": {"full_name": "gpakoh/proxy-parser"}}),
    )

    async def read_file(owner, repo, path, **kwargs):
        if path == guide._CATALOG_PATH:
            return remote_file(data)
        return {"ok": True, "result": {"sha": "d" * 40, "resolved_commit_sha": "e" * 40}}

    monkeypatch.setattr(guide.remote, "gitea_get_file", AsyncMock(side_effect=read_file))
    result = await guide.project_release_guide("proxy-parser")
    assert result["ok"] is True
    assert result["result"]["configured"] is True
    assert result["result"]["workspace"]["status"] == "unavailable"
    assert result["result"]["repository"] == "gpakoh/proxy-parser"


def test_catalog_rejects_unsafe_source_paths_and_unbound_services():
    data = catalog()
    data["projects"]["quart-core"]["source_evidence"] = [{
        "repository": "infra-quart", "path": "../secret", "ref": "main", "audited_blob_sha": "d" * 40,
    }]
    with pytest.raises(ValueError):
        guide._catalog(remote_file(data)["result"])
    data = catalog()
    data["projects"]["quart-core"]["compose_project"] = None
    with pytest.raises(ValueError):
        guide._catalog(remote_file(data)["result"])
