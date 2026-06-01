"""Tests for CircuitBreaker — state machine, concurrency, race conditions."""

import asyncio

import pytest

from app.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry, CircuitState


class TestCircuitBreakerStateMachine:
    """Basic state transitions: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    async def _open_breaker(self, cb: CircuitBreaker):
        for _ in range(cb._failure_threshold):
            await cb.record_failure()
        assert cb._state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_initial_state(self):
        cb = CircuitBreaker()
        assert cb._state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_closed_to_open_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=3600)
        for _ in range(3):
            await cb.record_failure()
        assert cb._state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_execution_rejected_when_open(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=3600)
        for _ in range(3):
            await cb.record_failure()
        ok = await cb.can_execute()
        assert ok is False

    @pytest.mark.asyncio
    async def test_open_to_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.05)
        for _ in range(3):
            await cb.record_failure()
        assert cb._state == CircuitState.OPEN

        await asyncio.sleep(0.06)
        ok = await cb.can_execute()
        assert ok is True
        assert cb._state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_to_closed_on_successes(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.05,
                            half_open_max_calls=3)
        for _ in range(3):
            await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.can_execute()  # transitions to HALF_OPEN

        for _ in range(3):
            await cb.record_success()
        assert cb._state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_to_open_on_failure(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.05)
        for _ in range(3):
            await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.can_execute()  # transitions to HALF_OPEN

        await cb.record_failure()
        assert cb._state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_success_in_closed_reduces_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        assert cb._failure_count == 1  # decremented from 2 to 1


# ═══════════════════════════════════════════════════════════════════════════════
# Race Conditions — Concurrent Can_execute In HALF_OPEN
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerRace:
    """The lock guard prevents _half_open_calls from exceeding max.

    Even with 100 concurrent callers, no more than half_open_max_calls
    should get True.
    """

    @pytest.mark.asyncio
    async def test_concurrent_half_open_respects_limit(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.05,
                            half_open_max_calls=5)
        # Force Into HALF_OPEN
        for _ in range(3):
            await cb.record_failure()
        await asyncio.sleep(0.06)
        # One Call Transitions To HALF_OPEN
        await cb.can_execute()

        # Fire 50 Concurrent Can_execute Calls
        results = await asyncio.gather(*[cb.can_execute() for _ in range(50)])

        granted = sum(1 for r in results if r is True)
        stats = await cb.get_stats()

        assert stats["half_open_calls"] <= 5, f"_half_open_calls exceeded max: {stats}"
        assert granted <= 5, f"{granted} calls granted, max is 5"

    @pytest.mark.asyncio
    async def test_concurrent_half_open_with_failures(self):
        """Mixed success/failure in concurrent calls."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.05,
                            half_open_max_calls=5)
        for _ in range(3):
            await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.can_execute()  # enter HALF_OPEN

        # 3 Succeed, 2 Fail Concurrently
        async def succeed():
            ok = await cb.can_execute()
            if ok:
                await cb.record_success()
            return ok

        async def fail():
            ok = await cb.can_execute()
            if ok:
                await cb.record_failure()
            return ok

        tasks = [succeed() for _ in range(3)] + [fail() for _ in range(2)]
        await asyncio.gather(*tasks)

        stats = await cb.get_stats()
        assert stats["half_open_calls"] <= 5

    @pytest.mark.asyncio
    async def test_successes_close_from_any_caller(self):
        """Any caller's record_success can transition to CLOSED."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.05,
                            half_open_max_calls=3)
        for _ in range(3):
            await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.can_execute()  # enter HALF_OPEN

        for _ in range(3):
            await cb.can_execute()
            await cb.record_success()
        assert cb._state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_while_others_succeeding(self):
        """A single failure in HALF_OPEN re-opens the breaker."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.05,
                            half_open_max_calls=5)
        for _ in range(3):
            await cb.record_failure()
        await asyncio.sleep(0.06)
        await cb.can_execute()  # enter HALF_OPEN

        # One Succeeds, One Fails
        await cb.can_execute()
        await cb.record_success()
        await cb.can_execute()
        await cb.record_failure()

        assert cb._state == CircuitState.OPEN


# ═══════════════════════════════════════════════════════════════════════════════
# Circuitbreakerregistry — Eviction, Threading, Isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerRegistry:
    @pytest.mark.asyncio
    async def test_get_breaker_creates_new(self):
        reg = CircuitBreakerRegistry(max_size=10)
        cb = await reg.get_breaker("host1")
        assert isinstance(cb, CircuitBreaker)
        assert cb._state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_get_breaker_reuses_existing(self):
        reg = CircuitBreakerRegistry(max_size=10)
        cb1 = await reg.get_breaker("host1")
        cb2 = await reg.get_breaker("host1")
        assert cb1 is cb2

    @pytest.mark.asyncio
    async def test_eviction_when_max_size_exceeded(self):
        reg = CircuitBreakerRegistry(max_size=3)
        await reg.get_breaker("A")
        await reg.get_breaker("B")
        await reg.get_breaker("C")
        await reg.get_breaker("D")  # should evict A

        assert "A" not in reg._breakers
        assert "D" in reg._breakers

    @pytest.mark.asyncio
    async def test_eviction_skips_recently_accessed(self):
        reg = CircuitBreakerRegistry(max_size=3)
        await reg.get_breaker("A")
        await reg.get_breaker("B")
        await reg.get_breaker("C")
        await reg.get_breaker("A")  # re-access A, making it recent
        await reg.get_breaker("D")  # should evict B (oldest)

        assert "A" in reg._breakers
        assert "B" not in reg._breakers

    @pytest.mark.asyncio
    async def test_remove(self):
        reg = CircuitBreakerRegistry(max_size=10)
        await reg.get_breaker("host1")
        removed = await reg.remove("host1")
        assert removed is True
        assert "host1" not in reg._breakers

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self):
        reg = CircuitBreakerRegistry(max_size=10)
        removed = await reg.remove("nonexistent")
        assert removed is False

    @pytest.mark.asyncio
    async def test_isolated_breakers(self):
        """Two hosts have independent breaker states."""
        reg = CircuitBreakerRegistry(max_size=10)
        cb1 = await reg.get_breaker("host1", failure_threshold=2, recovery_timeout=3600)
        cb2 = await reg.get_breaker("host2", failure_threshold=2, recovery_timeout=3600)

        await cb1.record_failure()
        await cb1.record_failure()
        assert cb1._state == CircuitState.OPEN
        assert cb2._state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_get_all_stats(self):
        reg = CircuitBreakerRegistry(max_size=10)
        await reg.get_breaker("host1")
        await reg.get_breaker("host2")
        stats = await reg.get_all_stats()
        assert "host1" in stats
        assert "host2" in stats
        assert stats["host1"]["state"] == "closed"
