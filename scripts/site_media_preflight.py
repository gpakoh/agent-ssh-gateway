"""Read-only, fail-closed qualification of the optional site-media Compose overlay.

Does not deploy, print secrets, pull, build, modify containers or touch Git.
Run from the repository root with operator-supplied environment variables:
  python scripts/site_media_preflight.py
"""

from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

IMAGE_PATTERN = re.compile(
    r"192\.168\.1\.103:3005/gpakoh/site-media-worker@sha256:[0-9a-f]{64}\Z"
)
ROOT = Path(__file__).resolve().parents[1]


class MediaPreflightError(ValueError):
    """The release must not be activated."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise MediaPreflightError(message)


def validate_environment(env: dict[str, str]) -> tuple[str, str]:
    """Return expected image/token privately; never display the token."""
    token = env.get("SITE_MEDIA_WORKER_TOKEN", "")
    image = env.get("SITE_MEDIA_WORKER_IMAGE", "")
    _check(
        32 <= len(token) <= 512
        and all(33 <= ord(char) <= 126 for char in token),
        "SITE_MEDIA_WORKER_TOKEN must be an operator-owned 32..512 character printable secret",
    )
    _check(
        IMAGE_PATTERN.fullmatch(image) is not None,
        "SITE_MEDIA_WORKER_IMAGE must be an approved registry image pinned by @sha256",
    )
    return image, token


def validate_compose(data: dict[str, Any], image: str, token: str) -> None:
    """Inspect the fully interpolated Compose model before any deployment."""
    services = data.get("services") or {}
    _check(isinstance(services, dict), "Compose services missing")
    mcp = services.get("mcp-oauth") or {}
    worker = services.get("site-media-worker") or {}
    _check(bool(mcp and worker), "Both MCP and worker must exist in the overlay")
    mcp_nets = mcp.get("networks") or {}
    worker_nets = worker.get("networks") or {}
    _check(
        isinstance(mcp_nets, dict) and {"internal_net", "media_net"} <= set(mcp_nets),
        "MCP must retain internal_net and gain media_net",
    )
    _check(
        isinstance(worker_nets, dict) and set(worker_nets) == {"media_net"},
        "Worker must be attached ONLY to media_net",
    )
    networks = data.get("networks") or {}
    media_net = networks.get("media_net") or {}
    _check(media_net.get("internal") is True, "media_net must have internal:true")
    _check(
        worker.get("image") == image and not worker.get("build"),
        "Worker must use the verified immutable image, not a local build",
    )
    _check(
        worker.get("profiles") == ["media"],
        "Worker requires an explicit opt-in media profile",
    )
    _check(
        not worker.get("ports") and not worker.get("volumes")
        and not worker.get("devices") and not worker.get("cap_add")
        and not worker.get("network_mode") and not worker.get("pid")
        and not worker.get("ipc") and worker.get("privileged") is not True,
        "Worker must expose no ports, device grants, host namespaces or workspace mounts",
    )
    _check(worker.get("read_only") is True, "Worker filesystem must be read-only")
    _check(
        "ALL" in worker.get("cap_drop", []),
        "Worker capabilities must be dropped",
    )
    _check(
        "no-new-privileges:true" in worker.get("security_opt", []),
        "Worker must deny privilege escalation",
    )
    _check(worker.get("user") == "1201:1201", "Worker must run non-root")
    _check(
        worker.get("pids_limit") == 64 and 0 < float(worker.get("cpus", 0)) <= 1
        and worker.get("mem_limit") in ("512m", 536870912),
        "Worker must retain bounded CPU, memory and PID limits",
    )
    for service in (mcp, worker):
        supplied = service.get("environment") or {}
        _check(
            isinstance(supplied, dict)
            and isinstance(supplied.get("SITE_MEDIA_WORKER_TOKEN"), str)
            and hmac.compare_digest(supplied["SITE_MEDIA_WORKER_TOKEN"], token),
            "MCP and worker must share the same operator-owned secret",
        )


def inspect_compose(env: dict[str, str]) -> dict[str, Any]:
    """Only resolves Compose configuration. All stdout/stderr remain private."""
    command = [
        "docker", "compose",
        "--project-directory", str(ROOT / "docker"),
        "-f", str(ROOT / "docker" / "docker-compose.yml"),
        "-f", str(ROOT / "docker" / "site-media.compose.yml"),
        "--profile", "media", "config", "--format", "json",
    ]
    try:
        proc = subprocess.run(
            command, cwd=ROOT, env=env, text=True,
            capture_output=True, check=False, timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaPreflightError("Docker Compose configuration unavailable") from exc
    # Compose error output may include interpolated secrets; do NOT echo it.
    _check(proc.returncode == 0, "Docker Compose configuration invalid or missing environment")
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise MediaPreflightError("Docker Compose returned invalid JSON") from exc
    _check(isinstance(data, dict), "Docker Compose response must be an object")
    return data


def main() -> int:
    try:
        image, token = validate_environment(dict(os.environ))
        compose = inspect_compose(dict(os.environ))
        validate_compose(compose, image, token)
    except (MediaPreflightError, KeyError, TypeError, ValueError) as exc:
        # Carefully authored errors never embed the operator secret.
        print(f"site-media preflight: NO-GO: {exc}", file=sys.stderr)
        return 1
    print("site-media preflight: GO (config only; NOT a live runtime qualification)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
