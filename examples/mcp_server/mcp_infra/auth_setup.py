"""Auth and backend-router bootstrap for the MCP composition root.

Extracted from server.py during audit #8 stage 3: the composition root
stays a thin facade. setup() is called once per (re)import of server.py
so importlib.reload re-reads the environment (safe-mode / auth-mode
tests rely on that); the module itself has no import-time side effects.

Returns (auth_settings, auth_provider, agent_router); server.py
re-exports them under its own names (_auth_settings, _auth_provider,
_agent_router) because tests address the provider by server-module
identity.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from examples.mcp_server.oauth_provider import (
    DEFAULT_SCOPES,
    SUPPORTED_SCOPES,
    GatewayOAuthProvider,
)


def setup() -> tuple[Any, Any, Any]:
    """Bootstrap auth provider/settings and the agent backend router.

    Returns (auth_settings, auth_provider, agent_router) -- agent_router
    is None unless MCP_AGENT_BACKEND_ROUTER_ENABLED=true.
    """
    auth_mode = os.environ.get("MCP_AUTH_MODE", "oauth").strip().lower()
    if auth_mode not in ("token", "oauth"):
        raise ValueError(f"Invalid auth_mode={auth_mode!r}; expected one of ('token', 'oauth')")

    auth_provider: GatewayOAuthProvider | None = None
    auth_settings = None

    if auth_mode == "oauth":
        auth_provider = GatewayOAuthProvider()

        _health_token = os.environ.get("MCP_HEALTHCHECK_BEARER_TOKEN", "")
        if _health_token:
            from examples.mcp_server.oauth_provider import StoredToken as _StoredToken
            from examples.mcp_server.oauth_provider import hash_token as _hash_tok

            _at_hash = _hash_tok(_health_token)
            auth_provider._tokens[_at_hash] = _StoredToken(
                token=_at_hash,
                client_id="mcp_healthcheck",
                # health (the only tool this credential exists to call) only
                # requires "mcp:read" (see tool_scopes.py) -- granting the full
                # SUPPORTED_SCOPES set (admin/execute/docker included) made a
                # credential whose entire purpose is an unauthenticated-adjacent
                # liveness probe as powerful as any operator token if it leaked.
                scopes=["mcp:read"],
                expires_at=float("inf"),
                type="access",
            )

        _extra_tokens_all: dict[str, str] = {}

        _extra_tokens_json = os.environ.get("MCP_EXTRA_TOKENS_JSON", "")
        if _extra_tokens_json:
            import json

            try:
                _extra_tokens_all.update(json.loads(_extra_tokens_json))
            except Exception as _exc:
                print(f"  MCP_EXTRA_TOKENS_JSON error: {_exc}", file=sys.stderr)

        _extra_tokens_file = os.environ.get("MCP_EXTRA_TOKENS_FILE", "")
        if _extra_tokens_file:
            if os.path.isfile(_extra_tokens_file):
                import json

                try:
                    with open(_extra_tokens_file) as _f:
                        _extra_tokens_all.update(json.load(_f))
                except Exception as _exc:
                    print(f"  MCP_EXTRA_TOKENS_FILE error: {_exc}", file=sys.stderr)
            else:
                print(
                    f"  MCP_EXTRA_TOKENS_FILE not found: {_extra_tokens_file}",
                    file=sys.stderr,
                )

        if _extra_tokens_all:
            from examples.mcp_server.oauth_provider import StoredToken as _StoredToken
            from examples.mcp_server.oauth_provider import hash_token as _hash_tok
            from examples.mcp_server.tool_scopes import get_profile_scopes as _get_profile_scopes

            for _token_str, _profile in _extra_tokens_all.items():
                _at_hash = _hash_tok(_token_str)
                _profile_scopes = _get_profile_scopes(_profile)
                auth_provider._tokens[_at_hash] = _StoredToken(
                    token=_at_hash,
                    client_id=f"mcp_extras_{_profile}",
                    scopes=list(_profile_scopes),
                    expires_at=float("inf"),
                    type="access",
                )
            print(f"  extra tokens: {len(_extra_tokens_all)} registered", file=sys.stderr)
            if _extra_tokens_file:
                print(f"  extra file  : {_extra_tokens_file}", file=sys.stderr)

        try:
            from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
            from pydantic import AnyHttpUrl

            auth_settings = AuthSettings(
                issuer_url=AnyHttpUrl(os.environ.get("MCP_ISSUER_URL", "https://gateway.example.com")),
                resource_server_url=AnyHttpUrl(
                    os.environ.get("MCP_RESOURCE_URL", "https://gateway.example.com/mcp")
                ),
                service_documentation_url=AnyHttpUrl("https://github.com/gpakoh/agent-ssh-gateway"),
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=SUPPORTED_SCOPES,
                    default_scopes=list(SUPPORTED_SCOPES),
                ),
                required_scopes=None,
            )
        except ImportError:
            pass
    elif auth_mode == "token":
        auth_provider = GatewayOAuthProvider()
        mcp_token = os.environ.get("MCP_PUBLIC_TOKEN", "")
        if not mcp_token:
            raise ValueError("MCP_PUBLIC_TOKEN is required in token mode")
        from examples.mcp_server.oauth_provider import StoredToken as _StoredToken
        from examples.mcp_server.oauth_provider import hash_token as _hash_tok

        _at_hash = _hash_tok(mcp_token)
        auth_provider._tokens[_at_hash] = _StoredToken(
            token=_at_hash,
            client_id="mcp_static_client",
            scopes=list(DEFAULT_SCOPES),
            expires_at=float("inf"),
            type="access",
        )

    # ── TokenStore: load persistent tokens from store ──────────────────
    if auth_provider is not None:
        try:
            from examples.mcp_server.token_store import TokenStore

            _token_store = TokenStore()
            auth_provider.set_token_store(_token_store)
            _loaded = auth_provider.load_tokens()
            if _loaded:
                print(
                    f"  TokenStore: {_loaded} tokens loaded from {_token_store._path}", file=sys.stderr
                )
        except Exception as _exc:
            print(f"  TokenStore: error loading tokens: {_exc}", file=sys.stderr)

    # ── ClientStore: load persisted dynamically-registered OAuth clients ──
    # Without this, GatewayOAuthProvider._clients was purely in-memory --
    # every restart forgot every client a connector had ever registered via
    # DCR, so the next reconnection attempt failed with "Client ID ... not
    # found" even though nothing about the connection itself had changed.
    if auth_provider is not None and auth_mode == "oauth":
        try:
            from examples.mcp_server.client_store import ClientStore

            _client_store = ClientStore()
            auth_provider.set_client_store(_client_store)
            _clients_loaded = auth_provider.load_clients()
            if _clients_loaded:
                print(
                    f"  ClientStore: {_clients_loaded} clients loaded from {_client_store._path}",
                    file=sys.stderr,
                )
        except Exception as _exc:
            print(f"  ClientStore: error loading clients: {_exc}", file=sys.stderr)

    # ── Agent Backend Router ─────────────────────────────────────────────
    agent_router: AgentBackendRouter | None = None
    if os.environ.get("MCP_AGENT_BACKEND_ROUTER_ENABLED", "false").strip().lower() == "true":
        try:
            from examples.mcp_server.agent_backend_router import AgentBackendRouter

            agent_router = AgentBackendRouter(
                fallback_order=[
                    x.strip()
                    for x in os.environ.get("MCP_BACKEND_FALLBACK_ORDER", "opencode").split(",")
                    if x.strip()
                ],
            )
            print(
                f"  backend router: enabled ({len(agent_router._backends)} backends)", file=sys.stderr
            )
        except Exception as _exc:
            print(f"  backend router: init error: {_exc}", file=sys.stderr)

    return auth_settings, auth_provider, agent_router
