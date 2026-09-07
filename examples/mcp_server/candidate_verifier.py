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
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tool_results import _redact_error_message


class CandidateVerificationError(RuntimeError):
    """An isolated candidate verification failure safe to expose as denial.

    The failure carries a stable machine ``code``, a ``phase`` label, a
    ``retryable`` flag and sanitized, bounded ``details`` so a calling tool
    can map it into a structured MCP error without casting to a generic
    ``CHECK_FAILED`` and without surfacing raw verifier output.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "CANDIDATE_VERIFICATION_FAILED",
        phase: str = "push_preflight",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.retryable = bool(retryable)
        self.details = dict(details) if details else {}


_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CONTAINER_NAME = "mcp-candidate-verifier"
_CONTAINER_SOURCE = "/candidate-src"

# Cap on the failed-check output tail a verifier returns.  The verifier only
# forwards a fixed number of bytes (see the script); this slices it further
# and redacts it before anything reaches a tool response.
_OUTPUT_TAIL_BYTES = 7000
_DETAIL_OUTPUT_CHARS = 4000
_MCP_VERIFY_EXIT_RE = re.compile(r"^MCP_VERIFY_EXIT=(\d+)$", re.MULTILINE)
_MCP_VERIFY_CHECK_RE = re.compile(r"^MCP_VERIFY_CHECK=(\d+)$", re.MULTILINE)

# Candidate-controlled output may try to echo credentials; scrub common
# credential shapes in addition to the shared path/endpoint redaction.
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)\b(?:authorization\s*[:=]\s*basic\s+"
    r"|(?:[a-z0-9_]*)(?:token|secret|password|passwd|api[_-]?key)\b\s*(?::=|=|:)\s*)"
    r"[^\s'\"{}]+"
)


def _q(value: str) -> str:
    return shlex.quote(value)


def sanitize_verifier_output_tail(text: str) -> str:
    """Return a bounded, redacted tail of candidate-verifier output.

    The output is candidate-controlled, so it is never echoed verbatim.  It is
    capped in bytes at the source and again in characters here, and passed
    through the shared gateway redaction policy (internal paths, API endpoints)
    plus credential-shape scrubbing before it can be surfaced.
    """
    raw = str(text or "")
    if not raw:
        return ""
    redacted, _was_redacted = _redact_error_message(raw)
    redacted = _CREDENTIAL_VALUE_RE.sub("[REDACTED]", redacted)
    lines = [line for line in redacted.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines)[-_DETAIL_OUTPUT_CHARS:]


def _structured_verifier_failure(
    exit_code: int,
    checks: list[str],
    stdout: str,
    stderr: str,
) -> CandidateVerificationError:
    """Build a structured CandidateVerificationError from a nonzero verifier exit.

    Only the failed-check command (operator-supplied via ``required_checks``),
    the phase and a bounded, redacted output tail are surfaced -- never the raw
    remote or any unredacted verifier output.
    """
    details: dict[str, Any] = {
        "phase": "required_checks",
        "exit_code": int(exit_code),
        "mutation_occurred": False,
    }
    check_match = _MCP_VERIFY_CHECK_RE.search(str(stdout or ""))
    if check_match:
        try:
            idx = int(check_match.group(1))
            if 0 <= idx < len(checks):
                details["check_index"] = idx
                details["failed_check"] = checks[idx]
        except (TypeError, ValueError):
            pass
    exit_match = _MCP_VERIFY_EXIT_RE.search(str(stdout or ""))
    if exit_match:
        try:
            details["check_exit_code"] = int(exit_match.group(1))
        except (TypeError, ValueError):
            pass
    tail = sanitize_verifier_output_tail(f"{stdout or ''}\n{stderr or ''}")
    if tail:
        details["output_tail"] = tail

    if exit_code == 83:
        details["phase"] = "candidate_check"
        return CandidateVerificationError(
            f"a required verification check failed with exit code {exit_code}",
            code="CANDIDATE_CHECK_FAILED",
            phase="candidate_check",
            retryable=False,
            details=details,
        )
    if exit_code == 81:
        details["phase"] = "candidate_checkout"
        return CandidateVerificationError(
            "candidate could not be checked out at the expected head "
            f"(exit code {exit_code})",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="candidate_checkout",
            retryable=False,
            details=details,
        )
    if exit_code == 82:
        details["phase"] = "verifier_env"
        return CandidateVerificationError(
            f"candidate dependency bootstrap failed before required checks "
            f"(exit code {exit_code})",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
            retryable=True,
            details=details,
        )
    if exit_code == 80:
        details["phase"] = "verifier_bootstrap"
        return CandidateVerificationError(
            f"candidate verifier could not materialize its disposable environment "
            f"(exit code {exit_code})",
            code="VERIFIER_BOOTSTRAP_FAILED",
            phase="verifier_bootstrap",
            retryable=True,
            details=details,
        )
    details["phase"] = "verifier_bootstrap"
    return CandidateVerificationError(
        f"isolated candidate verification failed with exit code {exit_code}",
        code="VERIFIER_BOOTSTRAP_FAILED",
        phase="verifier_bootstrap",
        retryable=True,
        details=details,
    )


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
    for index, check in enumerate(required_checks):
        lines.extend(
            [
                f"CHECK={_q(check)}",
                '(cd "$VERIFY_ROOT/repo" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV sh -c "$CHECK") '
                f'>"$VERIFY_ROOT/check.out" 2>"$VERIFY_ROOT/check.err" || {{ '
                'rc=$?; '
                f'printf "MCP_VERIFY_EXIT=%s\\nMCP_VERIFY_CHECK=%s\\n" "$rc" "{index}"; '
                'tail -c 7000 "$VERIFY_ROOT/check.err" 2>/dev/null; '
                'tail -c 7000 "$VERIFY_ROOT/check.out" 2>/dev/null; '
                'rm -f "$VERIFY_ROOT/check.out" "$VERIFY_ROOT/check.err"; '
                "exit 83; }",
            ]
        )
    lines.append("exit 0")
    return "\n".join(lines)


def _cfg_failure(
    message: str,
    *,
    code: str = "CANDIDATE_VERIFICATION_FAILED",
    phase: str = "push_preflight",
    retryable: bool = False,
) -> CandidateVerificationError:
    """A verifier preflight (config/boundary) denial, not retryable as-is."""
    return CandidateVerificationError(
        message,
        code=code,
        phase=phase,
        retryable=retryable,
        details={"phase": phase, "mutation_occurred": False},
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise _cfg_failure(
            f"{name} is required for isolated verification",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
            retryable=False,
        )
    return value


def _verification_timeout() -> int:
    raw = os.environ.get("MCP_CANDIDATE_VERIFY_TIMEOUT_SECONDS", "1800").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise _cfg_failure(
            "invalid candidate verifier timeout",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        ) from exc
    if value < 1 or value > 7200:
        raise _cfg_failure(
            "candidate verifier timeout is out of bounds",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        )
    return value


def _validated_volume_name() -> str:
    value = _required_env("MCP_TASK_CANDIDATE_VOLUME_NAME")
    if not _VOLUME_RE.fullmatch(value):
        raise _cfg_failure(
            "invalid candidate verifier volume name",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        )
    return value


def _validated_image() -> str:
    value = _required_env("MCP_VERIFIER_IMAGE")
    if (
        len(value) > 512
        or value.startswith("-")
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
    ):
        raise _cfg_failure(
            "invalid candidate verifier image reference",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        )
    return value


def _candidate_store_root() -> Path:
    candidate_root_raw = _required_env("MCP_TASK_CANDIDATE_ROOT")
    candidate_root = Path(candidate_root_raw)
    if not candidate_root.is_absolute() or candidate_root == Path("/"):
        raise _cfg_failure(
            "invalid candidate verifier root",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        )
    return candidate_root.resolve()


def _validated_staging_root(staging_root: Path) -> Path:
    candidate_root = _candidate_store_root()
    staging = staging_root.resolve()
    try:
        staging.relative_to(candidate_root)
    except ValueError as exc:
        raise _cfg_failure(
            "candidate verifier source escapes candidate root",
            code="CANDIDATE_VOLUME_SUBPATH_INVALID",
            phase="source_resolution",
        ) from exc
    if not staging.is_dir():
        raise _cfg_failure(
            "candidate verifier source is unavailable",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="source_resolution",
        )
    return staging


def _validated_volume_subpath(staging_root: Path) -> str:
    candidate_root = _candidate_store_root()
    staging = _validated_staging_root(staging_root)
    relative = staging.relative_to(candidate_root)
    if relative == Path(".") or not relative.parts:
        raise _cfg_failure(
            "candidate verifier must mount a task-scoped subpath",
            code="CANDIDATE_VOLUME_SUBPATH_INVALID",
            phase="source_resolution",
        )
    for part in relative.parts:
        if (
            part in {"", ".", ".."}
            or "," in part
            or any(ord(ch) < 32 for ch in part)
        ):
            raise _cfg_failure(
                "invalid candidate verifier volume subpath",
                code="CANDIDATE_VOLUME_SUBPATH_INVALID",
                phase="source_resolution",
            )
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
        raise CandidateVerificationError(
            "isolated candidate verification timed out",
            code="VERIFIER_BOOTSTRAP_FAILED",
            phase="verifier_bootstrap",
            retryable=True,
            details={
                "phase": "verifier_bootstrap",
                "mutation_occurred": False,
                "exit_code": 124,
            },
        ) from exc
    except OSError as exc:
        raise CandidateVerificationError(
            "isolated candidate verifier is unavailable",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
            retryable=True,
            details={
                "phase": "verifier_env",
                "mutation_occurred": False,
            },
        ) from exc
    finally:
        # `--rm` handles the normal path; force removal covers timeout, client
        # transport failure, or a verifier whose descendants kept PID 1 alive.
        _force_remove(runner)

    returncode = getattr(result, "returncode", None)
    exit_code = int(returncode) if returncode is not None else 1
    if exit_code != 0:
        raise _structured_verifier_failure(
            exit_code,
            checks=required_checks,
            stdout=getattr(result, "stdout", "") or "",
            stderr=getattr(result, "stderr", "") or "",
        )


def _is_under_candidate_store(path: Path) -> bool:
    candidate_root = _candidate_store_root()
    try:
        path.resolve().relative_to(candidate_root)
    except ValueError:
        return False
    return True


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_materialize_git(argv: list[str], *, cwd: Path, home: Path, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=_git_env(home),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateVerificationError(
            "candidate verifier could not materialize registered workspace source",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="source_resolution",
            retryable=True,
            details={"phase": "source_resolution", "mutation_occurred": False},
        ) from exc
    if result.returncode != 0:
        tail = sanitize_verifier_output_tail(f"{result.stdout or ''}\n{result.stderr or ''}")
        details: dict[str, Any] = {
            "phase": "source_resolution",
            "mutation_occurred": False,
            "exit_code": result.returncode,
        }
        if tail:
            details["output_tail"] = tail
        raise CandidateVerificationError(
            "candidate verifier could not materialize registered workspace source",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="source_resolution",
            retryable=True,
            details=details,
        )
    return result.stdout.strip()


def _materialize_workspace_source(workspace_root: Path, expected_sha: str) -> Path:
    """Copy one verified delivery workspace into the candidate volume for Docker.

    Docker verification deliberately mounts only subpaths of
    ``MCP_TASK_CANDIDATE_ROOT``. Prepared delivery workspaces can live in the
    workspace registry instead, so materialize an exact, no-hardlinks Git clone
    under the candidate volume and verify that disposable source. The caller is
    responsible for removing the returned path.
    """
    workspace = workspace_root.resolve()
    if not workspace.is_dir() or not (workspace / ".git").exists():
        raise _cfg_failure(
            "registered delivery workspace must be a Git worktree",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="source_resolution",
        )
    candidate_root = _candidate_store_root()
    materialized_root = candidate_root / "verified-workspaces"
    try:
        materialized_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f"verified-workspace-{expected_sha[:12]}-",
                dir=str(materialized_root),
            )
        )
    except OSError as exc:
        raise _cfg_failure(
            "candidate verifier staging root is unavailable",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
            retryable=True,
        ) from exc

    try:
        _run_materialize_git(
            [
                "git",
                "-c",
                f"safe.directory={workspace}",
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                str(workspace),
                str(staging),
            ],
            cwd=materialized_root,
            home=staging,
        )
        _run_materialize_git(
            ["git", "-C", str(staging), "checkout", "--detach", "--quiet", expected_sha],
            cwd=staging,
            home=staging,
        )
        actual = _run_materialize_git(
            ["git", "-C", str(staging), "rev-parse", "HEAD"],
            cwd=staging,
            home=staging,
        ).strip().lower()
        if actual != expected_sha:
            raise CandidateVerificationError(
                "candidate verifier materialized the wrong registered workspace commit",
                code="CANDIDATE_SOURCE_UNAVAILABLE",
                phase="source_resolution",
                retryable=False,
                details={"phase": "source_resolution", "mutation_occurred": False},
            )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def verify_workspace_via_docker(
    *,
    workspace_root: Path,
    expected_sha: str,
    required_checks: list[str],
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Verify a registered delivery workspace in the isolated Docker verifier."""
    workspace = workspace_root.resolve()
    if _is_under_candidate_store(workspace):
        verify_candidate_via_docker(
            staging_root=workspace,
            expected_sha=expected_sha,
            required_checks=required_checks,
            runner=runner,
        )
        return

    staging = _materialize_workspace_source(workspace, expected_sha)
    try:
        verify_candidate_via_docker(
            staging_root=staging,
            expected_sha=expected_sha,
            required_checks=required_checks,
            runner=runner,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
