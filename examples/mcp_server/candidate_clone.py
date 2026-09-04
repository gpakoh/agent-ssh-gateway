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

from app.workspace.registry import get_registry, reset_registry
from examples.mcp_server.project_registry_control import (
    ProjectRegistrationError,
    register_project,
)

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


def _run_git(
    cwd: Path,
    args: list[str],
    *,
    timeout: int = 60,
    operation: str = "git command",
) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
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
        raise _fail(
            "TOOL_EXECUTION_FAILED",
            f"git {operation} failed",
            retryable=False,
            details={
                "operation": operation,
                "exit_code": result.returncode,
                "stdout_tail": _git_diagnostic_tail(result.stdout, cwd),
                "stderr_tail": _git_diagnostic_tail(result.stderr, cwd),
            },
        )
    return result.stdout.strip()


def _status_state(repo: Path) -> tuple[bool, str, int]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
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
        result = register_project(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id=project_id,
            root=root,
            project_type="candidate-clone",
            description=f"Durable writeable candidate clone for {source_project}",
            tags=["candidate", "agent", "durable"],
            parent=None,
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

    base_expr = f"{base_ref}^{{commit}}" if base_ref else "HEAD^{commit}"
    base_sha = _run_git(
        source_root,
        ["rev-parse", "--verify", base_expr],
        operation="resolve base ref",
    ).lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise _fail("TOOL_EXECUTION_FAILED", "base ref did not resolve to a commit")
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
            _run_git(
                source_root,
                ["clone", "--local", "--no-hardlinks", str(source_root), str(tmp)],
                timeout=120,
                operation="clone source repository",
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
