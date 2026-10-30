"""Read-only qualification of the Gateway-to-Media API client overlay.

Dedicated media-processing runs under infra-quart; Gateway must never build it.
This check never prints Compose configuration (operator secrets) or mutates Docker.
"""

from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MEDIA_URL = "http://media-processing:8091"


class MediaPreflightError(ValueError):
    """Media client attachment must not be activated."""


def _check(allowed: bool, reason: str) -> None:
    if not allowed:
        raise MediaPreflightError(reason)


def validate_environment(env: dict[str, str]) -> str:
    token = env.get("SITE_MEDIA_WORKER_TOKEN", "")
    _check(
        32 <= len(token) <= 512 and all(33 <= ord(c) <= 126 for c in token),
        "SITE_MEDIA_WORKER_TOKEN must be 32..512 printable non-space characters",
    )
    _check(
        env.get("MEDIA_API_BASE_URL", MEDIA_URL) == MEDIA_URL,
        "MEDIA_API_BASE_URL must be the approved private Media API endpoint",
    )
    return token


def validate_compose(data: dict[str, Any], token: str) -> None:
    services = data.get("services") or {}
    _check(isinstance(services, dict), "services must be an object")
    _check("mcp-oauth" in services, "Gateway OAuth client missing from Compose")
    _check(
        "site-media-worker" not in services and "media-processing" not in services,
        "Gateway must not run any codec worker; media service belongs to infra-quart",
    )
    mcp = services["mcp-oauth"]
    _check(isinstance(mcp, dict), "invalid OAuth client model")
    networks = mcp.get("networks") or {}
    _check(
        isinstance(networks, dict) and {"internal_net", "media_net"} <= set(networks),
        "MCP must retain internal_net and join media_net",
    )
    media_net = (data.get("networks") or {}).get("media_net") or {}
    _check(
        media_net.get("external") is True
        and media_net.get("name") == "quart-media-internal",
        "media_net must use the approved external Quart media network",
    )
    env = mcp.get("environment") or {}
    _check(isinstance(env, dict), "MCP environment missing")
    _check(
        isinstance(env.get("SITE_MEDIA_WORKER_TOKEN"), str)
        and hmac.compare_digest(env["SITE_MEDIA_WORKER_TOKEN"], token),
        "MCP operator media credential mismatch",
    )
    _check(env.get("MEDIA_API_BASE_URL") == MEDIA_URL, "Media client URL changed")


def inspect_compose(env: dict[str, str]) -> dict[str, Any]:
    command = [
        "docker", "compose",
        "--project-directory", str(ROOT / "docker"),
        "-f", str(ROOT / "docker" / "docker-compose.yml"),
        "-f", str(ROOT / "docker" / "site-media.compose.yml"),
        "config", "--format", "json",
    ]
    try:
        proc = subprocess.run(
            command, cwd=ROOT, env=env, text=True,
            capture_output=True, check=False, timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaPreflightError("Docker Compose configuration unavailable") from exc
    # Do not surface stdout/stderr from interpolated Compose.
    _check(proc.returncode == 0, "Docker Compose configuration invalid or missing inputs")
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise MediaPreflightError("Docker Compose output is not valid JSON") from exc
    _check(isinstance(data, dict), "Docker Compose output must be an object")
    return data


def main() -> int:
    try:
        token = validate_environment(dict(os.environ))
        model = inspect_compose(dict(os.environ))
        validate_compose(model, token)
    except (MediaPreflightError, KeyError, TypeError, ValueError) as exc:
        print(f"Gateway Media API preflight: NO-GO: {exc}", file=sys.stderr)
        return 1
    print("Gateway Media API preflight: GO (config only; no runtime activation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
