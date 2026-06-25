"""Single source of truth for the application version.

Used by FastAPI OpenAPI, capabilities endpoint, Prometheus info, and CLI.
Import this instead of hardcoding version strings.
"""

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "agent-ssh-gateway"
FALLBACK_VERSION = "0.1.17a0"


def get_app_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return FALLBACK_VERSION


APP_VERSION = get_app_version()
