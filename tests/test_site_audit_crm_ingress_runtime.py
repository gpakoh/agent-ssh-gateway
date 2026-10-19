"""Read-only Docker-backed CRM ingress readiness and tool-registration regressions."""

from __future__ import annotations

from collections import Counter

import pytest

from examples.mcp_server.mcp_infra.adapters import docker as docker_adapter
from examples.mcp_server.site_audit_crm_ingress_runtime import (
    INGRESS,
    PROTECTED,
    TARGETS,
    IngressReadinessError,
    collect_live_readiness,
)
from examples.mcp_server.tool_modes import TOOL_NAMES_BY_MODE
from examples.mcp_server.tool_scopes import TOOL_SCOPES


def _container(name: str) -> dict:
    n = PROTECTED.index(name) + 1 if name in PROTECTED else TARGETS.index(name) + 11
    return {
        "Id": f"{n:064x}",
        "Image": "sha256:" + f"{n:064x}",
        "Config": {
            "Image": "trusted@sha256:" + "1" * 64,
            "Labels": {
                "com.docker.compose.project": "site-audit-platform",
                "com.docker.compose.config-hash": f"{n:064x}",
            },
        },
        "State": {
            "Running": True,
            "StartedAt": "2026-10-17T19:54:32Z",
            "Health": {"Status": "healthy"},
        },
        "HostConfig": {"PortBindings": {}, "PublishAllPorts": False},
        "NetworkSettings": {"Ports": {}},
        "RestartCount": 0,
    }


class ReadOnlyClient:
    def __init__(self) -> None:
        self.calls = Counter()
        self.exists = False
        self.mutate_calls = []

    async def container_exists(self, name: str) -> bool:
        assert name == INGRESS
        self.calls[name] += 1
        return self.exists

    async def inspect(self, name: str, max_lines: int = 10) -> list[dict]:
        assert name in PROTECTED + TARGETS
        assert max_lines == 10
        self.calls[name] += 1
        return [_container(name)]


@pytest.mark.asyncio
async def test_live_inventory_is_fixed_twice_and_never_authorizes() -> None:
    client = ReadOnlyClient()
    report = await collect_live_readiness(client)
    assert report["status"] == "no_go"
    assert report["authorizes_mutation"] is False
    assert report["protected_unchanged"] is True
    assert report["audit_targets_unchanged"] is True
    assert report["protected_dependencies"] == 6
    assert report["audit_targets"] == 5
    assert report["ingress_absent"] is True
    assert "wan_ipv4_exposure_unverified" in report["blockers"]
    assert "wan_ipv6_exposure_unverified" in report["blockers"]
    assert "protected_compose_drift_unresolved" in report["blockers"]
    assert "immutable_image_missing" in report["blockers"]
    assert all(client.calls[name] == 2 for name in PROTECTED + TARGETS)
    assert client.calls[INGRESS] == 2
    assert not client.mutate_calls


@pytest.mark.asyncio
async def test_existing_ingress_is_not_treated_as_new() -> None:
    client = ReadOnlyClient()
    client.exists = True
    report = await collect_live_readiness(client)
    assert not report["ingress_absent"]
    assert "ingress_already_present_or_unknown" in report["blockers"]


@pytest.mark.asyncio
async def test_detects_replacement_during_read_only_sampling() -> None:
    client = ReadOnlyClient()
    original = client.inspect

    async def changed(name: str, max_lines: int = 10) -> list[dict]:
        item = await original(name, max_lines=max_lines)
        if name == "site-audit-crm-web" and client.calls[name] == 2:
            item[0]["Id"] = "f" * 64
        return item

    client.inspect = changed
    report = await collect_live_readiness(client)
    assert not report["protected_unchanged"]
    assert "protected_changed" in report["blockers"]
    assert report["authorizes_mutation"] is False


@pytest.mark.asyncio
async def test_foreign_or_exposed_dependency_is_blocked() -> None:
    client = ReadOnlyClient()
    original = client.inspect

    async def modified(name: str, max_lines: int = 10) -> list[dict]:
        item = await original(name, max_lines=max_lines)
        if name == "site-audit-crm-web":
            item[0]["Config"]["Labels"]["com.docker.compose.project"] = "other"
            item[0]["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostPort": "8094"}]}
        return item

    client.inspect = modified
    report = await collect_live_readiness(client)
    assert "protected_project_mismatch" in report["blockers"]
    assert "protected_host_port_exposed" in report["blockers"]


@pytest.mark.asyncio
async def test_missing_docker_port_evidence_fails_closed() -> None:
    client = ReadOnlyClient()
    original = client.inspect

    async def incomplete(name: str, max_lines: int = 10) -> list[dict]:
        item = await original(name, max_lines=max_lines)
        if name == "site-audit-crm-web":
            del item[0]["HostConfig"]["PortBindings"]
        return item

    client.inspect = incomplete
    with pytest.raises(IngressReadinessError, match="host_port_evidence_missing"):
        await collect_live_readiness(client)


@pytest.mark.asyncio
async def test_docker_failure_does_not_leak_exception_details() -> None:
    client = ReadOnlyClient()

    async def fail(name: str, max_lines: int = 10) -> list[dict]:
        raise RuntimeError("SECRET-CANARY potentially sensitive host data")

    client.inspect = fail
    with pytest.raises(IngressReadinessError) as exc_info:
        await collect_live_readiness(client)
    assert str(exc_info.value) == "fixed_container_inspection_failed"
    assert "SECRET-CANARY" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_gateway_tool_reports_no_go_without_confirmation(monkeypatch) -> None:
    client = ReadOnlyClient()
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    response = await docker_adapter.docker_site_audit_crm_ingress_readiness()
    assert response["ok"] is True
    report = response["result"]
    assert report["status"] == "no_go"
    assert report["authorizes_mutation"] is False
    assert report["source_validation"] == "not_performed"
    assert report["firewall_policy_validation"] == "not_performed"
    assert not client.mutate_calls


@pytest.mark.asyncio
async def test_gateway_tool_does_not_echo_docker_error(monkeypatch) -> None:
    client = ReadOnlyClient()

    async def fail(name: str, max_lines: int = 10) -> list[dict]:
        raise RuntimeError("SECRET-CANARY")

    client.inspect = fail
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    response = await docker_adapter.docker_site_audit_crm_ingress_readiness()
    assert response["ok"] is False
    assert "SECRET-CANARY" not in repr(response)
    assert response["error"]["code"] == "DEPLOY_CONTRACT_PRECONDITION_FAILED"


def test_readonly_mcp_surface_and_scopes_are_registered() -> None:
    tool = "docker_site_audit_crm_ingress_readiness"
    assert tool in TOOL_NAMES_BY_MODE["mcp_client"]
    assert tool in TOOL_NAMES_BY_MODE["mcp_client_write"]
    assert TOOL_SCOPES[tool] == ["mcp:docker"]
    assert hasattr(docker_adapter, tool)

@pytest.mark.asyncio
async def test_live_audit_api_published_control_port_is_allowed() -> None:
    client = ReadOnlyClient()
    original = client.inspect

    async def published_api(name: str, max_lines: int = 10) -> list[dict]:
        item = await original(name, max_lines=max_lines)
        if name == "site-audit-api":
            item[0]["HostConfig"]["PortBindings"] = {
                "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3005"}]
            }
        return item

    client.inspect = published_api
    report = await collect_live_readiness(client)
    assert report["audit_targets_unchanged"] is True
    assert "targets_host_port_exposed" not in report["blockers"]
    assert "targets_current_host_port_exposed" not in report["blockers"]
    assert report["status"] == "no_go"
    assert report["authorizes_mutation"] is False

@pytest.mark.asyncio
async def test_effective_network_port_binding_blocks_crm_bypass() -> None:
    client = ReadOnlyClient()
    original = client.inspect

    async def port_visible_only_in_network(name: str, max_lines: int = 10) -> list[dict]:
        data = await original(name, max_lines=max_lines)
        if name == "site-audit-crm-web":
            data[0]["NetworkSettings"]["Ports"] = {
                "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8094"}]
            }
        return data

    client.inspect = port_visible_only_in_network
    report = await collect_live_readiness(client)
    assert "protected_host_port_exposed" in report["blockers"]
    assert "protected_current_host_port_exposed" in report["blockers"]
    assert report["authorizes_mutation"] is False


@pytest.mark.asyncio
async def test_publish_all_ports_blocks_crm_even_without_explicit_bindings() -> None:
    client = ReadOnlyClient()
    original = client.inspect

    async def publish_all(name: str, max_lines: int = 10) -> list[dict]:
        data = await original(name, max_lines=max_lines)
        if name == "site-audit-crm-db":
            data[0]["HostConfig"]["PublishAllPorts"] = True
        return data

    client.inspect = publish_all
    report = await collect_live_readiness(client)
    assert "protected_host_port_exposed" in report["blockers"]
    assert report["status"] == "no_go"


@pytest.mark.asyncio
async def test_network_port_metadata_missing_is_rejected() -> None:
    client = ReadOnlyClient()
    original = client.inspect

    async def missing_network(name: str, max_lines: int = 10) -> list[dict]:
        data = await original(name, max_lines=max_lines)
        if name == "site-audit-crm-web":
            data[0].pop("NetworkSettings")
        return data

    client.inspect = missing_network
    with pytest.raises(IngressReadinessError, match="network_port_evidence_missing"):
        await collect_live_readiness(client)


@pytest.mark.asyncio
async def test_publish_all_metadata_missing_is_rejected() -> None:
    client = ReadOnlyClient()
    original = client.inspect

    async def missing_publish_all(name: str, max_lines: int = 10) -> list[dict]:
        data = await original(name, max_lines=max_lines)
        if name == "site-audit-crm-web":
            del data[0]["HostConfig"]["PublishAllPorts"]
        return data

    client.inspect = missing_publish_all
    with pytest.raises(IngressReadinessError, match="publish_all_ports_evidence_missing"):
        await collect_live_readiness(client)