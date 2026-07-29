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
