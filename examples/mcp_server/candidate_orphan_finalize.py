"""Fail-closed finalization of delivered candidate tails that lost Git metadata.

This recovery path is intentionally separate from ``candidate_cleanup``. It is
only for historical candidate directories whose registry entry, cleanup
transaction, and ``.git`` directory are already gone. The directory name must
still encode the exact source/branch/base identity, the base commit must be an
ancestor of the trusted remote default head, and every surviving filesystem
entry must be byte-for-byte compatible with that delivered base tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.workspace.registry import WorkspacePolicyError, load_registry_roots
from examples.mcp_server.agent_sources import ManagedSourceBundleError, ensure_managed_source_bundle
from examples.mcp_server.candidate_clone import (
    CandidateCloneError,
    RemoteRefStatus,
    _candidate_clones_root,
    _candidate_has_active_evidence,
    _fail,
    _lineage_lock,
    _probe_remote_default_head,
    _project_id,
    _registry_has_candidate,
    _registry_store_paths,
    _require_delivery_branch_absent,
    _source_root,
    _validate_branch,
    _validate_candidate_root,
    _validate_full_sha,
    _workspace_root,
)
from examples.mcp_server.project_registry_control import project_registry_mutation_lock

_ORPHAN_JOURNAL_DIR = "candidate-orphan-finalize"
_ORPHAN_TOMBSTONE_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class CandidateOrphanFinalizeReceipt:
    project_id: str
    source_project: str
    branch: str
    base_sha: str
    remote_default_branch: str
    remote_default_head: str
    entries_verified: int
    directory_removed: bool
    already_finalized: bool
    tombstone_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_project": self.source_project,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "remote_default_branch": self.remote_default_branch,
            "remote_default_head": self.remote_default_head,
            "entries_verified": self.entries_verified,
            "directory_removed": self.directory_removed,
            "already_finalized": self.already_finalized,
            "tombstone_id": self.tombstone_id,
        }


def _orphan_tombstone_path(journal_root: Path, project_id: str) -> tuple[Path, str]:
    tombstone_id = hashlib.sha256(f"candidate-orphan-finalize\0{project_id}".encode()).hexdigest()
    return journal_root / _ORPHAN_JOURNAL_DIR / f"{tombstone_id}.json", tombstone_id


@contextlib.contextmanager
def _orphan_journal_dir(path: Path, *, create: bool) -> Iterator[int | None]:
    journal_root = path.parent.parent
    if path.parent.name != _ORPHAN_JOURNAL_DIR:
        raise _fail("POLICY_DENIED", "candidate orphan journal path is outside its journal area")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    odirectory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or odirectory is None:
        raise _fail("POLICY_DENIED", "secure candidate orphan journal access is unavailable")
    flags = os.O_RDONLY | nofollow | odirectory | getattr(os, "O_CLOEXEC", 0)
    if create:
        try:
            journal_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _fail("TOOL_EXECUTION_FAILED", "candidate orphan journal root could not be prepared") from exc
    elif not journal_root.exists():
        yield None
        return

    root_fd: int | None = None
    orphan_fd: int | None = None
    try:
        root_fd = os.open(journal_root, flags)
        if create:
            try:
                os.mkdir(_ORPHAN_JOURNAL_DIR, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        try:
            orphan_fd = os.open(_ORPHAN_JOURNAL_DIR, flags, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                yield None
                return
            raise
        metadata = os.fstat(orphan_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise _fail("WORKSPACE_CONTENDED", "candidate orphan journal directory is invalid")
        yield orphan_fd
    except CandidateCloneError:
        raise
    except OSError as exc:
        raise _fail("WORKSPACE_CONTENDED", "candidate orphan journal directory is unsafe or unavailable") from exc
    finally:
        if orphan_fd is not None:
            os.close(orphan_fd)
        if root_fd is not None:
            os.close(root_fd)


def _read_orphan_tombstone(path: Path) -> dict[str, Any] | None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise _fail("POLICY_DENIED", "secure candidate orphan tombstone reads are unavailable")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    with _orphan_journal_dir(path, create=False) as orphan_fd:
        if orphan_fd is None:
            return None
        try:
            fd = os.open(path.name, flags, dir_fd=orphan_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _fail("WORKSPACE_CONTENDED", "candidate orphan tombstone is unsafe") from exc
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > _ORPHAN_TOMBSTONE_MAX_BYTES
            ):
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan tombstone is invalid")
            raw = os.read(fd, _ORPHAN_TOMBSTONE_MAX_BYTES + 1)
            if len(raw) > _ORPHAN_TOMBSTONE_MAX_BYTES:
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan tombstone is invalid")
        finally:
            os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("WORKSPACE_CONTENDED", "candidate orphan tombstone is invalid") from exc
    if not isinstance(data, dict):
        raise _fail("WORKSPACE_CONTENDED", "candidate orphan tombstone is invalid")
    return data


def _write_orphan_tombstone(path: Path, data: dict[str, Any]) -> None:
    encoded = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > _ORPHAN_TOMBSTONE_MAX_BYTES:
        raise _fail("TOOL_EXECUTION_FAILED", "candidate orphan tombstone is too large")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise _fail("POLICY_DENIED", "secure candidate orphan tombstone writes are unavailable")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0)
    with _orphan_journal_dir(path, create=True) as orphan_fd:
        assert orphan_fd is not None
        tmp_name: str | None = None
        tmp_fd: int | None = None
        try:
            for counter in range(32):
                candidate = f".{path.name}.{os.getpid()}.{counter}.tmp"
                try:
                    tmp_fd = os.open(candidate, flags, 0o600, dir_fd=orphan_fd)
                except FileExistsError:
                    continue
                tmp_name = candidate
                break
            if tmp_fd is None or tmp_name is None:
                raise _fail("TOOL_EXECUTION_FAILED", "candidate orphan tombstone temporary file could not be allocated")
            view = memoryview(encoded)
            while view:
                written = os.write(tmp_fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = None
            os.replace(tmp_name, path.name, src_dir_fd=orphan_fd, dst_dir_fd=orphan_fd)
            tmp_name = None
            os.fsync(orphan_fd)
        except CandidateCloneError:
            raise
        except OSError as exc:
            raise _fail("TOOL_EXECUTION_FAILED", "candidate orphan tombstone could not be persisted") from exc
        finally:
            if tmp_fd is not None:
                os.close(tmp_fd)
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name, dir_fd=orphan_fd)
                except OSError:
                    pass


def _git_bytes(repo: Path, args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo.resolve()}", *args],
            cwd=str(repo),
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail("CHECK_FAILED", "delivered candidate tree could not be inspected", retryable=True) from exc
    if result.returncode != 0:
        raise _fail(
            "CHECK_FAILED",
            "delivered candidate tree could not be inspected",
            retryable=True,
            details={"git_operation": args[0] if args else "unknown"},
        )
    return result.stdout


def _git_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo.resolve()}", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(repo),
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail("CHECK_FAILED", "delivered candidate ancestry could not be verified", retryable=True) from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise _fail("CHECK_FAILED", "delivered candidate ancestry could not be verified", retryable=True)


def _git_blob_oid(path: Path, size: int) -> str:
    digest = hashlib.sha1()  # Git repositories addressed by 40-character SHAs use SHA-1 objects.
    digest.update(f"blob {size}\0".encode())
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise _fail("WORKSPACE_CONTENDED", "candidate orphan file could not be read safely", retryable=True) from exc
    return digest.hexdigest()


def _tree_manifest(repo: Path, base_sha: str) -> dict[str, tuple[str, str, str]]:
    raw = _git_bytes(repo, ["ls-tree", "-r", "-t", "-z", "--full-tree", base_sha])
    manifest: dict[str, tuple[str, str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            meta, path_raw = record.split(b"\t", 1)
            mode_raw, kind_raw, oid_raw = meta.split(b" ", 2)
            path = path_raw.decode("utf-8", "surrogateescape")
            mode = mode_raw.decode("ascii")
            kind = kind_raw.decode("ascii")
            oid = oid_raw.decode("ascii").lower()
        except (ValueError, UnicodeError) as exc:
            raise _fail("CHECK_FAILED", "delivered candidate tree manifest is malformed") from exc
        manifest[path] = (mode, kind, oid)
    return manifest


def _verify_subset(candidate_root: Path, reference_repo: Path, base_sha: str) -> int:
    manifest = _tree_manifest(reference_repo, base_sha)
    verified = 0

    def _walk(directory: Path, prefix: str) -> None:
        nonlocal verified
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise _fail("WORKSPACE_CONTENDED", "candidate orphan tree could not be scanned", retryable=True) from exc
        for entry in entries:
            rel = f"{prefix}/{entry.name}" if prefix else entry.name
            expected = manifest.get(rel)
            if expected is None:
                raise _fail(
                    "WORKSPACE_CONTENDED",
                    "candidate orphan contains content not present in the delivered base tree",
                    details={"path": rel},
                )
            mode, kind, oid = expected
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan entry could not be inspected", retryable=True) from exc
            if stat.S_ISDIR(metadata.st_mode):
                if kind != "tree":
                    raise _fail("WORKSPACE_CONTENDED", "candidate orphan entry type differs from delivered base", details={"path": rel})
                verified += 1
                _walk(Path(entry.path), rel)
                continue
            if stat.S_ISLNK(metadata.st_mode):
                if kind != "blob" or mode != "120000":
                    raise _fail("WORKSPACE_CONTENDED", "candidate orphan symlink differs from delivered base", details={"path": rel})
                try:
                    target = os.fsencode(os.readlink(entry.path))
                except OSError as exc:
                    raise _fail("WORKSPACE_CONTENDED", "candidate orphan symlink could not be read", retryable=True) from exc
                digest = hashlib.sha1(f"blob {len(target)}\0".encode() + target).hexdigest()
                if digest != oid:
                    raise _fail("WORKSPACE_CONTENDED", "candidate orphan symlink content differs from delivered base", details={"path": rel})
                verified += 1
                continue
            if stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1 or kind != "blob" or mode not in {"100644", "100755"}:
                    raise _fail("WORKSPACE_CONTENDED", "candidate orphan file type is unsafe or differs from delivered base", details={"path": rel})
                executable = bool(metadata.st_mode & 0o111)
                if executable != (mode == "100755") or _git_blob_oid(Path(entry.path), metadata.st_size) != oid:
                    raise _fail("WORKSPACE_CONTENDED", "candidate orphan file content differs from delivered base", details={"path": rel})
                verified += 1
                continue
            raise _fail("WORKSPACE_CONTENDED", "candidate orphan contains an unsupported filesystem entry", details={"path": rel})

    _walk(candidate_root, "")
    return verified


def _path_is_at_or_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_no_registered_descendants(config_dir: Path, candidate_root: Path) -> None:
    """Refuse removal when any registry store still points inside the orphan."""
    source_registry = config_dir / "projects.yaml"
    try:
        roots = load_registry_roots(source_registry)
        target_lexical = Path(os.path.abspath(os.fspath(candidate_root)))
        target_resolved = candidate_root.resolve(strict=True)
    except (OSError, WorkspacePolicyError) as exc:
        raise _fail(
            "TOOL_EXECUTION_FAILED",
            "workspace registry roots cannot be resolved for orphan finalization",
            retryable=True,
        ) from exc

    for registry_path in _registry_store_paths(config_dir):
        if not registry_path.exists():
            continue
        try:
            data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise _fail(
                "TOOL_EXECUTION_FAILED",
                "workspace registry cannot be read for orphan finalization",
                retryable=True,
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
            raise _fail(
                "WORKSPACE_CONTENDED",
                "workspace registry is malformed during orphan finalization",
                retryable=True,
            )

        for registered_id, entry in data["projects"].items():
            if not isinstance(registered_id, str) or not isinstance(entry, dict):
                raise _fail(
                    "WORKSPACE_CONTENDED",
                    "workspace registry contains an unsafe project entry",
                    retryable=True,
                )
            raw_root = entry.get("root")
            selector = entry.get("root_selector", "default")
            if (
                not isinstance(raw_root, str)
                or not raw_root.strip()
                or not isinstance(selector, str)
                or selector not in roots
            ):
                raise _fail(
                    "WORKSPACE_CONTENDED",
                    "workspace registry contains an unresolved project root",
                    retryable=True,
                    details={"registered_project_id": registered_id},
                )
            raw_path = Path(raw_root)
            project_path = raw_path if raw_path.is_absolute() else roots[selector] / raw_path
            project_lexical = Path(os.path.abspath(os.fspath(project_path)))
            try:
                project_resolved = project_lexical.resolve(strict=False)
            except OSError as exc:
                raise _fail(
                    "WORKSPACE_CONTENDED",
                    "registered project root cannot be resolved safely",
                    retryable=True,
                    details={"registered_project_id": registered_id},
                ) from exc
            if _path_is_at_or_below(project_lexical, target_lexical) or _path_is_at_or_below(
                project_resolved, target_resolved
            ):
                raise _fail(
                    "WORKSPACE_CONTENDED",
                    "candidate orphan contains a registered project root",
                    retryable=False,
                    details={"registered_project_id": registered_id},
                )


def _prove_delivered_subset(
    *,
    source_project: str,
    source_root: Path,
    candidate_root: Path,
    base_sha: str,
    expected_branch: str,
) -> tuple[str, str, int]:
    default = _probe_remote_default_head(source_root)
    if default.status is not RemoteRefStatus.FOUND or default.branch is None or default.sha is None:
        raise _fail("CHECK_FAILED", "trusted remote default state could not be verified", retryable=True)
    _require_delivery_branch_absent(source_root, expected_branch, context="orphan finalization")
    try:
        publication = ensure_managed_source_bundle(source_project, default.sha)
    except (ManagedSourceBundleError, ValueError) as exc:
        raise _fail("CHECK_FAILED", "trusted remote default source could not be materialized", retryable=True) from exc
    if publication is None:
        raise _fail("CHECK_FAILED", "managed source storage is required for orphan finalization", retryable=False)
    with tempfile.TemporaryDirectory(prefix="candidate-orphan-proof-") as scratch_raw:
        scratch = Path(scratch_raw)
        repo = scratch / "source"
        try:
            result = subprocess.run(
                ["git", "clone", "--quiet", "--no-hardlinks", publication.path, str(repo)],
                capture_output=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _fail("CHECK_FAILED", "trusted remote default source could not be inspected", retryable=True) from exc
        if result.returncode != 0:
            raise _fail("CHECK_FAILED", "trusted remote default source could not be inspected", retryable=True)
        resolved = (
            _git_bytes(repo, ["rev-parse", f"{default.sha}^{{commit}}"])
            .decode()
            .strip()
            .lower()
        )
        if resolved != default.sha:
            raise _fail("CHECK_FAILED", "managed source default head did not match trusted remote")
        if not _git_is_ancestor(repo, base_sha, default.sha):
            raise _fail(
                "CHECK_FAILED",
                "candidate base commit is not contained in the trusted remote default history",
                retryable=False,
            )
        entries = _verify_subset(candidate_root, repo, base_sha)
    return default.branch, default.sha, entries


def candidate_orphan_finalize(
    project_id: str,
    expected_base_sha: str,
    expected_branch: str,
    expected_source_project: str,
    *,
    config_dir: Path,
    journal_root: Path,
    reference_guard: Callable[[], None],
) -> CandidateOrphanFinalizeReceipt:
    """Remove one delivered, unregistered, Git-metadata-free candidate tail."""
    if not isinstance(project_id, str):
        raise _fail("INVALID_INPUT", "project_id must be a string")
    expected_base_sha = _validate_full_sha(expected_base_sha, field="expected_base_sha")
    expected_branch = _validate_branch(expected_branch)
    if not isinstance(expected_source_project, str) or not expected_source_project.strip():
        raise _fail("INVALID_INPUT", "expected_source_project must be non-empty")
    expected_source_project = expected_source_project.strip()
    expected_project_id = _project_id(expected_source_project, expected_branch, expected_base_sha)
    if project_id != expected_project_id:
        raise _fail(
            "POLICY_DENIED",
            "candidate orphan identity does not match source, branch, and base commit",
            details={"expected_project_id": expected_project_id},
        )
    if not callable(reference_guard):
        raise _fail("INVALID_INPUT", "reference_guard must be callable and is required")

    config_dir = config_dir.resolve()
    journal_root = journal_root.resolve()
    workspace_root = _workspace_root(config_dir)
    source_root = _source_root(config_dir, expected_source_project, workspace_root=workspace_root)
    candidates_root = _candidate_clones_root(workspace_root)
    candidate_root = candidates_root / project_id
    tombstone_path, tombstone_id = _orphan_tombstone_path(journal_root, project_id)
    identity = {
        "version": 1,
        "project_id": project_id,
        "source_project": expected_source_project,
        "branch": expected_branch,
        "base_sha": expected_base_sha,
    }

    with _lineage_lock(workspace_root, expected_source_project, expected_branch):
        with project_registry_mutation_lock(journal_root):
            tombstone = _read_orphan_tombstone(tombstone_path)
            if tombstone is not None:
                for key, value in identity.items():
                    if tombstone.get(key) != value:
                        raise _fail("WORKSPACE_CONTENDED", "candidate orphan tombstone identity mismatch")
                if tombstone.get("phase") not in {"prepared", "complete"}:
                    raise _fail("WORKSPACE_CONTENDED", "candidate orphan tombstone phase is invalid")

            exists = candidate_root.exists() or candidate_root.is_symlink()
            if not exists:
                if tombstone is not None and not _registry_has_candidate(config_dir, project_id):
                    if tombstone.get("phase") == "prepared":
                        completed = dict(tombstone)
                        completed["phase"] = "complete"
                        _write_orphan_tombstone(tombstone_path, completed)
                        tombstone = completed
                    if tombstone.get("phase") == "complete":
                        return CandidateOrphanFinalizeReceipt(
                            project_id=project_id,
                            source_project=expected_source_project,
                            branch=expected_branch,
                            base_sha=expected_base_sha,
                            remote_default_branch=str(tombstone.get("remote_default_branch") or ""),
                            remote_default_head=str(tombstone.get("remote_default_head") or ""),
                            entries_verified=int(tombstone.get("entries_verified") or 0),
                            directory_removed=False,
                            already_finalized=True,
                            tombstone_id=tombstone_id,
                        )
                raise _fail("PROJECT_NOT_FOUND", "candidate orphan directory does not exist")

            root_dev, root_ino = _validate_candidate_root(candidate_root, candidates_root)
            git_dir = candidate_root / ".git"
            try:
                git_dir.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan Git state cannot be inspected", retryable=True) from exc
            else:
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan still has Git metadata; use normal candidate cleanup")
            if _registry_has_candidate(config_dir, project_id):
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan unexpectedly exists in the workspace registry")
            if _candidate_has_active_evidence(candidate_root):
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan still has active task or delivery evidence")
            if tombstone is not None and (
                tombstone.get("root_dev") != root_dev or tombstone.get("root_ino") != root_ino
            ):
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan directory identity changed during recovery")
            _require_no_registered_descendants(config_dir, candidate_root)

            reference_guard()
            default_branch, default_head, entries_verified = _prove_delivered_subset(
                source_project=expected_source_project,
                source_root=source_root,
                candidate_root=candidate_root,
                base_sha=expected_base_sha,
                expected_branch=expected_branch,
            )
            if tombstone is None:
                tombstone = {
                    **identity,
                    "phase": "prepared",
                    "root_dev": root_dev,
                    "root_ino": root_ino,
                    "remote_default_branch": default_branch,
                    "remote_default_head": default_head,
                    "entries_verified": entries_verified,
                }
                _write_orphan_tombstone(tombstone_path, tombstone)

            current_dev, current_ino = _validate_candidate_root(candidate_root, candidates_root)
            if (current_dev, current_ino) != (root_dev, root_ino):
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan directory identity changed before removal")
            if _registry_has_candidate(config_dir, project_id):
                raise _fail("WORKSPACE_CONTENDED", "candidate orphan reappeared in the workspace registry")
            _require_no_registered_descendants(config_dir, candidate_root)
            reference_guard()
            final_default_branch, final_default_head, final_entries = _prove_delivered_subset(
                source_project=expected_source_project,
                source_root=source_root,
                candidate_root=candidate_root,
                base_sha=expected_base_sha,
                expected_branch=expected_branch,
            )
            if final_default_branch != default_branch or final_default_head != default_head:
                raise _fail("WORKSPACE_CONTENDED", "trusted remote default changed during orphan finalization", retryable=True)
            try:
                shutil.rmtree(candidate_root)
            except OSError as exc:
                raise _fail("TOOL_EXECUTION_FAILED", "candidate orphan directory removal did not complete", retryable=True) from exc
            if candidate_root.exists() or candidate_root.is_symlink():
                raise _fail("TOOL_EXECUTION_FAILED", "candidate orphan directory removal could not be verified", retryable=True)

            completed = dict(tombstone)
            completed.update(
                phase="complete",
                remote_default_branch=final_default_branch,
                remote_default_head=final_default_head,
                entries_verified=final_entries,
            )
            _write_orphan_tombstone(tombstone_path, completed)
            return CandidateOrphanFinalizeReceipt(
                project_id=project_id,
                source_project=expected_source_project,
                branch=expected_branch,
                base_sha=expected_base_sha,
                remote_default_branch=final_default_branch,
                remote_default_head=final_default_head,
                entries_verified=final_entries,
                directory_removed=True,
                already_finalized=False,
                tombstone_id=tombstone_id,
            )


__all__ = [
    "CandidateOrphanFinalizeReceipt",
    "candidate_orphan_finalize",
]
