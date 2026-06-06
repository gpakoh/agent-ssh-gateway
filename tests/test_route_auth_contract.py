"""Contract test: every /api/ route must have an explicit auth dependency.

This prevents accidentally public endpoints when new routers or features
are added.  The global HTTP middleware (auth_check) provides a first layer,
but each route should also declare its auth intent explicitly via Depends.

Public routes are whitelisted in PUBLIC_ROUTES.  Everything else must
have a ``verify_api_key``, ``verify_master_api_key``, or ``require_scope``
dependency.
"""

from fastapi.routing import APIRoute

from app.main import app

PUBLIC_ROUTES: set[tuple[str, str]] = {
    ("GET", "/health"),
    ("GET", "/"),
    ("GET", "/api/health"),
    ("GET", "/api/capabilities"),
    ("GET", "/api/config"),
    ("GET", "/api/help"),
    ("GET", "/api/sdk/download"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
}

PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/static",
    "/api/auth/",
)

AUTH_DEPENDENCY_NAMES: set[str] = {
    "verify_api_key",
    "verify_master_api_key",
    "require_master_key",
    "_identity",
}


def route_methods(route: APIRoute) -> set[str]:
    return {m for m in (route.methods or set()) if m not in {"HEAD", "OPTIONS"}}


def is_public_route(method: str, path: str) -> bool:
    if (method, path) in PUBLIC_ROUTES:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


def has_auth_dependency(route: APIRoute) -> bool:
    for dependant in route.dependant.dependencies:
        call = dependant.call

        if hasattr(call, "required_scope"):
            return True

        name = getattr(call, "__name__", "")
        if name in AUTH_DEPENDENCY_NAMES:
            return True

        qualname = getattr(call, "__qualname__", "")
        if "require_scope" in qualname:
            return True

    return False


def test_all_api_routes_are_explicitly_public_or_authenticated():
    failures: list[str] = []

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue

        path = route.path

        if not (
            path.startswith("/api")
            or path in {"/health", "/", "/openapi.json", "/docs",
                        "/docs/oauth2-redirect", "/redoc"}
        ):
            continue

        for method in route_methods(route):
            if is_public_route(method, path):
                continue
            if has_auth_dependency(route):
                continue
            failures.append(f"{method} {path}")

    assert not failures, (
        "Unprotected API routes (missing auth dependency):\n"
        + "\n".join(f"  {f}" for f in failures)
    )
