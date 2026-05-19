"""Circuit breaker pattern for SSH connections."""

import logging
import time
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered


class CircuitBreaker:
    """Circuit breaker for SSH connections.
    
    Prevents cascading failures when SSH servers are down.
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        half_open_max_calls: int = 3,
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._half_open_calls = 0
    
    @property
    def state(self) -> CircuitState:
        """Current circuit state."""
        if self._state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self._last_failure_time and \
               time.time() - self._last_failure_time >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
                logger.info("Circuit breaker entering HALF_OPEN state")
        
        return self._state
    
    def can_execute(self) -> bool:
        """Check if request can be executed."""
        current_state = self.state
        
        if current_state == CircuitState.CLOSED:
            return True
        
        if current_state == CircuitState.OPEN:
            return False
        
        if current_state == CircuitState.HALF_OPEN:
            if self._half_open_calls < self._half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False
        
        return True
    
    def record_success(self):
        """Record successful execution."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._half_open_max_calls:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._success_count = 0
                self._half_open_calls = 0
                logger.info("Circuit breaker CLOSED (recovered)")
        else:
            self._failure_count = max(0, self._failure_count - 1)
    
    def record_failure(self):
        """Record failed execution."""
        self._failure_count += 1
        self._last_failure_time = time.time()
        
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning("Circuit breaker OPEN (half-open test failed)")
        elif self._failure_count >= self._failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning("Circuit breaker OPEN (%d failures)", self._failure_count)
    
    def get_stats(self) -> dict:
        """Get circuit breaker statistics."""
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure_time": self._last_failure_time,
            "half_open_calls": self._half_open_calls,
        }


class CircuitBreakerRegistry:
    """Registry of circuit breakers per host."""
    
    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
    
    def get_breaker(self, host: str) -> CircuitBreaker:
        """Get or create circuit breaker for host."""
        if host not in self._breakers:
            self._breakers[host] = CircuitBreaker()
        return self._breakers[host]
    
    def get_all_stats(self) -> dict[str, dict]:
        """Get stats for all circuit breakers."""
        return {host: breaker.get_stats() for host, breaker in self._breakers.items()}
