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
    monkeypatch.setattr(guide.coordination_adapter, "_registered", lambda _: (_ for _ in ()).throw(ValueError()))
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
