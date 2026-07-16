"""Tests for server-owned profile routing."""

from __future__ import annotations

import json

from app.command_policy import evaluate_command_policy, parse_key_profiles, profile_for_identity


class TestParseKeyProfiles:
    def test_valid_json(self):
        raw = '{"abc123": "testlint", "def456": "readonly"}'
        result = parse_key_profiles(raw)
        assert result == {"abc123": "testlint", "def456": "readonly"}

    def test_empty_json(self):
        result = parse_key_profiles("{}")
        assert result == {}

    def test_invalid_json(self):
        result = parse_key_profiles("not json")
        assert result == {}

    def test_non_dict_json(self):
        result = parse_key_profiles('["abc"]')
        assert result == {}

    def test_numeric_keys(self):
        result = parse_key_profiles('{"123": "testlint"}')
        assert result == {"123": "testlint"}


class TestProfileForIdentity:
    def test_matching_fingerprint(self):
        key_profiles = {"abc123": "testlint", "def456": "readonly"}
        result = profile_for_identity("abc123", key_profiles=key_profiles)
        assert result == "testlint"

    def test_no_match_falls_back(self):
        key_profiles = {"abc123": "testlint"}
        result = profile_for_identity("xyz789", key_profiles=key_profiles)
        assert result == "default"

    def test_empty_fingerprint(self):
        result = profile_for_identity("", key_profiles={"abc123": "testlint"})
        assert result == "default"

    def test_none_fingerprint(self):
        result = profile_for_identity(None, key_profiles={"abc123": "testlint"})
        assert result == "default"

    def test_empty_key_profiles(self):
        result = profile_for_identity("abc123", key_profiles={})
        assert result == "default"

    def test_none_key_profiles(self):
        result = profile_for_identity("abc123", key_profiles=None)
        assert result == "default"

    def test_custom_default(self):
        result = profile_for_identity("abc123", key_profiles={}, default_profile="readonly")
        assert result == "readonly"

    def test_all_profiles(self):
        key_profiles = {
            "aaa": "readonly",
            "bbb": "testlint",
            "ccc": "project-automation",
            "ddd": "ops",
            "eee": "docker-admin",
        }
        assert profile_for_identity("aaa", key_profiles=key_profiles) == "readonly"
        assert profile_for_identity("bbb", key_profiles=key_profiles) == "testlint"
        assert profile_for_identity("ccc", key_profiles=key_profiles) == "project-automation"
        assert profile_for_identity("ddd", key_profiles=key_profiles) == "ops"
        assert profile_for_identity("eee", key_profiles=key_profiles) == "docker-admin"


class TestClientProfileRejected:
    """Verify that request body profile is not accepted."""

    def test_no_profile_in_execute_request(self):
        """The ExecuteRequest model should not have a profile field."""
        from app.models import ExecuteRequest
        fields = ExecuteRequest.model_fields
        assert "profile" not in fields, "ExecuteRequest should not accept profile from client"

    def test_no_profile_in_execute_argv_request(self):
        """The ExecuteArgvRequest model should not have a profile field."""
        from app.models import ExecuteArgvRequest
        fields = ExecuteArgvRequest.model_fields
        assert "profile" not in fields, "ExecuteArgvRequest should not accept profile from client"


class TestDecisionReflectsMappedProfile:
    """Verify effective_profile flows into evaluate_command_policy and audit."""

    def test_mapped_key_to_testlint(self):
        """Fingerprint mapped to testlint → pytest allowed, rm blocked."""
        key_profiles = {"abc123": "testlint"}
        effective = profile_for_identity("abc123", key_profiles=key_profiles)
        assert effective == "testlint"

        d_allowed = evaluate_command_policy("pytest -q", mode="enforce", profile=effective)
        assert d_allowed.allowed is True
        assert d_allowed.profile == "testlint"

        d_blocked = evaluate_command_policy("rm file.txt", mode="enforce", profile=effective)
        assert d_blocked.allowed is False
        assert d_blocked.profile == "testlint"

    def test_unmapped_key_to_default(self):
        """Unmapped fingerprint → default profile."""
        key_profiles = {"abc123": "testlint"}
        effective = profile_for_identity("xyz789", key_profiles=key_profiles)
        assert effective == "default"

        d = evaluate_command_policy("ls -la", mode="enforce", profile=effective)
        assert d.allowed is True
        assert d.profile == "default"

    def test_raw_key_never_in_decision(self):
        """Raw API key value must not appear in decision.profile."""
        raw_key = "f0200367590c791e5e0f74d991a2609789444819a72af332744c3954072bb215"
        fingerprint = raw_key[:12]
        key_profiles = {fingerprint: "testlint"}

        effective = profile_for_identity(fingerprint, key_profiles=key_profiles)
        d = evaluate_command_policy("pytest -q", mode="enforce", profile=effective)

        assert d.profile == "testlint"
        assert raw_key not in d.profile
        assert raw_key not in d.reason

    def test_raw_key_never_in_audit_string(self):
        """Raw API key must not appear in audit log strings."""
        raw_key = "f0200367590c791e5e0f74d991a2609789444819a72af332744c3954072bb215"
        fingerprint = raw_key[:12]
        key_profiles = {fingerprint: "testlint"}

        effective = profile_for_identity(fingerprint, key_profiles=key_profiles)
        d = evaluate_command_policy("pytest -q", mode="enforce", profile=effective)

        # Audit string format: "profile={decision.profile}; ..."
        audit_str = f"profile={d.profile}; mode={d.mode}; command_root={d.command_root}"
        assert raw_key not in audit_str
        assert raw_key[:12] not in audit_str  # fingerprint not in audit either


class TestMalformedConfigFailsSafe:
    """Malformed COMMAND_POLICY_KEY_PROFILES falls back to default."""

    def test_invalid_json_fails_safe(self):
        result = parse_key_profiles("not valid json {{{")
        assert result == {}

    def test_empty_string_fails_safe(self):
        result = parse_key_profiles("")
        assert result == {}

    def test_none_fails_safe(self):
        result = parse_key_profiles(None)
        assert result == {}

    def test_list_fails_safe(self):
        result = parse_key_profiles('["abc"]')
        assert result == {}

    def test_fails_safe_to_default_profile(self):
        """Malformed config → empty dict → profile_for_identity returns default."""
        key_profiles = parse_key_profiles("bad json")
        effective = profile_for_identity("any_fingerprint", key_profiles=key_profiles)
        assert effective == "default"


class TestConfigFormat:
    """Verify config format uses fingerprint/name, never raw key."""

    def test_key_profiles_uses_fingerprint(self):
        """Config keys should be short fingerprints, not full API keys."""
        raw = '{"abc123": "testlint"}'
        parsed = parse_key_profiles(raw)
        for key in parsed:
            assert len(key) <= 64, f"Key {key!r} looks like a full API key (too long)"
            assert " " not in key, f"Key {key!r} contains spaces"

    def test_all_valid_profiles_accepted(self):
        valid = {"readonly", "testlint", "project-automation", "ops", "docker-admin", "default"}
        for p in valid:
            raw = json.dumps({"key": p})
            parsed = parse_key_profiles(raw)
            assert parsed["key"] == p

    def test_invalid_profile_value_stored(self):
        """Invalid profile value is stored as-is; validation happens at decision time."""
        raw = '{"key": "nonexistent_profile"}'
        parsed = parse_key_profiles(raw)
        assert parsed["key"] == "nonexistent_profile"
        # evaluate_command_policy will fall back to default evaluator
