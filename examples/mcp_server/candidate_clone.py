"""Prepare durable writeable candidate clones for supervised work."""

from __future__ import annotations

import contextlib
import enum
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.workspace.registry import (
    WorkspacePolicyError,
    get_registry,
    load_registry_roots,
    reset_registry,
    resolve_runtime_registry_path,
)
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    _resolve_trusted_remote,
    _source_is_shallow,
    ensure_managed_source_bundle,
)
from examples.mcp_server.git_trust import with_scoped_safe_directories
from examples.mcp_server.managed_git import _minimal_git_env
from examples.mcp_server.project_registry_control import (
    ProjectRegistrationError,
    ProjectUnregistrationResult,
    project_registry_mutation_lock,
    register_project,
    unregister_project_exact,
)
from examples.mcp_server.registered_source_clone import (
    RegisteredSourceCloneError,
    clone_registered_commit_via_bundle,
)
from examples.mcp_server.source_publication_policy import classify_source_failure_message

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,160}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,200}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROTECTED_BRANCHES = frozenset({"master", "main"})
_METADATA_FILENAME = "mcp-candidate-clone.json"
_CANDIDATE_CLONES_DIRNAME = ".mcp-candidate-clones"
_LINEAGE_LOCKS_DIRNAME = ".locks"
_LINEAGE_LOCK_TIMEOUT_S = 30.0
_LINEAGE_LOCK_POLL_S = 0.05
_CLEANUP_TOMBSTONE_MAX_BYTES = 64 * 1024
_DEFAULT_GIT_NAME = "MCP Control Plane"
_DEFAULT_GIT_EMAIL = "control-plane@gateway.invalid"
_GIT_DIAGNOSTIC_LIMIT = 1200
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.:-])/(?:[^\s:'\"]+/)*[^\s:'\"]*")


class CandidateCloneError(RuntimeError):
    """Sanitized candidate-clone setup failure safe for MCP output."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details


@dataclass(frozen=True)
class CandidateCloneReceipt:
    project_id: str
    source_project: str
    branch: str
    base_ref: str
    base_sha: str
    head: str
    root: str
    recovered: bool
    registered: bool
    clean: bool
    git_identity: dict[str, str]
    recovery_policy: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_project": self.source_project,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head": self.head,
            "root": self.root,
            "recovered": self.recovered,
            "registered": self.registered,
            "clean": self.clean,
            "git_identity": self.git_identity,
            "recovery_policy": self.recovery_policy,
        }


@dataclass(frozen=True)
class CandidateCleanupReceipt:
    project_id: str
    source_project: str
    branch: str
    head: str
    preserved_ref: str
    registry_removed: bool
    directory_removed: bool
    already_cleaned: bool
    tombstone_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_project": self.source_project,
            "branch": self.branch,
            "head": self.head,
            "preserved_ref": self.preserved_ref,
            "registry_removed": self.registry_removed,
            "directory_removed": self.directory_removed,
            "already_cleaned": self.already_cleaned,
            "tombstone_id": self.tombstone_id,
        }


def _fail(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> CandidateCloneError:
    return CandidateCloneError(code, message, retryable=retryable, details=details)


def _validate_branch(branch: str) -> str:
    if not isinstance(branch, str):
        raise _fail("INVALID_INPUT", "branch must be a string")
    branch = branch.strip()
    invalid = (
        not branch
        or branch in _PROTECTED_BRANCHES
        or not _BRANCH_RE.fullmatch(branch)
        or branch.startswith("-")
        or branch.endswith("/")
        or branch.endswith(".lock")
        or ".." in branch
        or "//" in branch
        or ":" in branch
        or "\\" in branch
        or "@{" in branch
    )
    if invalid:
        raise _fail("INVALID_INPUT", "branch is not a safe non-protected feature branch")
    return branch


def _validate_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    if not isinstance(ref, str):
        raise _fail("INVALID_INPUT", "base_ref must be a string")
    ref = ref.strip()
    invalid = (
        not ref
        or not _REF_RE.fullmatch(ref)
        or ref.startswith("-")
        or ref.endswith("/")
        or ref.endswith(".lock")
        or ".." in ref
        or "//" in ref
        or ":" in ref
        or "\\" in ref
        or "@{" in ref
    )
    if invalid:
        raise _fail("INVALID_INPUT", "base_ref is not a safe git ref")
    return ref


def _slug(value: str, *, limit: int) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._").lower()
    return (slug or "x")[:limit].strip("-._") or "x"


def _git_diagnostic_tail(text: str | None, cwd: Path) -> str:
    """Return a bounded, host-path-redacted Git diagnostic snippet."""
    if not text:
        return ""
    cleaned = text
    for path in {str(cwd), str(cwd.parent)}:
        if path and path != "/":
            cleaned = cleaned.replace(f"{path}/", "./").replace(path, ".")
    cleaned = _ABSOLUTE_PATH_RE.sub("<path>", cleaned).strip()
    if len(cleaned) <= _GIT_DIAGNOSTIC_LIMIT:
        return cleaned
    return cleaned[-_GIT_DIAGNOSTIC_LIMIT:]


def _git_command(
    cwd: Path,
    args: list[str],
    *,
    extra_safe_directories: tuple[Path, ...] = (),
) -> list[str]:
    """Build a Git command with scoped per-command safe.directory exceptions.

    Candidate clone only calls this helper with registry-derived source roots,
    their Git directories, or candidate clone roots. Exceptions are deliberately
    per-command and never widened to '*', so operators do not need mutable
    global Git config.
    """
    safe_directories: list[str] = []
    for path in (cwd, *extra_safe_directories):
        safe_directory = str(path.resolve())
        if safe_directory == "*":
            raise _fail("POLICY_DENIED", "refusing wildcard git safe.directory")
        if safe_directory not in safe_directories:
            safe_directories.append(safe_directory)
    command = ["git"]
    for safe_directory in safe_directories:
        command.extend(["-c", f"safe.directory={safe_directory}"])
    command.extend(args)
    return command


def _git_ownership_error_code(operation: str) -> str:
    """Return the typed diagnostic for Git dubious-ownership failures."""
    source_operations = {
        "resolve base ref",
        "clone source repository",
    }
    if operation in source_operations:
        return "SOURCE_REPO_OWNERSHIP_BLOCKED"
    return "GIT_SAFE_DIRECTORY_REQUIRED"


def _git_failure(
    *,
    cwd: Path,
    operation: str,
    exit_code: int,
    stdout: str | None,
    stderr: str | None,
) -> CandidateCloneError:
    stdout_tail = _git_diagnostic_tail(stdout, cwd)
    stderr_tail = _git_diagnostic_tail(stderr, cwd)
    details = {
        "operation": operation,
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    if "detected dubious ownership" in stderr_tail.lower():
        code = _git_ownership_error_code(operation)
        message = (
            "source repository ownership is not trusted by Git"
            if code == "SOURCE_REPO_OWNERSHIP_BLOCKED"
            else "git safe.directory trust is required for this workspace"
        )
        return _fail(code, message, retryable=False, details=details)
    return _fail(
        "TOOL_EXECUTION_FAILED",
        f"git {operation} failed",
        retryable=False,
        details=details,
    )


def _source_ownership_error(
    *,
    operation: str,
    exc: BaseException,
    source_root: Path,
) -> CandidateCloneError | None:
    diagnostic = _git_diagnostic_tail(str(exc), source_root)
    if "detected dubious ownership" not in diagnostic.lower():
        return None
    return _fail(
        "SOURCE_REPO_OWNERSHIP_BLOCKED",
        "source repository ownership is not trusted by Git",
        retryable=False,
        details={"operation": operation, "stderr_tail": diagnostic},
    )


def _run_git(
    cwd: Path,
    args: list[str],
    *,
    timeout: int = 60,
    operation: str = "git command",
    extra_safe_directories: tuple[Path, ...] = (),
) -> str:
    try:
        git_env = with_scoped_safe_directories(
            (cwd, *extra_safe_directories),
        )
        result = subprocess.run(
            _git_command(cwd, args, extra_safe_directories=extra_safe_directories),
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=git_env,
        )
    except ValueError as exc:
        raise _fail(
            "POLICY_DENIED",
            "scoped Git safe.directory configuration is invalid",
            retryable=False,
            details={"operation": operation},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _fail(
            "TOOL_EXECUTION_FAILED",
            f"git {operation} did not complete",
            retryable=True,
            details={"operation": operation, "timeout_s": timeout},
        ) from exc
    except OSError as exc:
        raise _fail(
            "TOOL_EXECUTION_FAILED",
            f"git {operation} could not start",
            retryable=True,
            details={"operation": operation, "error": type(exc).__name__},
        ) from exc
    if result.returncode != 0:
        raise _git_failure(
            cwd=cwd,
            operation=operation,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result.stdout.strip()


def _local_commit_or_none(source_root: Path, ref: str) -> str | None:
    try:
        resolved = _run_git(
            source_root,
            ["rev-parse", "--verify", f"{ref}^{{commit}}"],
            operation="resolve base ref",
        ).lower()
    except CandidateCloneError as exc:
        details = exc.details or {}
        if (
            re.fullmatch(r"[0-9a-fA-F]{40}", ref)
            and exc.code == "TOOL_EXECUTION_FAILED"
            and exc.retryable
            and details.get("operation") == "resolve base ref"
        ):
            return None
        stderr = str(details.get("stderr_tail") or "").lower()
        missing_markers = (
            "needed a single revision",
            "unknown revision",
            "bad revision",
            "bad object",
            "not a valid object name",
            "ambiguous argument",
        )
        if any(marker in stderr for marker in missing_markers):
            return None
        raise
    return resolved if re.fullmatch(r"[0-9a-f]{40}", resolved) else None


class RemoteRefStatus(enum.StrEnum):
    """Strict trusted-remote ref probe outcome."""

    FOUND = "found"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RemoteRefProbe:
    status: RemoteRefStatus
    sha: str | None = None


def _probe_remote_ref(source_root: Path, ref: str) -> RemoteRefProbe:
    """Resolve one ref through the trusted remote with strict outcomes.

    FOUND means the ref definitively exists and resolved to a SHA, ABSENT
    means the remote definitively reported no matching ref, and UNKNOWN
    means the remote state could not be proven (networking, authentication,
    or protocol errors). Cleanup only proceeds on an ABSENT delivery branch
    and fails closed retryably on UNKNOWN.
    """
    try:
        clone_url, token = _resolve_trusted_remote(source_root)
    except ManagedSourceBundleError:
        return RemoteRefProbe(RemoteRefStatus.UNKNOWN)
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return RemoteRefProbe(RemoteRefStatus.FOUND, ref.lower())
    if ref.startswith("refs/"):
        patterns = [ref]
        if ref.startswith("refs/tags/"):
            patterns.append(ref + "^{}")
    else:
        patterns = [
            f"refs/heads/{ref}",
            f"refs/tags/{ref}",
            f"refs/tags/{ref}^{{}}",
        ]
    env = _minimal_git_env("_", token)
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", clone_url, *patterns],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return RemoteRefProbe(RemoteRefStatus.UNKNOWN)
    if result.returncode == 2:
        return RemoteRefProbe(RemoteRefStatus.ABSENT)
    if result.returncode != 0:
        return RemoteRefProbe(RemoteRefStatus.UNKNOWN)
    candidates: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        sha, refname = parts
        sha = sha.lower()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            candidates.append((refname, sha))
    peeled = [sha for refname, sha in candidates if refname.endswith("^{}")]
    if peeled:
        return RemoteRefProbe(RemoteRefStatus.FOUND, peeled[0])
    exact_heads = [sha for refname, sha in candidates if refname == f"refs/heads/{ref}"]
    if exact_heads:
        return RemoteRefProbe(RemoteRefStatus.FOUND, exact_heads[0])
    if candidates:
        return RemoteRefProbe(RemoteRefStatus.FOUND, candidates[0][1])
    return RemoteRefProbe(RemoteRefStatus.UNKNOWN)


def _remote_ref_sha(source_root: Path, base_ref: str) -> str | None:
    probe = _probe_remote_ref(source_root, base_ref)
    return probe.sha if probe.status is RemoteRefStatus.FOUND else None


def _status_state(repo: Path) -> tuple[bool, str, int]:
    try:
        result = subprocess.run(
            _git_command(repo, ["status", "--porcelain=v1"]),
            cwd=str(repo),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail("TOOL_EXECUTION_FAILED", "could not inspect candidate clone status", retryable=True) from exc
    if result.returncode != 0:
        raise _fail("TOOL_EXECUTION_FAILED", "could not inspect candidate clone status")
    stdout = result.stdout
    return bool(stdout.strip()), hashlib.sha256(stdout.encode()).hexdigest(), len([line for line in stdout.splitlines() if line.strip()])


def _workspace_root(config_dir: Path) -> Path:
    registry_path = config_dir / "projects.yaml"
    try:
        registry_roots = load_registry_roots(registry_path)
    except (OSError, UnicodeError, yaml.YAMLError, WorkspacePolicyError) as exc:
        raise _fail("TOOL_EXECUTION_FAILED", "workspace registry cannot be read") from exc
    root = registry_roots["default"]
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise _fail("TOOL_EXECUTION_FAILED", "workspace registry root is unavailable") from exc
    if not resolved.is_dir():
        raise _fail("TOOL_EXECUTION_FAILED", "workspace registry root is unavailable")
    return resolved


def _registry_root_allowlist(config_dir: Path) -> tuple[Path, ...]:
    registry_path = config_dir / "projects.yaml"
    try:
        return tuple(load_registry_roots(registry_path).values())
    except (OSError, UnicodeError, yaml.YAMLError, WorkspacePolicyError) as exc:
        raise _fail("TOOL_EXECUTION_FAILED", "workspace registry cannot be read") from exc


def _is_under_any_root(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _candidate_clones_root(workspace_root: Path) -> Path:
    return workspace_root / _CANDIDATE_CLONES_DIRNAME


def _source_root(config_dir: Path, project: str, *, workspace_root: Path) -> Path:
    try:
        info = get_registry(config_dir / "projects.yaml").project_info(project)
        raw = info.get("root")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("missing root")
        root = Path(raw).resolve(strict=True)
    except CandidateCloneError:
        raise
    except Exception as exc:
        raise _fail("PROJECT_NOT_FOUND", "source project is not registered or unavailable") from exc

    project_type = info.get("type") if isinstance(info, dict) else None
    clones_root = _candidate_clones_root(workspace_root).resolve(strict=False)
    try:
        under_candidate_root = root.relative_to(clones_root) is not None
    except ValueError:
        under_candidate_root = False
    if project_type == "candidate-clone" or under_candidate_root:
        raise _fail(
            "CANDIDATE_SOURCE_DENIED",
            "candidate clones cannot be used as candidate-clone sources",
            retryable=False,
            details={"source_project": project},
        )
    if not (root / ".git").exists():
        raise _fail("INVALID_INPUT", "source project must be a git worktree")
    return root


def _project_id(project: str, branch: str, base_sha: str) -> str:
    digest = hashlib.sha256(f"{project}\0{branch}\0{base_sha}".encode()).hexdigest()[:12]
    prefix = f"candidate-{_slug(project, limit=32)}-{_slug(branch, limit=48)}"
    value = f"{prefix}-{base_sha[:12]}-{digest}"
    if len(value) <= 128:
        return value
    return f"candidate-{_slug(project, limit=24)}-{base_sha[:12]}-{digest}"


def _metadata_path(candidate_root: Path) -> Path:
    return candidate_root / ".git" / _METADATA_FILENAME


def _write_metadata(path: Path, data: dict[str, Any]) -> None:
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        raise _fail("TOOL_EXECUTION_FAILED", "candidate clone metadata could not be written") from exc


def _read_metadata(candidate_root: Path) -> dict[str, Any]:
    git_dir = candidate_root / ".git"
    metadata_path = _metadata_path(candidate_root)
    try:
        git_stat = git_dir.lstat()
        metadata_stat = metadata_path.lstat()
    except OSError as exc:
        raise _fail("WORKSPACE_CONTENDED", "candidate clone metadata is unavailable or invalid") from exc
    if stat.S_ISLNK(git_stat.st_mode) or not stat.S_ISDIR(git_stat.st_mode):
        raise _fail("WORKSPACE_CONTENDED", "candidate clone is not a safe git worktree")
    if stat.S_ISLNK(metadata_stat.st_mode) or not stat.S_ISREG(metadata_stat.st_mode):
        raise _fail("WORKSPACE_CONTENDED", "candidate clone metadata is unsafe")
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("WORKSPACE_CONTENDED", "candidate clone metadata is unavailable or invalid") from exc
    if not isinstance(data, dict):
        raise _fail("WORKSPACE_CONTENDED", "candidate clone metadata is unavailable or invalid")
    return data


def _cleanup_tombstone_path(journal_root: Path, project_id: str) -> tuple[Path, str]:
    tombstone_id = hashlib.sha256(f"candidate-cleanup\0{project_id}".encode()).hexdigest()
    return journal_root / "candidate-cleanup" / f"{tombstone_id}.json", tombstone_id


@contextlib.contextmanager
def _cleanup_journal_dir(path: Path, *, create: bool) -> Iterator[int | None]:
    """Open the cleanup journal directory without following symlinks."""
    journal_root = path.parent.parent
    if path.parent.name != "candidate-cleanup":
        raise _fail("POLICY_DENIED", "candidate cleanup tombstone path is outside its journal area")

    nofollow = getattr(os, "O_NOFOLLOW", None)
    odirectory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or odirectory is None:
        raise _fail(
            "POLICY_DENIED",
            "secure candidate cleanup journal access is unavailable",
        )
    flags = os.O_RDONLY | nofollow | odirectory | getattr(os, "O_CLOEXEC", 0)

    if create:
        try:
            journal_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _fail(
                "TOOL_EXECUTION_FAILED",
                "candidate cleanup journal root could not be prepared",
            ) from exc
    elif not journal_root.exists():
        yield None
        return

    root_fd: int | None = None
    cleanup_fd: int | None = None
    try:
        root_fd = os.open(journal_root, flags)
        if create:
            try:
                os.mkdir("candidate-cleanup", 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        try:
            cleanup_fd = os.open("candidate-cleanup", flags, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                yield None
                return
            raise
        metadata = os.fstat(cleanup_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise _fail("WORKSPACE_CONTENDED", "candidate cleanup journal directory is invalid")
        yield cleanup_fd
    except CandidateCloneError:
        raise
    except OSError as exc:
        raise _fail(
            "WORKSPACE_CONTENDED",
            "candidate cleanup journal directory is unsafe or unavailable",
        ) from exc
    finally:
        if cleanup_fd is not None:
            os.close(cleanup_fd)
        if root_fd is not None:
            os.close(root_fd)


def _read_cleanup_tombstone(path: Path) -> dict[str, Any] | None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise _fail("POLICY_DENIED", "secure candidate cleanup tombstone reads are unavailable")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)

    with _cleanup_journal_dir(path, create=False) as cleanup_fd:
        if cleanup_fd is None:
            return None
        try:
            fd = os.open(path.name, flags, dir_fd=cleanup_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _fail("WORKSPACE_CONTENDED", "candidate cleanup tombstone is unsafe") from exc
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > _CLEANUP_TOMBSTONE_MAX_BYTES
            ):
                raise _fail("WORKSPACE_CONTENDED", "candidate cleanup tombstone is invalid")
            remaining = _CLEANUP_TOMBSTONE_MAX_BYTES + 1
            chunks: list[bytes] = []
            while remaining > 0:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > _CLEANUP_TOMBSTONE_MAX_BYTES:
                raise _fail("WORKSPACE_CONTENDED", "candidate cleanup tombstone is invalid")
        finally:
            os.close(fd)

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("WORKSPACE_CONTENDED", "candidate cleanup tombstone is invalid") from exc
    if not isinstance(data, dict):
        raise _fail("WORKSPACE_CONTENDED", "candidate cleanup tombstone is invalid")
    return data


def _write_cleanup_tombstone(path: Path, data: dict[str, Any]) -> None:
    encoded = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > _CLEANUP_TOMBSTONE_MAX_BYTES:
        raise _fail("TOOL_EXECUTION_FAILED", "candidate cleanup tombstone is too large")

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise _fail("POLICY_DENIED", "secure candidate cleanup tombstone writes are unavailable")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
    )

    with _cleanup_journal_dir(path, create=True) as cleanup_fd:
        assert cleanup_fd is not None
        try:
            existing = os.stat(path.name, dir_fd=cleanup_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise _fail("WORKSPACE_CONTENDED", "candidate cleanup tombstone is unsafe") from exc
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise _fail("WORKSPACE_CONTENDED", "candidate cleanup tombstone is unsafe")

        tmp_name: str | None = None
        tmp_fd: int | None = None
        try:
            for _ in range(16):
                candidate = f".{path.name}.{secrets.token_hex(12)}.tmp"
                try:
                    tmp_fd = os.open(candidate, flags, 0o600, dir_fd=cleanup_fd)
                except FileExistsError:
                    continue
                tmp_name = candidate
                break
            if tmp_fd is None or tmp_name is None:
                raise _fail(
                    "TOOL_EXECUTION_FAILED",
                    "candidate cleanup tombstone temporary file could not be allocated",
                )

            view = memoryview(encoded)
            while view:
                written = os.write(tmp_fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = None
            os.replace(
                tmp_name,
                path.name,
                src_dir_fd=cleanup_fd,
                dst_dir_fd=cleanup_fd,
            )
            tmp_name = None
            os.fsync(cleanup_fd)
        except CandidateCloneError:
            raise
        except OSError as exc:
            raise _fail(
                "TOOL_EXECUTION_FAILED",
                "candidate cleanup tombstone could not be persisted",
            ) from exc
        finally:
            if tmp_fd is not None:
                os.close(tmp_fd)
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name, dir_fd=cleanup_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def _candidate_has_active_evidence(candidate_root: Path) -> bool:
    bridge = candidate_root / ".ai-bridge"
    if (bridge / "current-plan.md").exists():
        return True
    tasks = bridge / "tasks"
    try:
        return tasks.is_dir() and any(tasks.iterdir())
    except OSError as exc:
        raise _fail("TOOL_EXECUTION_FAILED", "candidate task evidence could not be inspected") from exc


def _validate_candidate_root(candidate_root: Path, candidates_root: Path) -> tuple[int, int]:
    if candidate_root.is_symlink() or candidates_root.is_symlink():
        raise _fail("POLICY_DENIED", "candidate cleanup refuses symlinked roots")
    try:
        resolved_candidates = candidates_root.resolve(strict=True)
        resolved_candidate = candidate_root.resolve(strict=True)
    except OSError as exc:
        raise _fail("WORKSPACE_CONTENDED", "candidate clone root is unavailable") from exc
    if resolved_candidate.parent != resolved_candidates:
        raise _fail("POLICY_DENIED", "candidate clone root escapes the server-owned candidate area")
    if not resolved_candidate.is_dir():
        raise _fail("WORKSPACE_CONTENDED", "candidate clone root is unavailable")
    stat = resolved_candidate.stat()
    return stat.st_dev, stat.st_ino


def _reject_symlinked_git_dir(candidate_root: Path) -> None:
    git_dir = candidate_root / ".git"
    try:
        git_stat = git_dir.lstat()
    except OSError as exc:
        raise _fail("WORKSPACE_CONTENDED", "candidate clone is not a git worktree") from exc
    if stat.S_ISLNK(git_stat.st_mode) or not stat.S_ISDIR(git_stat.st_mode):
        raise _fail("WORKSPACE_CONTENDED", "candidate clone is not a git worktree")


def _verify_candidate_clean(
    candidate_root: Path,
    *,
    project_id: str,
    source_project: str,
    branch: str,
    head_sha: str,
    expected_registry_root: str,
) -> None:
    _reject_symlinked_git_dir(candidate_root)
    metadata = _read_metadata(candidate_root)
    for key, value in {
        "project_id": project_id,
        "source_project": source_project,
        "branch": branch,
        "root": expected_registry_root,
    }.items():
        if metadata.get(key) != value:
            raise _fail("WORKSPACE_CONTENDED", "candidate clone metadata identity mismatch")
    dirty, _status_sha, _status_entries = _status_state(candidate_root)
    current_branch = _run_git(
        candidate_root,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        operation="read candidate branch",
    )
    current_head = _run_git(
        candidate_root,
        ["rev-parse", "HEAD"],
        operation="read candidate head",
    ).lower()
    if dirty or current_branch != branch or current_head != head_sha:
        raise _fail(
            "WORKSPACE_CONTENDED",
            "candidate git state does not match cleanup expectations",
            details={
                "project_id": project_id,
                "branch": current_branch,
                "head": current_head,
                "dirty": dirty,
            },
        )
    if _candidate_has_active_evidence(candidate_root):
        raise _fail(
            "WORKSPACE_CONTENDED",
            "candidate clone still has active task or delivery evidence",
        )


def _require_preserved_head(
    source_root: Path,
    preserved_ref: str,
    expected_head_sha: str,
    *,
    context: str,
) -> None:
    probe = _probe_remote_ref(source_root, preserved_ref)
    if probe.status is not RemoteRefStatus.FOUND or probe.sha != expected_head_sha:
        raise _fail(
            "CHECK_FAILED",
            f"candidate head is not proven at the requested preservation ref ({context})",
            retryable=True,
        )


def _require_delivery_branch_absent(
    source_root: Path,
    branch: str,
    *,
    context: str,
) -> None:
    probe = _probe_remote_ref(source_root, branch)
    if probe.status is RemoteRefStatus.FOUND:
        raise _fail(
            "WORKSPACE_CONTENDED",
            f"candidate delivery branch is still published remotely ({context})",
        )
    if probe.status is RemoteRefStatus.UNKNOWN:
        raise _fail(
            "CHECK_FAILED",
            f"candidate delivery branch remote state is unknown ({context})",
            retryable=True,
        )


def _enforce_reference_guard(reference_guard: Callable[[], None]) -> None:
    if not callable(reference_guard):
        raise _fail(
            "INVALID_INPUT",
            "reference_guard must be a callable that raises on blocking active references",
        )
    try:
        reference_guard()
    except CandidateCloneError:
        raise
    except Exception as exc:
        raise _fail(
            "WORKSPACE_CONTENDED",
            "active reference guard blocked candidate cleanup",
            retryable=False,
            details={"guard_error": type(exc).__name__},
        ) from exc


def _unregister_candidate(
    *,
    config_dir: Path,
    journal_root: Path,
    project_id: str,
    root: str,
) -> ProjectUnregistrationResult:
    try:
        return unregister_project_exact(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id=project_id,
            expected_root=root,
            expected_type="candidate-clone",
        )
    except ProjectRegistrationError as exc:
        raise _fail(exc.code, exc.message) from exc


def _registry_store_paths(config_dir: Path) -> list[Path]:
    paths = [config_dir / "projects.yaml"]
    runtime = resolve_runtime_registry_path(config_dir / "projects.yaml")
    if runtime is not None:
        paths.append(runtime)
    return paths


def _registry_has_candidate(config_dir: Path, project_id: str) -> bool:
    """Return whether any raw source/runtime registry store still lists the id."""
    for path in _registry_store_paths(config_dir):
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise _fail("TOOL_EXECUTION_FAILED", "workspace registry cannot be read") from exc
        if not isinstance(data, dict):
            continue
        projects = data.get("projects")
        if not isinstance(projects, dict):
            continue
        entry = projects.get(project_id)
        if isinstance(entry, dict) and entry:
            return True
    return False


def _register_candidate(
    *,
    config_dir: Path,
    journal_root: Path,
    project_id: str,
    root: str,
    source_project: str,
) -> tuple[bool, str | None]:
    try:
        runtime_overlay = resolve_runtime_registry_path(config_dir / "projects.yaml")
        result = register_project(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id=project_id,
            root=root,
            project_type="candidate-clone",
            description=f"Durable writeable candidate clone for {source_project}",
            tags=["candidate", "agent", "durable"],
            parent=None,
            persist_to_source=runtime_overlay is None,
        )
        reset_registry()
        return True, result.registry_hash
    except ProjectRegistrationError as exc:
        if exc.code == "ALREADY_EXISTS":
            reset_registry()
            return False, None
        raise _fail(exc.code, exc.message) from exc


def _lineage_lock_name(project: str, branch: str) -> str:
    digest = hashlib.sha256(f"{project}\0{branch}".encode()).hexdigest()[:16]
    return f"{_slug(project, limit=32)}-{_slug(branch, limit=48)}-{digest}.lock"


@contextlib.contextmanager
def _lineage_lock(workspace_root: Path, project: str, branch: str) -> Iterator[None]:
    """Serialize one source-project/branch lineage without following symlinks."""
    clones_root = _candidate_clones_root(workspace_root)
    try:
        clones_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail(
            "CANDIDATE_LOCK_FAILED",
            "candidate lineage lock storage could not be prepared",
            retryable=True,
        ) from exc

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        clones_fd = os.open(str(clones_root), directory_flags | nofollow)
    except OSError as exc:
        raise _fail(
            "CANDIDATE_LOCK_FAILED",
            "candidate lineage lock storage is unsafe or unavailable",
            retryable=True,
        ) from exc

    locks_fd: int | None = None
    lock_fd: int | None = None
    acquired = False
    try:
        try:
            os.mkdir(_LINEAGE_LOCKS_DIRNAME, 0o700, dir_fd=clones_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _fail(
                "CANDIDATE_LOCK_FAILED",
                "candidate lineage lock directory could not be prepared",
                retryable=True,
            ) from exc

        try:
            locks_fd = os.open(
                _LINEAGE_LOCKS_DIRNAME,
                directory_flags | nofollow,
                dir_fd=clones_fd,
            )
        except OSError as exc:
            raise _fail(
                "CANDIDATE_LOCK_FAILED",
                "candidate lineage lock directory is unsafe or unavailable",
                retryable=True,
            ) from exc

        lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | nofollow
        try:
            lock_fd = os.open(
                _lineage_lock_name(project, branch),
                lock_flags,
                0o600,
                dir_fd=locks_fd,
            )
        except OSError as exc:
            raise _fail(
                "CANDIDATE_LOCK_FAILED",
                "candidate lineage lock is unsafe or unavailable",
                retryable=True,
            ) from exc

        try:
            lock_stat = os.fstat(lock_fd)
        except OSError as exc:
            raise _fail(
                "CANDIDATE_LOCK_FAILED",
                "candidate lineage lock could not be inspected",
                retryable=True,
            ) from exc
        if not stat.S_ISREG(lock_stat.st_mode):
            raise _fail(
                "CANDIDATE_LOCK_FAILED",
                "candidate lineage lock is not a regular file",
                retryable=False,
            )

        deadline = time.monotonic() + _LINEAGE_LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _fail(
                        "CANDIDATE_LOCK_TIMEOUT",
                        "candidate lineage lock could not be acquired",
                        retryable=True,
                    ) from None
                time.sleep(_LINEAGE_LOCK_POLL_S)
            except OSError as exc:
                raise _fail(
                    "CANDIDATE_LOCK_FAILED",
                    "candidate lineage lock could not be acquired",
                    retryable=True,
                ) from exc
        yield
    finally:
        if lock_fd is not None:
            if acquired:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(lock_fd)
        if locks_fd is not None:
            os.close(locks_fd)
        os.close(clones_fd)


def _lineage_scan_failure(message: str) -> CandidateCloneError:
    return _fail("CANDIDATE_LINEAGE_SCAN_FAILED", message, retryable=True)


def _read_candidate_metadata(candidate_dir: Path) -> dict[str, Any]:
    git_dir = candidate_dir / ".git"
    metadata_path = git_dir / _METADATA_FILENAME
    try:
        git_stat = git_dir.lstat()
        metadata_stat = metadata_path.lstat()
    except OSError as exc:
        raise _lineage_scan_failure("candidate lineage metadata is unavailable") from exc
    if stat.S_ISLNK(git_stat.st_mode) or not stat.S_ISDIR(git_stat.st_mode):
        raise _lineage_scan_failure("candidate lineage git metadata is unsafe")
    if stat.S_ISLNK(metadata_stat.st_mode) or not stat.S_ISREG(metadata_stat.st_mode):
        raise _lineage_scan_failure("candidate lineage metadata is unsafe")
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise _lineage_scan_failure("candidate lineage metadata cannot be read") from exc
    if not isinstance(data, dict):
        raise _lineage_scan_failure("candidate lineage metadata is malformed")
    if data.get("project_id") != candidate_dir.name:
        raise _lineage_scan_failure("candidate lineage metadata identity does not match its directory")
    if not isinstance(data.get("source_project"), str) or not isinstance(data.get("branch"), str):
        raise _lineage_scan_failure("candidate lineage metadata is incomplete")
    return data


def _find_lineage_claimant(
    workspace_root: Path,
    *,
    source_project: str,
    branch: str,
    exclude_project_id: str,
) -> dict[str, Any] | None:
    clones_root = _candidate_clones_root(workspace_root)
    try:
        with os.scandir(clones_root) as entries:
            for entry in entries:
                if entry.name == _LINEAGE_LOCKS_DIRNAME or entry.name == exclude_project_id:
                    continue
                if not entry.name.startswith("candidate-"):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        raise _lineage_scan_failure("candidate lineage entry is unsafe")
                except OSError as exc:
                    raise _lineage_scan_failure("candidate lineage entry cannot be inspected") from exc
                metadata = _read_candidate_metadata(Path(entry.path))
                if metadata.get("source_project") == source_project and metadata.get("branch") == branch:
                    return metadata
    except CandidateCloneError:
        raise
    except OSError as exc:
        raise _lineage_scan_failure("candidate lineage directory cannot be scanned") from exc
    return None


def _safe_claimant_details(claimant: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    project_id = claimant.get("project_id")
    if isinstance(project_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project_id):
        safe["existing_project_id"] = project_id
    base_sha = claimant.get("base_sha")
    if isinstance(base_sha, str) and re.fullmatch(r"[0-9a-f]{40}", base_sha):
        safe["existing_base_sha"] = base_sha
    head = claimant.get("head")
    if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head):
        safe["existing_head"] = head
    return safe


def _prepare_candidate_locked(
    *,
    workspace_root: Path,
    source_root: Path,
    project: str,
    branch: str,
    base_ref: str | None,
    requested_ref: str,
    base_sha: str,
    local_has_base: bool,
    config_dir: Path,
    journal_root: Path,
) -> CandidateCloneReceipt:
    project_id = _project_id(project, branch, base_sha)
    candidate_root = _candidate_clones_root(workspace_root) / project_id
    relative_root = candidate_root.relative_to(workspace_root).as_posix()
    try:
        candidate_stat = candidate_root.lstat()
    except FileNotFoundError:
        candidate_stat = None
    except OSError as exc:
        raise _fail(
            "WORKSPACE_CONTENDED",
            "candidate clone path cannot be inspected safely",
            retryable=True,
        ) from exc
    recovered = candidate_stat is not None

    if candidate_stat is not None:
        if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISDIR(candidate_stat.st_mode):
            raise _fail(
                "WORKSPACE_CONTENDED",
                "candidate clone path exists but is not a safe git worktree",
                retryable=False,
            )
        git_dir = candidate_root / ".git"
        try:
            git_stat = git_dir.lstat()
        except OSError:
            git_stat = None
        if (
            git_stat is None
            or stat.S_ISLNK(git_stat.st_mode)
            or not stat.S_ISDIR(git_stat.st_mode)
        ):
            raise _fail(
                "WORKSPACE_CONTENDED",
                "candidate clone git directory is missing or unsafe",
                retryable=False,
            )
        dirty, status_sha, status_entries = _status_state(candidate_root)
        current_branch = _run_git(
            candidate_root,
            ["rev-parse", "--abbrev-ref", "HEAD"],
            operation="read candidate branch",
        )
        current_head = _run_git(
            candidate_root,
            ["rev-parse", "HEAD"],
            operation="read candidate head",
        ).lower()
        if dirty or current_branch != branch or current_head != base_sha:
            raise _fail(
                "WORKSPACE_CONTENDED",
                "candidate clone already exists with different git state",
                retryable=True,
                details={
                    "project_id": project_id,
                    "branch": current_branch,
                    "head": current_head,
                    "dirty": dirty,
                    "status_sha256": status_sha,
                    "status_entries": status_entries,
                },
            )
    else:
        claimant = _find_lineage_claimant(
            workspace_root,
            source_project=project,
            branch=branch,
            exclude_project_id=project_id,
        )
        if claimant is not None:
            details: dict[str, Any] = {
                "branch": branch,
                "requested_base_sha": base_sha,
            }
            details.update(_safe_claimant_details(claimant))
            raise _fail(
                "CANDIDATE_LINEAGE_EXISTS",
                "a candidate clone already exists for this source project and branch",
                retryable=False,
                details=details,
            )

        candidate_root.parent.mkdir(parents=True, exist_ok=True)
        tmp = candidate_root.with_name(f".{candidate_root.name}.tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        try:
            if local_has_base:
                try:
                    clone_registered_commit_via_bundle(
                        source_root=source_root,
                        expected_sha=base_sha,
                        destination=tmp,
                        timeout=120,
                    )
                except RegisteredSourceCloneError as exc:
                    raise _fail(
                        "SOURCE_REPO_OWNERSHIP_BLOCKED"
                        if exc.phase in {"source_trust", "resolve_source", "resolve_source_objects"}
                        else "TOOL_EXECUTION_FAILED",
                        "source repository could not be materialized through the trusted bundle bridge",
                        retryable=exc.retryable,
                        details={
                            "operation": "clone source repository",
                            "phase": exc.phase,
                            "exit_code": exc.exit_code,
                        },
                    ) from exc
            else:
                try:
                    publication = ensure_managed_source_bundle(project, base_sha)
                except ManagedSourceBundleError as exc:
                    raise _fail(
                        "SOURCE_REPO_STALE",
                        "trusted remote base exists but cannot be materialized",
                        retryable=True,
                        details={
                            "base_ref": requested_ref,
                            "base_sha": base_sha,
                            "source_cause": classify_source_failure_message(str(exc)).value,
                        },
                    ) from exc
                except ValueError as exc:
                    raise _fail(
                        "SOURCE_REPO_STALE",
                        "trusted remote base exists but cannot be materialized",
                        retryable=False,
                        details={
                            "base_ref": requested_ref,
                            "base_sha": base_sha,
                            "source_cause": "invalid_source_metadata",
                        },
                    ) from exc
                if publication is None:
                    raise _fail(
                        "SOURCE_REPO_STALE",
                        "trusted remote base exists but managed source storage is unavailable",
                        retryable=True,
                        details={"base_ref": requested_ref, "base_sha": base_sha},
                    )
                _run_git(
                    candidate_root.parent,
                    ["clone", "--no-checkout", publication.path, str(tmp)],
                    timeout=120,
                    operation="clone managed source bundle",
                )
            _run_git(
                tmp,
                ["checkout", "-B", branch, base_sha],
                operation="checkout candidate branch",
            )
            _run_git(
                tmp,
                ["config", "user.name", _DEFAULT_GIT_NAME],
                operation="configure candidate git user.name",
            )
            _run_git(
                tmp,
                ["config", "user.email", _DEFAULT_GIT_EMAIL],
                operation="configure candidate git user.email",
            )
            tmp.replace(candidate_root)
        except Exception:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            raise

    registered, registry_hash = _register_candidate(
        config_dir=config_dir,
        journal_root=journal_root,
        project_id=project_id,
        root=relative_root,
        source_project=project,
    )
    head = _run_git(
        candidate_root,
        ["rev-parse", "HEAD"],
        operation="read prepared candidate head",
    ).lower()
    dirty, status_sha, status_entries = _status_state(candidate_root)
    metadata = {
        "version": 1,
        "project_id": project_id,
        "source_project": project,
        "branch": branch,
        "base_ref": base_ref or "HEAD",
        "base_sha": base_sha,
        "head": head,
        "root": relative_root,
        "registry_hash": registry_hash,
        "status_sha256": status_sha,
        "status_entries": status_entries,
    }
    _write_metadata(_metadata_path(candidate_root), metadata)
    return CandidateCloneReceipt(
        project_id=project_id,
        source_project=project,
        branch=branch,
        base_ref=base_ref or "HEAD",
        base_sha=base_sha,
        head=head,
        root=".",
        recovered=recovered,
        registered=registered,
        clean=not dirty,
        git_identity={"user.name": _DEFAULT_GIT_NAME, "user.email": _DEFAULT_GIT_EMAIL},
        recovery_policy=(
            "Clone is durable under the workspace registry root and is unique per exact source "
            "project and feature branch. Exact-base recovery is idempotent; a different base for "
            "an existing lineage fails closed until that lineage is explicitly reconciled."
        ),
    )


def prepare_candidate_clone(
    project: str,
    branch: str,
    base_ref: str | None = None,
    *,
    config_dir: Path,
    journal_root: Path,
) -> CandidateCloneReceipt:
    """Create or recover one durable writeable clone and register it as a project."""
    if not isinstance(project, str) or not project.strip():
        raise _fail("INVALID_INPUT", "project must be a non-empty string")
    project = project.strip()
    branch = _validate_branch(branch)
    base_ref = _validate_ref(base_ref)
    config_dir = config_dir.resolve()
    journal_root = journal_root.resolve()
    workspace_root = _workspace_root(config_dir)
    source_root = _source_root(config_dir, project, workspace_root=workspace_root)
    if not _is_under_any_root(source_root, _registry_root_allowlist(config_dir)):
        raise _fail("POLICY_DENIED", "source project root is outside configured registry roots")

    requested_ref = base_ref or "HEAD"
    local_sha = _local_commit_or_none(source_root, requested_ref)
    remote_sha = _remote_ref_sha(source_root, requested_ref) if base_ref else None
    if base_ref and remote_sha is None and local_sha is None:
        raise _fail(
            "SOURCE_REF_NOT_AVAILABLE",
            "base ref is unavailable in both trusted remote and local source",
            retryable=True,
            details={"base_ref": requested_ref},
        )
    base_sha = remote_sha or local_sha
    if base_sha is None:
        raise _fail(
            "SOURCE_REPO_STALE",
            "source repository cannot resolve the requested base commit",
            retryable=True,
            details={"base_ref": requested_ref},
        )
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise _fail("SOURCE_REF_NOT_AVAILABLE", "base ref did not resolve to a commit")
    try:
        source_is_shallow = _source_is_shallow(source_root)
    except ManagedSourceBundleError as exc:
        ownership_error = _source_ownership_error(
            operation="inspect source repository completeness",
            exc=exc,
            source_root=source_root,
        )
        if ownership_error is not None:
            raise ownership_error from exc
        raise _fail(
            "SOURCE_REPO_STALE",
            "source repository completeness could not be inspected",
            retryable=True,
            details={
                "base_ref": requested_ref,
                "base_sha": base_sha,
                "source_cause": classify_source_failure_message(str(exc)).value,
            },
        ) from exc
    local_has_base = (
        _local_commit_or_none(source_root, base_sha) == base_sha
        and not source_is_shallow
    )
    with _lineage_lock(workspace_root, project, branch):
        return _prepare_candidate_locked(
            workspace_root=workspace_root,
            source_root=source_root,
            project=project,
            branch=branch,
            base_ref=base_ref,
            requested_ref=requested_ref,
            base_sha=base_sha,
            local_has_base=local_has_base,
            config_dir=config_dir,
            journal_root=journal_root,
        )


def candidate_cleanup(
    project_id: str,
    expected_head_sha: str,
    expected_branch: str,
    expected_source_project: str,
    preserved_ref: str,
    *,
    config_dir: Path,
    journal_root: Path,
    reference_guard: Callable[[], None],
) -> CandidateCleanupReceipt:
    """Remove one exactly identified, remotely preserved candidate clone.

    The injected ``reference_guard`` is REQUIRED and fail-closed: there is no
    permissive default, so this core cannot be called unguarded. It is invoked
    before the registry unregister and again immediately before the destructive
    filesystem removal; it must raise whenever an active/open reference (open
    PR, external task-job-delivery refs, etc.) still needs the clone.

    Cleanup holds the same source-project+branch lineage lock used by
    ``prepare_candidate_clone`` across remote identity checks, the registry CAS
    unregister, and the filesystem delete.
    """
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise _fail("INVALID_INPUT", "project_id has an invalid format")
    if not isinstance(expected_head_sha, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", expected_head_sha.strip()
    ):
        raise _fail("INVALID_INPUT", "expected_head_sha must be a full commit SHA")
    expected_head_sha = expected_head_sha.strip().lower()
    expected_branch = _validate_branch(expected_branch)
    if not isinstance(expected_source_project, str) or not expected_source_project.strip():
        raise _fail("INVALID_INPUT", "expected_source_project must be non-empty")
    expected_source_project = expected_source_project.strip()
    validated_preserved_ref = _validate_ref(preserved_ref)
    if validated_preserved_ref is None or not validated_preserved_ref.startswith(
        "archive/candidate-"
    ):
        raise _fail(
            "INVALID_INPUT",
            "preserved_ref must be an explicit archive/candidate-* remote branch",
        )
    preserved_ref = validated_preserved_ref
    if not callable(reference_guard):
        raise _fail("INVALID_INPUT", "reference_guard must be callable and is required")

    config_dir = config_dir.resolve()
    journal_root = journal_root.resolve()
    workspace_root = _workspace_root(config_dir)
    source_root = _source_root(config_dir, expected_source_project, workspace_root=workspace_root)
    candidates_root = _candidate_clones_root(workspace_root)
    candidate_root = candidates_root / project_id
    expected_registry_root = f".mcp-candidate-clones/{project_id}"
    tombstone_path, tombstone_id = _cleanup_tombstone_path(journal_root, project_id)
    expected_identity = {
        "version": 1,
        "project_id": project_id,
        "source_project": expected_source_project,
        "branch": expected_branch,
        "head": expected_head_sha,
        "preserved_ref": preserved_ref,
        "registry_root": expected_registry_root,
    }

    with _lineage_lock(workspace_root, expected_source_project, expected_branch):
        with project_registry_mutation_lock(journal_root):
            return _cleanup_candidate_locked(
                project_id=project_id,
                expected_head_sha=expected_head_sha,
                expected_branch=expected_branch,
                expected_source_project=expected_source_project,
                preserved_ref=preserved_ref,
                config_dir=config_dir,
                journal_root=journal_root,
                source_root=source_root,
                candidates_root=candidates_root,
                candidate_root=candidate_root,
                expected_registry_root=expected_registry_root,
                expected_identity=expected_identity,
                tombstone_path=tombstone_path,
                tombstone_id=tombstone_id,
                reference_guard=reference_guard,
            )


def _cleanup_candidate_locked(
    *,
    project_id: str,
    expected_head_sha: str,
    expected_branch: str,
    expected_source_project: str,
    preserved_ref: str,
    config_dir: Path,
    journal_root: Path,
    source_root: Path,
    candidates_root: Path,
    candidate_root: Path,
    expected_registry_root: str,
    expected_identity: dict[str, Any],
    tombstone_path: Path,
    tombstone_id: str,
    reference_guard: Callable[[], None],
) -> CandidateCleanupReceipt:
    tombstone = _read_cleanup_tombstone(tombstone_path)
    if tombstone is not None:
        for key, value in expected_identity.items():
            if tombstone.get(key) != value:
                raise _fail("WORKSPACE_CONTENDED", "candidate cleanup tombstone identity mismatch")
        phase_value = tombstone.get("phase")
        if phase_value not in {"prepared", "registry_removed", "complete"}:
            raise _fail("WORKSPACE_CONTENDED", "candidate cleanup tombstone phase is invalid")

    _require_preserved_head(
        source_root,
        preserved_ref,
        expected_head_sha,
        context="before existence check",
    )
    _require_delivery_branch_absent(
        source_root,
        expected_branch,
        context="before existence check",
    )

    candidate_exists = candidate_root.exists() or candidate_root.is_symlink()
    phase = str(tombstone.get("phase")) if tombstone is not None else None
    if not candidate_exists:
        registry_present = _registry_has_candidate(config_dir, project_id)
        if phase == "complete" and not registry_present:
            return CandidateCleanupReceipt(
                project_id=project_id,
                source_project=expected_source_project,
                branch=expected_branch,
                head=expected_head_sha,
                preserved_ref=preserved_ref,
                registry_removed=False,
                directory_removed=False,
                already_cleaned=True,
                tombstone_id=tombstone_id,
            )
        if tombstone is None and not registry_present:
            raise _fail("PROJECT_NOT_FOUND", "candidate clone does not exist")
        _enforce_reference_guard(reference_guard)
        if tombstone is None:
            tombstone = {
                **expected_identity,
                "phase": "prepared",
            }
            _write_cleanup_tombstone(tombstone_path, tombstone)
        unregister_result = _unregister_candidate(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id=project_id,
            root=expected_registry_root,
        )
        reset_registry()
        completed = dict(expected_identity)
        completed["phase"] = "complete"
        _write_cleanup_tombstone(tombstone_path, completed)
        return CandidateCleanupReceipt(
            project_id=project_id,
            source_project=expected_source_project,
            branch=expected_branch,
            head=expected_head_sha,
            preserved_ref=preserved_ref,
            registry_removed=not unregister_result.already_absent,
            directory_removed=False,
            already_cleaned=unregister_result.already_absent,
            tombstone_id=tombstone_id,
        )

    root_dev, root_ino = _validate_candidate_root(candidate_root, candidates_root)
    if tombstone is not None:
        if tombstone.get("root_dev") != root_dev or tombstone.get("root_ino") != root_ino:
            raise _fail("WORKSPACE_CONTENDED", "candidate directory identity changed during cleanup")

    _verify_candidate_clean(
        candidate_root,
        project_id=project_id,
        source_project=expected_source_project,
        branch=expected_branch,
        head_sha=expected_head_sha,
        expected_registry_root=expected_registry_root,
    )

    _enforce_reference_guard(reference_guard)
    if tombstone is None:
        tombstone = {
            **expected_identity,
            "phase": "prepared",
            "root_dev": root_dev,
            "root_ino": root_ino,
        }
        _write_cleanup_tombstone(tombstone_path, tombstone)

    unregister_result = _unregister_candidate(
        config_dir=config_dir,
        journal_root=journal_root,
        project_id=project_id,
        root=expected_registry_root,
    )
    reset_registry()
    tombstone = dict(tombstone)
    tombstone["phase"] = "registry_removed"
    _write_cleanup_tombstone(tombstone_path, tombstone)

    if candidate_root.exists() or candidate_root.is_symlink():
        current_dev, current_ino = _validate_candidate_root(candidate_root, candidates_root)
        if current_dev != root_dev or current_ino != root_ino:
            raise _fail("WORKSPACE_CONTENDED", "candidate directory identity changed during cleanup")
        _require_preserved_head(
            source_root,
            preserved_ref,
            expected_head_sha,
            context="before filesystem removal",
        )
        _require_delivery_branch_absent(
            source_root,
            expected_branch,
            context="before filesystem removal",
        )
        _verify_candidate_clean(
            candidate_root,
            project_id=project_id,
            source_project=expected_source_project,
            branch=expected_branch,
            head_sha=expected_head_sha,
            expected_registry_root=expected_registry_root,
        )
        _enforce_reference_guard(reference_guard)
        try:
            shutil.rmtree(candidate_root)
        except OSError as exc:
            raise _fail(
                "TOOL_EXECUTION_FAILED",
                "candidate directory removal did not complete",
                retryable=True,
            ) from exc
        if candidate_root.exists() or candidate_root.is_symlink():
            raise _fail(
                "TOOL_EXECUTION_FAILED",
                "candidate directory removal could not be verified",
                retryable=True,
            )
        directory_removed = True
    else:
        directory_removed = False

    tombstone["phase"] = "complete"
    _write_cleanup_tombstone(tombstone_path, tombstone)
    return CandidateCleanupReceipt(
        project_id=project_id,
        source_project=expected_source_project,
        branch=expected_branch,
        head=expected_head_sha,
        preserved_ref=preserved_ref,
        registry_removed=not unregister_result.already_absent,
        directory_removed=directory_removed,
        already_cleaned=False,
        tombstone_id=tombstone_id,
    )


__all__ = [
    "CandidateCleanupReceipt",
    "CandidateCloneError",
    "CandidateCloneReceipt",
    "candidate_cleanup",
    "prepare_candidate_clone",
]
