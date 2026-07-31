"""Authorization invariant (P19.4).

Every registered route MUST require authentication unless it is explicitly
public (ALWAYS_PUBLIC, PUBLIC_AUTH_PATHS, /static) — verified by probing
each route without credentials. This guards against new endpoints silently
bypassing auth when api_auth_enabled=True.
"""

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute

from app.auth_middleware import ALWAYS_PUBLIC, PUBLIC_AUTH_PATHS
from app.main import app

TEST_API_KEY = "test-auth-invariant-key-008"


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _is_public(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return True
    if path in ALWAYS_PUBLIC or path in PUBLIC_AUTH_PATHS:
        return True
    if path == "/static" or path.startswith("/static/"):
        return True
    return False


def _auth_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                yield method, route.path
        elif isinstance(route, WebSocketRoute):
            yield "WEBSOCKET", route.path


class TestAuthorizationInvariant:
    def test_all_routes_require_auth(self, client):
        """Unauthenticated requests to non-public routes get 401/403."""
        for method, path in _auth_routes():
            if _is_public(path, method):
                continue
            if method == "WEBSOCKET":
                continue
            resp = client.request(method, path)
            assert resp.status_code in (401, 403), (
                f"{method} {path} returned {resp.status_code} without auth — "
                f"endpoint bypasses authentication"
            )

    def test_public_health_is_reachable(self, client):
        resp = client.get("/health")
        assert resp.status_code in (200, 503), (
            f"/health must stay public, got {resp.status_code}"
        )

    def test_auth_endpoints_do_not_require_api_key(self, client):
        """Auth endpoints are exempt from the API-key gate: never 503/401/403."""
        for path in PUBLIC_AUTH_PATHS:
            resp = client.post(path, json={})
            assert resp.status_code not in (401, 403, 503), (
                f"{path} must be exempt from the API-key middleware gate, "
                f"got {resp.status_code}"
            )
