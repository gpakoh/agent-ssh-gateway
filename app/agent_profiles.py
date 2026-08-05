"""Agent profiles with trust levels and per-agent pack overrides.

DCG-style agent profiles:
- TrustLevel: high/medium/low
- Per-agent: disabled_packs, extra_packs, additional_allowlist
- Detection from env vars and connection metadata
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum


class TrustLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_DEFAULT_TRUST = TrustLevel.MEDIUM


@dataclass
class AgentProfile:
    """Profile for an AI agent.

    trust_level: informational trust level (behavioral diff via other fields)
    disabled_packs: pattern packs to disable for this agent
    extra_packs: additional pattern packs enabled for this agent
    additional_allowlist: extra allowlist entries for this agent
    disabled_allowlist: completely disable allowlist for this agent
    """
    trust_level: TrustLevel = TrustLevel.MEDIUM
    disabled_packs: frozenset[str] = field(default_factory=frozenset)
    extra_packs: frozenset[str] = field(default_factory=frozenset)
    additional_allowlist: list[str] = field(default_factory=list)
    disabled_allowlist: bool = False


# Built-in profiles for known agent categories
BUILTIN_PROFILES: dict[str, AgentProfile] = {
    "chatgpt": AgentProfile(
        trust_level=TrustLevel.LOW,
        disabled_packs=frozenset({"docker", "kubernetes", "cloud", "loadbalancer", "firewall"}),
    ),
    "claude-code": AgentProfile(
        trust_level=TrustLevel.HIGH,
        extra_packs=frozenset({"git", "filesystem"}),
    ),
    "codex-cli": AgentProfile(
        trust_level=TrustLevel.HIGH,
        extra_packs=frozenset({"git", "filesystem"}),
    ),
    "aider": AgentProfile(
        trust_level=TrustLevel.HIGH,
        extra_packs=frozenset({"git", "filesystem"}),
    ),
    "cursor": AgentProfile(
        trust_level=TrustLevel.MEDIUM,
    ),
    "unknown": AgentProfile(
        trust_level=TrustLevel.MEDIUM,
    ),
}

# env var -> agent key mapping for agent detection
_ENV_AGENT_MAP: dict[str, str] = {
    "CLAUDE_CODE": "claude-code",
    "CODEX_CLI": "codex-cli",
    "AIDER": "aider",
    "CURSOR": "cursor",
}

# process name -> agent key mapping (for /proc/ppid/comm detection)
_PROC_NAME_MAP: dict[str, str] = {
    "claude": "claude-code",
    "codex": "codex-cli",
    "aider": "aider",
    "cursor": "cursor",
}


def detect_agent_from_env() -> str:
    """Detect agent identity from environment variables."""
    for env_var, agent_key in _ENV_AGENT_MAP.items():
        if os.environ.get(env_var):
            return agent_key
    return "unknown"


def detect_agent_from_proc() -> str:
    """Detect agent identity from parent process name (Linux)."""
    try:
        ppid = os.getppid()
        comm_path = f"/proc/{ppid}/comm"
        if os.path.isfile(comm_path):
            with open(comm_path) as f:
                name = f.read().strip()
            for proc_name, agent_key in _PROC_NAME_MAP.items():
                if proc_name in name.lower():
                    return agent_key
    except OSError:
        pass
    return "unknown"


def detect_agent() -> str:
    """Detect agent identity from all available sources.

    Priority: env vars > process name > unknown.
    """
    agent = detect_agent_from_env()
    if agent != "unknown":
        return agent
    return detect_agent_from_proc()


def get_agent_profile(agent_key: str | None = None) -> AgentProfile:
    """Resolve the effective profile for an agent.

    Falls back to 'unknown' and then default.
    """
    key = agent_key or detect_agent()
    return BUILTIN_PROFILES.get(key, BUILTIN_PROFILES["unknown"])


def effective_packs(
    base_packs: frozenset[str] | None = None,
    *,
    profile: AgentProfile | None = None,
) -> frozenset[str]:
    """Compute effective set of enabled packs for an agent."""
    base = base_packs or frozenset()
    if profile is None:
        return base
    return (base | profile.extra_packs) - profile.disabled_packs
