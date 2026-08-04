"""Regression tests: /oauth/consent must not reflect unescaped HTML.

Regression context: this is a public, unauthenticated endpoint (always
excluded from auth via OAUTH_PUBLIC_PREFIXES — it's part of the real OAuth
authorization flow a legitimate user goes through to connect ChatGPT/an MCP
client) that used to interpolate client_id/redirect_uri/scope/state/
code_challenge/resource/error straight from query params into CONSENT_HTML
via str.format() with no escaping. `error` in particular renders in a plain
text context (<div id="error-msg">{error}</div>), so
?error=<script>...</script> executed verbatim in whoever's browser opened
the (real-domain, legitimate-looking) consent page — a crafted link could
have exfiltrated the authorization password typed into that exact form, or
tampered with the submitted client_id/redirect_uri before the real POST.
No existing test touched this handler at all.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def consent_client():
    from starlette.testclient import TestClient

    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": "t", "MCP_AUTH_MODE": "oauth"}):
        import importlib

        import examples.mcp_client_remote.server as srv

        importlib.reload(srv)
        app = srv.create_proxy_app()
        yield TestClient(app)


class TestConsentPageEscapesQueryParams:
    def test_error_param_script_tag_is_escaped(self, consent_client):
        resp = consent_client.get(
            "/oauth/consent",
            params={"error": "<script>alert(document.cookie)</script>"},
        )
        assert resp.status_code == 200
        assert "<script>alert(document.cookie)</script>" not in resp.text
        assert "&lt;script&gt;" in resp.text

    def test_client_id_attribute_breakout_is_escaped(self, consent_client):
        resp = consent_client.get(
            "/oauth/consent",
            params={"client_id": 'x" onmouseover="alert(1)'},
        )
        assert resp.status_code == 200
        assert 'onmouseover="alert(1)"' not in resp.text
        assert "&#x27;" in resp.text or "&quot;" in resp.text

    def test_redirect_uri_is_escaped(self, consent_client):
        resp = consent_client.get(
            "/oauth/consent",
            params={"redirect_uri": "javascript:alert(1)\"><svg onload=alert(1)>"},
        )
        assert resp.status_code == 200
        assert "<svg onload=alert(1)>" not in resp.text

    def test_all_reflected_fields_are_escaped(self, consent_client):
        payload = "<script>x</script>"
        resp = consent_client.get(
            "/oauth/consent",
            params={
                "client_id": payload,
                "redirect_uri": payload,
                "scope": payload,
                "state": payload,
                "code_challenge": payload,
                "resource": payload,
                "error": payload,
            },
        )
        assert resp.status_code == 200
        assert payload not in resp.text

    def test_normal_values_still_render_correctly(self, consent_client):
        """The fix must not break the legitimate, non-malicious case."""
        resp = consent_client.get(
            "/oauth/consent",
            params={"client_id": "chatgpt-connector", "state": "abc123"},
        )
        assert resp.status_code == 200
        assert "chatgpt-connector" in resp.text
        assert "abc123" in resp.text
