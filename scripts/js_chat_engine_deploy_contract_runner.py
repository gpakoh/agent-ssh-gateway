#!/usr/bin/env python3
"""Run the repo-owned JS Chat Engine deploy script with fixed operator surfaces."""

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
_STATE_FIELDS = ("image", "git_sha", "deployed_at")
_STRIP_ENV = (
    "PROD_INFRA_ROOT",
    "PROD_ENV_FILE",
    "PROD_STATE_DIR",
    "QUART_CORE_DATABASES_ROOT",
    "JS_CHAT_ENGINE_RELEASE_ENV_FILE",
    "JS_CHAT_ENGINE_IMAGE_REPOSITORY",
    "JS_CHAT_ENGINE_IMAGE_DIGEST",
    "JS_CHAT_ENGINE_RELEASE_GIT_SHA",
    "JS_CHAT_ENGINE_APPS_JSON",
    "JS_CHAT_ENGINE_REQUIRE_LEAD_NOTIFICATION",
)


def _emit(
    *,
    exit_code: int,
    output: str,
    state: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "version": 1,
        "exit_code": int(exit_code),
        "output_tail": output[-_MAX_OUTPUT_CHARS:],
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


def _safe_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _read_state(path: Path) -> dict[str, Any]:
    if not _safe_regular_file(path):
        raise RuntimeError("JS Chat Engine deployment state is unavailable or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("JS Chat Engine deployment state is unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("JS Chat Engine deployment state is not an object")
    return {key: payload.get(key) for key in _STATE_FIELDS if key in payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--operator-root", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--core-databases-root", required=True)
    parser.add_argument("--target-image", required=True)
    parser.add_argument("--release-revision", required=True)
    parser.add_argument("--timeout", required=True, type=int)
    args = parser.parse_args()

    script = Path(args.script)
    source_root = Path(args.source_root)
    operator_root = Path(args.operator_root)
    env_file = Path(args.env_file)
    state_dir = Path(args.state_dir)
    core_databases_root = Path(args.core_databases_root)
    release_env = source_root / "deploy" / "js-chat-engine.release.env"
    state_file = state_dir / "js-chat-engine-deploy.json"
    timeout = max(60, min(args.timeout, 600))

    if not script.is_absolute() or not _safe_regular_file(script):
        _emit(exit_code=125, output="", error="deploy script is unavailable or unsafe")
        return 125
    if not source_root.is_absolute() or not _safe_directory(source_root):
        _emit(exit_code=125, output="", error="source root is unavailable or unsafe")
        return 125
    if not operator_root.is_absolute() or not _safe_directory(operator_root):
        _emit(exit_code=125, output="", error="operator root is unavailable or unsafe")
        return 125
    if not _safe_regular_file(env_file) or not _safe_regular_file(release_env):
        _emit(exit_code=125, output="", error="operator/release env file is unavailable or unsafe")
        return 125
    if not _safe_directory(state_dir) or not _safe_directory(core_databases_root):
        _emit(exit_code=125, output="", error="state/Core databases root is unavailable or unsafe")
        return 125
    try:
        script.resolve(strict=True).relative_to(source_root.resolve(strict=True))
        release_env.resolve(strict=True).relative_to(source_root.resolve(strict=True))
        env_file.resolve(strict=True).relative_to(operator_root.resolve(strict=True))
        state_dir.resolve(strict=True).relative_to(operator_root.resolve(strict=True))
    except (OSError, ValueError):
        _emit(exit_code=125, output="", error="deploy path escaped its bound root")
        return 125

    for command in ("bash", "curl", "docker", "flock", "python3", "timeout"):
        if shutil.which(command) is None:
            _emit(exit_code=125, output="", error=f"required helper command missing: {command}")
            return 125

    env = os.environ.copy()
    for key in _STRIP_ENV:
        env.pop(key, None)
    env.update(
        {
            "PROD_INFRA_ROOT": str(operator_root),
            "PROD_ENV_FILE": str(env_file),
            "PROD_STATE_DIR": str(state_dir),
            "QUART_CORE_DATABASES_ROOT": str(core_databases_root),
            "JS_CHAT_ENGINE_RELEASE_ENV_FILE": str(release_env),
            "HOME": "/tmp",
        }
    )

    try:
        completed = subprocess.run(
            ["/bin/bash", str(script)],
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
        _emit(exit_code=124, output=output, error="JS Chat Engine deploy contract timed out")
        return 124
    except OSError:
        _emit(exit_code=125, output="", error="JS Chat Engine deploy contract could not start")
        return 125

    state: dict[str, Any] = {}
    error: str | None = None
    if completed.returncode == 0:
        try:
            state = _read_state(state_file)
            if state.get("image") != args.target_image:
                raise RuntimeError("JS Chat Engine deployment state image differs from target")
            if state.get("git_sha") != args.release_revision:
                raise RuntimeError("JS Chat Engine deployment state revision differs from target")
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
