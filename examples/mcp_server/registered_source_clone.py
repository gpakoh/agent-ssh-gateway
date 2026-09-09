"""Materialize exact commits from registered worktrees without cloning them directly.

A registered project can be readable while its Git metadata is owned by a
separate UID.  Top-level Git commands can be scoped with exact
``safe.directory`` entries, but ``git clone <registered-worktree>`` may spawn a
child Git process that re-opens that source and rejects it as dubious
ownership.  This module avoids that boundary entirely: the trusted process
reads the exact commit through top-level Git commands, exposes only the object
database to a temporary bare repository via alternates, creates a self-contained
bundle, and clones the bundle instead of the production checkout.

No system/global/repository Git configuration is modified and wildcard trust is
never used.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from examples.mcp_server.git_trust import with_scoped_safe_directories

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_OBJECT_FORMATS = frozenset({"sha1"})


class RegisteredSourceCloneError(RuntimeError):
    """Sanitized failure while staging an exact registered-source commit."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        retryable: bool = False,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.retryable = retryable
        self.exit_code = exit_code


def _clean_env(base_env: Mapping[str, str] | None, *, home: Path) -> dict[str, str]:
    if base_env is None:
        env: dict[str, str] = {}
        for key in (
            "PATH",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTPS_PROXY",
            "https_proxy",
            "NO_PROXY",
            "no_proxy",
        ):
            value = os.environ.get(key)
            if value:
                env[key] = value
    else:
        env = dict(base_env)
    env["HOME"] = str(home)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    phase: str,
    timeout: int,
) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=dict(env),
        )
    except subprocess.TimeoutExpired as exc:
        raise RegisteredSourceCloneError(
            "registered source materialization timed out",
            phase=phase,
            retryable=True,
        ) from exc
    except OSError as exc:
        raise RegisteredSourceCloneError(
            "registered source materialization could not execute Git",
            phase=phase,
            retryable=True,
        ) from exc
    if result.returncode != 0:
        raise RegisteredSourceCloneError(
            "registered source materialization Git command failed",
            phase=phase,
            retryable=False,
            exit_code=result.returncode,
        )
    return result.stdout.strip()


def clone_registered_commit_via_bundle(
    *,
    source_root: str | Path,
    expected_sha: str,
    destination: str | Path,
    base_env: Mapping[str, str] | None = None,
    timeout: int = 120,
) -> None:
    """Clone ``expected_sha`` without passing ``source_root`` to ``git clone``.

    The source checkout is touched only by top-level Git commands whose trust is
    exact and process-local.  A temporary bare repository reads its object
    database through ``objects/info/alternates`` and emits a self-contained
    bundle.  The destination is cloned from that bundle with ``--no-checkout``;
    callers remain responsible for selecting/creating their desired branch or
    detached checkout afterwards.
    """
    expected = str(expected_sha or "").strip().lower()
    if not _SHA1_RE.fullmatch(expected):
        raise RegisteredSourceCloneError(
            "registered source expected commit is invalid",
            phase="validate_expected_sha",
            retryable=False,
        )

    try:
        source = Path(source_root).resolve(strict=True)
    except OSError as exc:
        raise RegisteredSourceCloneError(
            "registered source root is unavailable",
            phase="resolve_source",
            retryable=False,
        ) from exc
    if not source.is_dir() or not (source / ".git").exists():
        raise RegisteredSourceCloneError(
            "registered source must be a Git worktree",
            phase="resolve_source",
            retryable=False,
        )

    destination_path = Path(destination)
    if destination_path.is_symlink():
        raise RegisteredSourceCloneError(
            "registered source destination must not be a symlink",
            phase="validate_destination",
            retryable=False,
        )
    if destination_path.exists():
        if not destination_path.is_dir():
            raise RegisteredSourceCloneError(
                "registered source destination is not a directory",
                phase="validate_destination",
                retryable=False,
            )
        try:
            if any(destination_path.iterdir()):
                raise RegisteredSourceCloneError(
                    "registered source destination must be empty",
                    phase="validate_destination",
                    retryable=False,
                )
        except OSError as exc:
            raise RegisteredSourceCloneError(
                "registered source destination cannot be inspected",
                phase="validate_destination",
                retryable=True,
            ) from exc

    scratch_parent = destination_path.parent
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".registered-source-", dir=str(scratch_parent)
    ) as scratch_raw:
        scratch = Path(scratch_raw)
        base = _clean_env(base_env, home=scratch)
        try:
            source_env = with_scoped_safe_directories(
                (source, source / ".git"),
                base_env=base,
            )
        except ValueError as exc:
            raise RegisteredSourceCloneError(
                "registered source Git trust configuration is invalid",
                phase="source_trust",
                retryable=False,
            ) from exc

        source_git_prefix = [
            "git",
            "-c",
            f"safe.directory={source}",
            "-c",
            f"safe.directory={source / '.git'}",
        ]
        object_format = _run(
            [*source_git_prefix, "rev-parse", "--show-object-format"],
            cwd=source,
            env=source_env,
            phase="source_object_format",
            timeout=min(timeout, 30),
        ).lower()
        if object_format not in _ALLOWED_OBJECT_FORMATS:
            raise RegisteredSourceCloneError(
                "registered source object format is unsupported",
                phase="source_object_format",
                retryable=False,
            )

        resolved = _run(
            [*source_git_prefix, "rev-parse", "--verify", f"{expected}^{{commit}}"],
            cwd=source,
            env=source_env,
            phase="resolve_expected_commit",
            timeout=min(timeout, 30),
        ).lower()
        if resolved != expected:
            raise RegisteredSourceCloneError(
                "registered source resolved an unexpected commit",
                phase="resolve_expected_commit",
                retryable=False,
            )

        source_objects_raw = _run(
            [
                *source_git_prefix,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "objects",
            ],
            cwd=source,
            env=source_env,
            phase="resolve_source_objects",
            timeout=min(timeout, 30),
        )
        source_objects = Path(source_objects_raw)
        if not source_objects.is_dir():
            raise RegisteredSourceCloneError(
                "registered source object database is unavailable",
                phase="resolve_source_objects",
                retryable=False,
            )

        bare = scratch / "source.git"
        bundle = scratch / "source.bundle"
        _run(
            ["git", "init", "--quiet", "--bare", f"--object-format={object_format}", str(bare)],
            cwd=scratch,
            env=base,
            phase="init_staging_repo",
            timeout=min(timeout, 30),
        )
        alternates = bare / "objects" / "info" / "alternates"
        try:
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(f"{source_objects}\n", encoding="utf-8")
        except OSError as exc:
            raise RegisteredSourceCloneError(
                "registered source object bridge could not be prepared",
                phase="link_source_objects",
                retryable=True,
            ) from exc

        _run(
            ["git", f"--git-dir={bare}", "update-ref", "refs/heads/source", expected],
            cwd=scratch,
            env=base,
            phase="bind_expected_commit",
            timeout=min(timeout, 30),
        )
        staged = _run(
            ["git", f"--git-dir={bare}", "rev-parse", "refs/heads/source^{commit}"],
            cwd=scratch,
            env=base,
            phase="verify_staged_commit",
            timeout=min(timeout, 30),
        ).lower()
        if staged != expected:
            raise RegisteredSourceCloneError(
                "registered source staging resolved an unexpected commit",
                phase="verify_staged_commit",
                retryable=False,
            )
        _run(
            [
                "git",
                f"--git-dir={bare}",
                "bundle",
                "create",
                str(bundle),
                "refs/heads/source",
            ],
            cwd=scratch,
            env=base,
            phase="create_bundle",
            timeout=timeout,
        )
        _run(
            ["git", "-C", str(bare), "bundle", "verify", str(bundle)],
            cwd=scratch,
            env=base,
            phase="verify_bundle",
            timeout=timeout,
        )
        _run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-checkout",
                str(bundle),
                str(destination_path),
            ],
            cwd=scratch_parent,
            env=base,
            phase="clone_bundle",
            timeout=timeout,
        )
        destination_resolved = _run(
            ["git", "-C", str(destination_path), "rev-parse", "--verify", f"{expected}^{{commit}}"],
            cwd=destination_path,
            env=base,
            phase="verify_destination_commit",
            timeout=min(timeout, 30),
        ).lower()
        if destination_resolved != expected:
            raise RegisteredSourceCloneError(
                "registered source destination resolved an unexpected commit",
                phase="verify_destination_commit",
                retryable=False,
            )


__all__ = ["RegisteredSourceCloneError", "clone_registered_commit_via_bundle"]
