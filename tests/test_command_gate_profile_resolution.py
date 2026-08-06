"""Tests for app.services.command_gate.resolve_effective_profile_with_access_gate.

No test coverage existed for this specific integration point before this
file — tests/test_access_control.py only exercises AccessControlStore in
isolation, never through command_gate's actual call site.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.access_control import AccessControlStore
from app.auth_middleware import AuthIdentity
from app.services.command_gate import resolve_effective_profile_with_access_gate


def _identity(token: str) -> AuthIdentity:
    return AuthIdentity(token_type="agent", token=token, name="test-key")


class TestResolveEffectiveProfileWithAccessGate:
    def test_allowed_actor_keeps_more_restrictive_per_key_profile(self):
        """Regression: this function passed settings.command_policy_profile
        (the bare global default) as the `requested_profile` to
        AccessControlStore.resolve_access_policy(), instead of the per-key
        profile that COMMAND_POLICY_KEY_PROFILES explicitly configured for
        this identity. For an actor+IP already marked "allowed" in the
        access-control gate, resolve_access_policy() returns
        effective_profile=requested_profile verbatim -- so a key
        deliberately capped to "readonly" via COMMAND_POLICY_KEY_PROFILES
        silently widened back to the global default profile ("default",
        far more permissive) the moment that key's actor+IP got approved
        in access control. access_control_enabled defaults to True, so
        any deployment combining both features hit this by default.
        """
        identity = _identity("agent-token-xyz")

        store = MagicMock(spec=AccessControlStore)
        store.resolve_access_policy.return_value = MagicMock(
            state="allowed",
            effective_profile="readonly",  # what a correct call would return
            reason="operator approved",
            key_hash="deadbeef",
        )

        with (
            patch("app.services.command_gate._state") as mock_state,
            patch("app.services.command_gate.settings") as mock_settings,
        ):
            mock_state.access_control_store = store
            mock_settings.access_control_enabled = True
            mock_settings.access_control_enforce_master = False
            mock_settings.command_policy_profile = "default"
            mock_settings.command_policy_key_profiles = (
                '{"' + identity.fingerprint[:12] + '": "readonly"}'
            )
            mock_settings.trusted_proxy_cidrs = ""

            resolve_effective_profile_with_access_gate(
                None, identity, source_ip="1.2.3.4"
            )

        called_kwargs = store.resolve_access_policy.call_args.kwargs
        assert called_kwargs["requested_profile"] == "readonly", (
            f"must pass the per-key profile (readonly), not the global "
            f"default -- got {called_kwargs['requested_profile']!r}"
        )
