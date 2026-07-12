"""Build metadata — single source of truth for build SHA, time, and process start.

BUILD_SHA and BUILD_TIME are resolved once at import time from env vars.
STARTED_AT is set explicitly in the app lifespan (not at import time).
"""

import os
import subprocess
import time as _time
from datetime import UTC, datetime

BUILD_SHA: str = ""
BUILD_TIME: str = ""
_started_at: float | None = None


def _resolve_build_sha() -> str:
    env_sha = os.environ.get("BUILD_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _resolve_build_time() -> str:
    return os.environ.get("BUILD_TIME", "").strip()


BUILD_SHA = _resolve_build_sha()
BUILD_TIME = _resolve_build_time()


def set_started_at() -> None:
    """Call once during app lifespan to record process start time."""
    global _started_at
    _started_at = _time.time()


def get_started_at() -> float | None:
    """Return process start time as float, or None if not yet set."""
    return _started_at


def get_build_metadata() -> dict[str, str]:
    """Return build metadata as an ISO-8601 dict suitable for JSON serialization."""
    started_iso = ""
    if _started_at is not None:
        started_iso = datetime.fromtimestamp(_started_at, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "build_sha": BUILD_SHA,
        "build_time": BUILD_TIME,
        "started_at": started_iso,
    }
