"""Import trusted exact remote refs into an existing managed candidate.

The default branch import requires the published feature HEAD to match or
be an ancestor of the candidate's clean local HEAD. A separate feature-head
import permits ONLY a verified fast-forward from the local feature branch.
Neither operation mutates the feature branch, worktree or origin; callers can
use guarded git_refresh_branch_to_head after importing a remote feature.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from tool_results import build_command_result, tool_error, tool_success


class FetchRefError(RuntimeError):
    pass


def _recorded_staging_origin(root: Path, metadata: dict, origin: str, base: str) -> bool:
    """Accept only the factory's recorded disposable bundle origin.

    The staging directory may already be gone. Its path is an identity marker,
    never a file to read or a remote to fetch. Old clones without this creation
    receipt must use guarded cleanup rather than retroactive origin adoption.
    """
    path = Path(origin)
    return (
        metadata.get("clone_origin") == origin
        and metadata.get("head") == base
        and metadata.get("status_entries") == 0
        and path.is_absolute()
        and path.name == "source.bundle"
        and path.parent.parent == root.parent
        and path.parent.name.startswith((".registered-source-", ".candidate-remote-"))
        and ".." not in path.parts
    )


def _identity(project: str, root: Path, remote: str) -> dict:
    from app.workspace.policy import WorkspacePolicyError
    from app.workspace.registry import get_registry
    from examples.mcp_server import control_plane_git as git
    from examples.mcp_server.agent_paths import managed_source_bundle_path
    from examples.mcp_server.candidate_clone import (
        CandidateCloneError,
        _project_id,
        _read_candidate_metadata,
    )

    registry = get_registry()
    try:
        info = registry.project_info(project)
    except WorkspacePolicyError as exc:
        raise FetchRefError("GIT_REMOTE_NOT_ALLOWED") from exc
    if (
        remote != "origin"
        or info.get("type") != "candidate-clone"
        or root.name != project
        or root.parent.name != ".mcp-candidate-clones"
        or root.is_symlink()
        or root.parent.is_symlink()
        or Path(info["root"]).absolute() != root.absolute()
    ):
        raise FetchRefError("GIT_REMOTE_NOT_ALLOWED")
    try:
        metadata = _read_candidate_metadata(root)
    except CandidateCloneError as exc:
        raise FetchRefError("GIT_REMOTE_NOT_ALLOWED") from exc
    source_project = metadata.get("source_project")
    feature = metadata.get("branch")
    base = git._full_commit_id(metadata.get("base_sha"), "base_sha")
    if (
        metadata.get("version") != 1
        or not isinstance(source_project, str)
        or not source_project
        or not isinstance(feature, str)
        or feature in {"main", "master"}
        or base is None
        or _project_id(source_project, feature, base) != project
        or metadata.get("root") != f".mcp-candidate-clones/{project}"
    ):
        raise FetchRefError("GIT_REMOTE_NOT_ALLOWED")
    git._validate_name(feature, "feature_branch")
    expected_origin = managed_source_bundle_path(source_project, base)
    origin = git._remote_url(root, remote)
    if origin != expected_origin and not _recorded_staging_origin(root, metadata, origin, base):
        raise FetchRefError("GIT_REMOTE_NOT_ALLOWED")
    try:
        source_info = registry.project_info(source_project)
    except WorkspacePolicyError as exc:
        raise FetchRefError("GIT_REMOTE_NOT_ALLOWED") from exc
    source = Path(source_info["root"])
    if source_info.get("type") == "candidate-clone" or source.is_relative_to(root.parent):
        raise FetchRefError("GIT_REMOTE_NOT_ALLOWED")
    return {
        "source_project": source_project,
        "source_root": source,
        "feature_branch": feature,
        "base_sha": base,
        "origin": origin,
    }


def _clean_head(root: Path, feature: str) -> str:
    from examples.mcp_server import control_plane_git as git

    if git._current_branch(root) != feature:
        raise FetchRefError("WORKSPACE_CONTENDED")
    status = git._run_git(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        env={"GIT_OPTIONAL_LOCKS": "0"},
    )
    if status.returncode != 0 or status.stdout:
        raise FetchRefError("WORKSPACE_CONTENDED")
    head = git._read_ref(root, "HEAD")
    if not head:
        raise FetchRefError("GIT_LOCAL_REF_MISSING")
    return head


def _remote_lease(
    source: Path, feature: str, branch: str, expected: str, token: str
) -> tuple[str, str, str, str]:
    from examples.mcp_server import control_plane_git as git
    from examples.mcp_server.agent_sources import _resolve_trusted_remote

    _username, clone_url, _token = _resolve_trusted_remote(source)
    _host, owner, repo = git._parse_gitea_remote(clone_url)
    repo_data = git._gitea_get(f"/repos/{owner}/{repo}", token=token)
    default_branch = repo_data.get("default_branch")
    if (
        not isinstance(default_branch, str)
        or feature == default_branch
        or branch not in {default_branch, feature}
    ):
        raise FetchRefError("GIT_REMOTE_NOT_ALLOWED")
    heads = []
    for name in (default_branch, feature):
        data = git._gitea_get(
            f"/repos/{owner}/{repo}/branches/{quote(name, safe='')}", token=token
        )
        head = git._full_commit_id(
            str((data.get("commit") or {}).get("id") or ""), "remote_head"
        )
        if not head:
            raise FetchRefError("SOURCE_REF_NOT_AVAILABLE")
        heads.append(head)
    selected_head = heads[1] if branch == feature else heads[0]
    if selected_head != expected:
        raise FetchRefError("HEAD_MISMATCH")
    # Bind BOTH Gitea refs: a default-branch move also invalidates a feature import.
    return owner, repo, heads[1], heads[0]


def _is_symbolic_ref(root: Path, ref: str) -> bool:
    from examples.mcp_server import control_plane_git as git

    result = git._run_git(["git", "symbolic-ref", "--quiet", ref], cwd=root)
    if result.returncode not in {0, 1}:
        raise FetchRefError("GIT_OPERATION_FAILED")
    return result.returncode == 0


def _publish(
    root: Path, feature: str, head: str, tracking: str, target: str, previous: str | None
) -> bool:
    """Atomically verify the feature ref and CAS the tracking ref only."""
    if _is_symbolic_ref(root, tracking):
        return False
    transaction = (
        f"start\nverify refs/heads/{feature} {head}\n"
        f"update {tracking} {target} {previous or '0' * len(target)}\nprepare\ncommit\n"
    )
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "update-ref", "--no-deref", "--stdin"],
        cwd=root,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        input=transaction,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


@contextmanager
def _staging_ref(root: Path, ref: str):
    from examples.mcp_server import control_plane_git as git

    try:
        yield
    finally:
        result = git._run_git(["git", "update-ref", "--no-deref", "-d", ref], cwd=root)
        if result.returncode != 0:
            raise FetchRefError("WORKSPACE_VERIFICATION_FAILED")


def fetch_managed_candidate_base(
    project: str, root: Path, remote: str, branch: str, expected: str | None, *, token: str
) -> dict:
    """Bundle-only ref import with registry/lineage/SHA and fast-forward guards.

    The remote feature is importable only when it descends from the original
    candidate HEAD, leaving the local branch/worktree unchanged. Credentials
    remain exclusively inside the existing trusted private source publisher.
    """
    from examples.mcp_server import control_plane_git as git
    from examples.mcp_server.agent_sources import (
        ManagedSourceBundleError,
        materialize_trusted_remote_bundle,
    )
    from examples.mcp_server.candidate_clone import CandidateCloneError, _lineage_lock

    tracking = f"refs/remotes/{remote}/{branch}"
    staging = f"refs/mcp-fetch/{uuid.uuid4().hex}"
    update_attempted = False
    postcondition_reconciled = False
    stage = "input_validation"
    try:
        if not expected:
            raise FetchRefError("INVALID_INPUT")
        stage = "candidate_identity"
        identity = _identity(project, root, remote)
        feature = identity["feature_branch"]
        source = identity["source_root"]
        feature_import = branch == feature
        with (
            _lineage_lock(root.parent.parent, identity["source_project"], feature),
            _staging_ref(root, staging),
        ):
            stage = "candidate_state"
            head = _clean_head(root, feature)
            stage = "remote_lease"
            lease = _remote_lease(source, feature, branch, expected, token)
            if not feature_import and lease[2] != head:
                # Unpublished local commits are safe to retain if the published
                # feature tip is an exact ancestor. Divergent or unavailable
                # objects must not authorize importing a new default base.
                stage = "remote_feature_ancestry"
                ancestor = git._run_git(
                    ["git", "merge-base", "--is-ancestor", lease[2], head],
                    cwd=root,
                )
                if ancestor.returncode != 0:
                    raise FetchRefError("GIT_NON_FAST_FORWARD")
            if _is_symbolic_ref(root, tracking):
                raise FetchRefError("WORKSPACE_CONTENDED")
            previous = git._read_ref(root, tracking)
            with tempfile.TemporaryDirectory(prefix="mcp-candidate-base-") as scratch:
                bundle = Path(scratch) / "source.bundle"
                stage = "source_publication"
                publication = materialize_trusted_remote_bundle(source, expected, bundle)
                # The publisher proves the exact commit and self-contained bundle.
                # No auth environment is forwarded to this candidate Git command.
                if Path(publication.path) != bundle:
                    raise FetchRefError("WORKSPACE_VERIFICATION_FAILED")
                stage = "bundle_import"
                result = git._run_git(
                    [
                        "git",
                        "fetch",
                        "--no-tags",
                        "--no-write-fetch-head",
                        "--no-recurse-submodules",
                        str(bundle),
                        f"refs/heads/source:{staging}",
                    ],
                    cwd=root,
                )
                if result.returncode != 0:
                    raise FetchRefError("GIT_OPERATION_FAILED")
                if git._read_ref(root, staging) != expected:
                    raise FetchRefError("HEAD_MISMATCH")
                if feature_import:
                    # Importing a later PR commit may never discard local work.
                    # Do this on immutable objects, BEFORE publishing a ref.
                    stage = "feature_fast_forward"
                    ancestor = git._run_git(
                        ["git", "merge-base", "--is-ancestor", head, expected],
                        cwd=root,
                    )
                    if ancestor.returncode != 0:
                        raise FetchRefError("GIT_NON_FAST_FORWARD")
                stage = "identity_recheck"
                if (
                    _identity(project, root, remote) != identity
                    or _clean_head(root, feature) != head
                ):
                    raise FetchRefError("WORKSPACE_CONTENDED")
                stage = "remote_lease_recheck"
                if _remote_lease(source, feature, branch, expected, token) != lease:
                    raise FetchRefError("HEAD_MISMATCH")
                if _clean_head(root, feature) != head:
                    raise FetchRefError("WORKSPACE_CONTENDED")
                stage = "tracking_publication"
                update_attempted = True
                try:
                    published = _publish(root, feature, head, tracking, expected, previous)
                except (OSError, subprocess.TimeoutExpired):
                    # update-ref may have committed before its acknowledgment
                    # was lost. Inspect the exact ref; never replay the CAS.
                    published = False
                if not published:
                    stage = "tracking_reconciliation"
                    if (
                        _is_symbolic_ref(root, tracking)
                        or git._read_ref(root, tracking) != expected
                        or _clean_head(root, feature) != head
                        or _remote_lease(source, feature, branch, expected, token) != lease
                    ):
                        raise FetchRefError("WORKSPACE_CONTENDED")
                    postcondition_reconciled = True
            stage = "postverification"
            try:
                if git._read_ref(root, tracking) != expected or _clean_head(root, feature) != head:
                    raise FetchRefError("WORKSPACE_VERIFICATION_FAILED")
            except FetchRefError:
                # Restore only our exact write; never replace a concurrent writer.
                rollback = (
                    ["git", "update-ref", "--no-deref", tracking, previous, expected]
                    if previous
                    else ["git", "update-ref", "--no-deref", "-d", tracking, expected]
                )
                if not _is_symbolic_ref(root, tracking):
                    git._run_git(rollback, cwd=root)
                raise
            return tool_success(
                tool="git_fetch_ref",
                result={
                    **build_command_result(outcome="passed", exit_code=0, stdout="", stderr=""),
                    "project": project,
                    "remote": remote,
                    "branch": branch,
                    "source_project": identity["source_project"],
                    "feature_branch": feature,
                    "feature_head": head,
                    "remote_feature_head": lease[2],
                    "feature_head_relation": (
                        "feature_fast_forward"
                        if feature_import
                        else "matching" if head == lease[2] else "local_ahead"
                    ),
                    "owner": lease[0],
                    "repo": lease[1],
                    "local_ref": tracking,
                    "previous_head": previous,
                    "remote_head": expected,
                    "remote_default_head": lease[3],
                    "fetched_head": expected,
                    "import_mode": (
                        "managed_candidate_feature_fast_forward"
                        if feature_import else "managed_candidate_default_branch"
                    ),
                    "worktree_unchanged": True,
                    "origin_unchanged": True,
                    "tracking_postcondition_reconciled": postcondition_reconciled,
                },
                source="gitea",
            )
    except (
        RuntimeError,
        ValueError,
        OSError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.TimeoutExpired,
        CandidateCloneError,
        ManagedSourceBundleError,
    ) as exc:
        code = str(exc)
        allowed = {
            "GIT_REMOTE_NOT_ALLOWED",
            "INVALID_INPUT",
            "WORKSPACE_CONTENDED",
            "GIT_LOCAL_REF_MISSING",
            "SOURCE_REF_NOT_AVAILABLE",
            "HEAD_MISMATCH",
            "GIT_NON_FAST_FORWARD",
            "WORKSPACE_VERIFICATION_FAILED",
            "GIT_OPERATION_FAILED",
            "GIT_AUTH_FAILED",
            "GIT_REMOTE_UNAVAILABLE",
        }
        if code not in allowed:
            code = "GIT_OPERATION_FAILED"
        return tool_error(
            tool="git_fetch_ref",
            code=code,
            message=(
                "Managed candidate identity/origin proof is missing or mismatched; "
                "a never-modified legacy clone can use guarded abandon_unmodified cleanup."
                if stage == "candidate_identity"
                else "Managed candidate exact-ref import did not complete; inspect the reported stage and leases before retrying."
            ),
            retryable=code in {"HEAD_MISMATCH", "WORKSPACE_CONTENDED", "GIT_REMOTE_UNAVAILABLE"},
            details={
                "project": project,
                "remote": remote,
                "branch": branch,
                "local_ref": tracking,
                "tracking_ref_update_attempted": update_attempted,
                "stage": stage,
            },
            source="gitea",
        )
