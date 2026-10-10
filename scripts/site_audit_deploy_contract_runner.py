#!/usr/bin/env python3
"""Constrained helper for the guarded Site Audit deployment contract.

The MCP adapter binds every path/identity before this helper starts.  This
process only invokes the repository-owned ``deploy-site-audit.sh`` (optionally
with ``--recover``) and returns bounded, machine-readable state evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

_MAX_OUTPUT_CHARS = 32 * 1024
_STATE_FIELDS = (
    "schema_version",
    "source_revision",
    "deploy_generation",
    "stability_seconds",
    "crm_route_surface_verified",
    "protected_containers_unchanged",
    "previous",
    "deployed",
)
_PENDING_FIELDS = (
    "schema_version",
    "status",
    "source_revision",
    "deploy_generation",
    "previous",
    "target",
    "protected_before",
)


def _emit(
    *,
    exit_code: int,
    output: str,
    state: dict[str, Any] | None = None,
    pending_before: dict[str, Any] | None = None,
    pending_exists: bool = False,
    mode: str = "deploy",
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "version": 1,
        "mode": mode,
        "exit_code": int(exit_code),
        "output_tail": output[-_MAX_OUTPUT_CHARS:],
        "state": state or {},
        "pending_before": pending_before or {},
        "pending_exists": bool(pending_exists),
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


def _read_fields(path: Path, fields: tuple[str, ...], label: str) -> dict[str, Any]:
    if not _safe_regular_file(path):
        raise RuntimeError(f"{label} file is unavailable or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} file is unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} payload is not an object")
    return {key: payload.get(key) for key in fields if key in payload}


def _read_state(path: Path) -> dict[str, Any]:
    return _read_fields(path, _STATE_FIELDS, "Site Audit deployment state")


def _read_pending(path: Path) -> dict[str, Any]:
    return _read_fields(path, _PENDING_FIELDS, "Site Audit pending deployment state")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--image-namespace", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--timeout", required=True, type=int)
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()

    script = Path(args.script)
    source_root = Path(args.source_root)
    env_file = Path(args.env_file)
    state_file = Path(args.state_file)
    pending_file = Path(str(state_file) + ".pending")
    timeout = max(1, min(args.timeout, 900))

    if not script.is_absolute() or not _safe_regular_file(script):
        _emit(exit_code=125, output="", error="deploy script is unavailable or unsafe")
        return 125
    if not source_root.is_absolute() or not source_root.is_dir() or source_root.is_symlink():
        _emit(exit_code=125, output="", error="source root is unavailable or unsafe")
        return 125
    try:
        script.resolve(strict=True).relative_to(source_root.resolve(strict=True))
    except (OSError, ValueError):
        _emit(exit_code=125, output="", error="deploy script escapes source root")
        return 125
    if not env_file.is_absolute() or not _safe_regular_file(env_file):
        _emit(exit_code=125, output="", error="Site Audit env file is unavailable or unsafe")
        return 125
    if not state_file.is_absolute() or not state_file.parent.is_dir():
        _emit(exit_code=125, output="", error="Site Audit deploy-state parent is unavailable")
        return 125
    try:
        state_file.resolve(strict=False).relative_to(source_root.resolve(strict=True))
    except (OSError, ValueError):
        pass
    else:
        _emit(exit_code=125, output="", error="Site Audit deploy-state file must be outside source root")
        return 125

    for command in ("bash", "docker", "flock", "git", "python3"):
        if shutil.which(command) is None:
            _emit(exit_code=125, output="", error=f"required helper command missing: {command}")
            return 125

    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SITE_AUDIT_"):
            env.pop(key, None)
    env.update(
        {
            "SITE_AUDIT_SOURCE_REVISION": args.source_revision,
            "SITE_AUDIT_IMAGE_NAMESPACE": args.image_namespace,
            "SITE_AUDIT_DEPLOY_STATE_FILE": str(state_file),
            "SITE_AUDIT_ENV_FILE": str(env_file),
            "HOME": "/tmp",
        }
    )
    mode = "recover" if args.recover else "deploy"
    pending_before: dict[str, Any] = {}
    if args.recover:
        try:
            pending_before = _read_pending(pending_file)
        except RuntimeError as exc:
            _emit(
                exit_code=125,
                output="",
                pending_exists=pending_file.exists(),
                mode=mode,
                error=str(exc),
            )
            return 125

    argv = ["/bin/bash", str(script)]
    if args.recover:
        argv.append("--recover")

    try:
        completed = subprocess.run(
            argv,
            cwd=source_root,
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
        _emit(
            exit_code=124,
            output=output,
            pending_before=pending_before,
            pending_exists=pending_file.exists(),
            mode=mode,
            error="Site Audit deploy contract timed out",
        )
        return 124
    except OSError:
        _emit(
            exit_code=125,
            output="",
            pending_before=pending_before,
            pending_exists=pending_file.exists(),
            mode=mode,
            error="Site Audit deploy contract could not start",
        )
        return 125

    state: dict[str, Any] = {}
    error: str | None = None
    if completed.returncode == 0:
        try:
            if state_file.exists():
                state = _read_state(state_file)
            elif not args.recover:
                raise RuntimeError("Site Audit deployment state file is unavailable or unsafe")
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
        pending_before=pending_before,
        pending_exists=pending_file.exists(),
        mode=mode,
        error=error,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
