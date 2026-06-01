"""Circuit breaker pattern for SSH connections."""

import asyncio
import logging
import time
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker for SSH connections.

    Prevents cascading failures when SSH servers are down.
    Async-safe — all state mutations are guarded by asyncio.Lock.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        half_open_max_calls: int = 3,
    ):
        self._lock = asyncio.Lock()
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    async def can_execute(self) -> bool:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if self._last_failure_time is not None and \
                   time.time() - self._last_failure_time >= self._recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info("Circuit Breaker Entering HALF_OPEN State")
                else:
                    return False

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self._half_open_max_calls:
                    return False
                self._half_open_calls += 1

            return self._state != CircuitState.OPEN

    async def record_success(self):
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._half_open_max_calls:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
                    self._half_open_calls = 0
                    logger.info("Circuit Breaker CLOSED (recovered)")
            else:
                self._failure_count = max(0, self._failure_count - 1)

    async def record_failure(self):
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("Circuit Breaker OPEN (half-open Test Failed)")
            elif self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning("Circuit breaker OPEN (%d failures)", self._failure_count)

    async def get_stats(self) -> dict:
        async with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "last_failure_time": self._last_failure_time,
                "half_open_calls": self._half_open_calls,
            }


class CircuitBreakerRegistry:
    """Registry of circuit breakers per host.

    Evicts least-recently-accessed breakers when max_size is exceeded.
    """

    def __init__(self, max_size: int = 1000):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()
        self._max_size = max_size
        self._access_order: list[str] = []

    async def get_breaker(self, host: str, **kwargs) -> CircuitBreaker:
        async with self._lock:
            if host not in self._breakers:
                if len(self._breakers) >= self._max_size:
                    # Evict Oldest
                    oldest = self._access_order.pop(0)
                    del self._breakers[oldest]
                self._breakers[host] = CircuitBreaker(**kwargs)
            else:
                # Move To End (most Recently Used)
                self._access_order.remove(host)
            self._access_order.append(host)
            return self._breakers[host]

    async def get_all_stats(self) -> dict[str, dict]:
        async with self._lock:
            hosts = list(self._breakers.items())
        result = {}
        for host, breaker in hosts:
            result[host] = await breaker.get_stats()
        return result

    async def remove(self, host: str) -> bool:
        """Remove a circuit breaker by host."""
        async with self._lock:
            if host in self._breakers:
                del self._breakers[host]
                self._access_order.remove(host)
                return True
            return False
