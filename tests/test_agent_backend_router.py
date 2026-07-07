"""Tests for AgentBackendRouter — selection, cooldown, fallback."""

import time

import pytest

from examples.mcp_server.agent_backend_router import (
    COOLDOWN_PATTERNS,
    AgentBackendRouter,
    BackendEntry,
    BackendStatus,
    CooldownEntry,
    RoundRobin,
    TryPrimaryFallback,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def router():
    return AgentBackendRouter(
        backends={
            "opencode": BackendEntry(name="opencode", priority=0),
            "mimo": BackendEntry(name="mimo", priority=1),
        },
        enabled=True,
    )


@pytest.fixture
def router_disabled():
    return AgentBackendRouter(
        backends={
            "opencode": BackendEntry(name="opencode", priority=0),
            "mimo": BackendEntry(name="mimo", priority=1),
        },
        enabled=False,
    )


# ── select_backend: basic ─────────────────────────────────────────────────────


class TestSelectBackend:
    def test_returns_available(self, router):
        chosen = router.select_backend("opencode")
        assert chosen == "opencode"

    def test_returns_any_when_no_preferred(self, router):
        chosen = router.select_backend()
        assert chosen in ("opencode", "mimo")

    def test_skips_cooldown(self, router):
        router._backends["opencode"].status = BackendStatus.COOLDOWN
        router._backends["opencode"].cooldown_until = time.time() + 9999
        chosen = router.select_backend("opencode")
        assert chosen == "mimo"

    def test_skips_failed(self, router):
        router._backends["opencode"].status = BackendStatus.FAILED
        chosen = router.select_backend("opencode")
        assert chosen == "mimo"

    def test_skips_disabled(self, router):
        router._backends["opencode"].status = BackendStatus.DISABLED
        chosen = router.select_backend("opencode")
        assert chosen == "mimo"

    def test_none_when_all_unavailable(self, router):
        for b in router._backends.values():
            b.status = BackendStatus.FAILED
        chosen = router.select_backend("opencode")
        assert chosen is None

    def test_respects_priority(self, router):
        router._backends["mimo"].priority = 0
        router._backends["opencode"].priority = 1
        chosen = router.select_backend()
        assert chosen == "mimo"

    def test_expired_cooldown_auto_recovers(self, router):
        router._backends["opencode"].status = BackendStatus.COOLDOWN
        router._backends["opencode"].cooldown_until = time.time() - 1
        chosen = router.select_backend("opencode")
        assert chosen == "opencode"
        assert router._backends["opencode"].status == BackendStatus.AVAILABLE


# ── select_backend: disabled flag ────────────────────────────────────────────


class TestSelectBackendDisabled:
    def test_returns_preferred_when_disabled(self, router_disabled):
        chosen = router_disabled.select_backend("opencode")
        assert chosen == "opencode"

    def test_ignores_backend_status_when_disabled(self, router_disabled):
        router_disabled._backends["opencode"].status = BackendStatus.FAILED
        chosen = router_disabled.select_backend("opencode")
        assert chosen == "opencode"


# ── record_result: cooldown logic ─────────────────────────────────────────────


class TestRecordResult:
    def test_success_clears_status(self, router):
        router._backends["opencode"].status = BackendStatus.FAILED
        cd = router.record_result("opencode", 0, stdout="ok")
        assert cd is None
        assert router._backends["opencode"].status == BackendStatus.AVAILABLE

    def test_rate_limit_triggers_cooldown(self, router):
        cd = router.record_result("opencode", 1, stderr="Free usage exceeded, retrying in 7h")
        assert cd is not None
        assert cd.reason == "rate_limit"
        assert router._backends["opencode"].status == BackendStatus.COOLDOWN
        assert router._backends["opencode"].cooldown_until is not None

    def test_error_triggers_short_cooldown(self, router):
        cd = router.record_result("mimo", 1, stderr="Connection refused")
        assert cd is not None
        assert cd.reason == "error"
        assert router._backends["mimo"].status == BackendStatus.FAILED

    def test_cooldown_pattern_detected_mimo(self, router):
        cd = router.record_result("mimo", 1, stderr="ollama timeout after 30s")
        assert cd is not None
        assert cd.reason == "rate_limit"

    def test_no_pattern_match_fallback_to_error(self, router):
        cd = router.record_result("opencode", 1, stderr="unknown error")
        assert cd is not None
        assert cd.reason == "error"

    def test_record_unknown_backend_ignored(self, router):
        cd = router.record_result("nonexistent", 1, stderr="err")
        assert cd is None


# ── Fallback integration ────────────────────────────────────────────────────


class TestFallback:
    def test_opencode_cooldown_falls_to_mimo(self, router):
        router.record_result("opencode", 1, stderr="Free usage exceeded")
        chosen = router.select_backend("opencode")
        assert chosen == "mimo"

    def test_both_cooldown_returns_none(self, router):
        router.record_result("opencode", 1, stderr="Free usage exceeded")
        router.record_result("mimo", 1, stderr="model not found")
        chosen = router.select_backend("opencode")
        assert chosen is None

    def test_opencode_cooldown_mimo_available_returns_mimo(self, router):
        router.record_result("opencode", 1, stderr="rate limit")
        chosen = router.select_backend("opencode")
        assert chosen == "mimo"

    def test_opencode_recovers_after_cooldown_expires(self, router):
        router.record_result("opencode", 1, stderr="rate limit")
        router._backends["opencode"].cooldown_until = time.time() - 1
        chosen = router.select_backend("opencode")
        assert chosen == "opencode"


# ── CooldownEntry properties ─────────────────────────────────────────────────


class TestCooldownEntry:
    def test_active_when_until_in_future(self):
        cd = CooldownEntry(
            provider="t", detected_at=time.time(), cooldown_seconds=9999, reason="test"
        )
        assert cd.active is True

    def test_expired_when_until_in_past(self):
        cd = CooldownEntry(
            provider="t", detected_at=time.time() - 100, cooldown_seconds=1, reason="test"
        )
        assert cd.active is False

    def test_until_property(self):
        cd = CooldownEntry(provider="t", detected_at=1000, cooldown_seconds=500, reason="test")
        assert cd.until == 1500


# ── Cooldown detection patterns ──────────────────────────────────────────────


class TestCooldownPatterns:
    def test_opencode_rate_limit_pattern(self):
        patterns = COOLDOWN_PATTERNS["opencode"]
        assert any(p.search("Free usage exceeded") for p in patterns)
        assert any(p.search("rate limit hit") for p in patterns)
        assert any(p.search("please retry in 7 hours") for p in patterns)

    def test_mimo_patterns(self):
        patterns = COOLDOWN_PATTERNS["mimo"]
        assert any(p.search("model gemma4 not found") for p in patterns)
        assert any(p.search("ollama timeout") for p in patterns)
        assert any(p.search("OLLAMA_RETRY_EXCEEDED") for p in patterns)

    def test_no_false_positive(self):
        patterns = COOLDOWN_PATTERNS["opencode"]
        assert not any(p.search("normal output") for p in patterns)


# ── Policy: RoundRobin ──────────────────────────────────────────────────────


class TestRoundRobin:
    def test_cycles_through_available(self):
        policy = RoundRobin()
        backends = {
            "a": BackendEntry(name="a", priority=0),
            "b": BackendEntry(name="b", priority=0),
        }
        first = policy.select(backends, [])
        second = policy.select(backends, [])
        assert first != second

    def test_returns_none_when_no_available(self):
        policy = RoundRobin()
        backends = {
            "a": BackendEntry(name="a", priority=0, status=BackendStatus.FAILED),
        }
        assert policy.select(backends, []) is None

    def test_skips_unavailable(self):
        policy = RoundRobin()
        backends = {
            "a": BackendEntry(name="a", priority=0, status=BackendStatus.FAILED),
            "b": BackendEntry(name="b", priority=0),
        }
        chosen = policy.select(backends, [])
        assert chosen == "b"


# ── Policy: TryPrimaryFallback ──────────────────────────────────────────────


class TestTryPrimaryFallback:
    def test_preferred_when_available(self):
        policy = TryPrimaryFallback()
        backends = {
            "primary": BackendEntry(name="primary", priority=0),
            "fallback": BackendEntry(name="fallback", priority=1),
        }
        assert policy.select(backends, [], "primary") == "primary"

    def test_fallback_when_preferred_in_cooldown(self):
        policy = TryPrimaryFallback()
        backends = {
            "primary": BackendEntry(
                name="primary",
                priority=0,
                status=BackendStatus.COOLDOWN,
                cooldown_until=time.time() + 9999,
            ),
            "fallback": BackendEntry(name="fallback", priority=1),
        }
        assert policy.select(backends, [], "primary") == "fallback"

    def test_none_when_preferred_and_fallback_unavailable(self):
        policy = TryPrimaryFallback()
        backends = {
            "primary": BackendEntry(name="primary", priority=0, status=BackendStatus.FAILED),
            "fallback": BackendEntry(name="fallback", priority=1, status=BackendStatus.FAILED),
        }
        assert policy.select(backends, [], "primary") is None

    def test_returns_by_priority_when_no_preferred(self):
        policy = TryPrimaryFallback()
        backends = {
            "low": BackendEntry(name="low", priority=1),
            "high": BackendEntry(name="high", priority=0),
        }
        assert policy.select(backends, []) == "high"


# ── Router: get_status / get_cooldowns / reset ──────────────────────────────


class TestRouterAdmin:
    def test_get_status_returns_snapshot(self, router):
        status = router.get_status()
        assert "opencode" in status
        assert "mimo" in status

    def test_get_cooldowns_returns_active(self, router):
        router.record_result("opencode", 1, stderr="rate limit")
        cooldowns = router.get_cooldowns()
        assert len(cooldowns) == 1
        assert cooldowns[0].provider == "opencode"

    def test_get_cooldowns_excludes_expired(self, router):
        cd = CooldownEntry(
            provider="t", detected_at=time.time() - 9999, cooldown_seconds=1, reason="test"
        )
        router._cooldowns.append(cd)
        assert len(router.get_cooldowns()) == 0

    def test_reset_clears_status(self, router):
        router._backends["opencode"].status = BackendStatus.FAILED
        router._backends["opencode"].cooldown_until = time.time() + 9999
        router._cooldowns.append(
            CooldownEntry(
                provider="opencode",
                detected_at=time.time(),
                cooldown_seconds=9999,
                reason="test",
            )
        )
        assert router.reset_backend("opencode") is True
        assert router._backends["opencode"].status == BackendStatus.AVAILABLE
        assert router._backends["opencode"].cooldown_until is None
        assert len(router.get_cooldowns()) == 0

    def test_reset_unknown_backend(self, router):
        assert router.reset_backend("nonexistent") is False

    def test_get_cooldowns_empty_initially(self, router):
        assert router.get_cooldowns() == []


# ── Integration: full fallback flow ──────────────────────────────────────────


class TestFullFallbackFlow:
    def test_opencode_fails_mimo_succeeds(self, router):
        router.record_result("opencode", 1, stderr="rate limit")
        chosen = router.select_backend("opencode")
        assert chosen == "mimo"
        router.record_result("mimo", 0, stdout="ok")
        assert router._backends["mimo"].status == BackendStatus.AVAILABLE
