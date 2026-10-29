#!/usr/bin/env python3
"""Fixed helper for the governed XLOUD two-role production rollout.

The MCP adapter binds all public inputs before launching this helper.  The
Site Audit control credential is read only here from the operator-owned env
file and is passed to deploy-xloud.sh through the child environment; plaintext
never appears in Docker argv, helper output, confirmation metadata or deploy
state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_STATE_KEYS = frozenset(
    {
        "STATE_VERSION",
        "SOURCE_REVISION",
        "SITE_IMAGE",
        "INTAKE_IMAGE",
        "SITE_IMAGE_ID",
        "INTAKE_IMAGE_ID",
        "SITE_CONTAINER_ID",
        "INTAKE_CONTAINER_ID",
        "SITE_IPV4",
        "STABILITY_SECONDS",
        "PREVIOUS_SITE_CID",
        "PREVIOUS_INTAKE_CID",
        "PREVIOUS_SITE_REF",
        "PREVIOUS_INTAKE_REF",
        "PREVIOUS_SITE_IMAGE_ID",
        "PREVIOUS_INTAKE_IMAGE_ID",
    }
)


def _regular_file(path: Path, *, label: str) -> Path:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


def _under(root: Path, path: Path, *, label: str) -> Path:
    root = root.resolve(strict=True)
    path = _regular_file(path, label=label)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under its approved root") from exc
    return path


def _read_env_value(path: Path, key: str) -> str:
    if path.stat().st_size > 256 * 1024:
        raise ValueError("operator env file is unexpectedly large")
    found: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        lhs, rhs = stripped.split("=", 1)
        if lhs.strip() != key:
            continue
        value = rhs.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if found is not None:
            raise ValueError(f"duplicate {key} entry in operator env")
        found = value
    if found is None or not found:
        raise ValueError(f"{key} is missing from operator env")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in found):
        raise ValueError(f"{key} contains a control character")
    return found


def _fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    path = _regular_file(path, label="deploy state")
    if path.stat().st_size > 64 * 1024:
        raise ValueError("deploy state is unexpectedly large")
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        if key not in SAFE_STATE_KEYS:
            continue
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise ValueError("deploy state contains a control character")
        result[key] = value
    return result


def _tail(text: str, secret: str, *, lines: int = 80) -> list[str]:
    redacted = text.replace(secret, "<redacted>") if secret else text
    return redacted.splitlines()[-lines:]


def _run(args: argparse.Namespace) -> dict[str, object]:
    source_root = Path(args.source_root).resolve(strict=True)
    operator_root = Path(args.operator_root).resolve(strict=True)
    script = _under(source_root, Path(args.script), label="deploy script")
    env_file = _under(operator_root, Path(args.env_file), label="operator env file")
    state_dir = Path(args.state_dir).resolve(strict=True)
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise ValueError("deploy state directory must be a real directory")

    if not SHA_RE.fullmatch(args.source_revision):
        raise ValueError("source revision must be exact lowercase 40-hex")
    if not IMAGE_ID_RE.fullmatch(args.site_image_id) or not IMAGE_ID_RE.fullmatch(
        args.intake_image_id
    ):
        raise ValueError("image IDs must be exact sha256 IDs")
    if args.site_image_id == args.intake_image_id:
        raise ValueError("site and intake image IDs must differ")
    if not FINGERPRINT_RE.fullmatch(args.secret_fingerprint):
        raise ValueError("secret fingerprint is invalid")
    if args.mode not in {"inspect", "preflight", "deploy", "recover"}:
        raise ValueError("mode is invalid")

    secret = _read_env_value(env_file, "AUDIT_CONTROL_API_KEY")
    if _fingerprint(secret) != args.secret_fingerprint:
        raise ValueError("operator audit credential changed after confirmation")

    child_env = dict(os.environ)
    child_env.update(
        {
            "XLOUD_SOURCE_REVISION": args.source_revision,
            "XLOUD_SITE_IMAGE": args.site_image,
            "XLOUD_INTAKE_IMAGE": args.intake_image,
            "XLOUD_EXPECTED_SITE_IMAGE_ID": args.site_image_id,
            "XLOUD_EXPECTED_INTAKE_IMAGE_ID": args.intake_image_id,
            "XLOUD_DEPLOY_STATE_DIR": str(state_dir),
            "XLOUD_STABILITY_SECONDS": str(args.stability_seconds),
            "XLOUD_AUDIT_CONTROL_API_KEY": secret,
        }
    )
    # Inspect is read-only: no shell script, Docker or Compose invocation.
    if args.mode == "inspect":
        return {
            "version": 1,
            "mode": "inspect",
            "exit_code": 0,
            "timed_out": False,
            "pending_exists": (state_dir / "pending.state").exists(),
            "lkg_exists": (state_dir / "lkg.state").exists(),
            "failed_exists": (state_dir / "last-failed.state").exists(),
            "pending": _parse_state(state_dir / "pending.state"),
            "lkg": {},
            "failed": {},
            "stdout_tail": [],
            "stderr_tail": [],
        }
    timeout = max(30, min(int(args.timeout), 900))
    timed_out = False
    try:
        completed = subprocess.run(
            ["/bin/sh", str(script), args.mode],
            cwd=str(source_root),
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code = int(completed.returncode)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")

    payload: dict[str, object] = {
        "version": 1,
        "mode": args.mode,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "pending_exists": (state_dir / "pending.state").exists(),
        "lkg_exists": (state_dir / "lkg.state").exists(),
        "failed_exists": (state_dir / "last-failed.state").exists(),
        "lkg": _parse_state(state_dir / "lkg.state"),
        "pending": _parse_state(state_dir / "pending.state"),
        "failed": _parse_state(state_dir / "last-failed.state"),
        "stdout_tail": _tail(stdout, secret),
        "stderr_tail": _tail(stderr, secret),
    }
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--operator-root", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--site-image", required=True)
    parser.add_argument("--intake-image", required=True)
    parser.add_argument("--site-image-id", required=True)
    parser.add_argument("--intake-image-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--secret-fingerprint", required=True)
    parser.add_argument("--stability-seconds", type=int, default=90)
    parser.add_argument("--timeout", type=int, default=660)
    return parser


def main() -> int:
    try:
        payload = _run(_parser().parse_args())
    except Exception as exc:  # noqa: BLE001 - fixed helper boundary
        print(json.dumps({"version": 1, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
