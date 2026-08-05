from __future__ import annotations

import os

# Roots under which all project paths must resolve (symlink-safe).
# Environment MCP_ALLOWED_PROJECT_ROOTS overrides this list (comma-separated).
_ALLOWED_PROJECT_ROOTS_DEFAULT: list[str] = [
    "/media/1TB/Python/",
    "/var/www/",
]


def _load_allowed_roots() -> list[str]:
    raw = os.environ.get("MCP_ALLOWED_PROJECT_ROOTS", "").strip()
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    return list(_ALLOWED_PROJECT_ROOTS_DEFAULT)


ALLOWED_PROJECT_ROOTS: list[str] = _load_allowed_roots()
