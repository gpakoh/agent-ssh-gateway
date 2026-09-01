"""Ephemeral verifier execution for trusted task-candidate delivery.

Required checks execute candidate-controlled code, so process isolation must
not be shared across verifications.  Every verification therefore runs in its
own disposable Docker container.  Candidate storage is mounted read-only; the
container receives no Docker socket, agent runtime, authoritative workspace,
OAuth data, registry data, or project-internal Docker networks.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any


class CandidateVerificationError(RuntimeError):
    """An isolated candidate verification failure safe to expose as denial."""


_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CONTAINER_NAME = "mcp-candidate-verifier"
_CONTAINER_SOURCE = "/candidate-src"


def _q(value: str) -> str:
    return shlex.quote(value)


def build_candidate_verifier_script(
    *, staging_root: Path, expected_sha: str, required_checks: list[str]
) -> str:
    """Build a fail-closed verifier script that never writes candidate storage."""
    # The host-side staging path is deliberately not embedded in the script.
    # Docker mounts only that exact volume subpath at this fixed location, so
    # candidate-controlled checks cannot traverse sibling task receipts or
    # staging repositories in the candidate store.
    source = _CONTAINER_SOURCE
    lines = [
        "set -u",
        f"SOURCE={_q(source)}",
        f"EXPECTED={_q(expected_sha)}",
        'VERIFY_ROOT=$(mktemp -d /tmp/mcp-candidate-verify.XXXXXX) || exit 80',
        'cleanup() { chmod -R u+w "$VERIFY_ROOT" 2>/dev/null || true; rm -rf "$VERIFY_ROOT"; }',
        "trap cleanup EXIT",
        "trap 'exit 85' HUP INT TERM",
        'export HOME="$VERIFY_ROOT/home"',
        'export XDG_CACHE_HOME="$VERIFY_ROOT/cache"',
        'export XDG_DATA_HOME="$VERIFY_ROOT/data"',
        'mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" || exit 80',
        'git clone --no-hardlinks --no-checkout "$SOURCE" "$VERIFY_ROOT/repo" >/dev/null 2>&1 || exit 81',
        'git -C "$VERIFY_ROOT/repo" checkout --detach --quiet "$EXPECTED" >/dev/null 2>&1 || exit 81',
        'ACTUAL=$(git -C "$VERIFY_ROOT/repo" rev-parse HEAD 2>/dev/null || true)',
        '[ "$ACTUAL" = "$EXPECTED" ] || exit 81',
        f"CHECKS_PRESENT={'1' if required_checks else '0'}",
        'if [ "$CHECKS_PRESENT" = "1" ] && [ -f "$VERIFY_ROOT/repo/uv.lock" ] && [ -f "$VERIFY_ROOT/repo/pyproject.toml" ]; then',
        "  DEV_EXTRA=$(cd \"$VERIFY_ROOT/repo\" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV python3 -c 'import tomllib; data=tomllib.load(open(\"pyproject.toml\", \"rb\")); print(\"1\" if \"dev\" in data.get(\"project\", {}).get(\"optional-dependencies\", {}) else \"0\")' 2>/dev/null) || exit 82",
        '  if [ "$DEV_EXTRA" = "1" ]; then',
        '    (cd "$VERIFY_ROOT/repo" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV uv sync --frozen --extra dev) >/dev/null 2>&1 || exit 82',
        "  fi",
        "fi",
    ]
    for check in required_checks:
        lines.extend(
            [
                f"CHECK={_q(check)}",
                '(cd "$VERIFY_ROOT/repo" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV sh -c "$CHECK") >/dev/null 2>&1 || exit 83',
            ]
        )
    lines.append("exit 0")
    return "\n".join(lines)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CandidateVerificationError(f"{name} is required for isolated verification")
    return value


def _verification_timeout() -> int:
    raw = os.environ.get("MCP_CANDIDATE_VERIFY_TIMEOUT_SECONDS", "1800").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise CandidateVerificationError("invalid candidate verifier timeout") from exc
    if value < 1 or value > 7200:
        raise CandidateVerificationError("candidate verifier timeout is out of bounds")
    return value


def _validated_volume_name() -> str:
    value = _required_env("MCP_TASK_CANDIDATE_VOLUME_NAME")
    if not _VOLUME_RE.fullmatch(value):
        raise CandidateVerificationError("invalid candidate verifier volume name")
    return value


def _validated_image() -> str:
    value = _required_env("MCP_VERIFIER_IMAGE")
    if (
        len(value) > 512
        or value.startswith("-")
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
    ):
        raise CandidateVerificationError("invalid candidate verifier image reference")
    return value


def _validated_staging_root(staging_root: Path) -> Path:
    candidate_root_raw = _required_env("MCP_TASK_CANDIDATE_ROOT")
    candidate_root = Path(candidate_root_raw)
    if not candidate_root.is_absolute() or candidate_root == Path("/"):
        raise CandidateVerificationError("invalid candidate verifier root")
    candidate_root = candidate_root.resolve()
    staging = staging_root.resolve()
    try:
        staging.relative_to(candidate_root)
    except ValueError as exc:
        raise CandidateVerificationError("candidate verifier source escapes candidate root") from exc
    if not staging.is_dir():
        raise CandidateVerificationError("candidate verifier source is unavailable")
    return staging


def _validated_volume_subpath(staging_root: Path) -> str:
    candidate_root = Path(_required_env("MCP_TASK_CANDIDATE_ROOT")).resolve()
    staging = _validated_staging_root(staging_root)
    relative = staging.relative_to(candidate_root)
    if relative == Path(".") or not relative.parts:
        raise CandidateVerificationError("candidate verifier must mount a task-scoped subpath")
    for part in relative.parts:
        if (
            part in {"", ".", ".."}
            or "," in part
            or any(ord(ch) < 32 for ch in part)
        ):
            raise CandidateVerificationError("invalid candidate verifier volume subpath")
    return relative.as_posix()


def build_ephemeral_verifier_argv(
    *, staging_root: Path, volume_name: str, image: str, timeout_seconds: int
) -> list[str]:
    """Build the fixed-security docker argv for one disposable verifier."""
    volume_subpath = _validated_volume_subpath(staging_root)
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        _CONTAINER_NAME,
        "--init",
        "--network",
        "bridge",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        "16g",
        "--cpus",
        "2.0",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,exec,size=8g",
        "--mount",
        f"type=volume,src={volume_name},dst={_CONTAINER_SOURCE},volume-subpath={volume_subpath},readonly",
        "--user",
        "mcpuser",
        "--entrypoint",
        "/usr/bin/timeout",
        image,
        "-s",
        "KILL",
        str(timeout_seconds),
        "/bin/sh",
        "-s",
    ]


def _run(
    runner: Callable[..., Any],
    argv: list[str],
    *,
    input_text: str | None = None,
    timeout: int,
) -> Any:
    return runner(
        argv,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _force_remove(runner: Callable[..., Any]) -> None:
    try:
        _run(runner, ["docker", "rm", "-f", _CONTAINER_NAME], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def verify_candidate_via_docker(
    *,
    staging_root: Path,
    expected_sha: str,
    required_checks: list[str],
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Verify one candidate in a fresh container and destroy all descendants."""
    staging = _validated_staging_root(staging_root)
    volume_name = _validated_volume_name()
    image = _validated_image()
    timeout = _verification_timeout()
    script = build_candidate_verifier_script(
        staging_root=staging,
        expected_sha=expected_sha,
        required_checks=required_checks,
    )
    argv = build_ephemeral_verifier_argv(
        staging_root=staging,
        volume_name=volume_name,
        image=image,
        timeout_seconds=timeout,
    )

    # Materialization is serialized by the candidate-store flock.  A fixed
    # name therefore doubles as crash recovery: any container left by a dead
    # control-plane process is stale and is removed before the next run.
    _force_remove(runner)
    try:
        result = _run(runner, argv, input_text=script, timeout=timeout + 60)
    except subprocess.TimeoutExpired as exc:
        raise CandidateVerificationError("isolated candidate verification timed out") from exc
    except OSError as exc:
        raise CandidateVerificationError("isolated candidate verifier is unavailable") from exc
    finally:
        # `--rm` handles the normal path; force removal covers timeout, client
        # transport failure, or a verifier whose descendants kept PID 1 alive.
        _force_remove(runner)

    exit_code = getattr(result, "returncode", None)
    if exit_code != 0:
        raise CandidateVerificationError(
            f"isolated candidate verification failed with exit code {exit_code}"
        )
