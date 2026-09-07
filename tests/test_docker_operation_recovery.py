from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from examples.mcp_server.docker_operations import (
    ComposeOperationRequest,
    ComposeServiceSetConflict,
    DockerExecutionResult,
    DockerJournalUnavailable,
    DockerOperationCoordinator,
    DockerOperationStatus,
    InMemoryDockerOperationJournal,
    build_compose_request,
)


def _request(action_id: str = "act-1", **kwargs):
    base_kwargs = {
        "services": ["api"],
        "timeout": 30,
        "detach": True,
        "build": False,
    }
    base_kwargs.update(kwargs.pop("compose_kwargs", {}))
    return build_compose_request(
        action_id=action_id,
        owner_fingerprint=kwargs.pop("owner", "owner-a"),
        tool=kwargs.pop("tool", "docker_compose_up"),
        project_identity=kwargs.pop("project_identity", "project:demo"),
        compose_config_digest=kwargs.pop("config_digest", "sha256:compose"),
        kwargs=base_kwargs,
        resolved_services=kwargs.pop("resolved_services", None),
        transport_wait_deadline_seconds=kwargs.pop("transport_wait", 35),
    )


def test_request_metadata_is_bounded_sorted_and_immutable():
    request = _request(compose_kwargs={"services": ["worker", "api", "api"]})

    public = request.public_request()

    assert public["services"] == ["api", "worker"]
    assert "owner_fingerprint" not in public
    assert "path" not in public
    assert request.request_digest.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        request.tool = "docker_compose_down"  # type: ignore[misc]


def test_untracked_compose_tool_is_rejected_before_acceptance():
    with pytest.raises(ValueError, match="untracked compose tool"):
        ComposeOperationRequest(
            action_id="act-unsupported",
            owner_fingerprint="owner-a",
            tool="docker_ps",
            project_identity="project:demo",
            compose_config_digest="sha256:compose",
            services=("api",),
        )


@pytest.mark.asyncio
async def test_lost_response_replay_returns_existing_receipt_without_second_dispatch():
    calls = 0

    async def executor(_request_obj):
        nonlocal calls
        calls += 1
        return DockerExecutionResult(
            ok=True,
            evidence={"containers": [{"service": "api", "state": "running"}]},
        )

    journal = InMemoryDockerOperationJournal()
    coordinator = DockerOperationCoordinator(journal, executor)
    request = _request()

    first = await coordinator.submit(request)
    replay = await coordinator.submit(request)

    assert first["status"] == DockerOperationStatus.SUCCEEDED.value
    assert replay["status"] == DockerOperationStatus.SUCCEEDED.value
    assert first["request_digest"] == replay["request_digest"]
    assert first["dispatch_count"] == 1
    assert replay["dispatch_count"] == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_confirmation_claim_dispatches_once():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def executor(_request_obj):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return DockerExecutionResult(ok=True)

    journal = InMemoryDockerOperationJournal()
    coordinator = DockerOperationCoordinator(journal, executor)
    request = _request()

    first_task = asyncio.create_task(coordinator.submit(request))
    await started.wait()
    second = await coordinator.submit(request)
    release.set()
    first = await first_task

    assert calls == 1
    assert first["dispatch_count"] == 1
    assert second["dispatch_count"] == 1
    assert second["status"] == DockerOperationStatus.RUNNING.value


@pytest.mark.asyncio
async def test_journal_accept_failure_happens_before_docker_dispatch():
    calls = 0

    async def executor(_request_obj):
        nonlocal calls
        calls += 1
        return DockerExecutionResult(ok=True)

    coordinator = DockerOperationCoordinator(
        InMemoryDockerOperationJournal(fail_accept=True),
        executor,
    )

    with pytest.raises(DockerJournalUnavailable):
        await coordinator.submit(_request())

    assert calls == 0


@pytest.mark.asyncio
async def test_unfinished_claim_after_restart_becomes_ambiguous_without_redispatch():
    calls = 0

    async def hanging_executor(_request_obj):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        return DockerExecutionResult(ok=True)

    journal = InMemoryDockerOperationJournal()
    coordinator = DockerOperationCoordinator(journal, hanging_executor)
    request = _request("act-restart")

    task = asyncio.create_task(coordinator.submit(request))
    while calls == 0:
        await asyncio.sleep(0)

    restarted = DockerOperationCoordinator(
        journal,
        lambda _request_obj: asyncio.sleep(0, result=DockerExecutionResult(ok=True)),
    )
    reconciled = await restarted.reconcile_after_restart()
    status = await restarted.status("act-restart", "owner-a")
    replay = await restarted.submit(request)

    task.cancel()

    assert len(reconciled) == 1
    assert status["status"] == DockerOperationStatus.AMBIGUOUS.value
    assert status["phase"] == "reconciling"
    assert status["dispatch_count"] == 1
    assert status["evidence"]["redispatch_allowed"] is False
    assert replay["dispatch_count"] == 1
    assert replay["status"] == DockerOperationStatus.AMBIGUOUS.value


@pytest.mark.asyncio
async def test_owner_mismatch_status_is_bounded_and_does_not_expose_request_payload():
    async def executor(_request_obj):
        return DockerExecutionResult(ok=True)

    coordinator = DockerOperationCoordinator(InMemoryDockerOperationJournal(), executor)
    request = _request(compose_kwargs={"services": ["api"], "timeout": 30})
    await coordinator.submit(request)

    wrong_owner = await coordinator.status("act-1", "owner-b")

    assert wrong_owner == {
        "action_id": "act-1",
        "status": "operation_not_tracked",
    }


def test_services_none_must_be_resolved_before_acceptance():
    with pytest.raises(ComposeServiceSetConflict):
        build_compose_request(
            action_id="act-all",
            owner_fingerprint="owner-a",
            tool="docker_compose_restart",
            project_identity="project:demo",
            compose_config_digest="sha256:compose",
            kwargs={"services": None, "timeout": 20},
        )


def test_resolved_service_set_is_immutable_and_sorted_in_receipt():
    request = build_compose_request(
        action_id="act-all",
        owner_fingerprint="owner-a",
        tool="docker_compose_down",
        project_identity="project:demo",
        compose_config_digest="sha256:compose",
        kwargs={"services": None, "timeout": 20, "remove_orphans": False, "volumes": False},
        resolved_services=["worker", "api", "api"],
        transport_wait_deadline_seconds=30,
    )

    public = request.public_request()

    assert public["services"] == ["api", "worker"]
    assert public["stop_grace_seconds"] == 20
    assert public["execution_deadline_seconds"] == 20
    assert public["transport_wait_deadline_seconds"] == 30


@pytest.mark.asyncio
async def test_timeout_result_maps_to_ambiguous_not_failed_noop():
    async def executor(_request_obj):
        raise TimeoutError("docker compose wait deadline exceeded")

    coordinator = DockerOperationCoordinator(InMemoryDockerOperationJournal(), executor)
    result = await coordinator.submit(_request())

    assert result["status"] == DockerOperationStatus.AMBIGUOUS.value
    assert result["phase"] == "final"
    assert result["result"]["timed_out"] is True
    assert result["result"]["exit_code"] is None
