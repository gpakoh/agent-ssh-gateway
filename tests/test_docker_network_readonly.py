"""Issue #650: configured-daemon network inventory, fail-closed and read-only."""

from __future__ import annotations

import json
import re

import pytest

from examples.mcp_client_remote.fleet.docker_client import DOCKER_BIN, DockerClient
from examples.mcp_server.mcp_infra.adapters import docker as gateway_docker
from examples.mcp_server.tool_modes import should_register_tool
from examples.mcp_server.tool_scopes import get_required_scopes

ID1 = "a" * 64
ID2 = "b" * 64
CONTAINER = "c" * 64


def _network(
    name: str = "security-smoke.jobs", network_id: str = ID1,
    containers: dict | None = None,
) -> dict:
    return {
        "Id": network_id, "Name": name, "Driver": "bridge", "Scope": "local",
        "Internal": True, "Containers": containers if containers is not None else {},
        "Labels": {
            "com.docker.compose.project": "security-smoke",
            "com.docker.compose.network": "jobs",
            "secret.token": "super-secret",
            "com.docker.compose.project.working_dir": "/root/private",
        },
        "IPAM": {"Config": [{"Subnet": "10.0.0.0/8"}]},
        "Options": {"com.docker.network.bridge.name": "private-bridge"},
    }


@pytest.mark.asyncio
async def test_network_ls_exact_filter_and_no_raw_labels(monkeypatch):
    client = DockerClient()
    seen = []

    async def runner(argv, **_):
        seen.append(argv)
        return json.dumps({"ID": ID1, "Name": "security-smoke.jobs",
                           "Driver": "bridge", "Scope": "local",
                           "Labels": "secret.token=super-secret"})

    monkeypatch.setattr(client, "_run", runner)
    rows = await client.network_ls(name="security-smoke.jobs", limit=3)
    assert seen == [[DOCKER_BIN, "network", "ls", "--no-trunc", "--format", "json",
                     "--filter", f"name=^{re.escape('security-smoke.jobs')}$"]]
    assert rows == [{"id": ID1, "name": "security-smoke.jobs",
                     "driver": "bridge", "scope": "local"}]
    assert client.last_truncated is False
    assert "secret" not in repr(rows)


@pytest.mark.asyncio
async def test_network_ls_absence_only_when_full_and_well_formed(monkeypatch):
    client = DockerClient()

    async def empty(_argv):
        client.last_output_truncated = False
        return ""

    monkeypatch.setattr(client, "_run", empty)
    monkeypatch.setattr(gateway_docker, "_docker_client", lambda: client)
    absent = await gateway_docker.docker_network_ls(name="missing")
    assert absent["ok"] is True
    assert absent["result"]["query_status"] == "absent"
    assert absent["result"]["inventory"]["host_completeness"] == "unknown"

    async def truncated(_argv):
        client.last_output_truncated = True
        return "[output truncated]"

    monkeypatch.setattr(client, "_run", truncated)
    uncertain = await gateway_docker.docker_network_ls(name="missing")
    assert uncertain["ok"] is True
    assert uncertain["result"]["query_status"] == "unknown"
    assert uncertain["result"]["inventory"]["daemon_completeness"] == "truncated"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "bad/name", "../a", "a;id", "a\nb", "a" * 129, 4])
async def test_network_name_rejected_before_docker(monkeypatch, name):
    client = DockerClient()

    async def cannot_run(_argv):
        raise AssertionError("untrusted name reached docker")

    monkeypatch.setattr(client, "_run", cannot_run)
    with pytest.raises(ValueError):
        await client.network_ls(name=name)
    with pytest.raises(ValueError):
        await client.network_inspect(name=name)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, -1, "2", True])
async def test_network_ls_limit_validation(monkeypatch, limit):
    client = DockerClient()

    async def cannot_run(_argv):
        raise AssertionError("invalid limit reached docker")

    monkeypatch.setattr(client, "_run", cannot_run)
    with pytest.raises(ValueError):
        await client.network_ls(limit=limit)


@pytest.mark.asyncio
async def test_network_ls_rejects_bad_full_id_and_malformed_json(monkeypatch):
    client = DockerClient()

    async def badid(_argv):
        return json.dumps({"ID": "12345", "Name": "jobs"}) + "\n"

    monkeypatch.setattr(client, "_run", badid)
    with pytest.raises(RuntimeError, match="full ID"):
        await client.network_ls()

    async def malformed(_argv):
        return "not JSON\n"

    monkeypatch.setattr(client, "_run", malformed)
    with pytest.raises(RuntimeError, match="malformed"):
        await client.network_ls()


@pytest.mark.asyncio
async def test_network_ls_limit_exposes_partial_not_global(monkeypatch):
    client = DockerClient()

    async def runner(_argv):
        return "\n".join(json.dumps({
            "ID": chr(97 + i) * 64, "Name": f"network-{i}",
            "Driver": "bridge", "Scope": "local"
        }) for i in range(3))

    monkeypatch.setattr(client, "_run", runner)
    monkeypatch.setattr(gateway_docker, "_docker_client", lambda: client)
    result = await gateway_docker.docker_network_ls(limit=2)
    assert result["ok"] is True
    assert result["result"]["count"] == 2
    assert result["meta"]["truncated"] is True
    assert result["result"]["inventory"]["daemon_completeness"] == "truncated"


@pytest.mark.asyncio
async def test_network_inspect_sanitizes_privacy_and_proves_attachment_ids(monkeypatch):
    client = DockerClient()
    seen = []

    async def runner(argv):
        seen.append(argv)
        return json.dumps([_network(containers={
            CONTAINER: {"Name": "private-hostname", "IPv4Address": "10.1.1.2/24"}
        })])

    monkeypatch.setattr(client, "_run", runner)
    result = await client.network_inspect("security-smoke.jobs")
    assert seen == [[DOCKER_BIN, "network", "inspect", "security-smoke.jobs"]]
    assert result["status"] == "present"
    net = result["network"]
    assert net["id"] == ID1 and net["attached_container_ids"] == [CONTAINER]
    assert net["attached_count"] == 1 and net["attachments_complete"] is True
    assert net["labels"] == {
        "com.docker.compose.project": "security-smoke",
        "com.docker.compose.network": "jobs",
    }
    assert net["other_label_values_redacted"] is True
    for secret in ("10.1", "/root", "super-secret", "private-hostname"):
        assert secret not in repr(net)


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", [
    "docker exited 1: Error: No such network: missing",
    "docker exited 1: Error response from daemon: network missing not found",
])
async def test_network_inspect_proven_absent_is_not_a_weak_error_match(monkeypatch, diagnostic):
    client = DockerClient()

    async def absent(_argv):
        raise RuntimeError(diagnostic)

    monkeypatch.setattr(client, "_run", absent)
    assert await client.network_inspect("missing") == {
        "status": "absent", "network": None,
    }
    monkeypatch.setattr(gateway_docker, "_docker_client", lambda: client)
    reply = await gateway_docker.docker_network_inspect("missing")
    assert reply["ok"] is True
    assert reply["result"]["status"] == "absent"
    assert reply["result"]["inventory"]["scope"] == "configured_docker_daemon"

    async def ambiguous(_argv):
        raise RuntimeError("docker exited 1: daemon not responding")

    monkeypatch.setattr(client, "_run", ambiguous)
    uncertain = await gateway_docker.docker_network_inspect("missing")
    assert uncertain["ok"] is False
    assert uncertain["error"]["code"] == "DOCKER_COMMAND_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    "not json", "[]", "[{}, {}]", json.dumps([{"Id": "short", "Name": "network"}]),
    json.dumps([_network(containers={CONTAINER[:12]: {}})]),
    json.dumps([_network(containers=[])]),
])
async def test_network_inspect_fails_closed_on_bad_identity(raw, monkeypatch):
    client = DockerClient()

    async def runner(_argv):
        return raw

    monkeypatch.setattr(client, "_run", runner)
    with pytest.raises(RuntimeError):
        await client.network_inspect("security-smoke.jobs")


@pytest.mark.asyncio
async def test_network_inspect_truncation_never_proves_absence(monkeypatch):
    client = DockerClient()

    async def runner(_argv):
        client.last_output_truncated = True
        return "[]\n[output truncated]"

    monkeypatch.setattr(client, "_run", runner)
    with pytest.raises(RuntimeError, match="incomplete"):
        await client.network_inspect("network")


@pytest.mark.asyncio
async def test_network_inventory_does_not_accept_inexact_filter(monkeypatch):
    client = DockerClient()

    async def wrong(_argv):
        return json.dumps({
            "ID": ID2, "Name": "security-smoke-jobs-other",
            "Driver": "bridge", "Scope": "local",
        })

    monkeypatch.setattr(client, "_run", wrong)
    with pytest.raises(RuntimeError, match="exact-name"):
        await client.network_ls(name="security-smoke-jobs")
    monkeypatch.setattr(gateway_docker, "_docker_client", lambda: client)
    response = await gateway_docker.docker_network_ls(name="security-smoke-jobs")
    assert response["ok"] is False
    assert response["error"]["code"] == "DOCKER_COMMAND_FAILED"


@pytest.mark.asyncio
async def test_network_inspect_refuses_partial_name_or_missing_internal(monkeypatch):
    client = DockerClient()

    async def other(_argv):
        return json.dumps([_network(name="security-smoke-jobs-other")])

    monkeypatch.setattr(client, "_run", other)
    with pytest.raises(RuntimeError, match="identity differs"):
        await client.network_inspect("security-smoke-jobs")

    async def malformed_internal(_argv):
        data = _network()
        data["Internal"] = "false"
        return json.dumps([data])

    monkeypatch.setattr(client, "_run", malformed_internal)
    with pytest.raises(RuntimeError, match="metadata"):
        await client.network_inspect("security-smoke.jobs")


@pytest.mark.asyncio
async def test_network_inspect_rejects_more_than_100_attachments(monkeypatch):
    client = DockerClient()

    async def too_many(_argv):
        attached = {f"{i:064x}": {"Name": f"private-{i}"} for i in range(101)}
        return json.dumps([_network(containers=attached)])

    monkeypatch.setattr(client, "_run", too_many)
    with pytest.raises(RuntimeError, match="more than 100"):
        await client.network_inspect("security-smoke.jobs")


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", [
    "docker exited 1: Error: No such network: missing2",
    "docker exited 1: Error response from daemon: network missing2 not found",
    "docker exited 2: Error response from daemon: network missing not found",
    "docker exited 1: Error response from daemon: network missing not found; connection lost",
    "Command timed out after 30s: docker network inspect missing",
])
async def test_network_not_found_error_requires_exact_target(monkeypatch, diagnostic):
    client = DockerClient()

    async def uncertain(_argv):
        raise RuntimeError(diagnostic)

    monkeypatch.setattr(client, "_run", uncertain)
    with pytest.raises(RuntimeError):
        await client.network_inspect("missing")


def test_network_tools_only_require_docker_read_scope():
    for tool in ("docker_network_ls", "docker_network_inspect"):
        assert get_required_scopes(tool) == ["mcp:docker"]
        assert should_register_tool(tool, "mcp_client") is True
