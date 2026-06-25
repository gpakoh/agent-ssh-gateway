"""Tests for GatewayOAuthProvider."""

import hashlib
import base64
import secrets
import time

import pytest

from examples.mcp_server.oauth_provider import (
    _verify_pkce,
    _generate_code_challenge,
    SUPPORTED_SCOPES,
    DEFAULT_SCOPES,
)


def test_pkce_verification_valid():
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _generate_code_challenge(code_verifier)
    assert _verify_pkce(code_verifier, code_challenge) is True


def test_pkce_verification_invalid():
    code_verifier = secrets.token_urlsafe(64)
    wrong_challenge = "AAAA" + _generate_code_challenge(code_verifier)[4:]
    assert _verify_pkce(code_verifier, wrong_challenge) is False


def test_pkce_verifier_too_short():
    with pytest.raises(ValueError):
        _verify_pkce("short", "challenge")


def test_generate_code_challenge_deterministic():
    verifier = secrets.token_urlsafe(64)
    c1 = _generate_code_challenge(verifier)
    c2 = _generate_code_challenge(verifier)
    assert c1 == c2


def test_generate_code_challenge_differs():
    v1 = secrets.token_urlsafe(64)
    v2 = secrets.token_urlsafe(64)
    assert _generate_code_challenge(v1) != _generate_code_challenge(v2)


def test_scope_constants():
    assert "mcp:read" in SUPPORTED_SCOPES
    assert "mcp:admin" not in DEFAULT_SCOPES
