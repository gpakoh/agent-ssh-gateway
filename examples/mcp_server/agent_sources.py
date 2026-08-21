"""Trusted publication of immutable Git sources for managed agent workers.

The worker/executor only receives ``MCP_AGENT_SOURCE_ROOT`` read-only. This
module runs in the MCP control plane, resolves a registered project root, and
materializes one content-addressed Git bundle for an exact commit id. A dirty
working tree is deliberately irrelevant: only committed Git objects are
fetched into a temporary bare repository before the final bundle is published
atomically.

When the local object database does not contain the requested commit (e.g.
``git cat-file -e`` returns *fatal: bad object*), a safe fallback fetches
the exact SHA from the trusted Gitea remote.  The remote is only consulted
after the project's ``origin`` URL passes the Gitea allowlist and the
repository is confirmed via the Gitea API.  Authentication uses one-shot
``http.extraHeader`` (never embedded in the URL, never persisted to
``.git/config``).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from app.workspace.registry import get_registry
from examples.mcp_server.agent_paths import managed_source_bundle_path
from examples.mcp_server.agent_tasks import validate_base_ref

_GIT_TIMEOUT_SECONDS = 120
_BAD_OBJECT_RE = re.compile(
    r"fatal:\s*(?:bad object|not a valid object name)\s+(\S+)", re.IGNORECASE
)


class ManagedSourceBundleError(RuntimeError):
    """Raised when trusted source publication cannot prove the requested SHA."""


def _git_subcommand(args: list[str]) -> str:
    """Return the first non-option arg so error messages name the subcommand."""
    for arg in args:
        if not arg.startswith("-") and arg != "git":
            return arg
    return args[0] if args else "git"


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    safe_directory: Path | None = None,
) -> str:
    command = ["git"]
    if safe_directory is not None:
        command.extend(["-c", f"safe.directory={safe_directory}"])
    command.extend(args)
    subcmd = _git_subcommand(args)
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ManagedSourceBundleError(
            f"managed source publication timed out during git {subcmd}"
        ) from exc
    except OSError as exc:
        raise ManagedSourceBundleError(
            "git is unavailable for managed source publication"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        msg = f"managed source publication failed during git {subcmd}"
        if detail:
            msg = f"{msg}: {detail}"
        raise ManagedSourceBundleError(msg)
    return result.stdout


def _bundle_head(path: Path) -> str | None:
    output = _run_git(["bundle", "list-heads", str(path)])
    heads = [
        line.split(maxsplit=1)[0].lower()
        for line in output.splitlines()
        if line.strip()
    ]
    return heads[0] if len(heads) == 1 else None


def _is_missing_object_error(exc: ManagedSourceBundleError) -> bool:
    """Return True if *exc* was caused by a missing Git object (``fatal: bad object``).

    Only this specific failure mode triggers the trusted remote fallback.
    All other errors (I/O, permission, timeout) fail closed.
    """
    msg = str(exc)
    return bool(_BAD_OBJECT_RE.search(msg))


def _resolve_trusted_remote(project_root: Path) -> tuple[str, str]:
    """Return ``(clone_url, token)`` for the trusted Gitea remote of *project_root*.

    Reads ``git remote get-url origin`` from the registered project root,
    validates through the Gitea allowlist (``_parse_gitea_remote``), and
    resolves the HTTPS clone URL via the Gitea API (``_repo_https_target``).

    Raises ``ManagedSourceBundleError`` on any failure (fail closed).
    """
    from examples.mcp_server.control_plane_git import (
        _parse_gitea_remote,
        _repo_https_target,
    )

    token = os.environ.get("GITEA_TOKEN", "").strip()
    if not token:
        raise ManagedSourceBundleError(
            "trusted remote fallback requires GITEA_TOKEN"
        )

    try:
        origin_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(project_root),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if origin_url.returncode != 0 or not origin_url.stdout.strip():
            raise ManagedSourceBundleError(
                "registered project has no trusted remote origin"
            )
        url = origin_url.stdout.strip()
        _host, owner, repo = _parse_gitea_remote(url)
        _username, clone_url = _repo_https_target(owner, repo, token=token)
    except ManagedSourceBundleError:
        raise
    except Exception as exc:
        raise ManagedSourceBundleError(
            "trusted remote resolution failed"
        ) from exc
    return clone_url, token


def _fetch_remote_object(
    clone_url: str,
    token: str,
    expected: str,
    bare_dir: Path,
) -> None:
    """Fetch *expected* SHA from *clone_url* into a bare repository.

    Uses ``_minimal_git_env`` for one-shot Basic auth (token in
    ``http.extraHeader``, never in URL, never persisted).  Redirects
    are disabled.
    """
    from examples.mcp_server.managed_git import _minimal_git_env

    env = _minimal_git_env("_", token)
    try:
        result = subprocess.run(
            [
                "git",
                "fetch",
                "--no-tags",
                clone_url,
                expected,
            ],
            cwd=str(bare_dir),
            text=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ManagedSourceBundleError(
            "trusted remote fetch timed out"
        ) from exc
    except OSError as exc:
        raise ManagedSourceBundleError(
            "trusted remote fetch failed"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        msg = "trusted remote fetch failed"
        if detail:
            msg = f"{msg}: {detail}"
        raise ManagedSourceBundleError(msg)


def _materialize_from_remote(
    project: str,
    expected: str,
    bundle_path: Path,
) -> str | None:
    """Fetch *expected* from the trusted remote and publish a verified bundle.

    Returns the bundle path on success.  Raises on any failure.
    """
    project_root = Path(get_registry().project_info(project)["root"])
    clone_url, token = _resolve_trusted_remote(project_root)

    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{expected}.", suffix=".bundle.tmp", dir=bundle_path.parent
    )
    os.close(temp_fd)
    temp_bundle = Path(temp_name)
    temp_bundle.unlink()

    try:
        with tempfile.TemporaryDirectory(prefix="mcp-agent-remote-") as bare_dir_path:
            bare_dir = Path(bare_dir_path)
            bare = bare_dir / "source.git"
            subprocess.run(
                ["git", "init", "--bare", str(bare)],
                cwd=str(bare_dir),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )

            _fetch_remote_object(clone_url, token, expected, bare)

            fetched = subprocess.run(
                ["git", f"--git-dir={bare}", "rev-parse", f"{expected}^{{commit}}"],
                cwd=str(bare_dir),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if fetched.returncode != 0:
                raise ManagedSourceBundleError(
                    "trusted remote did not contain the requested commit"
                )
            resolved = fetched.stdout.strip().lower()
            if resolved != expected:
                raise ManagedSourceBundleError(
                    "trusted remote resolved to an unexpected commit"
                )

            subprocess.run(
                [
                    "git",
                    f"--git-dir={bare}",
                    "update-ref",
                    "refs/heads/source",
                    expected,
                ],
                cwd=str(bare_dir),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            bundle_result = subprocess.run(
                [
                    "git",
                    f"--git-dir={bare}",
                    "bundle",
                    "create",
                    str(temp_bundle),
                    "refs/heads/source",
                ],
                cwd=str(bare_dir),
                text=True,
                capture_output=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
            )
            if bundle_result.returncode != 0:
                raise ManagedSourceBundleError(
                    "trusted remote bundle creation failed"
                )

        if _bundle_head(temp_bundle) != expected:
            raise ManagedSourceBundleError("remote bundle verification failed")

        os.replace(temp_bundle, bundle_path)
        if _bundle_head(bundle_path) != expected:
            bundle_path.unlink(missing_ok=True)
            raise ManagedSourceBundleError(
                "published remote bundle verification failed"
            )
        return str(bundle_path)
    finally:
        temp_bundle.unlink(missing_ok=True)


def ensure_managed_source_bundle(project: str, base_ref: str | None) -> str | None:
    """Publish/reuse a self-contained bundle for ``project`` at ``base_ref``.

    Returns ``None`` when managed source storage is not configured (legacy
    local/dev mode).  Once ``MCP_AGENT_SOURCE_ROOT`` is configured, an exact
    full commit id is mandatory and publication fails closed.

    When the local object database does not contain the requested commit
    (``git cat-file -e`` → *fatal: bad object*), a safe fallback fetches the
    exact SHA from the trusted Gitea remote.  All other local failures (I/O,
    permission, timeout) fail closed without fallback.
    """

    validate_base_ref(base_ref)
    if not base_ref:
        if not os.environ.get("MCP_AGENT_SOURCE_ROOT", "").strip():
            return None
        raise ValueError("managed OpenCode tasks require an exact base_ref")

    bundle_raw = managed_source_bundle_path(project, base_ref)
    if bundle_raw is None:
        return None

    expected = base_ref.lower()
    bundle_path = Path(bundle_raw)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    if bundle_path.is_file() and _bundle_head(bundle_path) == expected:
        return str(bundle_path)

    project_root = Path(get_registry().project_info(project)["root"])

    object_missing = False
    try:
        _run_git(
            ["cat-file", "-e", f"{base_ref}^{{commit}}"],
            cwd=project_root,
            safe_directory=project_root,
        )
    except ManagedSourceBundleError as exc:
        if _is_missing_object_error(exc):
            object_missing = True
        else:
            raise

    if object_missing:
        return _materialize_from_remote(project, expected, bundle_path)

    source_objects_raw = _run_git(
        ["rev-parse", "--path-format=absolute", "--git-path", "objects"],
        cwd=project_root,
        safe_directory=project_root,
    ).strip()
    source_objects = Path(source_objects_raw)
    if not source_objects.is_dir():
        raise ManagedSourceBundleError(
            "registered source object database is unavailable"
        )

    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{expected}.", suffix=".bundle.tmp", dir=bundle_path.parent
    )
    os.close(temp_fd)
    temp_bundle = Path(temp_name)
    temp_bundle.unlink()

    try:
        with tempfile.TemporaryDirectory(prefix="mcp-agent-source-") as bare_dir:
            bare = Path(bare_dir) / "source.git"
            _run_git(["init", "--bare", str(bare)])
            alternates = bare / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(f"{source_objects}\n", encoding="utf-8")
            _run_git(
                [f"--git-dir={bare}", "update-ref", "refs/heads/source", base_ref]
            )
            fetched = _run_git(
                [f"--git-dir={bare}", "rev-parse", "refs/heads/source^{commit}"]
            ).strip().lower()
            if fetched != expected:
                raise ManagedSourceBundleError(
                    "managed source fetch resolved to an unexpected commit"
                )
            _run_git(
                [
                    f"--git-dir={bare}",
                    "bundle",
                    "create",
                    str(temp_bundle),
                    "refs/heads/source",
                ]
            )

        if _bundle_head(temp_bundle) != expected:
            raise ManagedSourceBundleError("managed source bundle verification failed")

        os.replace(temp_bundle, bundle_path)
        if _bundle_head(bundle_path) != expected:
            bundle_path.unlink(missing_ok=True)
            raise ManagedSourceBundleError(
                "published managed source bundle verification failed"
            )
        return str(bundle_path)
    finally:
        temp_bundle.unlink(missing_ok=True)
