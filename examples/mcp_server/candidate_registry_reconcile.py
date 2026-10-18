"""Guarded cleanup of stale candidate registrations in the runtime overlay.

A missing candidate directory is not proof that its registered source project
has been delivered. This module removes *metadata only*, never worktree bytes,
and never mutates the source-controlled project registry.
"""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from typing import Any

import yaml

from app.workspace.registry import load_registry_roots, resolve_runtime_registry_path
from examples.mcp_server.project_registry_control import (
    ProjectRegistrationError,
    project_registry_mutation_lock,
)
from examples.mcp_server.supervisor_integration import integrate_file

_SHA_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ID_RE = re.compile(r"^candidate-[A-Za-z0-9_.-]{1,118}$")
_CLONES_DIR = ".mcp-candidate-clones"


def _fail(code: str, message: str) -> ProjectRegistrationError:
    return ProjectRegistrationError(code, message)


def _load_yaml(path: Path, *, required: bool) -> tuple[bytes, dict[str, Any]]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise _fail("CHECK_FAILED", "Required project registry is absent.") from None
        return b"", {"version": 1, "projects": {}}
    except OSError as exc:
        raise _fail("CHECK_FAILED", "Project registry cannot be inspected.") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _fail("POLICY_DENIED", "Project registry is not a safe regular file.")
    try:
        content = path.read_bytes()
        parsed = yaml.safe_load(content)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _fail("CHECK_FAILED", "Project registry cannot be read.") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("projects"), dict):
        raise _fail("CHECK_FAILED", "Project registry projects mapping is malformed.")
    return content, parsed


def _candidate_root_absent(store: Path, candidate_id: str) -> bool:
    path = store / candidate_id
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise _fail("CHECK_FAILED", "Candidate directory cannot be inspected.") from exc
    return False


def _delete_exact_blocks(original: bytes, candidate_ids: set[str]) -> bytes:
    """Preserve every unrelated YAML byte, rejecting noncanonical block shapes."""
    if not candidate_ids:
        return original
    lines = original.decode("utf-8").splitlines(keepends=True)
    markers = {f"  {candidate_id}:": candidate_id for candidate_id in candidate_ids}
    seen: set[str] = set()
    kept: list[str] = []
    removing = False
    for line in lines:
        marker = line.rstrip("\r\n")
        if marker in markers:
            candidate_id = markers[marker]
            if candidate_id in seen:
                raise _fail("CHECK_FAILED", "Duplicate candidate registry block.")
            seen.add(candidate_id)
            removing = True
            continue
        if removing and line.strip() and not line.startswith("    "):
            removing = False
        if not removing:
            kept.append(line)
    if seen != candidate_ids:
        raise _fail("CHECK_FAILED", "Candidate registry cannot be updated without reserialization.")
    return "".join(kept).encode("utf-8")


def reconcile_runtime_candidate_registry(
    *,
    config_dir: Path,
    journal_root: Path,
    apply: bool = False,
    expected_runtime_sha256: str | None = None,
    expected_stale_count: int | None = None,
) -> dict[str, Any]:
    """Preview or CAS-retire only proven absent candidate *runtime* entries.

    An apply is fenced to the exact registry bytes and eligible count returned
    by a preceding preview. Both probes are repeated under the same lock as
    candidate creation/unregistration. No source registry or workspace bytes
    are modified; existing or unsafe candidate paths are always preserved.
    """
    if not isinstance(apply, bool):
        raise _fail("INVALID_INPUT", "apply must be boolean.")
    if apply and (
        not isinstance(expected_runtime_sha256, str)
        or not _SHA_RE.fullmatch(expected_runtime_sha256)
        or type(expected_stale_count) is not int
        or not 1 <= expected_stale_count <= 10000
    ):
        raise _fail("INVALID_INPUT", "Apply requires preview SHA-256 and bounded positive stale count.")

    config_dir = config_dir.resolve()
    with project_registry_mutation_lock(journal_root):
        source_path = config_dir / "projects.yaml"
        _, source_data = _load_yaml(source_path, required=True)
        try:
            roots = load_registry_roots(source_path)
            runtime_path = resolve_runtime_registry_path(source_path)
        except (OSError, ValueError) as exc:
            raise _fail("CHECK_FAILED", "Registry root configuration is unavailable.") from exc
        if runtime_path is None:
            raise _fail("CHECK_FAILED", "Runtime registry overlay is not configured.")
        if runtime_path == source_path.resolve():
            raise _fail("POLICY_DENIED", "Runtime overlay must differ from the source project registry.")
        original, runtime_data = _load_yaml(runtime_path, required=False)
        actual_hash = "sha256:" + hashlib.sha256(original).hexdigest()
        source_entries = source_data["projects"]
        runtime_entries = runtime_data["projects"]
        workspace_root = roots["default"]
        if not workspace_root.is_dir():
            raise _fail("CHECK_FAILED", "Workspace registry root is unavailable.")

        store = workspace_root / _CLONES_DIR
        try:
            store_stat = store.lstat()
        except FileNotFoundError:
            # An unmounted or missing candidate store is not evidence of cleanup.
            raise _fail("CHECK_FAILED", "Candidate store root is absent.") from None
        except OSError as exc:
            raise _fail("CHECK_FAILED", "Candidate store root cannot be inspected.") from exc
        if stat.S_ISLNK(store_stat.st_mode) or not stat.S_ISDIR(store_stat.st_mode):
            raise _fail("POLICY_DENIED", "Candidate store root is unsafe.")

        # Precompute dependency roots once: legacy overlays may contain
        # thousands of entries, so a scan per candidate is quadratic.
        blocked: set[str] = set()
        for other_id, other in {**source_entries, **runtime_entries}.items():
            if not isinstance(other, dict):
                raise _fail("CHECK_FAILED", "Project registry contains malformed entries.")
            parent_id = other.get("parent")
            if isinstance(parent_id, str):
                blocked.add(parent_id)
            if other.get("root_selector", "default") != "default":
                continue
            other_root = other.get("root")
            if isinstance(other_root, str):
                parts = other_root.split("/")
                if len(parts) >= 2 and parts[0] == _CLONES_DIR:
                    # Ignore the candidate's own root, not another project's
                    # exact-root alias or a descendant using that candidate.
                    if len(parts) > 2 or other_id != parts[1]:
                        blocked.add(parts[1])

        eligible: set[str] = set()
        preserved_count = 0
        for candidate_id, entry in runtime_entries.items():
            if not isinstance(candidate_id, str) or not candidate_id.startswith("candidate-"):
                continue
            if (
                not _ID_RE.fullmatch(candidate_id)
                or not isinstance(entry, dict)
                or entry.get("type") != "candidate-clone"
                or entry.get("root") != f"{_CLONES_DIR}/{candidate_id}"
                or entry.get("root_selector", "default") != "default"
                or candidate_id in source_entries
                or candidate_id in blocked
            ):
                preserved_count += 1
                continue
            if not _candidate_root_absent(store, candidate_id):
                preserved_count += 1
                continue
            eligible.add(candidate_id)

        count = len(eligible)
        if apply:
            if actual_hash != expected_runtime_sha256 or count != expected_stale_count:
                raise _fail("WORKSPACE_CONTENDED", "Runtime registry or stale candidate set changed since preview.")
            # Recheck the filesystem immediately before CAS; registry writers
            # share project_registry_mutation_lock.
            if any(not _candidate_root_absent(store, candidate_id) for candidate_id in eligible):
                raise _fail("WORKSPACE_CONTENDED", "Candidate directory reappeared since preview.")
            updated = _delete_exact_blocks(original, eligible)
            # YAML anchors or noncanonical blocks can refer across entries.
            # Refuse a metadata write if textual pruning would invalidate the
            # overlay or silently change an unrelated project's parsed data.
            try:
                updated_data = yaml.safe_load(updated)
            except yaml.YAMLError as exc:
                raise _fail("CHECK_FAILED", "Registry pruning would invalidate YAML references.") from exc
            expected_data = {
                **runtime_data,
                "projects": {
                    key: value for key, value in runtime_entries.items() if key not in eligible
                },
            }
            if updated_data != expected_data:
                raise _fail("CHECK_FAILED", "Registry pruning would alter unrelated entries.")
            result = integrate_file(
                runtime_path.parent,
                runtime_path.name,
                actual_hash,
                updated,
                journal_root,
            )
            return {
                "applied": True,
                "removed_count": count,
                "stale_count": 0,
                "preserved_count": preserved_count,
                "runtime_sha256": result.new_hash,
                "workspace_bytes_mutated": False,
                "source_registry_mutated": False,
            }
        return {
            "applied": False,
            "stale_count": count,
            "preserved_count": preserved_count,
            "runtime_sha256": actual_hash,
            "sample_ids": sorted(eligible)[:10],
            "workspace_bytes_mutated": False,
            "source_registry_mutated": False,
        }
