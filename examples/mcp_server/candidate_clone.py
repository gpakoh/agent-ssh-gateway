"""Prepare durable writeable candidate clones for supervised work."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.workspace.registry import get_registry, reset_registry, resolve_runtime_registry_path
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    _resolve_trusted_remote,
    _source_is_shallow,
    ensure_managed_source_bundle,
)
from examples.mcp_server.managed_git import _minimal_git_env
from examples.mcp_server.project_registry_control import (
    ProjectRegistrationError,
    register_project,
)
from examples.mcp_server.source_publication_policy import classify_source_failure_message

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,160}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,200}$")
_PROTECTED_BRANCHES = frozenset({"master", "main"})
_METADATA_FILENAME = "mcp-candidate-clone.json"
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
        result = subprocess.run(
            _git_command(cwd, args, extra_safe_directories=extra_safe_directories),
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
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


def _remote_ref_sha(source_root: Path, base_ref: str) -> str | None:
    try:
        clone_url, token = _resolve_trusted_remote(source_root)
    except ManagedSourceBundleError:
        return None
    if re.fullmatch(r"[0-9a-f]{40}", base_ref):
        return base_ref.lower()
    if base_ref.startswith("refs/"):
        patterns = [base_ref]
        if base_ref.startswith("refs/tags/"):
            patterns.append(base_ref + "^{}")
    else:
        patterns = [
            f"refs/heads/{base_ref}",
            f"refs/tags/{base_ref}",
            f"refs/tags/{base_ref}^{{}}",
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
        return None
    if result.returncode != 0:
        return None
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
        return peeled[0]
    exact_heads = [sha for refname, sha in candidates if refname == f"refs/heads/{base_ref}"]
    if exact_heads:
        return exact_heads[0]
    return candidates[0][1] if candidates else None


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
        data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _fail("TOOL_EXECUTION_FAILED", "workspace registry cannot be read") from exc
    if not isinstance(data, dict):
        raise _fail("TOOL_EXECUTION_FAILED", "workspace registry is malformed")
    raw = data.get("registry_root", ".")
    if not isinstance(raw, str) or not raw.strip():
        raise _fail("TOOL_EXECUTION_FAILED", "workspace registry root is malformed")
    root = Path(raw.strip())
    if not root.is_absolute():
        root = config_dir / root
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise _fail("TOOL_EXECUTION_FAILED", "workspace registry root is unavailable") from exc
    if not resolved.is_dir():
        raise _fail("TOOL_EXECUTION_FAILED", "workspace registry root is unavailable")
    return resolved


def _source_root(config_dir: Path, project: str) -> Path:
    try:
        info = get_registry(config_dir / "projects.yaml").project_info(project)
        raw = info.get("root")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("missing root")
        root = Path(raw).resolve(strict=True)
    except Exception as exc:
        raise _fail("PROJECT_NOT_FOUND", "source project is not registered or unavailable") from exc
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
    source_root = _source_root(config_dir, project)
    try:
        source_root.relative_to(workspace_root)
    except ValueError as exc:
        raise _fail("POLICY_DENIED", "source project root is outside workspace registry root") from exc

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
    project_id = _project_id(project, branch, base_sha)
    candidate_root = workspace_root / ".mcp-candidate-clones" / project_id
    relative_root = candidate_root.relative_to(workspace_root).as_posix()
    recovered = candidate_root.exists()

    if candidate_root.exists():
        if not candidate_root.is_dir() or not (candidate_root / ".git").exists():
            raise _fail("WORKSPACE_CONTENDED", "candidate clone path exists but is not a git worktree", retryable=False)
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
        candidate_root.parent.mkdir(parents=True, exist_ok=True)
        tmp = candidate_root.with_name(f".{candidate_root.name}.tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        try:
            if local_has_base:
                _run_git(
                    source_root,
                    ["clone", "--local", "--no-hardlinks", str(source_root), str(tmp)],
                    timeout=120,
                    operation="clone source repository",
                    extra_safe_directories=(source_root / ".git",),
                )
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
            "Clone is durable under the workspace registry root and can be reused by calling "
            "prepare_candidate_clone with the same project, branch, and base_ref. Dirty or moved "
            "clones fail closed with WORKSPACE_CONTENDED."
        ),
    )


__all__ = ["CandidateCloneError", "CandidateCloneReceipt", "prepare_candidate_clone"]
