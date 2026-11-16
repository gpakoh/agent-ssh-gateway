"""Explicit, audited archival of a terminal legacy candidate handoff.

The core does NOT claim terminal task, delivery or lease state itself. A trusted
adapter must supply a guard which proves those facts on every invocation. The
legacy plan and status bytes are durably archived outside the clone before they
are removed; ordinary candidate_cleanup remains fail-closed by default.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from examples.mcp_server import candidate_clone as clone
from examples.mcp_server.project_registry_control import project_registry_mutation_lock

_MAX_TOTAL_BYTES = 42_000
_MAX_SINGLE_BYTES = 36_000
_SHA_RE = re.compile(r"[0-9a-f]{64}")


def _reject(message: str, *, code: str = "WORKSPACE_CONTENDED") -> NoReturn:
    raise clone.CandidateCloneError(code, message)


def _snapshot(fd: int, name: str) -> tuple[bytes, os.stat_result] | None:
    """Read a bounded owned regular file via a nofollow directory descriptor."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        handle = os.open(name, flags, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError:
        _reject("legacy handoff evidence cannot be opened safely")
    try:
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _reject("legacy handoff evidence is not an owned regular file")
        if before.st_size > _MAX_SINGLE_BYTES:
            _reject("legacy handoff evidence exceeds archive bound")
        parts: list[bytes] = []
        size = 0
        while size <= _MAX_SINGLE_BYTES:
            chunk = os.read(handle, min(65536, _MAX_SINGLE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            parts.append(chunk)
        after = os.fstat(handle)
        if (
            size != before.st_size
            or size > _MAX_SINGLE_BYTES
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            _reject("legacy handoff evidence changed during snapshot")
        return b"".join(parts), before
    finally:
        os.close(handle)


def _require_exact_entry(fd: int, name: str, data: bytes, original: os.stat_result) -> None:
    """Prove no replacement or content change just before unlinkat."""
    try:
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except OSError:
        _reject("legacy handoff evidence disappeared before archival")
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        != (
            original.st_dev,
            original.st_ino,
            original.st_size,
            original.st_mtime_ns,
            original.st_ctime_ns,
        )
    ):
        _reject("legacy handoff evidence was replaced before archival")
    probe = _snapshot(fd, name)
    if probe is None or probe[0] != data:
        _reject("legacy handoff evidence bytes changed before archival")


def _check_layout(fd: int) -> None:
    """No other ignored work or active/archived task may be discarded."""
    with os.scandir(fd) as entries:
        for entry in entries:
            if entry.name in {"current-plan.md", "agent-status.md"}:
                if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    _reject("legacy handoff entry is unsafe")
            elif entry.name in {"tasks", "archive"}:
                if not entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                    _reject("legacy task directory is unsafe")
                # Empty task roots are expected. All populated roots remain blockers.
                dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                task_fd = os.open(entry.name, dir_flags, dir_fd=fd)
                try:
                    with os.scandir(task_fd) as children:
                        if next(children, None) is not None:
                            _reject("legacy candidate still has task or archive entries")
                finally:
                    os.close(task_fd)
            else:
                _reject("legacy handoff has unclassified evidence")


def _archive_identity(
    *,
    project_id: str,
    expected_head_sha: str,
    expected_branch: str,
    expected_source_project: str,
    preserved_ref: str,
    expected_plan_sha256: str,
) -> dict[str, str | int]:
    return {
        "version": 1,
        "project_id": project_id,
        "head": expected_head_sha,
        "branch": expected_branch,
        "source_project": expected_source_project,
        "preserved_ref": preserved_ref,
        "plan_sha256": expected_plan_sha256,
    }


def archive_terminal_legacy_handoff(
    project_id: str,
    expected_head_sha: str,
    expected_branch: str,
    expected_source_project: str,
    preserved_ref: str,
    expected_plan_sha256: str,
    reason: str,
    *,
    config_dir: Path,
    journal_root: Path,
    terminal_guard: Callable[[], None],
) -> dict[str, Any]:
    """Durably preserve two ignored files after authoritative terminal proof.

    This never changes candidate Git objects, source branches, or the registry.
    It is NOT a substitute for candidate_cleanup's separate remote CAS guards.
    """
    if not isinstance(project_id, str) or not clone._PROJECT_ID_RE.fullmatch(project_id):
        _reject("invalid candidate identity", code="INVALID_INPUT")
    if not isinstance(expected_head_sha, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", expected_head_sha
    ):
        _reject("invalid full commit SHA", code="INVALID_INPUT")
    expected_head_sha = expected_head_sha.lower()
    expected_branch = clone._validate_branch(expected_branch)
    if not isinstance(expected_source_project, str) or not expected_source_project.strip():
        _reject("source project is required", code="INVALID_INPUT")
    expected_source_project = expected_source_project.strip()
    validated_ref = clone._validate_ref(preserved_ref)
    if validated_ref is None or not validated_ref.startswith("archive/candidate-"):
        _reject("explicit archive/candidate-* preservation ref required", code="INVALID_INPUT")
    preserved_ref = validated_ref
    if not isinstance(expected_plan_sha256, str) or not _SHA_RE.fullmatch(expected_plan_sha256):
        _reject("expected_plan_sha256 must be lowercase SHA-256", code="INVALID_INPUT")
    if not isinstance(reason, str) or not (1 <= len(reason.strip()) <= 500) or "\x00" in reason:
        _reject("explicit bounded terminal justification required", code="INVALID_INPUT")
    if not callable(terminal_guard):
        _reject("trusted terminal proof callback is required", code="INVALID_INPUT")
    reason = reason.strip()

    config_dir, journal_root = config_dir.resolve(), journal_root.resolve()
    workspace = clone._workspace_root(config_dir)
    source = clone._source_root(config_dir, expected_source_project, workspace_root=workspace)
    candidates_root = clone._candidate_clones_root(workspace)
    candidate = candidates_root / project_id
    identity = _archive_identity(
        project_id=project_id,
        expected_head_sha=expected_head_sha,
        expected_branch=expected_branch,
        expected_source_project=expected_source_project,
        preserved_ref=preserved_ref,
        expected_plan_sha256=expected_plan_sha256,
    )
    tombstone, _ = clone._cleanup_tombstone_path(journal_root, project_id)
    archive = tombstone.with_name(tombstone.stem + ".legacy-handoff.json")

    with clone._lineage_lock(workspace, expected_source_project, expected_branch):
        with project_registry_mutation_lock(journal_root):
            clone._registry_has_candidate(
                config_dir,
                project_id,
                expected_root=f".mcp-candidate-clones/{project_id}",
            )
            clone._require_preserved_head(
                source, preserved_ref, expected_head_sha, context="before legacy handoff archive"
            )
            clone._require_delivery_branch_absent(
                source, expected_branch, context="before legacy handoff archive"
            )
            clone._validate_candidate_root(candidate, candidates_root)
            clone._reject_symlinked_git_dir(candidate)
            metadata = clone._read_metadata(candidate)
            for key, value in {
                "project_id": project_id,
                "source_project": expected_source_project,
                "branch": expected_branch,
                "root": f".mcp-candidate-clones/{project_id}",
            }.items():
                if metadata.get(key) != value:
                    _reject("legacy candidate metadata identity mismatch")
            dirty, _status_sha, _count = clone._status_state(candidate)
            current_branch = clone._run_git(
                candidate,
                ["rev-parse", "--abbrev-ref", "HEAD"],
                operation="read legacy candidate branch",
            )
            current_head = clone._run_git(
                candidate, ["rev-parse", "HEAD"], operation="read legacy candidate head"
            )
            if dirty or current_branch != expected_branch or current_head != expected_head_sha:
                _reject("legacy candidate Git state differs")
            clone._require_no_inbound_candidate_dependencies(candidates_root, candidate)

            bridge = candidate / ".ai-bridge"
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                bridge_fd = os.open(bridge, flags)
            except OSError:
                _reject("legacy handoff root must be a real directory")
            try:
                _check_layout(bridge_fd)
                plan = _snapshot(bridge_fd, "current-plan.md")
                status = _snapshot(bridge_fd, "agent-status.md")
                old = clone._read_cleanup_tombstone(archive)
                if old is not None:
                    if any(old.get(k) != v for k, v in identity.items()):
                        _reject("legacy archive identity changed")
                    if old.get("version") != 1 or old.get("phase") != "archived":
                        _reject("legacy archive receipt is malformed")
                    for key in ("plan", "status"):
                        raw = old.get(key + "_base64")
                        digest = old.get(key + "_sha256")
                        if not isinstance(raw, str) or not isinstance(digest, str):
                            _reject("legacy archive receipt is incomplete")
                        try:
                            blob = base64.b64decode(raw, validate=True)
                        except Exception:
                            _reject("legacy archive bytes are invalid")
                        if hashlib.sha256(blob).hexdigest() != digest:
                            _reject("legacy archive bytes were modified")
                if plan is None:
                    if old is None:
                        _reject("legacy plan is absent without a terminal archive receipt")
                    terminal_guard()
                    if status is not None:
                        dangling_data, dangling_stat = status
                        if old["status_base64"] != base64.b64encode(dangling_data).decode("ascii"):
                            _reject("legacy status differs from its durable archive")
                        _require_exact_entry(
                            bridge_fd, "agent-status.md", dangling_data, dangling_stat
                        )
                        os.unlink("agent-status.md", dir_fd=bridge_fd)
                        os.fsync(bridge_fd)
                    return {
                        "outcome": "already_archived",
                        "archive_name": archive.name,
                        "plan_sha256": expected_plan_sha256,
                        "archived_status_sha256": old["status_sha256"],
                    }
                plan_data, plan_stat = plan
                if hashlib.sha256(plan_data).hexdigest() != expected_plan_sha256:
                    _reject("legacy plan content fingerprint mismatch", code="CHECK_FAILED")
                status_data, status_stat = status if status is not None else (b"", None)
                if len(plan_data) + len(status_data) > _MAX_TOTAL_BYTES:
                    _reject("legacy handoff exceeds bounded durable archive")
                terminal_guard()
                if old is None:
                    record = {
                        **identity,
                        "phase": "archived",
                        "reason": reason,
                        "plan_base64": base64.b64encode(plan_data).decode("ascii"),
                        "status_base64": base64.b64encode(status_data).decode("ascii"),
                        "status_sha256": hashlib.sha256(status_data).hexdigest(),
                    }
                    clone._write_cleanup_tombstone(archive, record)
                else:
                    if old["plan_base64"] != base64.b64encode(plan_data).decode("ascii") or (
                        status is not None
                        and old["status_base64"] != base64.b64encode(status_data).decode("ascii")
                    ):
                        _reject("legacy evidence differs from durable archive")
                # Re-establish terminal, remote, journal and Git identity before unlink.
                terminal_guard()
                clone._require_preserved_head(
                    source, preserved_ref, expected_head_sha, context="before handoff unlink"
                )
                clone._require_delivery_branch_absent(
                    source, expected_branch, context="before handoff unlink"
                )
                if clone._read_cleanup_tombstone(archive) != (record if old is None else old):
                    _reject("durable legacy archive changed before unlink")
                if clone._status_state(candidate)[0]:
                    _reject("candidate became dirty before legacy handoff unlink")
                if (
                    clone._run_git(
                        candidate, ["rev-parse", "HEAD"], operation="recheck legacy head"
                    )
                    != expected_head_sha
                ):
                    _reject("candidate HEAD changed before legacy handoff unlink")
                _check_layout(bridge_fd)
                _require_exact_entry(bridge_fd, "current-plan.md", plan_data, plan_stat)
                if status_stat is not None:
                    _require_exact_entry(bridge_fd, "agent-status.md", status_data, status_stat)
                os.unlink("current-plan.md", dir_fd=bridge_fd)
                if status_stat is not None:
                    os.unlink("agent-status.md", dir_fd=bridge_fd)
                os.fsync(bridge_fd)
                return {
                    "outcome": "archived",
                    "archive_name": archive.name,
                    "plan_sha256": expected_plan_sha256,
                    "archived_status_sha256": (
                        old["status_sha256"]
                        if old is not None
                        else hashlib.sha256(status_data).hexdigest()
                    ),
                }
            finally:
                os.close(bridge_fd)
