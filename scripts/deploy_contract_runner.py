#!/usr/bin/env python3
"""One-shot helper for an already-validated repository deploy contract.

This module is not an MCP tool. The MCP adapter constructs every argument after
binding a registered candidate clone, immutable image identity, infra root and
state path. The helper only executes that fixed plan inside a constrained
Docker container and returns bounded, machine-readable evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

_REDACTED_OUTPUT = "[redacted: deploy-script output]"
_STATE_FIELDS = (
    "gpt_browser_bridge_image",
    "gpt_browser_bridge_image_id",
    "gpt_browser_bridge_compose_config_fingerprint",
    "gpt_browser_bridge_container_id",
    "gpt_browser_bridge_deploy_generation",
    "deployed_at",
)
_ALLOWED_IMAGE_ENV = {"GPT_BRIDGE_TARGET_IMAGE", "GPT_BRIDGE_RELEASE_IMAGE"}


def _emit(*, exit_code: int, output: str, state: dict[str, Any] | None = None, error: str | None = None) -> None:
    payload: dict[str, Any] = {
        "version": 1,
        "exit_code": int(exit_code),
        # Captured deploy stdout may include credentials unavailable to the
        # Gateway's static redactor. Never forward the untrusted text into
        # the helper JSON, Docker logs or MCP responses.
        "output_tail": _REDACTED_OUTPUT if output else "",
        "state": state or {},
    }
    if error:
        payload["error"] = error
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _safe_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _read_state(path: Path) -> dict[str, Any]:
    if not _safe_regular_file(path):
        raise RuntimeError("deployment state file is unavailable or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("deployment state file is unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("deployment state payload is not an object")

    # State is script-supplied. Before serializing fields into helper stdout,
    # constrain each to its expected non-secret wire format. The adapter
    # separately compares those fields with live image/container identity.
    patterns = {
        "gpt_browser_bridge_image": r"[A-Za-z0-9._:/-]{1,300}@sha256:[0-9a-f]{64}",
        "gpt_browser_bridge_image_id": r"sha256:[0-9a-f]{64}",
        "gpt_browser_bridge_compose_config_fingerprint": r"[0-9a-f]{64}",
        "gpt_browser_bridge_container_id": r"[0-9a-f]{64}",
        "gpt_browser_bridge_deploy_generation": r"[0-9a-f]{64}",
    }
    for key, pattern in patterns.items():
        value = payload.get(key)
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise RuntimeError("deployment state field is missing or invalid")
    timestamp = payload.get("deployed_at")
    if not isinstance(timestamp, str) or not 10 <= len(timestamp) <= 64:
        raise RuntimeError("deployment state timestamp is invalid")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise RuntimeError("deployment state timestamp is invalid") from exc
    if parsed_timestamp.tzinfo is None:
        raise RuntimeError("deployment state timestamp has no timezone")
    return {key: payload[key] for key in _STATE_FIELDS}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--infra-root", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--image-env", required=True, choices=sorted(_ALLOWED_IMAGE_ENV))
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--timeout", required=True, type=int)
    args = parser.parse_args()

    script = Path(args.script)
    infra_root = Path(args.infra_root)
    state_file = Path(args.state_file)
    timeout = max(1, min(args.timeout, 600))

    if not script.is_absolute() or not _safe_regular_file(script):
        _emit(exit_code=125, output="", error="deploy script is unavailable or unsafe")
        return 125
    if not infra_root.is_absolute() or not infra_root.is_dir() or infra_root.is_symlink():
        _emit(exit_code=125, output="", error="infra root is unavailable or unsafe")
        return 125
    try:
        state_file.resolve(strict=False).relative_to(infra_root.resolve(strict=True))
    except (OSError, ValueError):
        _emit(exit_code=125, output="", error="state file escapes infra root")
        return 125

    for command in ("bash", "curl", "docker", "flock", "python3"):
        if shutil.which(command) is None:
            _emit(exit_code=125, output="", error=f"required helper command missing: {command}")
            return 125

    env = os.environ.copy()
    env.pop("GPT_BRIDGE_TARGET_IMAGE", None)
    env.pop("GPT_BRIDGE_RELEASE_IMAGE", None)
    env["INFRA_ROOT"] = str(infra_root)
    env[args.image_env] = args.image_ref
    env["HOME"] = "/tmp"

    try:
        completed = subprocess.run(
            ["/bin/bash", str(script)],
            cwd=infra_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        _emit(exit_code=124, output=output, error="deploy contract timed out")
        return 124
    except OSError:
        _emit(exit_code=125, output="", error="deploy contract could not start")
        return 125

    state: dict[str, Any] = {}
    error: str | None = None
    if completed.returncode == 0:
        try:
            state = _read_state(state_file)
        except RuntimeError as exc:
            error = str(exc)
            completed = subprocess.CompletedProcess(
                completed.args,
                125,
                completed.stdout,
                completed.stderr,
            )

    _emit(
        exit_code=int(completed.returncode),
        output=completed.stdout or "",
        state=state,
        error=error,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
