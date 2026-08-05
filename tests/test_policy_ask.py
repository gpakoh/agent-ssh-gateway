"""Tests for DecisionMode Ask (operator approval)."""

from __future__ import annotations

from app.command_policy import (
    CommandPolicyMode,
    evaluate_command_policy,
)
from app.policy_ask import (
    approve_request,
    create_approval_request,
    deny_request,
    find_and_consume_approval,
    get_pending_requests,
    get_request,
)


class TestCommandPolicyMode:
    def test_ask_mode_exists(self):
        assert CommandPolicyMode.ASK == "ask"

    def test_enforce_blocks_heredoc(self):
        r = evaluate_command_policy(
            "cat << EOF\nrm -rf /data\nEOF",
            mode="enforce",
            profile="default",
        )
        assert not r.allowed
        assert not r.requires_approval

    def test_ask_heredoc_requires_approval(self):
        r = evaluate_command_policy(
            "cat << EOF\nrm -rf /data\nEOF",
            mode="ask",
            profile="default",
        )
        assert not r.allowed
        assert r.requires_approval
        assert r.approval_id is not None

    def test_ask_profile_requires_approval(self):
        """Profile gate in ask mode should also require approval."""
        r = evaluate_command_policy("rm /etc", mode="ask", profile="default")
        if not r.allowed and r.requires_approval:
            assert r.approval_id is not None

    def test_ask_metachar_still_blocks(self):
        r = evaluate_command_policy("ls | grep foo", mode="ask", profile="default")
        assert not r.allowed
        assert not r.requires_approval

    def test_ask_argument_shape_still_blocks(self):
        r = evaluate_command_policy(
            "python3 -c 'print(1)'",
            mode="ask",
            profile="default",
        )
        assert not r.allowed
        assert not r.requires_approval

    def test_ask_harmless_allowed(self):
        r = evaluate_command_policy("ls -la", mode="ask", profile="default")
        assert r.allowed

    def test_off_mode_ignores_ask(self):
        r = evaluate_command_policy(
            "cat << EOF\nrm -rf /data\nEOF",
            mode="off",
            profile="default",
        )
        assert r.allowed
        assert not r.requires_approval

    def test_audit_logs_ask_decision(self):
        r = evaluate_command_policy(
            "cat << EOF\nrm -rf /data\nEOF",
            mode="audit",
            profile="default",
        )
        assert r.allowed  # audit never blocks
        assert "AUDIT_ONLY" in r.reason
        assert not r.requires_approval


class TestApprovalStore:
    def test_create_and_get(self):
        req = create_approval_request("rm -rf /data", "default", "profile", "denied")
        assert req.approval_id is not None
        assert req.approved is None  # pending

        got = get_request(req.approval_id)
        assert got is not None
        assert got.approval_id == req.approval_id

    def test_approve(self):
        req = create_approval_request("rm -rf /", "default", "profile", "denied")
        ok = approve_request(req.approval_id, "operator1")
        assert ok

        got = get_request(req.approval_id)
        assert got is not None
        assert got.approved is True
        assert got.approved_by == "operator1"

    def test_deny(self):
        req = create_approval_request("docker system prune", "docker-admin", "profile", "denied")
        ok = deny_request(req.approval_id, "operator2")
        assert ok

        got = get_request(req.approval_id)
        assert got is not None
        assert got.approved is False
        assert got.approved_by == "operator2"

    def test_double_approve_fails(self):
        req = create_approval_request("rm -rf /tmp", "default", "profile", "denied")
        assert approve_request(req.approval_id)
        assert not approve_request(req.approval_id)  # already approved

    def test_double_deny_fails(self):
        req = create_approval_request("rm -rf /tmp", "default", "profile", "denied")
        assert deny_request(req.approval_id)
        assert not deny_request(req.approval_id)  # already denied

    def test_approve_after_deny_fails(self):
        req = create_approval_request("rm -rf /tmp", "default", "profile", "denied")
        assert deny_request(req.approval_id)
        assert not approve_request(req.approval_id)  # already denied

    def test_get_nonexistent(self):
        assert get_request("nonexistent") is None

    def test_approve_nonexistent(self):
        assert not approve_request("nonexistent")

    def test_get_pending_requests(self):
        # Clear store
        from app.policy_ask import _store
        _store.clear()

        req1 = create_approval_request("cmd1", "default", "profile", "r1")
        req2 = create_approval_request("cmd2", "default", "profile", "r2")
        approve_request(req1.approval_id)

        pending = get_pending_requests()
        assert len(pending) == 1
        assert pending[0].approval_id == req2.approval_id

    def test_approval_id_uniqueness(self):
        ids = {
            create_approval_request("cmd", "default", "profile", "r").approval_id
            for _ in range(100)
        }
        assert len(ids) == 100  # all unique


class TestFindAndConsumeApproval:
    """Regression: approving a request used to do nothing observable —
    nothing ever read the approved flag back to let the original command
    through. evaluate_command_policy()'s ASK branch now calls this before
    creating a new approval request, so a caller's retry of the identical
    command is what actually consumes the approval.
    """

    def test_returns_none_when_no_matching_request(self):
        assert find_and_consume_approval("nonexistent-cmd", "default") is None

    def test_returns_none_for_pending_request(self):
        create_approval_request("docker system prune -f", "docker-admin", "profile", "r")
        assert find_and_consume_approval("docker system prune -f", "docker-admin") is None

    def test_returns_none_for_denied_request(self):
        req = create_approval_request("rm -rf /tmp/x", "default", "profile", "r")
        deny_request(req.approval_id)
        assert find_and_consume_approval("rm -rf /tmp/x", "default") is None

    def test_returns_approved_request_and_consumes_it(self):
        req = create_approval_request("docker system prune -f", "docker-admin", "profile", "r")
        approve_request(req.approval_id, "operator3")

        found = find_and_consume_approval("docker system prune -f", "docker-admin")
        assert found is not None
        assert found.approval_id == req.approval_id
        assert found.approved_by == "operator3"

        # Consumed — a second lookup for the same command must not replay it.
        assert find_and_consume_approval("docker system prune -f", "docker-admin") is None
        assert get_request(req.approval_id) is None

    def test_profile_must_also_match(self):
        req = create_approval_request("rm -rf /tmp/y", "default", "profile", "r")
        approve_request(req.approval_id)
        assert find_and_consume_approval("rm -rf /tmp/y", "docker-admin") is None


class TestAskModeApprovalConsumedOnRetry:
    """End-to-end: blocked in ASK mode -> operator approves -> the caller's
    retry of the identical command is what actually lets it through.
    """

    def test_retry_after_approval_is_allowed(self):
        command = "docker system prune -f"
        first = evaluate_command_policy(command, mode="ask", profile="docker-admin")
        assert not first.allowed
        assert first.requires_approval
        assert first.approval_id is not None

        approve_request(first.approval_id, "operator4")

        retry = evaluate_command_policy(command, mode="ask", profile="docker-admin")
        assert retry.allowed
        assert not retry.requires_approval
        assert "operator4" in retry.reason

    def test_retry_without_approval_creates_a_new_pending_request(self):
        command = "docker system prune -af"
        first = evaluate_command_policy(command, mode="ask", profile="docker-admin")
        assert first.requires_approval

        # Never approved — retrying just gets another pending request,
        # not silently allowed.
        retry = evaluate_command_policy(command, mode="ask", profile="docker-admin")
        assert not retry.allowed
        assert retry.requires_approval
        assert retry.approval_id != first.approval_id

    def test_approval_cannot_be_replayed_for_a_second_retry(self):
        command = "docker system prune -f --volumes"
        first = evaluate_command_policy(command, mode="ask", profile="docker-admin")
        approve_request(first.approval_id, "operator5")

        retry1 = evaluate_command_policy(command, mode="ask", profile="docker-admin")
        assert retry1.allowed  # consumes the approval

        retry2 = evaluate_command_policy(command, mode="ask", profile="docker-admin")
        assert not retry2.allowed  # approval already consumed — blocked again
        assert retry2.requires_approval
        assert retry2.approval_id != first.approval_id
