"""Trusted task-candidate materialization for agent Git delivery.

Agent workspaces are evidence producers, never Git delivery authorities.  A
candidate is materialized by the control plane from the immutable BASE_HEAD
and the supervisor-owned implementation diff only after the machine-readable
supervisor verdict is fully green.  The resulting commit remains in a
persistent, task-scoped staging repository under a control-plane-only candidate
root and is bound to an atomic receipt before any push can occur.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from examples.mcp_client_remote.fleet.shared import validate_repo_owner_or_name
from examples.mcp_server.agent_paths import task_dir
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    ensure_managed_source_bundle,
)
from examples.mcp_server.agent_tasks import (
    validate_required_checks,
    validate_scope_contract,
    validate_task_id,
)

RECEIPT_VERSION = 1
CONTRACT_VERSION = 1
ATTEMPT_BINDING_VERSION = 1
RECEIPT_FILENAME = "candidate-receipt.json"
CONTRACT_FILENAME = "delivery-contract.json"
ATTEMPT_BINDING_FILENAME = "attempt-binding.json"
STAGING_DIRNAME = "candidate-staging"
_CANDIDATE_ROOT_ENV = "MCP_TASK_CANDIDATE_ROOT"

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_PROTECTED_BRANCHES = frozenset({"main", "master"})


class CandidateError(RuntimeError):
    """A sanitized task-candidate failure safe to expose through MCP."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "POLICY_DENIED",
        retryable: bool = False,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.hint = hint
        self.details = details


def implementation_diff_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_sha1(value: Any, *, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA1_RE.fullmatch(normalized):
        raise CandidateError(f"{label} must be a full 40-character lowercase Git SHA-1")
    return normalized


def _validate_sha256(value: Any, *, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise CandidateError(f"{label} must be a full 64-character lowercase SHA-256")
    return normalized


def _validate_branch(value: str) -> str:
    branch = value.strip()
    if (
        not branch
        or branch in _PROTECTED_BRANCHES
        or not _BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or "//" in branch
        or branch.endswith("/")
        or branch.startswith("-")
    ):
        raise CandidateError(f"invalid or protected destination branch: {branch!r}")
    return branch


def _task_state_root(project_root: str | Path, project: str, task_id: str) -> Path:
    validate_task_id(task_id)
    raw = Path(task_dir(project, task_id))
    if raw.is_absolute():
        state_root_raw = os.environ.get("MCP_AGENT_STATE_ROOT", "").strip()
        if not state_root_raw:
            raise CandidateError("absolute task state requires MCP_AGENT_STATE_ROOT")
        state_root = Path(state_root_raw)
        if not state_root.is_absolute() or state_root == Path("/"):
            raise CandidateError("MCP_AGENT_STATE_ROOT must be an absolute non-root path")
        task_path = raw
        trust_anchor = state_root
    else:
        trust_anchor = Path(project_root)
        task_path = trust_anchor / raw

    _assert_no_symlink_chain(trust_anchor, task_path)
    return task_path


def _assert_no_symlink_chain(anchor: Path, target: Path) -> None:
    """Fail closed if anchor/target ancestry contains a symlink or escapes."""
    anchor = anchor.absolute()
    target = target.absolute()
    try:
        relative = target.relative_to(anchor)
    except ValueError as exc:
        raise CandidateError("task candidate path escapes trusted state root") from exc

    current = anchor
    paths = [current]
    for part in relative.parts:
        current = current / part
        paths.append(current)
    for path in paths:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CandidateError("cannot verify task candidate path ancestry") from exc
        if stat.S_ISLNK(mode):
            raise CandidateError("task candidate path ancestry contains a symlink")


def _read_regular(path: Path, *, max_bytes: int = 8 * 1024 * 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CandidateError(f"required candidate artifact is unavailable: {path.name}") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise CandidateError(f"candidate artifact is not a regular file: {path.name}")
        if metadata.st_size > max_bytes:
            raise CandidateError(f"candidate artifact is too large: {path.name}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise CandidateError(f"candidate artifact is too large: {path.name}")
        return data
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(_read_regular(path).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CandidateError(f"candidate artifact is not valid JSON: {path.name}") from exc
    if not isinstance(parsed, dict):
        raise CandidateError(f"candidate artifact must be a JSON object: {path.name}")
    return parsed


def _compile_scope_glob(pattern: str) -> re.Pattern[str]:
    normalized = pattern.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = PurePosixPath(normalized).parts if normalized else ()
    if not normalized or normalized.startswith("/") or ".." in parts:
        raise CandidateError(f"invalid scope pattern: {pattern!r}")
    out = ["^"]
    index = 0
    while index < len(normalized):
        if normalized[index : index + 3] == "**/":
            out.append("(?:.*/)?")
            index += 3
        elif normalized[index : index + 2] == "**":
            out.append(".*")
            index += 2
        elif normalized[index] == "*":
            out.append("[^/]*")
            index += 1
        elif normalized[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(normalized[index]))
            index += 1
    out.append("$")
    return re.compile("".join(out))


def _json_string_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CandidateError(f"trusted delivery contract {label} must be an array of strings")
    return list(value)


def _validated_contract_lists(
    allowed_files: list[str], forbidden_files: list[str], required_checks: list[str]
) -> tuple[list[str], list[str], list[str]]:
    validate_scope_contract(allowed_files, forbidden_files)
    validate_required_checks(required_checks)
    allowed = [item.strip() for item in allowed_files]
    forbidden = [item.strip() for item in forbidden_files]
    checks = [item.strip() for item in required_checks]
    if any(not item for item in allowed + forbidden + checks):
        raise CandidateError("delivery contract entries must be non-empty strings")
    for pattern in allowed + forbidden:
        _compile_scope_glob(pattern)
    return allowed, forbidden, checks


def _load_evidence(project_root: str | Path, project: str, task_id: str) -> dict[str, Any]:
    td = _task_state_root(project_root, project, task_id)
    contract, binding = _load_trusted_task_state(project, task_id)
    base_head = _validate_sha1(
        _read_regular(td / "base-head.txt", max_bytes=256).decode("ascii").strip(),
        label="base_head",
    )
    if base_head != contract["base_ref"]:
        raise CandidateError("executor BASE_HEAD does not match immutable delivery contract")
    diff = _read_regular(td / "implementation-diff.patch")
    return {
        "task_dir": td,
        "base_head": base_head,
        "implementation_diff": diff,
        "implementation_diff_sha256": implementation_diff_sha256(diff),
        "attempt_id": binding["attempt_id"],
        "fingerprint": binding["fingerprint"],
        "job_id": binding["job_id"],
        "allowed_files": contract["allowed_files"],
        "forbidden_files": contract["forbidden_files"],
        "required_checks": contract["required_checks"],
        "delivery_contract_sha256": _delivery_contract_sha256(contract),
    }


def _managed_source_bundle(project: str, base_head: str) -> Path | None:
    """Return a verified immutable source bundle path for ``base_head``."""
    try:
        publication = ensure_managed_source_bundle(project, base_head)
    except (ManagedSourceBundleError, ValueError) as exc:
        raise CandidateError(
            "managed source bundle for candidate base_head is unavailable",
            code="SOURCE_UNAVAILABLE",
            retryable=True,
            hint="Confirm the trusted remote contains the exact base commit.",
        ) from exc
    if publication is None:
        return None
    return Path(publication.path)


def _run_git(cwd: Path, args: list[str], *, env: dict[str, str] | None = None) -> str:
    subcommand = args[0] if args else "git"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateError(f"candidate Git {subcommand} operation did not complete") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:1024]
        message = f"candidate Git {subcommand} operation failed"
        if detail:
            message = f"{message}: {detail}"
        raise CandidateError(message)
    return result.stdout.strip()


def _candidate_root() -> Path:
    raw = os.environ.get(_CANDIDATE_ROOT_ENV, "").strip()
    if not raw:
        raise CandidateError(f"{_CANDIDATE_ROOT_ENV} is required for trusted candidate delivery")
    root = Path(raw)
    if not root.is_absolute() or root == Path("/"):
        raise CandidateError(f"{_CANDIDATE_ROOT_ENV} must be an absolute non-root path")
    if ".." in root.parts:
        raise CandidateError(f"{_CANDIDATE_ROOT_ENV} must not contain '..'")
    anchor = Path(root.anchor)
    _assert_no_symlink_chain(anchor, root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CandidateError(f"{_CANDIDATE_ROOT_ENV} is unavailable") from exc
    _assert_no_symlink_chain(anchor, root)
    resolved = root.resolve()
    if resolved != root:
        raise CandidateError(f"{_CANDIDATE_ROOT_ENV} must not traverse symlinks")
    return root


@contextmanager
def _candidate_root_lock() -> Iterator[None]:
    root = _candidate_root()
    lock_path = root / ".materialize.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise CandidateError("trusted candidate materialization lock is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise CandidateError("trusted candidate materialization lock is invalid")
        fcntl.flock(fd, fcntl.LOCK_EX)
        _assert_no_symlink_chain(Path(root.anchor), root)
        yield
    except OSError as exc:
        raise CandidateError("trusted candidate materialization lock failed") from exc
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _candidate_task_dir(project: str, task_id: str) -> Path:
    validate_task_id(task_id)
    root = _candidate_root()
    project_key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]
    task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
    task_root = root / f"project-{project_key}" / f"task-{task_key}"
    _assert_no_symlink_chain(root, task_root)
    return task_root


def _candidate_record_dir(project: str, task_id: str, attempt_id: str) -> Path:
    task_root = _candidate_task_dir(project, task_id)
    attempt_key = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:24]
    record = task_root / f"attempt-{attempt_key}"
    _assert_no_symlink_chain(_candidate_root(), record)
    return record


def _contract_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in (
            "version",
            "project",
            "task_id",
            "base_ref",
            "allowed_files",
            "forbidden_files",
            "required_checks",
        )
    }


def _delivery_contract_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        _contract_semantics(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_task_delivery_contract(
    *,
    project: str,
    task_id: str,
    base_ref: str,
    allowed_files: list[str],
    forbidden_files: list[str],
    required_checks: list[str],
) -> dict[str, Any]:
    """Persist the immutable delivery contract before an executor can run."""
    base = _validate_sha1(base_ref, label="base_ref")
    allowed, forbidden, checks = _validated_contract_lists(
        allowed_files, forbidden_files, required_checks
    )
    desired = {
        "version": CONTRACT_VERSION,
        "project": project,
        "task_id": task_id,
        "base_ref": base,
        "allowed_files": allowed,
        "forbidden_files": forbidden,
        "required_checks": checks,
        "created_at": datetime.now(UTC).isoformat(),
    }
    with _candidate_root_lock():
        path = _candidate_task_dir(project, task_id) / CONTRACT_FILENAME
        if path.exists() or path.is_symlink():
            existing = _read_json(path)
            if _contract_semantics(existing) != _contract_semantics(desired):
                raise CandidateError("task delivery contract is immutable")
            return existing
        _atomic_write_json(path, desired)
    return desired


def resolve_task_attempt_identity(
    *, project: str, task_id: str, fingerprint: str
) -> tuple[str, str | None]:
    """Create/read the control-plane-owned attempt identity before submission."""
    normalized_fingerprint = fingerprint.strip()
    if not normalized_fingerprint:
        raise CandidateError("trusted attempt identity requires fingerprint")
    with _candidate_root_lock():
        path = _candidate_task_dir(project, task_id) / ATTEMPT_BINDING_FILENAME
        if path.exists() or path.is_symlink():
            existing = _read_json(path)
            if (
                existing.get("version") != ATTEMPT_BINDING_VERSION
                or existing.get("project") != project
                or existing.get("task_id") != task_id
                or existing.get("fingerprint") != normalized_fingerprint
            ):
                raise CandidateError("task attempt identity is immutable")
            attempt_id = existing.get("attempt_id")
            job_id = existing.get("job_id")
            if not isinstance(attempt_id, str) or not attempt_id.strip():
                raise CandidateError("trusted attempt identity is missing attempt_id")
            if job_id is not None and (not isinstance(job_id, str) or not job_id.strip()):
                raise CandidateError("trusted attempt identity has invalid job_id")
            return attempt_id.strip(), job_id.strip() if isinstance(job_id, str) else None
        attempt_id = uuid.uuid4().hex
        payload = {
            "version": ATTEMPT_BINDING_VERSION,
            "project": project,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "fingerprint": normalized_fingerprint,
            "job_id": None,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _atomic_write_json(path, payload)
        return attempt_id, None


def bind_task_attempt_job(
    *, project: str, task_id: str, attempt_id: str, fingerprint: str, job_id: str
) -> dict[str, Any]:
    """Bind the first accepted gateway job to the preclaimed trusted attempt."""
    values = {
        "attempt_id": attempt_id.strip(),
        "fingerprint": fingerprint.strip(),
        "job_id": job_id.strip(),
    }
    if any(not value for value in values.values()):
        raise CandidateError("trusted attempt job binding requires attempt_id, fingerprint, and job_id")
    with _candidate_root_lock():
        path = _candidate_task_dir(project, task_id) / ATTEMPT_BINDING_FILENAME
        existing = _read_json(path)
        if (
            existing.get("version") != ATTEMPT_BINDING_VERSION
            or existing.get("project") != project
            or existing.get("task_id") != task_id
            or existing.get("attempt_id") != values["attempt_id"]
            or existing.get("fingerprint") != values["fingerprint"]
        ):
            raise CandidateError("task attempt identity is immutable")
        current_job = existing.get("job_id")
        if current_job is not None:
            if current_job != values["job_id"]:
                raise CandidateError("task attempt job binding is immutable")
            return existing
        updated = dict(existing)
        updated["job_id"] = values["job_id"]
        updated["job_bound_at"] = datetime.now(UTC).isoformat()
        _atomic_write_json(path, updated)
        return updated


def _load_trusted_task_state(project: str, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    task_root = _candidate_task_dir(project, task_id)
    contract = _read_json(task_root / CONTRACT_FILENAME)
    binding = _read_json(task_root / ATTEMPT_BINDING_FILENAME)
    if (
        contract.get("version") != CONTRACT_VERSION
        or contract.get("project") != project
        or contract.get("task_id") != task_id
    ):
        raise CandidateError("trusted delivery contract binding is invalid")
    contract["base_ref"] = _validate_sha1(contract.get("base_ref"), label="contract base_ref")
    allowed, forbidden, checks = _validated_contract_lists(
        _json_string_list(contract.get("allowed_files"), label="allowed_files"),
        _json_string_list(contract.get("forbidden_files"), label="forbidden_files"),
        _json_string_list(contract.get("required_checks"), label="required_checks"),
    )
    contract["allowed_files"] = allowed
    contract["forbidden_files"] = forbidden
    contract["required_checks"] = checks
    if (
        binding.get("version") != ATTEMPT_BINDING_VERSION
        or binding.get("project") != project
        or binding.get("task_id") != task_id
    ):
        raise CandidateError("trusted attempt binding is invalid")
    for key in ("attempt_id", "fingerprint", "job_id"):
        if not isinstance(binding.get(key), str) or not binding[key].strip():
            raise CandidateError(f"trusted attempt binding is missing {key}")
        binding[key] = binding[key].strip()
    return contract, binding


def _staging_repo(record_dir: Path) -> Path:
    return record_dir / STAGING_DIRNAME / "repo"


def _remove_candidate_tree(path: Path) -> None:
    """Remove a candidate directory without following attacker-controlled symlinks."""
    root = _candidate_root().absolute()
    target = path.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise CandidateError("candidate cleanup path escapes trusted state root") from exc
    if not relative.parts:
        raise CandidateError("candidate cleanup cannot remove trusted state root")

    nofollow = getattr(os, "O_NOFOLLOW", None)
    odirectory = getattr(os, "O_DIRECTORY", None)
    if (
        nofollow is None
        or odirectory is None
        or not getattr(shutil.rmtree, "avoids_symlink_attacks", False)
    ):
        raise CandidateError("secure candidate cleanup is unavailable")

    flags = os.O_RDONLY | nofollow | odirectory | getattr(os, "O_CLOEXEC", 0)
    opened: list[int] = []
    try:
        current_fd = os.open(root, flags)
        opened.append(current_fd)
        for part in relative.parts[:-1]:
            current_fd = os.open(part, flags, dir_fd=current_fd)
            opened.append(current_fd)

        leaf = relative.parts[-1]
        metadata = os.stat(leaf, dir_fd=current_fd, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise CandidateError("orphan candidate staging is not a trusted directory")
        shutil.rmtree(leaf, dir_fd=current_fd)
    except CandidateError:
        raise
    except OSError as exc:
        raise CandidateError("orphan candidate staging cannot be safely removed") from exc
    finally:
        for fd in reversed(opened):
            try:
                os.close(fd)
            except OSError:
                pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_chain(_candidate_root(), path.parent)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _receipt_destination(owner: str, repo: str, branch: str) -> dict[str, str]:
    return {
        "owner": validate_repo_owner_or_name(owner, label="owner"),
        "repo": validate_repo_owner_or_name(repo, label="repo"),
        "branch": _validate_branch(branch),
    }


def _validate_receipt_shape(receipt: dict[str, Any]) -> None:
    if receipt.get("version") != RECEIPT_VERSION:
        raise CandidateError("candidate receipt version is invalid")
    for name in (
        "project",
        "task_id",
        "attempt_id",
        "fingerprint",
        "job_id",
        "base_head",
        "implementation_diff_sha256",
        "delivery_contract_sha256",
        "candidate_head_sha",
        "created_at",
    ):
        if not isinstance(receipt.get(name), str) or not receipt[name]:
            raise CandidateError(f"candidate receipt is missing {name}")
    _validate_sha1(receipt["base_head"], label="receipt base_head")
    _validate_sha1(receipt["candidate_head_sha"], label="receipt candidate_head_sha")
    if not _SHA256_RE.fullmatch(receipt["implementation_diff_sha256"]):
        raise CandidateError("candidate receipt diff digest is invalid")
    if not _SHA256_RE.fullmatch(receipt["delivery_contract_sha256"]):
        raise CandidateError("candidate receipt delivery contract digest is invalid")
    destination = receipt.get("destination")
    if not isinstance(destination, dict):
        raise CandidateError("candidate receipt destination is invalid")
    _receipt_destination(
        str(destination.get("owner") or ""),
        str(destination.get("repo") or ""),
        str(destination.get("branch") or ""),
    )


def _changed_candidate_paths(repo: Path, base_head: str, candidate_head: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", base_head, candidate_head, "--"],
            cwd=repo,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateError("candidate scope diff did not complete") from exc
    if result.returncode != 0:
        raise CandidateError("candidate scope diff failed")
    return [part.decode("utf-8", "surrogateescape") for part in result.stdout.split(b"\0") if part]


def _enforce_candidate_scope(
    repo: Path,
    *,
    base_head: str,
    candidate_head: str,
    allowed_files: list[str],
    forbidden_files: list[str],
) -> None:
    allowed = [_compile_scope_glob(pattern) for pattern in allowed_files]
    forbidden = [(pattern, _compile_scope_glob(pattern)) for pattern in forbidden_files]
    for path in _changed_candidate_paths(repo, base_head, candidate_head):
        if not any(regex.fullmatch(path) for regex in allowed):
            raise CandidateError(f"candidate changes file outside immutable allowed scope: {path}")
        for pattern, regex in forbidden:
            if regex.fullmatch(path):
                raise CandidateError(f"candidate changes forbidden file for pattern {pattern!r}")


def _make_verifier_readable(root: Path) -> None:
    """Make a temporary candidate tree readable by the isolated verifier UID."""
    try:
        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            if not current_path.is_symlink():
                current_path.chmod(current_path.stat().st_mode | 0o055)
            for dirname in dirnames:
                path = current_path / dirname
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o055)
            for filename in filenames:
                path = current_path / filename
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o044)
    except OSError as exc:
        raise CandidateError("candidate staging cannot be exposed read-only to verifier") from exc


def _require_terminal_job(
    job_id: str, job_result: Callable[[str], dict[str, Any]] | None
) -> tuple[str, int | None]:
    """Require an authoritative terminal job, but do not trust worker success.

    A failed/cancelled agent can still leave a useful supervisor-owned diff.
    Once the gateway proves the job is terminal those bytes are stable enough
    for architect approval by digest.  Candidate materialization then rebuilds
    from the immutable BASE_HEAD, enforces the immutable file scope, and runs
    the required checks again in the isolated verifier.  Worker exit zero is
    therefore not a trust prerequisite; terminality is.
    """
    if job_result is None:
        raise CandidateError("authoritative job result verifier is required")
    try:
        result = job_result(job_id)
    except Exception as exc:
        raise CandidateError("authoritative agent job result is unavailable") from exc
    if not isinstance(result, dict):
        raise CandidateError("authoritative agent job result is invalid")
    status = str(result.get("status") or "").strip().lower()
    if status not in {"completed", "failed", "cancelled"}:
        raise CandidateError("agent job is not terminal")
    exit_code_raw = result.get("exit_code")
    if exit_code_raw is not None and not isinstance(exit_code_raw, int):
        raise CandidateError("authoritative agent job exit_code is invalid")
    return status, exit_code_raw


def _materialize_task_candidate_unlocked(
    *,
    project_root: str | Path,
    project: str,
    task_id: str,
    destination_owner: str,
    destination_repo: str,
    destination_branch: str,
    expected_diff_sha256: str,
    job_result: Callable[[str], dict[str, Any]] | None,
    verify_candidate: Callable[[Path, str, list[str]], None] | None,
) -> dict[str, Any]:
    """Materialize one persistent candidate and atomically bind its receipt."""
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise CandidateError("registered project root is unavailable")
    evidence = _load_evidence(root, project, task_id)
    approved_diff = _validate_sha256(
        expected_diff_sha256, label="expected_diff_sha256"
    )
    if evidence["implementation_diff_sha256"] != approved_diff:
        raise CandidateError("implementation diff changed since architect approval")
    job_terminal_status, job_exit_code = _require_terminal_job(
        evidence["job_id"], job_result
    )
    destination = _receipt_destination(
        destination_owner, destination_repo, destination_branch
    )
    record_dir = _candidate_record_dir(project, task_id, evidence["attempt_id"])
    receipt_path = record_dir / RECEIPT_FILENAME
    staging = _staging_repo(record_dir)

    if receipt_path.exists():
        existing = _read_json(receipt_path)
        _validate_receipt_shape(existing)
        validate_task_candidate_for_push(
            project_root=root,
            project=project,
            task_id=task_id,
            destination_owner=destination["owner"],
            destination_repo=destination["repo"],
            destination_branch=destination["branch"],
            expected_sha=existing["candidate_head_sha"],
        )
        return existing
    if staging.is_symlink():
        raise CandidateError("orphan candidate staging exists without a trusted receipt")
    if staging.exists() and not staging.is_dir():
        raise CandidateError("orphan candidate staging exists without a trusted receipt")
    if staging.is_dir():
        _remove_candidate_tree(staging)

    staging.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_chain(_candidate_root(), staging.parent)
    tmp_parent = staging.parent
    tmp = Path(tempfile.mkdtemp(prefix=".materialize-", dir=tmp_parent))
    # The isolated verifier mounts candidate storage read-only under a
    # different executor UID. tempfile.mkdtemp() is 0700 by default, which
    # would make the otherwise read-only candidate unreachable. Expose only
    # path traversal/readability; the verifier mount itself remains :ro.
    os.chmod(tmp, 0o755)
    repo_tmp = tmp / "repo"
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        bundle_path = _managed_source_bundle(project, evidence["base_head"])
        if bundle_path is not None:
            _run_git(
                tmp,
                ["clone", "--no-checkout", str(bundle_path), str(repo_tmp)],
                env=clean_env,
            )
        else:
            _run_git(
                root,
                [
                    "clone",
                    "--local",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(root),
                    str(repo_tmp),
                ],
                env=clean_env,
            )
        _run_git(
            repo_tmp,
            ["checkout", "--detach", "--quiet", evidence["base_head"]],
            env=clean_env,
        )
        commit_date = _run_git(
            repo_tmp,
            ["show", "-s", "--format=%cI", evidence["base_head"]],
            env=clean_env,
        )
        if not commit_date:
            raise CandidateError("candidate base commit date is unavailable")
        commit_env = dict(clean_env)
        commit_env.update(
            {
                "GIT_AUTHOR_NAME": "MCP Control Plane",
                "GIT_AUTHOR_EMAIL": "control-plane@gateway.invalid",
                "GIT_COMMITTER_NAME": "MCP Control Plane",
                "GIT_COMMITTER_EMAIL": "control-plane@gateway.invalid",
                "GIT_AUTHOR_DATE": commit_date,
                "GIT_COMMITTER_DATE": commit_date,
            }
        )
        patch_path = tmp / "implementation-diff.patch"
        patch_path.write_bytes(evidence["implementation_diff"])
        _run_git(repo_tmp, ["apply", "--binary", str(patch_path)], env=clean_env)
        _run_git(repo_tmp, ["add", "-A", "--", "."], env=clean_env)
        _run_git(repo_tmp, ["commit", "--quiet", "--no-gpg-sign", "-m", f"task candidate {project}/{task_id}"], env=commit_env)
        candidate_head = _validate_sha1(
            _run_git(repo_tmp, ["rev-parse", "HEAD"], env=clean_env),
            label="candidate_head_sha",
        )
        _enforce_candidate_scope(
            repo_tmp,
            base_head=evidence["base_head"],
            candidate_head=candidate_head,
            allowed_files=evidence["allowed_files"],
            forbidden_files=evidence["forbidden_files"],
        )
        if verify_candidate is None:
            raise CandidateError("isolated candidate verifier is required")
        _make_verifier_readable(tmp)
        try:
            verify_candidate(repo_tmp, candidate_head, evidence["required_checks"])
        except CandidateError:
            raise
        except Exception as exc:
            raise CandidateError("isolated candidate verification failed") from exc
        os.replace(repo_tmp, staging)
        receipt = {
            "version": RECEIPT_VERSION,
            "project": project,
            "task_id": task_id,
            "attempt_id": evidence["attempt_id"],
            "fingerprint": evidence["fingerprint"],
            "job_id": evidence["job_id"],
            "job_terminal_status": job_terminal_status,
            "job_exit_code": job_exit_code,
            "base_head": evidence["base_head"],
            "implementation_diff_sha256": evidence["implementation_diff_sha256"],
            "delivery_contract_sha256": evidence["delivery_contract_sha256"],
            "candidate_head_sha": candidate_head,
            "destination": destination,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _validate_receipt_shape(receipt)
        _atomic_write_json(receipt_path, receipt)
        return receipt
    finally:
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass


def materialize_task_candidate(
    *,
    project_root: str | Path,
    project: str,
    task_id: str,
    destination_owner: str,
    destination_repo: str,
    destination_branch: str,
    expected_diff_sha256: str,
    job_result: Callable[[str], dict[str, Any]] | None = None,
    verify_candidate: Callable[[Path, str, list[str]], None] | None = None,
) -> dict[str, Any]:
    """Serialize materialization and publish one receipt-bound candidate."""
    with _candidate_root_lock():
        return _materialize_task_candidate_unlocked(
            project_root=project_root,
            project=project,
            task_id=task_id,
            destination_owner=destination_owner,
            destination_repo=destination_repo,
            destination_branch=destination_branch,
            expected_diff_sha256=expected_diff_sha256,
            job_result=job_result,
            verify_candidate=verify_candidate,
        )


def validate_task_candidate_for_push(
    *,
    project_root: str | Path,
    project: str,
    task_id: str,
    destination_owner: str,
    destination_repo: str,
    destination_branch: str,
    expected_sha: str,
) -> tuple[dict[str, Any], Path]:
    """Validate current evidence against receipt and return its exact staging repo."""
    root = Path(project_root).resolve()
    evidence = _load_evidence(root, project, task_id)
    record_dir = _candidate_record_dir(project, task_id, evidence["attempt_id"])
    receipt = _read_json(record_dir / RECEIPT_FILENAME)
    _validate_receipt_shape(receipt)
    expected = _validate_sha1(expected_sha, label="expected_sha")
    destination = _receipt_destination(
        destination_owner, destination_repo, destination_branch
    )
    if receipt["project"] != project or receipt["task_id"] != task_id:
        raise CandidateError("candidate receipt project/task binding mismatch")
    if (
        receipt["attempt_id"] != evidence["attempt_id"]
        or receipt["fingerprint"] != evidence["fingerprint"]
        or receipt["job_id"] != evidence["job_id"]
    ):
        raise CandidateError("candidate receipt attempt binding mismatch")
    if receipt["base_head"] != evidence["base_head"]:
        raise CandidateError("candidate receipt BASE_HEAD changed")
    if receipt["implementation_diff_sha256"] != evidence["implementation_diff_sha256"]:
        raise CandidateError("implementation diff changed after candidate receipt")
    if receipt["delivery_contract_sha256"] != evidence["delivery_contract_sha256"]:
        raise CandidateError("delivery contract changed after candidate receipt")
    if receipt["destination"] != destination:
        raise CandidateError("candidate receipt destination binding mismatch")
    if receipt["candidate_head_sha"] != expected:
        raise CandidateError("expected_sha does not match trusted candidate receipt")

    staging = _staging_repo(record_dir)
    _assert_no_symlink_chain(_candidate_root(), staging)
    if not staging.is_dir():
        raise CandidateError("trusted candidate staging repository is unavailable")
    resolved = _run_git(staging, ["rev-parse", "--verify", f"{expected}^{{commit}}"])
    if resolved.lower() != expected:
        raise CandidateError("trusted staging repository does not contain exact candidate")
    head = _run_git(staging, ["rev-parse", "HEAD"])
    if head.lower() != expected:
        raise CandidateError("trusted staging HEAD does not match candidate receipt")
    return receipt, staging
