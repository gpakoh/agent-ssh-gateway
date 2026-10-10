from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from tool_modes import tools_for_mode
from tool_scopes import get_required_scopes

import examples.mcp_server.mcp_infra.adapters.agent as agent_adapter
from examples.mcp_server.fleet_state import WorkerLease

POOL = "ssh-gateway/agent-sshd"
TASK_ID = "demo:historical-task"
LEASE_TOKEN = "11111111-1111-1111-1111-111111111111"


def test_fleet_admin_tools_are_write_only_and_admin_scoped():
    safe_tools = tools_for_mode("mcp_client")
    write_tools = tools_for_mode("mcp_client_write")
    for name in ("fleet_status", "fleet_reconcile_unbound"):
        assert name not in safe_tools
        assert name in write_tools
        assert get_required_scopes(name) == ["mcp:agent-run", "mcp:admin"]


def _lease(*, submit_state: str = "attempted", job_id: str | None = None) -> WorkerLease:
    return WorkerLease(
        task_id=TASK_ID,
        pool=POOL,
        lease_token=LEASE_TOKEN,
        coordinator_id="old-coordinator",
        job_id=job_id,
        claimed_at=None,
        heartbeat_at=None,
        submit_state=submit_state,
        submit_attempted_at=None,
    )


@pytest.mark.asyncio
async def test_fleet_status_reports_disabled_without_runtime(monkeypatch):
    get_fleet = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", get_fleet)

    result = await agent_adapter.gateway_fleet_status()

    assert result["ok"] is True
    assert result["result"] == {"enabled": False, "reason": "fleet_disabled"}


@pytest.mark.asyncio
async def test_fleet_reconcile_requires_explicit_ack_before_runtime_lookup(monkeypatch):
    get_fleet = AsyncMock()
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", get_fleet)

    result = await agent_adapter.gateway_fleet_reconcile_unbound(
        TASK_ID,
        LEASE_TOKEN,
        "attempted",
        acknowledge=False,
        reason="operator reviewed historical row",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    get_fleet.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_reconcile_rejects_invalid_fence_before_runtime_lookup(monkeypatch):
    get_fleet = AsyncMock()
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", get_fleet)

    result = await agent_adapter.gateway_fleet_reconcile_unbound(
        TASK_ID,
        "not-a-uuid",
        "attempted",
        acknowledge=True,
        reason="operator reviewed historical row",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    get_fleet.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_reconcile_resolver_miss_without_generation_evidence_is_non_mutating(monkeypatch):
    fleet = MagicMock()
    fleet.pool_name = POOL
    fleet.state.get_lease = AsyncMock(return_value=_lease())
    fleet.reconcile_unbound_lease = AsyncMock()
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", AsyncMock(return_value=fleet))
    monkeypatch.setattr(agent_adapter, "_trusted_fleet_job_resolver", lambda: MagicMock(return_value=None))
    boundary = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_adapter, "_execution_plane_recovery_boundary", boundary)

    result = await agent_adapter.gateway_fleet_reconcile_unbound(
        TASK_ID,
        LEASE_TOKEN,
        "attempted",
        acknowledge=True,
        reason="operator reviewed historical row",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "CHECK_FAILED"
    boundary.assert_awaited_once()
    fleet.reconcile_unbound_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_reconcile_lost_bind_response_is_idempotent_without_second_audit(monkeypatch):
    fleet = MagicMock()
    fleet.pool_name = POOL
    fleet.state.get_lease = AsyncMock(return_value=_lease(job_id="job-existing"))
    fleet.reconcile_unbound_lease = AsyncMock()
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", AsyncMock(return_value=fleet))
    monkeypatch.setattr(
        agent_adapter,
        "_trusted_fleet_job_resolver",
        lambda: MagicMock(return_value="job-existing"),
    )
    monkeypatch.setattr(
        agent_adapter,
        "server_attr",
        lambda _name: (_ for _ in ()).throw(AssertionError("audit must not repeat")),
    )

    result = await agent_adapter.gateway_fleet_reconcile_unbound(
        TASK_ID,
        LEASE_TOKEN,
        "attempted",
        acknowledge=True,
        reason="operator reviewed historical row",
    )

    assert result["ok"] is True
    assert result["result"] == {
        "action": "already_bound_existing_job",
        "task_id": TASK_ID,
        "job_id": "job-existing",
        "released": False,
    }
    fleet.reconcile_unbound_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_reconcile_bound_row_with_mismatched_trusted_identity_fails_closed(monkeypatch):
    fleet = MagicMock()
    fleet.pool_name = POOL
    fleet.state.get_lease = AsyncMock(return_value=_lease(job_id="job-bound"))
    fleet.reconcile_unbound_lease = AsyncMock()
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", AsyncMock(return_value=fleet))
    monkeypatch.setattr(
        agent_adapter,
        "_trusted_fleet_job_resolver",
        lambda: MagicMock(return_value="job-other"),
    )

    result = await agent_adapter.gateway_fleet_reconcile_unbound(
        TASK_ID,
        LEASE_TOKEN,
        "attempted",
        acknowledge=True,
        reason="operator reviewed historical row",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "WORKSPACE_CONTENDED"
    fleet.reconcile_unbound_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_reconcile_idempotent_retry_does_not_emit_second_intent(monkeypatch):
    fleet = MagicMock()
    fleet.pool_name = POOL
    fleet.state.get_lease = AsyncMock(return_value=None)
    fleet.reconcile_unbound_lease = AsyncMock(
        return_value={
            "action": "already_reconciled",
            "task_id": TASK_ID,
            "terminal_status": "ambiguous",
            "released": True,
        }
    )
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", AsyncMock(return_value=fleet))
    monkeypatch.setattr(
        agent_adapter,
        "_trusted_fleet_job_resolver",
        lambda: (_ for _ in ()).throw(AssertionError("resolver must not run on idempotent retry")),
    )
    monkeypatch.setattr(
        agent_adapter,
        "server_attr",
        lambda _name: (_ for _ in ()).throw(AssertionError("audit must not repeat")),
    )

    result = await agent_adapter.gateway_fleet_reconcile_unbound(
        TASK_ID,
        LEASE_TOKEN,
        "attempted",
        acknowledge=True,
        reason="operator reviewed historical row",
    )

    assert result["ok"] is True
    assert result["result"]["action"] == "already_reconciled"
    fleet.reconcile_unbound_lease.assert_awaited_once()


@pytest.mark.asyncio
async def test_fleet_reconcile_audit_failure_blocks_binding_or_release(monkeypatch):
    fleet = MagicMock()
    fleet.pool_name = POOL
    fleet.state.get_lease = AsyncMock(return_value=_lease())
    fleet.reconcile_unbound_lease = AsyncMock()
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", AsyncMock(return_value=fleet))
    monkeypatch.setattr(
        agent_adapter,
        "_trusted_fleet_job_resolver",
        lambda: MagicMock(return_value="job-existing"),
    )
    audit = MagicMock()
    audit.append_required.side_effect = agent_adapter.AuditWriteError("disk unavailable")
    monkeypatch.setattr(
        agent_adapter,
        "server_attr",
        lambda name: (lambda: audit) if name == "get_audit_logger" else None,
    )

    result = await agent_adapter.gateway_fleet_reconcile_unbound(
        TASK_ID,
        LEASE_TOKEN,
        "attempted",
        acknowledge=True,
        reason="operator reviewed historical row",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    fleet.reconcile_unbound_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_reconcile_audits_before_binding_recovered_job(monkeypatch):
    order: list[str] = []
    fleet = MagicMock()
    fleet.pool_name = POOL
    fleet.state.get_lease = AsyncMock(return_value=_lease())

    async def reconcile(**kwargs):
        order.append("mutation")
        assert kwargs["resolved_job_id"] == "job-existing"
        assert kwargs["recovery_boundary"] is None
        return {
            "action": "bound_existing_job",
            "task_id": TASK_ID,
            "job_id": "job-existing",
            "released": False,
        }

    fleet.reconcile_unbound_lease = AsyncMock(side_effect=reconcile)
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", AsyncMock(return_value=fleet))
    monkeypatch.setattr(
        agent_adapter,
        "_trusted_fleet_job_resolver",
        lambda: MagicMock(return_value="job-existing"),
    )
    boundary = AsyncMock()
    monkeypatch.setattr(agent_adapter, "_execution_plane_recovery_boundary", boundary)

    audit = MagicMock()
    audit.append_required.side_effect = lambda _event: order.append("audit")
    monkeypatch.setattr(
        agent_adapter,
        "server_attr",
        lambda name: (lambda: audit) if name == "get_audit_logger" else None,
    )

    result = await agent_adapter.gateway_fleet_reconcile_unbound(
        TASK_ID,
        LEASE_TOKEN,
        "attempted",
        acknowledge=True,
        reason="operator reviewed historical row",
    )

    assert result["ok"] is True
    assert result["result"]["action"] == "bound_existing_job"
    assert order == ["audit", "mutation"]
    boundary.assert_not_awaited()
    event = audit.append_required.call_args.args[0]
    assert "lease_token" not in event.metadata
    expected_fence = hashlib.sha256(LEASE_TOKEN.encode()).hexdigest()
    assert event.metadata["lease_fence_sha256"] == expected_fence
    assert agent_adapter.redact_secrets(event.metadata)["lease_fence_sha256"] == expected_fence
    assert event.request_id == result["result"]["correlation_id"]


@pytest.mark.asyncio
async def test_fleet_reconcile_audits_before_generation_tombstone_release(monkeypatch):
    order: list[str] = []
    fleet = MagicMock()
    fleet.pool_name = POOL
    fleet.state.get_lease = AsyncMock(return_value=_lease())
    boundary = MagicMock(name="recovery-boundary")

    async def reconcile(**kwargs):
        order.append("mutation")
        assert kwargs["resolved_job_id"] is None
        assert kwargs["recovery_boundary"] is boundary
        return {
            "action": "tombstoned_ambiguous",
            "task_id": TASK_ID,
            "terminal_status": "ambiguous",
            "released": True,
        }

    fleet.reconcile_unbound_lease = AsyncMock(side_effect=reconcile)
    monkeypatch.setattr(agent_adapter, "get_fleet_runtime", AsyncMock(return_value=fleet))
    monkeypatch.setattr(
        agent_adapter,
        "_trusted_fleet_job_resolver",
        lambda: MagicMock(return_value=None),
    )
    recovery = AsyncMock(return_value=boundary)
    monkeypatch.setattr(agent_adapter, "_execution_plane_recovery_boundary", recovery)

    audit = MagicMock()
    audit.append_required.side_effect = lambda _event: order.append("audit")
    monkeypatch.setattr(
        agent_adapter,
        "server_attr",
        lambda name: (lambda: audit) if name == "get_audit_logger" else None,
    )

    result = await agent_adapter.gateway_fleet_reconcile_unbound(
        TASK_ID,
        LEASE_TOKEN,
        "attempted",
        acknowledge=True,
        reason="operator reviewed replacement generations",
    )

    assert result["ok"] is True
    assert result["result"]["action"] == "tombstoned_ambiguous"
    assert result["result"]["released"] is True
    assert order == ["audit", "mutation"]
    recovery.assert_awaited_once()
    fleet.reconcile_unbound_lease.assert_awaited_once()
    audit.append_required.assert_called_once()
    audit.append.assert_called_once()
    intent = audit.append_required.call_args.args[0]
    outcome = audit.append.call_args.args[0]
    assert intent.request_id == outcome.request_id == result["result"]["correlation_id"]
    assert outcome.metadata["result_action"] == "tombstoned_ambiguous"
