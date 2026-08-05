"""Tests for agent profiles."""

from __future__ import annotations

import os

from app.agent_profiles import (
    BUILTIN_PROFILES,
    AgentProfile,
    TrustLevel,
    detect_agent,
    detect_agent_from_env,
    detect_agent_from_proc,
    effective_packs,
    get_agent_profile,
)


class TestTrustLevel:
    def test_values(self):
        assert TrustLevel.HIGH == "high"
        assert TrustLevel.MEDIUM == "medium"
        assert TrustLevel.LOW == "low"


class TestAgentProfile:
    def test_default_profile(self):
        p = AgentProfile()
        assert p.trust_level == TrustLevel.MEDIUM
        assert len(p.disabled_packs) == 0
        assert len(p.extra_packs) == 0

    def test_custom_profile(self):
        p = AgentProfile(
            trust_level=TrustLevel.LOW,
            disabled_packs=frozenset({"docker", "kubernetes"}),
            extra_packs=frozenset({"git"}),
        )
        assert p.trust_level == TrustLevel.LOW
        assert "docker" in p.disabled_packs
        assert "git" in p.extra_packs

    def test_builtin_chatgpt(self):
        p = BUILTIN_PROFILES["chatgpt"]
        assert p.trust_level == TrustLevel.LOW
        assert "docker" in p.disabled_packs

    def test_builtin_claude_code(self):
        p = BUILTIN_PROFILES["claude-code"]
        assert p.trust_level == TrustLevel.HIGH


class TestAgentDetection:
    def test_detect_from_env_known(self):
        os.environ["TEST_AGENT_DETECT"] = "1"
        try:
            os.environ["CLAUDE_CODE"] = "1"
            agent = detect_agent_from_env()
            assert agent == "claude-code"
        finally:
            os.environ.pop("CLAUDE_CODE", None)
            os.environ.pop("TEST_AGENT_DETECT", None)

    def test_detect_from_env_unknown(self):
        saved = {}
        for ev in ("CLAUDE_CODE", "CODEX_CLI", "AIDER", "CURSOR"):
            saved[ev] = os.environ.pop(ev, None)
        try:
            assert detect_agent_from_env() == "unknown"
        finally:
            for ev, val in saved.items():
                if val is not None:
                    os.environ[ev] = val

    def test_detect_from_proc_fallback(self):
        """detect_agent should return at least 'unknown'."""
        agent = detect_agent()
        assert isinstance(agent, str)

    def test_detect_from_proc_uses_parent_pid_not_own_pid(self, monkeypatch):
        """Regression: detect_agent_from_proc() read /proc/{os.getpid()}/comm
        — its own PID — instead of /proc/{os.getppid()}/comm, despite the
        docstring ("parent process name") and the local variable itself
        being named `ppid`. It always read the gateway's own process name
        (never one of _PROC_NAME_MAP's entries), so this fallback silently
        always returned "unknown" no matter what actually launched it.
        """
        own_pid = 111
        parent_pid = 222
        monkeypatch.setattr(os, "getpid", lambda: own_pid)
        monkeypatch.setattr(os, "getppid", lambda: parent_pid)

        def fake_isfile(path):
            if path == f"/proc/{own_pid}/comm":
                raise AssertionError("must read the PARENT pid's comm, not our own")
            return path == f"/proc/{parent_pid}/comm"

        monkeypatch.setattr(os.path, "isfile", fake_isfile)

        from unittest.mock import mock_open, patch

        m = mock_open(read_data="claude\n")
        with patch("builtins.open", m):
            result = detect_agent_from_proc()

        m.assert_called_once_with(f"/proc/{parent_pid}/comm")
        assert result == "claude-code"

    def test_get_profile_known(self):
        p = get_agent_profile("chatgpt")
        assert p.trust_level == TrustLevel.LOW

    def test_get_profile_unknown(self):
        p = get_agent_profile("nonexistent-agent")
        assert p.trust_level == TrustLevel.MEDIUM

    def test_get_profile_none(self):
        p = get_agent_profile(None)
        assert isinstance(p, AgentProfile)


class TestEffectivePacks:
    def test_no_profile_no_change(self):
        packs = frozenset({"docker", "git"})
        result = effective_packs(packs)
        assert result == packs

    def test_extra_packs_added(self):
        profile = AgentProfile(extra_packs=frozenset({"git"}))
        result = effective_packs(frozenset({"docker"}), profile=profile)
        assert "docker" in result
        assert "git" in result

    def test_disabled_packs_removed(self):
        profile = AgentProfile(disabled_packs=frozenset({"docker"}))
        result = effective_packs(frozenset({"docker", "git"}), profile=profile)
        assert "docker" not in result
        assert "git" in result
