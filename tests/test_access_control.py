"""Tests for access control gate — Phase 12B unit tests."""

from __future__ import annotations

import time

import pytest

from app.access_control import (
    AccessControlStore,
    AccessDeniedError,
    capped_profile,
    make_access_key_hash,
)

# ---------------------------------------------------------------------------
# Key hashing
# ---------------------------------------------------------------------------


class TestKeyHash:
    def test_no_raw_ip_or_fingerprint(self):
        h = make_access_key_hash("abc123def456", "10.0.0.1")
        assert len(h) == 16
        assert h.isascii()
        assert h.isalnum()
        assert "10.0.0.1" not in h
        assert "abc123" not in h

    def test_deterministic(self):
        a = make_access_key_hash("fp1", "1.2.3.4")
        b = make_access_key_hash("fp1", "1.2.3.4")
        assert a == b

    def test_different_inputs_different_hashes(self):
        a = make_access_key_hash("fp1", "1.2.3.4")
        b = make_access_key_hash("fp1", "1.2.3.5")
        assert a != b

    def test_different_fingerprints(self):
        a = make_access_key_hash("fp1", "1.2.3.4")
        b = make_access_key_hash("fp2", "1.2.3.4")
        assert a != b


# ---------------------------------------------------------------------------
# capped_profile
# ---------------------------------------------------------------------------


class TestCappedProfile:
    def test_passthrough_readonly(self):
        assert capped_profile("readonly") == "readonly"

    def test_passthrough_testlint(self):
        assert capped_profile("testlint") == "testlint"

    def test_downgrades_default(self):
        assert capped_profile("default") == "readonly"

    def test_downgrades_ops(self):
        assert capped_profile("ops") == "readonly"

    def test_downgrades_docker_admin(self):
        assert capped_profile("docker-admin") == "readonly"

    def test_downgrades_project_automation(self):
        assert capped_profile("project-automation") == "readonly"


# ---------------------------------------------------------------------------
# Store CRUD + TTL
# ---------------------------------------------------------------------------


class TestStoreCRUD:
    def test_get_missing_returns_none(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        assert store.get("fp1", "1.2.3.4") is None

    def test_set_and_get(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.2.3.4", "allowed", "operator", "operator")
        entry = store.get("fp1", "1.2.3.4")
        assert entry is not None
        assert entry.decision == "allowed"
        assert entry.actor_fingerprint == "fp1"
        assert entry.source_ip == "1.2.3.4"

    def test_delete(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.2.3.4", "allowed", "test", "system")
        store.delete("fp1", "1.2.3.4")
        assert store.get("fp1", "1.2.3.4") is None

    def test_cleanup_expired(self):
        store = AccessControlStore(pending_ttl=1, allow_ttl=1, deny_ttl=1)
        store.set("fp1", "1.2.3.4", "pending", "test", "system")
        time.sleep(1.1)
        count = store.cleanup_expired()
        assert count == 1
        assert store.get("fp1", "1.2.3.4") is None

    def test_custom_ttl(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.2.3.4", "allowed", "test", "system", ttl_seconds=1)
        time.sleep(1.1)
        assert store.get("fp1", "1.2.3.4") is None

    def test_ttl_expiry_pending(self):
        store = AccessControlStore(pending_ttl=1, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.2.3.4", "pending", "test", "system")
        time.sleep(1.1)
        assert store.get("fp1", "1.2.3.4") is None

    def test_ttl_expiry_deny(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=1)
        store.set("fp1", "1.2.3.4", "denied", "test", "system")
        time.sleep(1.1)
        assert store.get("fp1", "1.2.3.4") is None


# ---------------------------------------------------------------------------
# resolve_access_policy
# ---------------------------------------------------------------------------


class TestResolveAccessPolicy:
    def test_unknown_tuple_pending_capped_profile(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        result = store.resolve_access_policy(
            actor_fingerprint="fp1",
            token_type="agent",
            source_ip="1.2.3.4",
            requested_profile="ops",
        )
        assert result.state == "pending"
        assert result.effective_profile == "readonly"

    def test_allowed_passes_requested_profile(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.2.3.4", "allowed", "operator", "operator")
        result = store.resolve_access_policy(
            actor_fingerprint="fp1",
            token_type="agent",
            source_ip="1.2.3.4",
            requested_profile="ops",
        )
        assert result.state == "allowed"
        assert result.effective_profile == "ops"

    def test_denied_raises_access_denied_error(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.2.3.4", "denied", "bad actor", "operator")
        with pytest.raises(AccessDeniedError):
            store.resolve_access_policy(
                actor_fingerprint="fp1",
                token_type="agent",
                source_ip="1.2.3.4",
                requested_profile="default",
            )

    def test_master_exempt_by_default(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        result = store.resolve_access_policy(
            actor_fingerprint="fp1",
            token_type="master",
            source_ip="1.2.3.4",
            requested_profile="ops",
            enforce_master=False,
        )
        assert result.state == "exempt"
        assert result.effective_profile == "ops"

    def test_master_enforced_when_enabled(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        result = store.resolve_access_policy(
            actor_fingerprint="fp1",
            token_type="master",
            source_ip="1.2.3.4",
            requested_profile="ops",
            enforce_master=True,
        )
        assert result.state == "pending"


# ---------------------------------------------------------------------------
# Store recent / clear / ttl
# ---------------------------------------------------------------------------


class TestStoreRecent:
    def test_recent_newest_default(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.1.1.1", "allowed", "first", "operator", ttl_seconds=100)
        time.sleep(0.01)
        store.set("fp2", "2.2.2.2", "denied", "second", "operator", ttl_seconds=200)
        recent = store.recent()
        assert len(recent) == 2
        assert recent[0].actor_fingerprint == "fp2"
        assert recent[1].actor_fingerprint == "fp1"

    def test_recent_oldest_sort(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.1.1.1", "allowed", "first", "operator")
        time.sleep(0.01)
        store.set("fp2", "2.2.2.2", "denied", "second", "operator")
        recent = store.recent(sort="oldest")
        assert recent[0].actor_fingerprint == "fp1"
        assert recent[1].actor_fingerprint == "fp2"

    def test_recent_limit(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        for i in range(5):
            store.set(f"fp{i}", f"1.0.0.{i}", "allowed", "t", "operator")
        recent = store.recent(limit=2)
        assert len(recent) == 2

    def test_recent_decision_filter(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.1.1.1", "allowed", "a", "operator")
        store.set("fp2", "2.2.2.2", "denied", "d", "operator")
        recent = store.recent(decision="denied")
        assert len(recent) == 1
        assert recent[0].decision == "denied"

    def test_recent_excludes_expired(self):
        store = AccessControlStore(pending_ttl=1, allow_ttl=1, deny_ttl=86400)
        store.set("fp1", "1.1.1.1", "allowed", "a", "operator")
        time.sleep(1.1)
        recent = store.recent()
        assert len(recent) == 0

    def test_ttl_remaining_positive(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        entry = store.set("fp1", "1.1.1.1", "allowed", "a", "operator", ttl_seconds=600)
        remaining = AccessControlStore.ttl_remaining(entry)
        assert 599 <= remaining <= 600

    def test_ttl_remaining_zero_when_expired(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=1, deny_ttl=86400)
        entry = store.set("fp1", "1.1.1.1", "allowed", "a", "operator")
        time.sleep(1.1)
        remaining = AccessControlStore.ttl_remaining(entry)
        assert remaining == 0.0


class TestStoreClear:
    def test_clear_removes_from_memory(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.1.1.1", "denied", "bad", "operator")
        entry = store.clear("fp1", "1.1.1.1", reason="test clear")
        assert entry is not None
        assert entry.decision == "denied"
        assert store.get("fp1", "1.1.1.1") is None

    def test_clear_returns_none_when_missing(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        entry = store.clear("fp1", "1.1.1.1")
        assert entry is None

    def test_clear_tuple_returns_pending(self):
        store = AccessControlStore(pending_ttl=900, allow_ttl=86400, deny_ttl=86400)
        store.set("fp1", "1.1.1.1", "denied", "bad", "operator")
        store.clear("fp1", "1.1.1.1")
        result = store.resolve_access_policy(
            actor_fingerprint="fp1",
            token_type="agent",
            source_ip="1.1.1.1",
            requested_profile="ops",
        )
        assert result.state == "pending"
        assert result.effective_profile == "readonly"
        assert result.effective_profile == "readonly"
