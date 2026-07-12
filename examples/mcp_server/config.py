from __future__ import annotations

import json
import os
from pathlib import Path

# ── Project registry configuration ──────────────────────────────
# Maps logical project names to filesystem paths.
# Environment MCP_PROJECT_MAP_JSON overrides this dict (JSON format).
_PROJECT_MAP_DEFAULT: dict[str, str] = {
    "web-ssh-gateway": "/media/1TB/Python/web_ssh/web-ssh-gateway",
    "quart-ollama_bot": "/media/1TB/Python/quart-ollama_bot",
    "NOD_gateway": "/media/1TB/Python/NOD_gateway",
}

# Roots under which all project paths must resolve (symlink-safe).
# Environment MCP_ALLOWED_PROJECT_ROOTS overrides this list (comma-separated).
_ALLOWED_PROJECT_ROOTS_DEFAULT: list[str] = [
    "/media/1TB/Python/",
    "/var/www/",
]


def _load_project_map() -> dict[str, str]:
    raw = os.environ.get("MCP_PROJECT_MAP_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    # Fallback: single project from legacy MCP_GATEWAY_PROJECT_ROOT
    legacy = os.environ.get("MCP_GATEWAY_PROJECT_ROOT", "").strip().rstrip("/")
    if legacy:
        name = Path(legacy).name
        return {name: legacy}
    return dict(_PROJECT_MAP_DEFAULT)


def _load_allowed_roots() -> list[str]:
    raw = os.environ.get("MCP_ALLOWED_PROJECT_ROOTS", "").strip()
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    return list(_ALLOWED_PROJECT_ROOTS_DEFAULT)


PROJECT_MAP: dict[str, str] = _load_project_map()
ALLOWED_PROJECT_ROOTS: list[str] = _load_allowed_roots()
