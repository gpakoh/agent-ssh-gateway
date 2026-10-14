#!/usr/bin/env python3
"""Constrained helper for the guarded Telegram Storage deployment contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

_MAX_OUTPUT_CHARS = 32 * 1024
_STATE_FIELDS = ("telegram_storage_service_image", "deployed_at")


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


def _read_state(path: Path) -> dict[str, Any]:
    if not _safe_regular_file(path):
        raise RuntimeError("Telegram Storage deployment state is unavailable or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Telegram Storage deployment state is unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Telegram Storage deployment state is not an object")
    return {key: payload.get(key) for key in _STATE_FIELDS if key in payload}


def _bootstrap_state(path: Path, *, image: str, deployed_at: str) -> None:
    if os.path.lexists(path):
        raise RuntimeError("Telegram Storage deployment state appeared before bootstrap")
    payload = {
        "telegram_storage_service_image": image,
        "deployed_at": deployed_at,
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temp = path.parent / f".{path.name}.bootstrap-{os.getpid()}"
    fd: int | None = None
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except FileExistsError as exc:
        raise RuntimeError("Telegram Storage deployment state appeared before bootstrap") from exc
    except OSError as exc:
        raise RuntimeError("Telegram Storage rollback state bootstrap failed") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--target-image", required=True)
    parser.add_argument("--bootstrap-image")
    parser.add_argument("--bootstrap-deployed-at")
    parser.add_argument("--timeout", required=True, type=int)
    args = parser.parse_args()

    script = Path(args.script)
    source_root = Path(args.source_root)
    env_file = Path(args.env_file)
    state_dir = Path(args.state_dir)
    state_file = state_dir / "telegram-storage-service-deploy.json"
    timeout = max(1, min(args.timeout, 600))
    bootstrap_image = args.bootstrap_image
    bootstrap_deployed_at = args.bootstrap_deployed_at

    if (bootstrap_image is None) != (bootstrap_deployed_at is None):
        _emit(exit_code=125, output="", error="rollback bootstrap requires image and deployed_at together")
        return 125
    if bootstrap_image is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", bootstrap_image):
            _emit(exit_code=125, output="", error="rollback bootstrap image is invalid")
            return 125
        if (
            not isinstance(bootstrap_deployed_at, str)
            or not bootstrap_deployed_at.strip()
            or any(ch in bootstrap_deployed_at for ch in ("\x00", "\n", "\r"))
        ):
            _emit(exit_code=125, output="", error="rollback bootstrap deployed_at is invalid")
            return 125

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
        _emit(exit_code=125, output="", error="Telegram Storage operator env file is unavailable or unsafe")
        return 125
    if not state_dir.is_absolute() or not state_dir.is_dir() or state_dir.is_symlink():
        _emit(exit_code=125, output="", error="Telegram Storage deploy-state directory is unavailable")
        return 125
    try:
        state_dir.resolve(strict=True).relative_to(source_root.resolve(strict=True))
    except (OSError, ValueError):
        pass
    else:
        _emit(exit_code=125, output="", error="Telegram Storage deploy-state directory must be outside source root")
        return 125
    if not args.target_image.isascii() or not args.target_image:
        _emit(exit_code=125, output="", error="target image is invalid")
        return 125

    for command in ("bash", "docker", "python3"):
        if shutil.which(command) is None:
            _emit(exit_code=125, output="", error=f"required helper command missing: {command}")
            return 125

    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("TELEGRAM_STORAGE_", "TELEGRAM_BOT_")):
            env.pop(key, None)
    env.update(
        {
            "TELEGRAM_STORAGE_SERVICE_TARGET_IMAGE": args.target_image,
            "TELEGRAM_STORAGE_OPERATOR_ENV_FILE": str(env_file),
            "TELEGRAM_STORAGE_DEPLOY_STATE_DIR": str(state_dir),
            "HOME": "/tmp",
        }
    )

    if bootstrap_image is not None and bootstrap_deployed_at is not None:
        try:
            _bootstrap_state(
                state_file,
                image=bootstrap_image,
                deployed_at=bootstrap_deployed_at,
            )
        except RuntimeError as exc:
            _emit(exit_code=125, output="", error=str(exc))
            return 125

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
        _emit(exit_code=124, output=output, error="Telegram Storage deploy contract timed out")
        return 124
    except OSError:
        _emit(exit_code=125, output="", error="Telegram Storage deploy contract could not start")
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
