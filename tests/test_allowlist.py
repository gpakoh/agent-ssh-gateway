"""Tests for P6 — four-layer hierarchical allowlist."""

from app.allowlist import Allowlist, AllowlistEntry, get_allowlist, reset_allowlist

# ── AllowlistEntry tests ──────────────────────────────────────────────────────

class TestAllowlistEntry:
    def test_is_expired_none_expires_at(self):
        e = AllowlistEntry(id="1", layer="system", selector_type="exact", selector_value="ls", created_at=0, expires_at=None)
        assert e.is_expired() is False

    def test_is_expired_future(self):
        import time
        e = AllowlistEntry(id="1", layer="agent", selector_type="exact", selector_value="ls", created_at=0, expires_at=time.time() + 3600)
        assert e.is_expired() is False

    def test_is_expired_past(self):
        import time
        e = AllowlistEntry(id="1", layer="agent", selector_type="exact", selector_value="ls", created_at=0, expires_at=time.time() - 1)
        assert e.is_expired() is True


# ── Allowlist core tests ──────────────────────────────────────────────────────

class TestAllowlistCore:
    def test_add_and_list_layer(self):
        a = Allowlist()
        e = a.add("agent", "exact", "rm -rf /", created_by="test", reason="allowed")
        entries = a.list_layer("agent")
        assert len(entries) == 1
        assert entries[0].id == e.id
        assert entries[0].selector_type == "exact"
        assert entries[0].selector_value == "rm -rf /"

    def test_add_system_no_default_ttl(self):
        a = Allowlist()
        e = a.add("system", "exact", "shutdown")
        assert e.expires_at is None

    def test_add_agent_has_default_ttl(self):
        a = Allowlist()
        e = a.add("agent", "exact", "shutdown")
        assert e.expires_at is not None
        assert e.ttl is None  # default applied at creation

    def test_add_custom_ttl(self):
        a = Allowlist()
        e = a.add("agent", "exact", "reboot", ttl=60)
        assert e.ttl == 60
        assert e.expires_at is not None

    def test_remove_existing(self):
        a = Allowlist()
        e = a.add("system", "exact", "reboot")
        assert a.remove(e.id) is True
        assert len(a.list_layer("system")) == 0

    def test_remove_missing(self):
        a = Allowlist()
        assert a.remove("nonexistent") is False

    def test_clear_layer(self):
        a = Allowlist()
        a.add("agent", "exact", "cmd1")
        a.add("agent", "exact", "cmd2")
        a.add("project", "exact", "cmd3")
        assert a.clear_layer("agent") == 2
        assert len(a.list_layer("agent")) == 0
        assert len(a.list_layer("project")) == 1

    def test_clear_all(self):
        a = Allowlist()
        a.add("agent", "exact", "cmd1")
        a.add("project", "exact", "cmd2")
        assert a.clear_all() == 2
        assert a.list_all() == []

    def test_stale_expiration(self):
        a = Allowlist()
        a.add("agent", "exact", "old-cmd", ttl=-1)  # expired immediately
        assert len(a.list_all()) == 0  # _expire_stale called by list_all

    def test_list_all_returns_in_layer_order(self):
        a = Allowlist()
        a.add("system", "exact", "sys")
        a.add("user", "exact", "usr")
        a.add("project", "exact", "prj")
        a.add("agent", "exact", "agt")
        ids = [e.selector_value for e in a.list_all()]
        assert ids == ["agt", "prj", "usr", "sys"]  # agent > project > user > system


# ── Selector matching tests ───────────────────────────────────────────────────

class TestSelectorMatching:
    def test_exact_match(self):
        a = Allowlist()
        a.add("system", "exact", "ls -la")
        assert a.check("ls -la") is not None
        assert a.check("ls -la /tmp") is None

    def test_prefix_match(self):
        a = Allowlist()
        a.add("system", "prefix", "docker exec")
        assert a.check("docker exec web-ssh-gateway ls") is not None
        assert a.check("docker ps") is None

    def test_prefix_match_requires_word_boundary(self):
        """Regression: a bare command.startswith(prefix) let an entry meant
        for one command family also cover an unrelated command that merely
        shares the same leading characters — a "docker" prefix entry would
        also match "dockerize-evil.sh", not just real docker invocations.
        """
        a = Allowlist()
        a.add("system", "prefix", "docker")
        assert a.check("docker ps") is not None
        assert a.check("docker") is not None
        assert a.check("dockerize-evil.sh") is None
        assert a.check("docker-compose up") is None

    def test_regex_match(self):
        a = Allowlist()
        a.add("system", "regex", r"rm\s+-rf\s+/\s*$")
        assert a.check("rm -rf /") is not None
        assert a.check("rm -rf /var") is None

    def test_rule_id_match(self):
        a = Allowlist()
        a.add("system", "rule_id", "rm-rf-root")
        assert a.check("rm -rf /") is not None

    def test_no_match_returns_none(self):
        a = Allowlist()
        a.add("system", "exact", "ls")
        assert a.check("rm -rf /") is None


# ── Layer priority tests ──────────────────────────────────────────────────────

class TestLayerPriority:
    def test_agent_highest_priority(self):
        a = Allowlist()
        a.add("system", "prefix", "rm")
        a.add("agent", "exact", "rm -rf /")
        match = a.check("rm -rf /")
        assert match is not None
        assert match.entry.layer == "agent"

    def test_project_over_user(self):
        a = Allowlist()
        a.add("user", "exact", "cmd")
        a.add("project", "exact", "cmd")
        match = a.check("cmd")
        assert match is not None
        assert match.entry.layer == "project"

    def test_user_over_system(self):
        a = Allowlist()
        a.add("system", "exact", "cmd")
        a.add("user", "exact", "cmd")
        match = a.check("cmd")
        assert match is not None
        assert match.entry.layer == "user"


# ── Singleton tests ───────────────────────────────────────────────────────────

class TestAllowlistSingleton:
    def setup_method(self):
        reset_allowlist()

    def test_get_allowlist_singleton(self):
        a1 = get_allowlist()
        a2 = get_allowlist()
        assert a1 is a2

    def test_singleton_persists_entries(self):
        a = get_allowlist()
        a.add("system", "exact", "test-cmd")
        assert len(a.list_all()) == 1

    def test_reset_allowlist(self):
        a = get_allowlist()
        a.add("system", "exact", "will-be-lost")
        reset_allowlist()
        b = get_allowlist()
        assert b is not a
        assert len(b.list_all()) == 0


# ── Policy integration tests ──────────────────────────────────────────────────

class TestPolicyIntegration:
    def test_allowlist_bypasses_policy(self):
        from app.command_policy import evaluate_command_policy
        a = get_allowlist()
        a.add("system", "exact", "rm -rf /")
        try:
            decision = evaluate_command_policy(
                "rm -rf /",
                mode="enforce",
                profile="default",
            )
            assert decision.allowed is True
            assert "Allowlist match" in decision.reason
        finally:
            a.clear_all()

    def test_allowlist_no_match_still_blocked(self):
        from app.command_policy import CommandPolicyMode, evaluate_command_policy
        decision = evaluate_command_policy(
            "rm -rf /",
            mode="enforce",
            profile="default",
        )
        assert decision.allowed is False
        assert decision.mode == CommandPolicyMode.ENFORCE.value

    def test_allowlist_with_rule_id_integration(self):
        from app.command_policy import evaluate_command_policy
        a = get_allowlist()
        a.add("system", "rule_id", "rm-rf-root")
        try:
            decision = evaluate_command_policy(
                "rm -rf /",
                mode="enforce",
                profile="default",
            )
            assert decision.allowed is True
            assert "rm-rf-root" in decision.reason
        finally:
            a.clear_all()

    def test_allowlist_off_mode_still_allows(self):
        """OFF mode bypasses policy entirely, allowlist not needed."""
        from app.command_policy import evaluate_command_policy
        decision = evaluate_command_policy(
            "rm -rf /",
            mode="off",
            profile="default",
        )
        assert decision.allowed is True
        assert "disabled" in decision.reason
