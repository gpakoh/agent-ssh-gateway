"""Agent Backend Router — selection, cooldown tracking, fallback between runners.

Auto-selection disabled by default (MCP_AGENT_BACKEND_ROUTER_ENABLED=false).
When enabled, wraps project_run_opencode / project_run_mimo with automatic
fallback on cooldown/failure.
"""

from __future__ import annotations

import enum
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

# ── Status ────────────────────────────────────────────────────────────────────


class BackendStatus(enum.StrEnum):
    AVAILABLE = "available"
    COOLDOWN = "cooldown"
    FAILED = "failed"
    DISABLED = "disabled"


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class BackendEntry:
    name: str
    priority: int
    status: BackendStatus = BackendStatus.AVAILABLE
    cooldown_until: float | None = None
    last_error: str | None = None
    last_tried_at: float | None = None


@dataclass
class CooldownEntry:
    provider: str
    detected_at: float
    cooldown_seconds: int
    reason: str

    @property
    def until(self) -> float:
        return self.detected_at + self.cooldown_seconds

    @property
    def active(self) -> bool:
        return time.time() < self.until


# ── Cooldown detection patterns ─────────────────────────────────────────────


COOLDOWN_PATTERNS: dict[str, list[re.Pattern]] = {
    "opencode": [
        re.compile(r"Free usage exceeded", re.IGNORECASE),
        re.compile(r"rate.limit", re.IGNORECASE),
        re.compile(r"retry in", re.IGNORECASE),
    ],
    "mimo": [
        re.compile(r"model.*not.*found", re.IGNORECASE),
        re.compile(r"ollama.*timeout", re.IGNORECASE),
        re.compile(r"OLLAMA_RETRY_EXCEEDED", re.IGNORECASE),
    ],
}

_DEFAULT_COOLDOWN_PATTERNS_SERIALIZED: str = ""

# ── Selection policies ──────────────────────────────────────────────────────


class SelectionPolicy(ABC):
    @abstractmethod
    def select(
        self,
        backends: dict[str, BackendEntry],
        cooldowns: list[CooldownEntry],
        preferred: str | None = None,
    ) -> str | None: ...


class TryPrimaryFallback(SelectionPolicy):
    """Try preferred — skip cooldown/failed/disabled — fall back by priority."""

    def select(
        self,
        backends: dict[str, BackendEntry],
        cooldowns: list[CooldownEntry],
        preferred: str | None = None,
    ) -> str | None:
        def _is_available(name: str) -> bool:
            entry = backends.get(name)
            if not entry:
                return False
            if entry.status in (BackendStatus.FAILED, BackendStatus.DISABLED):
                return False
            if entry.status == BackendStatus.COOLDOWN:
                if entry.cooldown_until and time.time() >= entry.cooldown_until:
                    entry.status = BackendStatus.AVAILABLE
                    entry.cooldown_until = None
                    return True
                return False
            return True

        if preferred and _is_available(preferred):
            return preferred

        sorted_backends = sorted(backends.values(), key=lambda b: b.priority)
        for entry in sorted_backends:
            if _is_available(entry.name):
                return entry.name

        return None


class RoundRobin(SelectionPolicy):
    """Cycle through available backends evenly."""

    def __init__(self) -> None:
        self._index: dict[str, int] = {}  # backend_name -> counter

    def select(
        self,
        backends: dict[str, BackendEntry],
        cooldowns: list[CooldownEntry],
        preferred: str | None = None,
    ) -> str | None:
        available = sorted(
            [name for name, entry in backends.items() if entry.status == BackendStatus.AVAILABLE]
        )
        if not available:
            return None

        key = preferred or "__default__"
        idx = self._index.get(key, 0) % len(available)
        self._index[key] = idx + 1
        return available[idx]


# ── Router ────────────────────────────────────────────────────────────────────

_ENABLED = os.environ.get("MCP_AGENT_BACKEND_ROUTER_ENABLED", "false").strip().lower() == "true"
_FALLBACK_ORDER = os.environ.get("MCP_BACKEND_FALLBACK_ORDER", "opencode,mimo").strip().split(",")
_COOLDOWN_DEFAULT = int(os.environ.get("MCP_BACKEND_COOLDOWN_DEFAULT", "25200"))
_COOLDOWN_ERROR = int(os.environ.get("MCP_BACKEND_COOLDOWN_ERROR", "300"))
_POLICY_NAME = (
    os.environ.get("MCP_BACKEND_SELECTION_POLICY", "try-primary-fallback").strip().lower()
)


class AgentBackendRouter:
    """Select backends, track cooldowns, record execution results.

    When :func:`record_result` detects a cooldown pattern, the backend
    is marked COOLDOWN for a configurable duration.  When the backends
    are exhausted, :func:`select_backend` returns ``None``.
    """

    def __init__(
        self,
        backends: dict[str, BackendEntry] | None = None,
        policy: SelectionPolicy | None = None,
        cooldown_default: int = _COOLDOWN_DEFAULT,
        cooldown_error: int = _COOLDOWN_ERROR,
        fallback_order: list[str] | None = None,
        enabled: bool = _ENABLED,
    ) -> None:
        if backends is None:
            order = fallback_order or _FALLBACK_ORDER
            backends = {name: BackendEntry(name=name, priority=i) for i, name in enumerate(order)}
        self._backends = backends
        self._policy = policy or self._default_policy()
        self._cooldown_default = cooldown_default
        self._cooldown_error = cooldown_error
        self._cooldowns: list[CooldownEntry] = []
        self._enabled = enabled

    @staticmethod
    def _default_policy() -> SelectionPolicy:
        if _POLICY_NAME == "round-robin":
            return RoundRobin()
        return TryPrimaryFallback()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def select_backend(self, task_agent: str | None = None) -> str | None:
        if not self._enabled:
            return task_agent or next(iter(self._backends), None)
        preferred = task_agent if task_agent in self._backends else None
        return self._policy.select(self._backends, self._cooldowns, preferred)

    def record_result(
        self, backend: str, exit_code: int, stdout: str = "", stderr: str = ""
    ) -> CooldownEntry | None:
        entry = self._backends.get(backend)
        if not entry:
            return None
        entry.last_tried_at = time.time()

        if exit_code == 0:
            entry.status = BackendStatus.AVAILABLE
            entry.cooldown_until = None
            entry.last_error = None
            return None

        combined = stdout + "\n" + stderr
        patterns = COOLDOWN_PATTERNS.get(backend, [])
        is_rate_limit = any(p.search(combined) for p in patterns)

        now = time.time()
        if is_rate_limit:
            cooldown = self._cooldown_default
            entry.status = BackendStatus.COOLDOWN
            entry.cooldown_until = now + cooldown
            entry.last_error = "rate_limit"
        else:
            cooldown = self._cooldown_error
            entry.status = BackendStatus.FAILED
            entry.cooldown_until = now + cooldown
            entry.last_error = (stderr or stdout)[:200]

        cd = CooldownEntry(
            provider=backend,
            detected_at=now,
            cooldown_seconds=cooldown,
            reason="rate_limit" if is_rate_limit else "error",
        )
        existing = [c for c in self._cooldowns if c.provider == backend]
        if existing:
            if cd.until > existing[0].until:
                self._cooldowns.remove(existing[0])
                self._cooldowns.append(cd)
        else:
            self._cooldowns.append(cd)
        return cd

    def get_status(self) -> dict[str, BackendEntry]:
        return dict(self._backends)

    def get_cooldowns(self) -> list[CooldownEntry]:
        return [c for c in self._cooldowns if c.active]

    def reset_backend(self, name: str) -> bool:
        entry = self._backends.get(name)
        if not entry:
            return False
        entry.status = BackendStatus.AVAILABLE
        entry.cooldown_until = None
        entry.last_error = None
        self._cooldowns = [c for c in self._cooldowns if c.provider != name]
        return True
