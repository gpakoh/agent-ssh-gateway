"""Single source of truth for the application version.

Used by FastAPI OpenAPI, capabilities endpoint, Prometheus info, and CLI.
Import this instead of hardcoding version strings.

Version resolution:
  1. Env APP_VERSION — deployment override
  2. FALLBACK_VERSION — hardcoded in source, bumped with each release
     in sync with pyproject.toml (primary source for source-tree deployments)
  3. importlib.metadata — fallback for pip-installed package without source
"""

import os
from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "agent-ssh-gateway"
FALLBACK_VERSION = "0.1.24a0"

_VERSION_SOURCE: str | None = None


def get_app_version() -> str:
    global _VERSION_SOURCE

    env_version = os.environ.get("APP_VERSION")
    if env_version:
        _VERSION_SOURCE = "env"
        return env_version

    _VERSION_SOURCE = "source"
    return FALLBACK_VERSION


def get_version_source() -> str:
    get_app_version()
    return _VERSION_SOURCE or "unknown"


def get_package_version() -> str | None:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return None


APP_VERSION = get_app_version()
