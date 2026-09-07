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

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.workspace.registry import get_registry
from examples.mcp_server.agent_paths import managed_source_bundle_path
from examples.mcp_server.agent_tasks import validate_base_ref
from examples.mcp_server.source_publication_policy import (
    LocalSourceState,
    PublicationRoute,
    choose_publication_route,
)

_GIT_TIMEOUT_SECONDS = 120
_BAD_OBJECT_RE = re.compile(
    r"fatal:\s*(?:bad object|not a valid object name)\s+(\S+)", re.IGNORECASE
)


class ManagedSourceBundleError(RuntimeError):
    """Raised when trusted source publication cannot prove the requested SHA."""


class ManagedSourceDigestError(ManagedSourceBundleError, ValueError):
    """Digest binding failed: metadata invalid or artifact bytes diverged.

    Inherits ``ValueError`` so launch wrappers that already fail closed on
    ``ValueError`` reject bad digest metadata before any worker starts.
    """


_MANAGED_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MANAGED_COPY_CHUNK_BYTES = 1024 * 1024


def validate_bundle_digest(value: object) -> str:
    """Strictly validate a SHA-256 digest crossing the trust boundary."""
    if not isinstance(value, str) or not _MANAGED_DIGEST_RE.fullmatch(value):
        raise ManagedSourceDigestError(
            "managed source digest must be a 64-character lowercase hex string"
        )
    return value


@dataclass(frozen=True)
class ManagedSourcePublication:
    """Published artifact bound to the exact bytes proven by control plane."""

    path: str
    sha256: str


def _open_artifact_fd(bundle_path: Path) -> int:
    """Open *bundle_path* read-only without following symlinks.

    Rejects symlinks via ``lstat`` before and ``fstat`` after opening, so a
    swap between the two checks to a symlink or a non-regular file fails
    closed instead of reading foreign bytes.
    """
    try:
        st = os.lstat(bundle_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise ManagedSourceDigestError(
                "managed source bundle is not a regular file"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(bundle_path, flags)
    except OSError as exc:
        raise ManagedSourceDigestError(
            f"managed source bundle unavailable: {exc}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ManagedSourceDigestError(
                "managed source bundle is not a regular file"
            )
    except OSError as exc:
        raise ManagedSourceDigestError(
            f"managed source bundle unavailable: {exc}"
        ) from exc
    return fd


def capture_bundle_digest(bundle_path: str | Path) -> str:
    """SHA-256 over exactly the bytes at ``bundle_path`` (symlink-safe)."""
    fd = _open_artifact_fd(Path(bundle_path))
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "rb", closefd=True) as handle:
            while chunk := handle.read(_MANAGED_COPY_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise ManagedSourceDigestError(
            f"managed source bundle unreadable during digest capture: {exc}"
        ) from exc
    return digest.hexdigest()


def capture_private_snapshot(
    bundle_path: str | Path,
    dest_dir: str | Path | None = None,
) -> tuple[Path, str]:
    """One private read pass of the published artifact.

    Opens the bundle without following symlinks and streams it once into a
    private ``0700`` directory while hashing exactly the bytes written.
    Returns ``(snapshot_path, sha256)`` -- callers must run every semantic
    proof against ``snapshot_path`` so that proof and digest bind the same
    bytes, closing the verify->use TOCTOU window on the control plane side.
    """
    fd = _open_artifact_fd(Path(bundle_path))
    if dest_dir is not None:
        os.makedirs(dest_dir, exist_ok=True)
    private_dir = Path(tempfile.mkdtemp(prefix="managed-source-", dir=dest_dir))
    dst = private_dir / "source.bundle"
    digest = hashlib.sha256()
    try:
        with (
            os.fdopen(fd, "rb", closefd=True) as fin,
            open(dst, "wb") as fout,
        ):
            while chunk := fin.read(_MANAGED_COPY_CHUNK_BYTES):
                digest.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
    except OSError as exc:
        dst.unlink(missing_ok=True)
        private_dir.rmdir()
        raise ManagedSourceDigestError(
            f"managed source private snapshot failed: {exc}"
        ) from exc
    os.chmod(dst, 0o400)
    return dst, digest.hexdigest()


def secure_copy_and_verify(
    bundle_path: str | Path,
    expected_digest: str,
    dest_dir: str | Path | None = None,
) -> Path:
    """Private verified copy of the managed artifact (worker-side primitive).

    Takes one private snapshot of the mutable published path and fails
    closed unless its bytes hash to ``expected_digest``. Downstream
    consumers must treat the returned path as the only trusted artifact and
    never re-open ``bundle_path``.
    """
    expected = validate_bundle_digest(expected_digest)
    snapshot, actual = capture_private_snapshot(bundle_path, dest_dir)
    if actual != expected:
        snapshot.unlink(missing_ok=True)
        snapshot.parent.rmdir()
        raise ManagedSourceDigestError(
            f"managed source digest mismatch: expected {expected}, got {actual}"
        )
    return snapshot


def bind_publication_bytes(bundle_path: Path, expected: str) -> ManagedSourcePublication:
    """Prove semantics and capture the digest over the SAME snapshot bytes.

    The control plane makes one private snapshot of the published artifact
    and runs the complete evidence chain (single advertised head equal to
    *expected*, ``git bundle verify``, scratch clone pinned to *expected*)
    plus the SHA-256 computation on those identical bytes. The returned
    digest therefore describes bytes the trusted plane fully proved --
    workers later demand exactly this digest from their own private copy.
    """
    snapshot, sha256 = capture_private_snapshot(bundle_path)
    try:
        if _bundle_head(snapshot) != expected:
            raise ManagedSourceBundleError(
                "published managed source bundle verification failed"
            )
        _assert_bundle_usable(snapshot, expected, full_proof=True)
    finally:
        snapshot.unlink(missing_ok=True)
        snapshot.parent.rmdir()
    return ManagedSourcePublication(path=str(bundle_path), sha256=sha256)


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


def _assert_bundle_usable(
    path: Path,
    expected: str,
    *,
    full_proof: bool,
) -> None:
    """Fail closed unless *path* is a usable bundle for *expected*.

    ``git bundle list-heads`` alone cannot detect bundles that clone fails
    on (prerequisite-restricted artifacts, truncated histories).  Every
    accepted artifact therefore passes ``git bundle verify`` inside a fresh
    scratch repository; ``full_proof`` additionally performs a real scratch
    clone and pins its HEAD to *expected* — the acceptance evidence for
    publication.
    """
    with tempfile.TemporaryDirectory(prefix="mcp-agent-bundle-verify-") as scratch:
        bare = Path(scratch) / "verify.git"
        _run_git(["init", "--quiet", "--bare", str(bare)])
        _run_git(["-C", str(bare), "bundle", "verify", str(path)])
        if not full_proof:
            return
        clone_dir = Path(scratch) / "clone"
        _run_git(["clone", "--quiet", "--no-hardlinks", str(path), str(clone_dir)])
        _run_git(["-C", str(clone_dir), "checkout", "--quiet", "--detach", expected])
        resolved = _run_git(["-C", str(clone_dir), "rev-parse", "HEAD"]).strip().lower()
    if resolved != expected:
        raise ManagedSourceBundleError(
            f"managed bundle clone resolved to {resolved}, expected {expected}"
        )


def _source_is_shallow(project_root: Path) -> bool:
    """Return whether the registered source has incomplete shallow history.

    Git execution failures still fail closed. A shallow result is routing
    input: publication may use only the trusted remote materializer, never
    the incomplete local object database.
    """
    is_shallow = (
        _run_git(
            ["rev-parse", "--is-shallow-repository"],
            cwd=project_root,
            safe_directory=project_root,
        )
        .strip()
        .lower()
    )
    return is_shallow == "true"


def _is_missing_object_error(exc: ManagedSourceBundleError) -> bool:
    """Return True if *exc* was caused by a missing Git object (``fatal: bad object``).

    Only this specific failure mode triggers the trusted remote fallback.
    All other errors (I/O, permission, timeout) fail closed.
    """
    msg = str(exc)
    return bool(_BAD_OBJECT_RE.search(msg))


def _resolve_trusted_remote(project_root: Path) -> tuple[str, str]:
    """Return ``(clone_url, token)`` for the registered repo's trusted Gitea identity.

    Remote *names* are not trust anchors.  Enumerate the registered checkout's
    configured remotes, keep only URLs accepted by the Gitea host allowlist,
    and require every accepted remote to resolve to the same ``owner/repo``.
    This supports deployments where the trusted remote is named ``gitea`` or
    ``mcp-gitea`` instead of ``origin`` while still failing closed on ambiguous
    repository identity.  The actual fetch target is always re-resolved via
    the authenticated Gitea API; checkout remote URLs are never used for auth.

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
        listed = _run_git(
            ["remote"],
            cwd=project_root,
            safe_directory=project_root,
        )
        names = sorted({line.strip() for line in listed.splitlines() if line.strip()})
        if not names:
            raise ManagedSourceBundleError(
                "registered project has no trusted Gitea remote"
            )

        identities: set[tuple[str, str]] = set()
        for name in names:
            try:
                remote_url = _run_git(
                    ["remote", "get-url", "--push", name],
                    cwd=project_root,
                    safe_directory=project_root,
                ).strip()
            except ManagedSourceBundleError:
                continue
            if not remote_url:
                continue
            try:
                _host, owner, repo = _parse_gitea_remote(remote_url)
            except RuntimeError as exc:
                if str(exc) == "GIT_REMOTE_NOT_ALLOWED":
                    continue
                raise
            identities.add((owner, repo))

        if not identities:
            raise ManagedSourceBundleError(
                "registered project has no trusted Gitea remote"
            )
        if len(identities) != 1:
            raise ManagedSourceBundleError(
                "registered project has conflicting trusted remote identities"
            )

        owner, repo = next(iter(identities))
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
) -> ManagedSourcePublication:
    """Fetch *expected* from the trusted remote and publish a verified bundle.

    Returns the bound publication on success.  Raises on any failure.
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

        _assert_bundle_usable(temp_bundle, expected, full_proof=True)

        os.replace(temp_bundle, bundle_path)
        return bind_publication_bytes(bundle_path, expected)
    finally:
        temp_bundle.unlink(missing_ok=True)


def ensure_managed_source_bundle(
    project: str, base_ref: str | None
) -> ManagedSourcePublication | None:
    """Publish/reuse a self-contained bundle for ``project`` at ``base_ref``.

    Returns ``None`` when managed source storage is not configured (legacy
    local/dev mode).  Once ``MCP_AGENT_SOURCE_ROOT`` is configured, an exact
    full commit id is mandatory and publication fails closed.

    Every accepted artifact is bound cryptographically: the control plane
    takes one private snapshot of the published bytes and runs the full
    semantic proof AND the SHA-256 capture over those identical snapshot
    bytes (:func:`bind_publication_bytes`).  The returned digest therefore
    describes exactly the bytes that were proven -- workers later demand
    this digest from their own private copy.

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

    if bundle_path.is_file():
        try:
            return bind_publication_bytes(bundle_path, expected)
        except ManagedSourceBundleError:
            # A pre-existing artifact that fails verification is not
            # consumable: fall through and attempt a clean rebuild instead
            # of handing it to workers.
            pass

    project_root = Path(get_registry().project_info(project)["root"])
    local_state = (
        LocalSourceState.SHALLOW
        if _source_is_shallow(project_root)
        else LocalSourceState.FULL
    )
    if choose_publication_route(local_state) is PublicationRoute.TRUSTED_REMOTE:
        return _materialize_from_remote(project, expected, bundle_path)

    try:
        _run_git(
            ["cat-file", "-e", f"{base_ref}^{{commit}}"],
            cwd=project_root,
            safe_directory=project_root,
        )
    except ManagedSourceBundleError as exc:
        if _is_missing_object_error(exc) or (
            re.fullmatch(r"[0-9a-fA-F]{40}", base_ref)
            and "timed out during git cat-file" in str(exc)
        ):
            local_state = LocalSourceState.MISSING_COMMIT
        else:
            raise

    if choose_publication_route(local_state) is PublicationRoute.TRUSTED_REMOTE:
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

        _assert_bundle_usable(temp_bundle, expected, full_proof=True)

        os.replace(temp_bundle, bundle_path)
        return bind_publication_bytes(bundle_path, expected)
    finally:
        temp_bundle.unlink(missing_ok=True)
