"""Remote (Gitea/GitHub) adapter.

GiteaClient/GitHubClient are resolved through the server module at call
time: tests monkeypatch examples.mcp_server.server.GiteaClient and
examples.mcp_server.server.GitHubClient (test_mcp_contract_v1_gitea_github)
and expect the patched classes here.

Tools are registered explicitly via register_all() (called by server.py
after runtime.set_mcp) instead of import-time decorator side effects:
server.py may be importlib.reloaded, and the adapters are cached in
sys.modules, so import-time registration would miss the new FastMCP
instance.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from tool_results import tool_error, tool_success, validate_pagination

from examples.mcp_client_remote.fleet.gitea_client import (
    GiteaActionRunResponseError,
    GiteaMutationOutcomeUnknown,
)
from examples.mcp_client_remote.fleet.github_client import (
    normalize_list_response,
)
from examples.mcp_client_remote.fleet.shared import (
    list_pagination_meta,
    minimize_issue_payload,
)
from examples.mcp_server.candidate_verifier import (
    CandidateVerificationError,
    sanitize_verifier_output_tail,
    verify_candidate_via_docker,
    verify_workspace_via_docker,
)
from examples.mcp_server.managed_git import (
    ManagedGitError,
    configured_gitea_git_base,
    delete_remote_branch_with_lease,
    push_exact_sha,
    push_trusted_staging_sha,
    validate_expected_sha,
    validate_feature_branch,
)
from examples.mcp_server.mcp_audit import AuditWriteError, McpAuditEvent
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.tool_registry import register_tool
from examples.mcp_server.task_candidate import (
    CandidateError,
    materialize_task_candidate,
    record_task_delivery_contract,
    validate_task_candidate_for_push,
    verifier_receipt_is_trusted,
)
from examples.mcp_server.verified_workspace import (
    VerifiedWorkspaceError,
    verify_registered_delivery_workspace,
)

# Default cap, in bytes, for the *decoded* content returned by gitea_get_file.
# Keeps large base64 blobs out of tool responses by default; callers that need
# a fuller file explicitly raise max_content_bytes (or pass 0 for the client's
# MAX_FILE_SIZE fallback). See DEFAULT_GET_FILE_MAX_CONTENT_BYTES in
# gitea_client.py for the client-level counterpart.
GITEA_GET_FILE_DEFAULT_MAX_CONTENT_BYTES = 16 * 1024
GITEA_ACTION_JOB_LOG_DEFAULT_MAX_BYTES = 64 * 1024
GITEA_ACTION_JOB_LOG_DEFAULT_TAIL_LINES = 300
GITEA_ACTION_JOB_LOG_MAX_TAIL_LINES = 1000


def _server_gitea_client():
    return server_attr("GiteaClient")


def _server_github_client():
    return server_attr("GitHubClient")

def _server_agent_client():
    return server_attr("get_agent_client")()


def _server_workspace_registry():
    return server_attr("_get_workspace_registry")()


def _get_gitea_audit_logger():
    return server_attr("get_audit_logger")()


def _caller_fingerprint() -> str | None:
    """Return the opaque authenticated caller fingerprint, or None.

    Resolved through the server module's ``_current_auth_reuse_key`` so the
    raw bearer token never enters adapter code, audit records, or errors.
    Absent/None degrades to an AUTH_ERROR at the destructive call site.
    """
    try:
        getter = server_attr("_current_auth_reuse_key")
        value = getter()
    except Exception:
        return None
    if not isinstance(value, str) or not value:
        return None
    return value


async def _require_gitea_identity(client: Any) -> tuple[str, str] | None:
    """Resolve the mutable Gitea username and caller fingerprint.

    Returns ``(username, caller_fingerprint)`` only when both are available;
    otherwise returns None so the caller fails closed with AUTH_ERROR.
    """
    user = await client.get_user()
    username = str(user.get("login") or user.get("username") or "").strip()
    fingerprint = _caller_fingerprint()
    if not username or not fingerprint:
        return None
    return username, fingerprint


def _contains_ascii_control(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)



# ── Gitea/GitHub tools ───────────────────────────────────────────


def _remote_api_error(tool: str, source: str, exc: Exception) -> dict[str, Any]:
    """Map a GiteaClient/GitHubClient exception to a Contract v1 error.

    Both clients raise ValueError (bad endpoint/owner/repo/path input),
    PermissionError (401/403 from the remote API), httpx.HTTPStatusError
    (any other non-2xx, e.g. 404 for a typo'd repo/issue number), or
    httpx.TransportError (connect/read/write timeout, DNS failure,
    connection refused -- no HTTP response was ever received) -- see
    gitea_client.py/github_client.py's _get(). Their messages are already
    scrubbed of the resolved base URL by those clients.

    httpx.TransportError used to fall through to the generic
    INTERNAL_ERROR/retryable=False branch below -- P2 audit finding: a
    transient network problem (Gitea/GitHub briefly unreachable) looked
    identical to an internal program defect, and retryable=False told a
    calling agent not to bother retrying a condition that was, in fact,
    exactly the kind of thing a retry fixes.
    """
    if isinstance(exc, ValueError):
        return tool_error(tool=tool, code="INVALID_INPUT", message=str(exc), source=source)
    if isinstance(exc, PermissionError):
        return tool_error(tool=tool, code="AUTH_ERROR", message=str(exc), source=source)
    if isinstance(exc, httpx.HTTPStatusError):
        return tool_error(
            tool=tool,
            code="REMOTE_API_ERROR",
            message=str(exc),
            hint="Check that owner/repo/number exist and the token has access.",
            source=source,
        )
    if isinstance(exc, httpx.TransportError):
        return tool_error(
            tool=tool,
            code="REMOTE_UNAVAILABLE",
            message=str(exc),
            retryable=True,
            hint="The remote API host did not respond -- transient network issue, retry later.",
            source=source,
        )
    return tool_error(tool=tool, code="INTERNAL_ERROR", message=str(exc), source=source)


# ── trusted delivery verification diagnostics ──────────────────────


_VERIFIED_PHASE_BY_CODE = {
    "HEAD_MISMATCH": "candidate_checkout",
    "BASE_MISMATCH": "source_resolution",
    "SOURCE_REF_NOT_AVAILABLE": "source_resolution",
    "WORKSPACE_DIRTY": "clean_tree_check",
    "CANDIDATE_SCOPE_VIOLATION": "allowed_files_check",
    "CANDIDATE_CHECK_FAILED": "candidate_check",
    "CANDIDATE_SOURCE_UNAVAILABLE": "source_resolution",
    "CANDIDATE_VOLUME_SUBPATH_INVALID": "source_resolution",
    "VERIFIER_BOOTSTRAP_FAILED": "verifier_bootstrap",
    "VERIFIER_ENV_UNAVAILABLE": "verifier_env",
    "CHECK_FAILED": "push_preflight",
    "INVALID_INPUT": "push_preflight",
    "WORKSPACE_VERIFICATION_FAILED": "push_preflight",
}


def _delivery_verification_error(
    *,
    tool: str,
    project: str,
    owner: str,
    repo: str,
    destination_branch: str,
    expected_base_sha: str,
    expected_head_sha: str,
    allowed: list[str],
    checks: list[str],
    exc: Exception,
) -> dict[str, Any]:
    """Convert a verified-delivery denial into a structured, redacted tool error.

    Binds the delivery contract (base/head SHAs, destination branch, allowed
    files, required checks) to the failing verification phase and, where safe,
    the failed check name and a bounded, redacted output tail.  ``exc`` is
    either a ``VerifiedWorkspaceError`` or a ``CandidateVerificationError`` --
    both already carry a stable code, a sanitized message, a retryable flag
    and sanitized details.  Nothing here is echoed verbatim from a candidate.
    """
    code = getattr(exc, "code", None) or "CANDIDATE_VERIFICATION_FAILED"
    message = getattr(exc, "message", None) or str(exc)
    retryable = bool(getattr(exc, "retryable", False))
    exc_details = getattr(exc, "details", None) or {}
    phase = (
        getattr(exc, "phase", None)
        or _VERIFIED_PHASE_BY_CODE.get(code, "")
        or "push_preflight"
    )
    details: dict[str, Any] = {
        "phase": phase,
        "project": project,
        "owner": owner,
        "repo": repo,
        "branch": destination_branch,
        "expected_base_sha": expected_base_sha,
        "expected_head_sha": expected_head_sha,
        "allowed_files": allowed,
        "required_checks": checks,
        "mutation_occurred": False,
    }
    details.update(exc_details)
    # Defensive re-sanitization: even if a caller built a structured error
    # with a raw tail, nothing candidate-controlled is surfaced unredacted.
    if "output_tail" in details:
        details["output_tail"] = sanitize_verifier_output_tail(
            str(details.get("output_tail") or "")
        )
        if not details["output_tail"]:
            details.pop("output_tail", None)

    hint = None
    if phase == "clean_tree_check":
        hint = "Rebuild the candidate from the expected base so the delivery workspace is clean before the trusted push."
    elif phase == "allowed_files_check":
        hint = "Restrict the delivery commit to paths named by allowed_files and retry."
    elif phase in {"required_checks", "candidate_check"}:
        hint = "Fix or revert the failing required check (see details.failed_check) and re-run trusted delivery."
    elif phase == "source_resolution":
        hint = "Rebuild or re-materialize the candidate source at the expected head before retrying trusted delivery."
    elif retryable:
        hint = "Verification environment was transiently unavailable; retry once the candidate store/docker is reachable."

    return tool_error(
        tool=tool,
        code=code,
        message=message,
        retryable=retryable,
        hint=hint,
        details=details,
        source="gitea",
    )


def _minimize_gitea_repo(data: dict[str, Any]) -> dict[str, Any]:
    """Trim a Gitea repo payload to non-PII fields.

    The raw Gitea API response embeds the full owner user object, including
    their email address, in every repo lookup -- unnecessary for the tool's
    purpose and a PII leak. Keep only login/name/visibility/default_branch/
    permissions/counters/topics.
    """
    owner = data.get("owner") or {}
    if data.get("private"):
        visibility = "private"
    elif data.get("internal"):
        visibility = "internal"
    else:
        visibility = "public"
    return {
        "owner": {"login": owner.get("login")},
        "name": data.get("name"),
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "visibility": visibility,
        "default_branch": data.get("default_branch"),
        "permissions": data.get("permissions"),
        "counters": {
            "stars": data.get("stars_count"),
            "forks": data.get("forks_count"),
            "watchers": data.get("watchers_count"),
            "open_issues": data.get("open_issues_count"),
        },
        "topics": data.get("topics", []),
        "archived": data.get("archived"),
        "html_url": data.get("html_url"),
    }


def _minimize_gitea_pull_request(data: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields needed to continue the review workflow."""
    head = data.get("head") or {}
    base = data.get("base") or {}
    return {
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "html_url": data.get("html_url"),
        "head": head.get("ref") or head.get("label"),
        "base": base.get("ref") or base.get("label"),
        "mergeable": data.get("mergeable"),
    }


_PR_OUTDATED_OPERATOR_CHOICES = [
    {
        "action": "update_branch_to_base_and_rerun_ci",
        "description": "Update the PR branch with the current base branch, then use only CI from the new head.",
    },
    {
        "action": "continue_stale_ci_explicitly",
        "description": "Continue reviewing the stale head only after accepting that CI did not run on the current base.",
    },
]


def _pr_ref(data: dict[str, Any], key: str, field: str) -> str | None:
    ref = data.get(key)
    if not isinstance(ref, dict):
        return None
    value = ref.get(field)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _compare_total_commits(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    value = data.get("total_commits")
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    commits = data.get("commits")
    if isinstance(commits, list):
        return len(commits)
    return None


async def _gitea_pr_branch_tracking(
    client: Any,
    owner: str,
    repo: str,
    pr: dict[str, Any],
) -> dict[str, Any]:
    """Return whether a PR head contains the current base branch head.

    A green pull_request run proves only that one head SHA was tested. It does
    not prove that the head was tested against the current target branch. Query
    Gitea compare in both directions so the operator can see ahead/behind state
    before relying on CI or merging.
    """
    base_ref = _pr_ref(pr, "base", "ref")
    head_ref = _pr_ref(pr, "head", "ref")
    base_sha = (_pr_ref(pr, "base", "sha") or "").lower() or None
    head_sha = (_pr_ref(pr, "head", "sha") or "").lower() or None
    compare_base = base_sha or ""
    compare_head = head_sha or ""
    tracking: dict[str, Any] = {
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_ref": head_ref,
        "head_sha": head_sha,
        "compare_by": "sha" if compare_base and compare_head else None,
        "branch_contains_base": None,
        "branch_is_current": None,
        "ahead_by": None,
        "behind_by": None,
        "warning": "BASE_TRACKING_UNKNOWN",
        "operator_choices": _PR_OUTDATED_OPERATOR_CHOICES,
    }
    if not base_ref or not head_ref:
        tracking["tracking_error"] = "missing_pr_refs"
        return tracking
    if not compare_base or not compare_head:
        tracking["tracking_error"] = "missing_pr_shas"
        return tracking

    try:
        ahead = await client.compare_commits(owner, repo, base=compare_base, head=compare_head)
        behind = await client.compare_commits(owner, repo, base=compare_head, head=compare_base)
    except Exception as exc:
        tracking["tracking_error"] = type(exc).__name__
        return tracking

    ahead_by = _compare_total_commits(ahead)
    behind_by = _compare_total_commits(behind)
    tracking["ahead_by"] = ahead_by
    tracking["behind_by"] = behind_by
    if behind_by is None:
        tracking["tracking_error"] = "compare_response_missing_total_commits"
        return tracking

    branch_is_current = behind_by == 0
    tracking["branch_contains_base"] = branch_is_current
    tracking["branch_is_current"] = branch_is_current
    if branch_is_current:
        tracking["warning"] = None
        tracking["operator_choices"] = []
    else:
        tracking["warning"] = "PR_BRANCH_OUTDATED"
    return tracking


_MERGE_RECONCILE_ATTEMPTS = 3
_MERGE_RECONCILE_DELAY_SECONDS = 0.05
_GITEA_CI_PAGE_LIMIT = 50
_GITEA_CI_MAX_RUNS = 1000
_GITEA_CI_MAX_PAGES = 20


def _configured_workflow_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    has_total = "total_count" in payload
    has_workflows = "workflows" in payload
    total_count = payload.get("total_count")
    workflows = payload.get("workflows")
    if has_total and (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
    ):
        return None
    if has_workflows and not isinstance(workflows, list):
        return None
    if (
        has_total
        and has_workflows
        and isinstance(total_count, int)
        and isinstance(workflows, list)
        and len(workflows) > total_count
    ):
        return None
    if has_total and isinstance(total_count, int):
        if total_count == 0:
            return 0 if has_workflows and workflows == [] else None
        return total_count
    if has_workflows and isinstance(workflows, list) and workflows:
        return len(workflows)
    return None


def _run_ids_descend_from(run_ids: list[int], previous_oldest_id: int | None) -> bool:
    if any(left <= right for left, right in zip(run_ids, run_ids[1:], strict=False)):
        return False
    if not run_ids or previous_oldest_id is None:
        return True
    return run_ids[0] < previous_oldest_id


def _validated_action_run_page(
    payload: Any,
) -> tuple[int, list[dict[str, Any]], list[int]] | None:
    if not isinstance(payload, dict):
        return None
    total_count = payload.get("total_count")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
    ):
        return None
    raw_runs = payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        return None
    runs: list[dict[str, Any]] = []
    run_ids: list[int] = []
    seen_ids: set[int] = set()
    for run in raw_runs:
        if not isinstance(run, dict):
            return None
        run_id = run.get("id")
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            return None
        if run_id in seen_ids:
            return None
        seen_ids.add(run_id)
        runs.append(run)
        run_ids.append(run_id)
    return total_count, runs, run_ids


_CI_EVIDENCE_ERROR_POLICIES: dict[str, tuple[str, bool, str]] = {
    "CI_NOT_CONFIGURED": (
        "Gitea Actions has no configured workflows for this repository",
        False,
        "Configure the repository's required CI workflows before merging.",
    ),
    "NO_REQUIRED_RUN_FOUND": (
        "no Actions run exists for the expected pull request head",
        False,
        "Run the repository's required CI for the exact pull request head.",
    ),
    "CI_TRIGGER_INCOMPATIBLE": (
        "Actions runs exist for the expected head, but none use the pull_request trigger",
        False,
        "Configure the required workflow to run for pull_request events.",
    ),
    "CI_EVIDENCE_INCOMPLETE": (
        "Gitea Actions CI evidence is incomplete or changed while it was being read",
        True,
        "Re-read the complete Actions evidence and retry after it stabilizes.",
    ),
    "CI_NOT_GREEN": (
        "latest pull_request CI for the expected head is not successful",
        True,
        "Wait for the latest required pull_request run to complete successfully.",
    ),
}


async def _gitea_ci_evidence(
    client: Any,
    owner: str,
    repo: str,
    expected_head_sha: str,
) -> str:
    workflow_payload = await client.list_workflows(owner, repo)
    configured_count = _configured_workflow_count(workflow_payload)
    if configured_count == 0:
        return "CI_NOT_CONFIGURED"
    if configured_count is None:
        return "CI_EVIDENCE_INCOMPLETE"

    try:
        first_payload = await client.list_action_runs(
            owner,
            repo,
            status=None,
            limit=_GITEA_CI_PAGE_LIMIT,
            page=1,
        )
    except GiteaActionRunResponseError:
        return "CI_EVIDENCE_INCOMPLETE"
    first_page = _validated_action_run_page(first_payload)
    if first_page is None:
        return "CI_EVIDENCE_INCOMPLETE"
    expected_total, _, first_page_ids = first_page

    page = first_page
    page_number = 1
    scanned_runs = 0
    oldest_seen_id: int | None = None
    seen_ids: set[int] = set()
    saw_exact_head_run = False
    latest_exact_head_run: dict[str, Any] | None = None
    while True:
        total_count, page_runs, page_run_ids = page
        if total_count != expected_total:
            return "CI_EVIDENCE_INCOMPLETE"
        if scanned_runs + len(page_runs) > expected_total:
            return "CI_EVIDENCE_INCOMPLETE"
        if any(run_id in seen_ids for run_id in page_run_ids):
            return "CI_EVIDENCE_INCOMPLETE"
        if not _run_ids_descend_from(page_run_ids, oldest_seen_id):
            return "CI_EVIDENCE_INCOMPLETE"
        if (
            scanned_runs + len(page_runs) < expected_total
            and len(page_runs) < _GITEA_CI_PAGE_LIMIT
        ):
            return "CI_EVIDENCE_INCOMPLETE"
        seen_ids.update(page_run_ids)
        if page_run_ids:
            oldest_seen_id = page_run_ids[-1]
        scanned_runs += len(page_runs)
        for run in page_runs:
            if run.get("head_sha") != expected_head_sha:
                continue
            saw_exact_head_run = True
            if run.get("event") == "pull_request" and latest_exact_head_run is None:
                latest_exact_head_run = run
        if latest_exact_head_run is not None or scanned_runs >= expected_total:
            break
        if page_number >= _GITEA_CI_MAX_PAGES or scanned_runs >= _GITEA_CI_MAX_RUNS:
            return "CI_EVIDENCE_INCOMPLETE"
        try:
            page_payload = await client.list_action_runs(
                owner,
                repo,
                status=None,
                limit=_GITEA_CI_PAGE_LIMIT,
                page=page_number + 1,
            )
        except GiteaActionRunResponseError:
            return "CI_EVIDENCE_INCOMPLETE"
        next_page = _validated_action_run_page(page_payload)
        if next_page is None:
            return "CI_EVIDENCE_INCOMPLETE"
        page = next_page
        page_number += 1

    try:
        reread_payload = await client.list_action_runs(
            owner,
            repo,
            status=None,
            limit=_GITEA_CI_PAGE_LIMIT,
            page=1,
        )
    except GiteaActionRunResponseError:
        return "CI_EVIDENCE_INCOMPLETE"
    reread = _validated_action_run_page(reread_payload)
    if reread is None:
        return "CI_EVIDENCE_INCOMPLETE"
    reread_total, _, reread_ids = reread
    if reread_total != expected_total or reread_ids != first_page_ids:
        return "CI_EVIDENCE_INCOMPLETE"

    if latest_exact_head_run is None:
        if not saw_exact_head_run:
            return "NO_REQUIRED_RUN_FOUND"
        return "CI_TRIGGER_INCOMPATIBLE"
    if (
        latest_exact_head_run.get("status") != "completed"
        or latest_exact_head_run.get("conclusion") != "success"
    ):
        return "CI_NOT_GREEN"
    return "CI_GREEN"


def _ci_evidence_tool_error(status: str) -> dict[str, Any]:
    message, retryable, hint = _CI_EVIDENCE_ERROR_POLICIES[status]
    return tool_error(
        tool="gitea_merge_pull_request",
        code=status,
        message=message,
        retryable=retryable,
        hint=hint,
        source="gitea",
    )


def _valid_commit_sha(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if len(text) != 40 or any(ch not in "0123456789abcdef" for ch in text):
        return None
    return text


def _merge_exception_is_ambiguous(exc: BaseException) -> bool:
    """Return whether a failed merge request may still have mutated Gitea."""
    if isinstance(
        exc,
        (GiteaMutationOutcomeUnknown, asyncio.CancelledError, httpx.TransportError, TimeoutError),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return response is not None and response.status_code >= 500
    return False


async def _reconcile_merge_postcondition(
    client: Any,
    owner: str,
    repo: str,
    pull_number: int,
    *,
    expected_head_sha: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Boundedly prove a merge after crossing the irreversible mutation boundary."""
    last_observed: dict[str, Any] = {}
    last_read_error_class: str | None = None
    attempts = 0
    for attempt in range(_MERGE_RECONCILE_ATTEMPTS):
        attempts = attempt + 1
        try:
            pr = await client.get_pull_request(owner, repo, pull_number)
        except Exception as exc:
            last_read_error_class = type(exc).__name__
        else:
            head = pr.get("head") or {}
            observed_head_sha = _valid_commit_sha(head.get("sha"))
            merge_commit_sha = _valid_commit_sha(pr.get("merge_commit_sha"))
            last_observed = {
                "state": pr.get("state"),
                "merged": pr.get("merged"),
                "head_sha": observed_head_sha,
                "merge_commit_sha": merge_commit_sha,
            }
            if (
                pr.get("merged") is True
                and observed_head_sha == expected_head_sha
                and merge_commit_sha is not None
            ):
                return pr, {
                    "reconciliation_attempts": attempts,
                    "observed": last_observed,
                }
        if attempt + 1 < _MERGE_RECONCILE_ATTEMPTS:
            await asyncio.sleep(_MERGE_RECONCILE_DELAY_SECONDS)

    details: dict[str, Any] = {
        "reconciliation_attempts": attempts,
        "observed": last_observed,
    }
    if last_read_error_class:
        details["last_read_error_class"] = last_read_error_class
    return None, details


def _minimize_github_repo(data: dict[str, Any]) -> dict[str, Any]:
    """Trim a GitHub repo payload to non-PII fields (mirrors _minimize_gitea_repo)."""
    owner = data.get("owner") or {}
    return {
        "owner": {"login": owner.get("login")},
        "name": data.get("name"),
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "visibility": data.get("visibility") or ("private" if data.get("private") else "public"),
        "default_branch": data.get("default_branch"),
        "permissions": data.get("permissions"),
        "counters": {
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "watchers": data.get("watchers_count"),
            "open_issues": data.get("open_issues_count"),
        },
        "topics": data.get("topics", []),
        "archived": data.get("archived"),
        "html_url": data.get("html_url"),
    }


async def gitea_get_repo(owner: str, repo: str) -> dict[str, Any]:
    """Get Gitea repository metadata (login, visibility, default branch, permissions, counters, topics)."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_repo",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with _server_gitea_client()(token) as client:
            data = await client.get_repo(owner, repo)
    except Exception as exc:
        return _remote_api_error("gitea_get_repo", "gitea", exc)
    return tool_success("gitea_get_repo", result=_minimize_gitea_repo(data), source="gitea")


async def gitea_list_branches(owner: str, repo: str, limit: int = 30) -> dict[str, Any]:
    """List branches in a Gitea repository."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_branches",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with _server_gitea_client()(token) as client:
            raw = await client.list_branches(owner, repo, limit=limit)
            data = normalize_list_response(raw, meta=list_pagination_meta(len(raw), limit))
    except Exception as exc:
        return _remote_api_error("gitea_list_branches", "gitea", exc)
    return tool_success("gitea_list_branches", result=data, source="gitea")


async def gitea_list_commits(
    owner: str, repo: str, sha: str | None = None, limit: int = 30
) -> dict[str, Any]:
    """List commits in a Gitea repository. Optionally filter by branch SHA."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_commits",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with _server_gitea_client()(token) as client:
            raw = await client.list_commits(owner, repo, sha=sha, limit=limit)
            data = normalize_list_response(raw, meta=list_pagination_meta(len(raw), limit))
    except Exception as exc:
        return _remote_api_error("gitea_list_commits", "gitea", exc)
    return tool_success("gitea_list_commits", result=data, source="gitea")


async def gitea_get_file(
    owner: str,
    repo: str,
    path: str = "",
    branch: str | None = None,
    *,
    include_content: bool = True,
    max_content_bytes: int = GITEA_GET_FILE_DEFAULT_MAX_CONTENT_BYTES,
) -> dict[str, Any]:
    """Get a file or directory from a Gitea repository. Omit path (or
    pass "") to list the repository root.

    Content contract (keeps large base64 blobs out of chat by default):
      * include_content=False returns metadata only (path, name, sha,
        download_url, html_url, last_commit_sha) and marks the result
        content_omitted -- no "content" blob.
      * max_content_bytes bounds the decoded content bytes; larger files
        come back "truncated" with a content_bytes size marker instead of
        a full blob. Default 16 KiB; pass 0 to fall back to the client's
        256 KiB safety cap.
    """
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_file",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    if max_content_bytes < 0:
        return tool_error(
            tool="gitea_get_file",
            code="INVALID_INPUT",
            message="max_content_bytes must be >= 0",
            source="gitea",
        )
    try:
        async with _server_gitea_client()(token) as client:
            data = await client.get_file(
                owner,
                repo,
                path,
                branch=branch,
                include_content=include_content,
                max_content_bytes=(max_content_bytes or None),
            )
    except Exception as exc:
        return _remote_api_error("gitea_get_file", "gitea", exc)
    return tool_success("gitea_get_file", result=data, source="gitea")


async def gitea_list_issues(
    owner: str, repo: str, state: str = "open", limit: int = 30
) -> dict[str, Any]:
    """List issues in a Gitea repository. State: open, closed, all."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_issues",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with _server_gitea_client()(token) as client:
            raw = await client.list_issues(owner, repo, state=state, limit=limit)
            data = normalize_list_response(
                [minimize_issue_payload(i, provider="gitea") for i in raw],
                meta=list_pagination_meta(len(raw), limit),
            )
    except Exception as exc:
        return _remote_api_error("gitea_list_issues", "gitea", exc)
    return tool_success("gitea_list_issues", result=data, source="gitea")


async def gitea_get_issue(owner: str, repo: str, issue_number: int) -> dict[str, Any]:
    """Get details of a specific Gitea issue by number."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_issue",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with _server_gitea_client()(token) as client:
            raw = await client.get_issue(owner, repo, issue_number)
            data = minimize_issue_payload(raw, provider="gitea")
    except Exception as exc:
        return _remote_api_error("gitea_get_issue", "gitea", exc)
    return tool_success("gitea_get_issue", result=data, source="gitea")


async def gitea_list_pull_requests(
    owner: str, repo: str, state: str = "open", limit: int = 30
) -> dict[str, Any]:
    """List pull requests in a Gitea repository. State: open, closed, all."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_pull_requests",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with _server_gitea_client()(token) as client:
            raw = await client.list_pull_requests(owner, repo, state=state, limit=limit)
            data = normalize_list_response(
                [minimize_issue_payload(i, provider="gitea") for i in raw],
                meta=list_pagination_meta(len(raw), limit),
            )
    except Exception as exc:
        return _remote_api_error("gitea_list_pull_requests", "gitea", exc)
    return tool_success("gitea_list_pull_requests", result=data, source="gitea")


async def gitea_get_pull_request(owner: str, repo: str, pull_number: int) -> dict[str, Any]:
    """Get details of a specific Gitea pull request by number."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_pull_request",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with _server_gitea_client()(token) as client:
            raw = await client.get_pull_request(owner, repo, pull_number)
            data = minimize_issue_payload(raw, provider="gitea")
            data["merged"] = raw.get("merged")
            data["merge_commit_sha"] = _valid_commit_sha(raw.get("merge_commit_sha"))
            data["mergeable"] = raw.get("mergeable")
            data["branch_tracking"] = await _gitea_pr_branch_tracking(
                client,
                owner,
                repo,
                raw,
            )
    except Exception as exc:
        return _remote_api_error("gitea_get_pull_request", "gitea", exc)
    return tool_success("gitea_get_pull_request", result=data, source="gitea")


async def gitea_create_pull_request(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
) -> dict[str, Any]:
    """Create a same-repository Gitea pull request. Does not merge it."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_create_pull_request",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with _server_gitea_client()(token) as client:
            raw = await client.create_pull_request(
                owner,
                repo,
                title=title,
                head=head,
                base=base,
                body=body,
            )
            data = _minimize_gitea_pull_request(raw)
            data["branch_tracking"] = await _gitea_pr_branch_tracking(
                client,
                owner,
                repo,
                raw,
            )
    except Exception as exc:
        return _remote_api_error("gitea_create_pull_request", "gitea", exc)
    return tool_success("gitea_create_pull_request", result=data, source="gitea")


async def gitea_merge_pull_request(
    owner: str,
    repo: str,
    pull_number: int,
    expected_head_sha: str,
    expected_base_sha: str | None = None,
    method: str = "merge",
    allow_outdated_base: bool = False,
) -> dict[str, Any]:
    """Merge an open, mergeable PR only when its expected head has green CI and current base."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_merge_pull_request",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )

    expected_head_sha = expected_head_sha.strip().lower()
    if len(expected_head_sha) != 40 or any(c not in "0123456789abcdef" for c in expected_head_sha):
        return tool_error(
            tool="gitea_merge_pull_request",
            code="INVALID_INPUT",
            message="expected_head_sha must be a 40-character SHA-1",
            source="gitea",
        )
    if expected_base_sha is not None:
        expected_base_sha = expected_base_sha.strip().lower()
        if len(expected_base_sha) != 40 or any(c not in "0123456789abcdef" for c in expected_base_sha):
            return tool_error(
                tool="gitea_merge_pull_request",
                code="INVALID_INPUT",
                message="expected_base_sha must be a 40-character SHA-1 when provided",
                source="gitea",
            )
    if method not in {"merge", "squash"}:
        return tool_error(
            tool="gitea_merge_pull_request",
            code="INVALID_INPUT",
            message="merge method must be one of: merge, squash",
            source="gitea",
        )

    try:
        async with _server_gitea_client()(token) as client:
            pr = await client.get_pull_request(owner, repo, pull_number)
            head = pr.get("head") or {}
            base = pr.get("base") or {}
            actual_head_sha = str(head.get("sha") or "").lower()
            actual_base_sha = str(base.get("sha") or "").lower()
            base_ref = str(base.get("ref") or "")

            if pr.get("state") != "open" or pr.get("merged") is True:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="PR_NOT_OPEN",
                    message=f"pull request #{pull_number} is not open",
                    source="gitea",
                )
            if actual_head_sha != expected_head_sha:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="HEAD_MISMATCH",
                    message="pull request head changed; re-read the PR and CI before merging",
                    source="gitea",
                )
            if not actual_base_sha:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="BASE_TRACKING_UNKNOWN",
                    message="pull request base SHA is unavailable; refusing merge without immutable base evidence",
                    retryable=True,
                    hint="Re-read the PR and ensure the provider returns base.sha before merging.",
                    source="gitea",
                )
            if expected_base_sha is not None and actual_base_sha != expected_base_sha:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="BASE_MISMATCH",
                    message="pull request base changed; re-read the PR and CI before merging",
                    details={
                        "expected_base_sha": expected_base_sha,
                        "observed_base_sha": actual_base_sha,
                    },
                    source="gitea",
                )
            if base_ref not in {"main", "master"}:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="POLICY_DENIED",
                    message=f"merging into base branch {base_ref!r} is not allowed",
                    source="gitea",
                )
            if pr.get("mergeable") is not True:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="PR_NOT_MERGEABLE",
                    message=f"pull request #{pull_number} is not currently mergeable",
                    retryable=True,
                    source="gitea",
                )

            branch_tracking = await _gitea_pr_branch_tracking(client, owner, repo, pr)
            if branch_tracking["branch_is_current"] is None:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="BASE_TRACKING_UNKNOWN",
                    message="could not prove that pull request head contains the current base branch",
                    retryable=True,
                    hint="Re-read the PR or update the branch to the current base before relying on CI.",
                    details={"branch_tracking": branch_tracking},
                    source="gitea",
                )
            if branch_tracking["branch_is_current"] is not True and not allow_outdated_base:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="PR_BRANCH_OUTDATED",
                    message="pull request branch does not contain the current base branch; CI may be stale",
                    retryable=True,
                    hint="Use update_branch_to_base_and_rerun_ci, or retry with allow_outdated_base=true only after accepting stale-base risk.",
                    details={"branch_tracking": branch_tracking},
                    source="gitea",
                )

            ci_status = await _gitea_ci_evidence(
                client,
                owner,
                repo,
                expected_head_sha,
            )
            if ci_status != "CI_GREEN":
                return _ci_evidence_tool_error(ci_status)

            latest_pr = await client.get_pull_request(owner, repo, pull_number)
            latest_head = latest_pr.get("head") or {}
            latest_base = latest_pr.get("base") or {}
            latest_head_sha = str(latest_head.get("sha") or "").lower()
            latest_base_sha = str(latest_base.get("sha") or "").lower()
            latest_base_ref = str(latest_base.get("ref") or "")
            if latest_pr.get("state") != "open" or latest_pr.get("merged") is True:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="PR_NOT_OPEN",
                    message=f"pull request #{pull_number} is not open after CI evidence was read",
                    source="gitea",
                )
            if latest_head_sha != expected_head_sha:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="HEAD_MISMATCH",
                    message="pull request head changed after CI evidence was read",
                    source="gitea",
                )
            if latest_base_sha != actual_base_sha:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="BASE_MISMATCH",
                    message="pull request base changed after CI evidence was read",
                    details={
                        "expected_base_sha": actual_base_sha,
                        "observed_base_sha": latest_base_sha or None,
                    },
                    source="gitea",
                )
            if latest_base_ref != base_ref:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="BASE_MISMATCH",
                    message="pull request base ref changed after CI evidence was read",
                    details={"expected_base_ref": base_ref, "observed_base_ref": latest_base_ref},
                    source="gitea",
                )
            if latest_pr.get("mergeable") is not True:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="PR_NOT_MERGEABLE",
                    message=f"pull request #{pull_number} is no longer mergeable",
                    retryable=True,
                    source="gitea",
                )
            branch_tracking = await _gitea_pr_branch_tracking(client, owner, repo, latest_pr)
            if branch_tracking["branch_is_current"] is None:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="BASE_TRACKING_UNKNOWN",
                    message="could not prove that pull request head contains the current base branch before merge",
                    retryable=True,
                    hint="Re-read the PR or update the branch to the current base before relying on CI.",
                    details={"branch_tracking": branch_tracking},
                    source="gitea",
                )
            if branch_tracking["branch_is_current"] is not True and not allow_outdated_base:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="PR_BRANCH_OUTDATED",
                    message="pull request branch does not contain the current base branch before merge; CI may be stale",
                    retryable=True,
                    hint="Use update_branch_to_base_and_rerun_ci, or retry with allow_outdated_base=true only after accepting stale-base risk.",
                    details={"branch_tracking": branch_tracking},
                    source="gitea",
                )

            ci_status = await _gitea_ci_evidence(
                client,
                owner,
                repo,
                expected_head_sha,
            )
            if ci_status != "CI_GREEN":
                return _ci_evidence_tool_error(ci_status)

            mutation_error: BaseException | None = None
            try:
                await client.merge_pull_request(
                    owner,
                    repo,
                    pull_number,
                    expected_head_sha=expected_head_sha,
                    method=method,
                )
            except asyncio.CancelledError as exc:
                # Cancellation after entering the POST await is an ambiguous
                # mutation outcome, not evidence that the write did not happen.
                # Consume this delivered cancellation so bounded postcondition
                # reconciliation can run; a later cancellation request may still
                # interrupt normally.
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    current_task.uncancel()
                mutation_error = exc
            except Exception as exc:
                if not _merge_exception_is_ambiguous(exc):
                    raise
                mutation_error = exc

            merged_pr, reconciliation = await _reconcile_merge_postcondition(
                client,
                owner,
                repo,
                pull_number,
                expected_head_sha=expected_head_sha,
            )
            if merged_pr is None:
                details: dict[str, Any] = {
                    "expected_head_sha": expected_head_sha,
                    "base_ref": base_ref,
                    "base_sha": actual_base_sha,
                    "method": method,
                    "mutation_started": True,
                    **reconciliation,
                }
                if mutation_error is not None:
                    details["mutation_error_class"] = type(mutation_error).__name__
                    if isinstance(mutation_error, httpx.HTTPStatusError):
                        details["mutation_http_status"] = mutation_error.response.status_code
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="MUTATION_OUTCOME_UNKNOWN",
                    message="merge mutation was attempted but its server-side outcome could not be proven",
                    retryable=False,
                    hint="Do not retry the merge mutation. Re-read the pull request until merged state and merge_commit_sha are authoritative, then reconcile from that state.",
                    details=details,
                    source="gitea",
                )

            merge_commit_sha = _valid_commit_sha(merged_pr.get("merge_commit_sha"))
            assert merge_commit_sha is not None
            data = {
                "number": pull_number,
                "merged": True,
                "outcome": "completed_after_ambiguous_response"
                if mutation_error is not None
                else "completed",
                "head_sha": expected_head_sha,
                "base": base_ref,
                "base_sha": actual_base_sha,
                "method": method,
                "branch_tracking": branch_tracking,
                "outdated_base_accepted": branch_tracking["branch_is_current"] is not True,
                "merge_commit_sha": merge_commit_sha,
                "html_url": merged_pr.get("html_url"),
                "reconciliation_attempts": reconciliation["reconciliation_attempts"],
            }
    except Exception as exc:
        return _remote_api_error("gitea_merge_pull_request", "gitea", exc)
    return tool_success("gitea_merge_pull_request", result=data, source="gitea")


def _normalize_superseding_branch(superseding_ref: str) -> str:
    """Validate/normalize a same-repo superseding branch name for PR close.

    Accepts ``refs/heads/<branch>`` or a plain feature branch name that
    passes the existing feature-branch validation. Rejects other ref
    namespaces and any branch shape the feature-branch validator refuses
    (including default/protected names, which that helper already rejects).
    Returns the canonical branch name (without the ``refs/heads/`` prefix);
    the caller later re-prefixes it for audit metadata.
    """
    value = str(superseding_ref or "").strip()
    if value.startswith("refs/heads/"):
        value = value[len("refs/heads/") :]
    elif value.startswith("refs/"):
        raise ValueError("superseding_ref must be refs/heads/<branch> or a branch name")
    return validate_feature_branch(value)


async def gitea_close_pull_request(
    owner: str,
    repo: str,
    pull_number: int,
    expected_head_sha: str,
    reason: str,
    superseding_ref: str,
) -> dict[str, Any]:
    """Close an open PR protected by exact head-SHA and unmerged-state checks.

    Never merges and never deletes branches: the only mutation is a
    single state=closed PATCH issued after a fresh GET confirms the PR
    is still open and its head still equals expected_head_sha. Any head
    mismatch fails closed with zero writes. A PR already closed whose
    head still matches and is explicitly merged=false is an idempotent
    success (already_closed=true).

    A human-readable ``reason`` (1..500 chars) is required. The
    ``superseding_ref`` argument is required by the tool schema: callers
    must always supply a ``refs/heads/<branch>`` or plain feature branch
    name in the SAME repository (<=255 chars; strip-prefixed, non-empty,
    validated only through the existing feature-branch rules); no default
    ref is invented. For a REAL transition from open+unmerged to closed,
    immediately before the destructive-intent audit and close mutation the
    adapter freshly resolves that branch via the same Gitea client and
    requires its exact head commit to equal ``expected_head_sha``. Missing
    branch, malformed ref, moved/different head, or lookup ambiguity fails
    closed with zero audit intent and zero close mutation. No arbitrary
    URL/repo-qualified strings are accepted. A PR already closed whose
    head still matches and is explicitly merged=false remains an idempotent
    success (already_closed=true): the argument is still required for schema
    compliance but is never resolved through the branch API and no
    destructive-intent audit is emitted because no mutation occurs.

    Before a real close the adapter emits a strict, attributed destructive-intent
    audit event (Gitea username + caller fingerprint + correlation id, no
    credentials); the audit metadata records the canonical superseding
    ``refs/heads/<branch>`` and its verified head SHA. If that audit cannot be
    persisted the tool fails closed with AUDIT_UNAVAILABLE before any server
    mutation. After a confirmed close a best-effort success audit event is
    emitted with the same correlation id. After mutation the PR is re-read and
    state=closed, merged=false, head SHA and base ref are verified.
    """
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_close_pull_request",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )

    if pull_number < 1:
        return tool_error(
            tool="gitea_close_pull_request",
            code="INVALID_INPUT",
            message="pull_number must be >= 1",
            source="gitea",
        )
    expected_head_sha = expected_head_sha.strip().lower()
    if len(expected_head_sha) != 40 or any(c not in "0123456789abcdef" for c in expected_head_sha):
        return tool_error(
            tool="gitea_close_pull_request",
            code="INVALID_INPUT",
            message="expected_head_sha must be a 40-character SHA-1",
            source="gitea",
        )
    reason = str(reason or "").strip()
    if not reason:
        return tool_error(
            tool="gitea_close_pull_request",
            code="INVALID_INPUT",
            message="reason is required to close a pull request",
            source="gitea",
        )
    if len(reason) > 500:
        return tool_error(
            tool="gitea_close_pull_request",
            code="INVALID_INPUT",
            message="reason must be 500 characters or fewer",
            source="gitea",
        )
    if _contains_ascii_control(reason):
        return tool_error(
            tool="gitea_close_pull_request",
            code="INVALID_INPUT",
            message="reason must not contain control characters",
            source="gitea",
        )
    normalized_superseding_ref: str | None = superseding_ref
    if normalized_superseding_ref is not None:
        normalized_superseding_ref = str(normalized_superseding_ref).strip() or None
        if normalized_superseding_ref is not None and len(normalized_superseding_ref) > 255:
            return tool_error(
                tool="gitea_close_pull_request",
                code="INVALID_INPUT",
                message="superseding_ref must be 255 characters or fewer",
                source="gitea",
            )
        if normalized_superseding_ref is not None and _contains_ascii_control(
            normalized_superseding_ref
        ):
            return tool_error(
                tool="gitea_close_pull_request",
                code="INVALID_INPUT",
                message="superseding_ref must not contain control characters",
                source="gitea",
            )

    already_closed = False
    audit_metadata: dict[str, Any] | None = None
    try:
        async with _server_gitea_client()(token) as client:
            pr = await client.get_pull_request(owner, repo, pull_number)
            head = pr.get("head") or {}
            base = pr.get("base") or {}
            actual_head_sha = str(head.get("sha") or "").lower()
            base_ref = str(base.get("ref") or "")

            if actual_head_sha != expected_head_sha:
                return tool_error(
                    tool="gitea_close_pull_request",
                    code="HEAD_MISMATCH",
                    message="pull request head changed; re-read the PR before closing",
                    source="gitea",
                )
            if pr.get("merged") is True:
                return tool_error(
                    tool="gitea_close_pull_request",
                    code="CLOSE_NOT_CONFIRMED",
                    message="pull request is already merged; refusing close-without-merge cleanup",
                    source="gitea",
                )
            if pr.get("state") == "closed":
                already_closed = True
                confirmed_pr = await client.get_pull_request(owner, repo, pull_number)
            elif pr.get("state") != "open":
                return tool_error(
                    tool="gitea_close_pull_request",
                    code="PR_NOT_OPEN",
                    message=f"pull request #{pull_number} is not open",
                    source="gitea",
                )
            else:
                if normalized_superseding_ref is None:
                    return tool_error(
                        tool="gitea_close_pull_request",
                        code="POLICY_DENIED",
                        message=(
                            "closing an open pull request requires a superseding_ref "
                            "branch in the same repository whose exact head equals "
                            "expected_head_sha"
                        ),
                        source="gitea",
                    )
                try:
                    superseding_branch = _normalize_superseding_branch(normalized_superseding_ref)
                except ValueError as exc:
                    return tool_error(
                        tool="gitea_close_pull_request",
                        code="INVALID_INPUT",
                        message=str(exc),
                        source="gitea",
                    )
                identity = await _require_gitea_identity(client)
                if identity is None:
                    return tool_error(
                        tool="gitea_close_pull_request",
                        code="AUTH_ERROR",
                        message=(
                            "Authenticated Gitea identity and caller fingerprint "
                            "are required to close a pull request"
                        ),
                        source="gitea",
                    )
                username, fingerprint = identity
                try:
                    superseding = await client.get_branch(owner, repo, superseding_branch)
                except httpx.HTTPStatusError as exc:
                    response = getattr(exc, "response", None)
                    if response is not None and response.status_code == 404:
                        return tool_error(
                            tool="gitea_close_pull_request",
                            code="POLICY_DENIED",
                            message=(
                                f"superseding branch {superseding_branch!r} does not exist "
                                "in the target repository"
                            ),
                            source="gitea",
                        )
                    raise
                superseding_commit = superseding.get("commit") or {}
                superseding_head_sha = str(
                    superseding_commit.get("id") or superseding_commit.get("sha") or ""
                ).lower()
                if superseding_head_sha != expected_head_sha:
                    return tool_error(
                        tool="gitea_close_pull_request",
                        code="POLICY_DENIED",
                        message=(
                            f"superseding branch {superseding_branch!r} head does not "
                            "equal expected_head_sha; re-read the branch before closing"
                        ),
                        details={
                            "superseding_branch": superseding_branch,
                            "expected_head_sha": expected_head_sha,
                            "observed_superseding_head_sha": superseding_head_sha,
                        },
                        source="gitea",
                    )
                canonical_superseding_ref = f"refs/heads/{superseding_branch}"
                correlation_id = uuid.uuid4().hex
                audit_metadata = {
                    "owner": owner,
                    "repo": repo,
                    "pull_number": pull_number,
                    "expected_head_sha": expected_head_sha,
                    "reason": reason,
                    "superseding_ref": canonical_superseding_ref,
                    "superseding_head_sha": superseding_head_sha,
                    "gitea_username": username,
                    "caller_fingerprint": fingerprint,
                    "correlation_id": correlation_id,
                }
                audit_logger = _get_gitea_audit_logger()
                try:
                    audit_logger.append_required(
                        McpAuditEvent(
                            event_type="mcp.gitea_destructive_intent",
                            tool="gitea_close_pull_request",
                            action="close_pull_request",
                            decision="allow",
                            reason=reason,
                            metadata=audit_metadata,
                        )
                    )
                except AuditWriteError:
                    return tool_error(
                        tool="gitea_close_pull_request",
                        code="AUDIT_UNAVAILABLE",
                        message="Audit log unavailable; destructive operation refused",
                        source="gitea",
                    )
                await client.close_pull_request(owner, repo, pull_number)
                confirmed_pr = await client.get_pull_request(owner, repo, pull_number)

            confirmed_head = confirmed_pr.get("head") or {}
            confirmed_base = confirmed_pr.get("base") or {}
            confirmed_head_sha = str(confirmed_head.get("sha") or "").lower()
            confirmed_base_ref = str(confirmed_base.get("ref") or "")
            if confirmed_head_sha != expected_head_sha:
                return tool_error(
                    tool="gitea_close_pull_request",
                    code="CLOSE_NOT_CONFIRMED",
                    message="pull request head changed while closing",
                    retryable=True,
                    details={
                        "expected_head_sha": expected_head_sha,
                        "observed_head_sha": confirmed_head_sha,
                    },
                    source="gitea",
                )
            if confirmed_base_ref != base_ref:
                return tool_error(
                    tool="gitea_close_pull_request",
                    code="CLOSE_NOT_CONFIRMED",
                    message="pull request base changed while closing",
                    retryable=True,
                    details={
                        "expected_base": base_ref,
                        "observed_base": confirmed_base_ref,
                    },
                    source="gitea",
                )
            if confirmed_pr.get("state") != "closed" or confirmed_pr.get("merged") is not False:
                return tool_error(
                    tool="gitea_close_pull_request",
                    code="CLOSE_NOT_CONFIRMED",
                    message="Gitea accepted the close request but state=closed and merged=false were not observed",
                    retryable=True,
                    details={
                        "observed_state": confirmed_pr.get("state"),
                        "observed_merged": confirmed_pr.get("merged"),
                    },
                    source="gitea",
                )
            data = {
                "number": pull_number,
                "closed": True,
                "already_closed": already_closed,
                "merged": False,
                "head_sha": expected_head_sha,
                "base": confirmed_base_ref,
                "html_url": confirmed_pr.get("html_url"),
                "verified": True,
            }
            if audit_metadata is not None:
                try:
                    audit_logger = _get_gitea_audit_logger()
                    audit_logger.append(
                        McpAuditEvent(
                            event_type="mcp.gitea_destructive_success",
                            tool="gitea_close_pull_request",
                            action="close_pull_request",
                            decision="allow",
                            reason=reason,
                            metadata=audit_metadata,
                        )
                    )
                except Exception:
                    pass  # best-effort
    except Exception as exc:
        return _remote_api_error("gitea_close_pull_request", "gitea", exc)
    return tool_success("gitea_close_pull_request", result=data, source="gitea")



def _same_gitea_repo_from_pr_head(
    head: dict[str, Any], *, owner: str, repo: str
) -> bool | None:
    """Return whether a PR head is proven to belong to the target repo.

    True means the head repo is the same repository being mutated. False means
    the payload proves a different repo/fork. None means the head repo identity
    is unavailable or ambiguous, so destructive branch deletion must fail closed.
    """
    head_repo = head.get("repo")
    if not isinstance(head_repo, dict):
        return None

    expected_full_name = f"{owner}/{repo}".lower()
    full_name = str(head_repo.get("full_name") or "").strip().lower()
    if full_name:
        return full_name == expected_full_name

    repo_name = str(head_repo.get("name") or "").strip().lower()
    owner_payload = head_repo.get("owner") or {}
    if isinstance(owner_payload, dict):
        owner_name = str(
            owner_payload.get("login") or owner_payload.get("username") or ""
        ).strip().lower()
    else:
        owner_name = ""
    if owner_name and repo_name:
        return owner_name == owner.lower() and repo_name == repo.lower()
    return None


def _gitea_root_tree_signature(payload: Any) -> tuple[tuple[str, str, str], ...] | None:
    """Return a stable root-tree signature from a Gitea contents listing.

    Directory listings expose the root entry name/path, entry type and Git blob
    or tree SHA.  This deliberately compares only the repository root tree: it
    is enough for stale branch cleanup where the operator already identified
    a branch as top-level-tree equivalent, while avoiding a recursive content
    crawl before a destructive branch delete.
    """
    if not isinstance(payload, list):
        return None
    entries: list[tuple[str, str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        path = str(item.get("path") or item.get("name") or "").strip()
        kind = str(item.get("type") or "").strip()
        sha = str(item.get("sha") or "").strip().lower()
        if not path or not kind or not sha:
            return None
        entries.append((path, kind, sha))
    return tuple(sorted(entries))


async def _gitea_root_tree_matches_ref(
    client: Any,
    owner: str,
    repo: str,
    *,
    left_ref: str,
    right_ref: str,
) -> bool | None:
    """Fail-closed proof that two refs have the same repository root tree."""
    try:
        left = await client.get_file(owner, repo, "", branch=left_ref, include_content=False)
        right = await client.get_file(owner, repo, "", branch=right_ref, include_content=False)
    except Exception:
        return None
    left_sig = _gitea_root_tree_signature(left)
    right_sig = _gitea_root_tree_signature(right)
    if left_sig is None or right_sig is None:
        return None
    return left_sig == right_sig


async def gitea_delete_branch(
    owner: str,
    repo: str,
    branch: str,
    expected_head_sha: str,
) -> dict[str, Any]:
    """Delete one remote feature branch with an exact-SHA server-side Git lease."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_delete_branch",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )

    try:
        branch = validate_feature_branch(branch)
        expected_head_sha = validate_expected_sha(expected_head_sha)
    except ValueError as exc:
        return _remote_api_error("gitea_delete_branch", "gitea", exc)

    try:
        async with _server_gitea_client()(token) as client:
            metadata = await client.get_repo(owner, repo)
            default_branch = str(metadata.get("default_branch") or "").strip()
            if branch == default_branch:
                return tool_error(
                    tool="gitea_delete_branch",
                    code="POLICY_DENIED",
                    message=f"deleting default branch {branch!r} is not allowed",
                    source="gitea",
                )
            if metadata.get("archived") is True:
                return tool_error(
                    tool="gitea_delete_branch",
                    code="POLICY_DENIED",
                    message="deleting branches from an archived repository is not allowed",
                    source="gitea",
                )

            branch_info = await client.get_branch(owner, repo, branch)
            if branch_info.get("protected") is True or branch_info.get(
                "effective_branch_protection_name"
            ):
                return tool_error(
                    tool="gitea_delete_branch",
                    code="POLICY_DENIED",
                    message=f"deleting protected branch {branch!r} is not allowed",
                    source="gitea",
                )
            commit = branch_info.get("commit") or {}
            actual_head_sha = str(commit.get("id") or commit.get("sha") or "").lower()
            if actual_head_sha != expected_head_sha:
                return tool_error(
                    tool="gitea_delete_branch",
                    code="HEAD_MISMATCH",
                    message="remote branch head changed; re-read the branch before deleting",
                    source="gitea",
                )

            closed_unmerged_pr_cleanup: list[dict[str, Any]] = []
            page = 1
            while True:
                page_prs = await client.list_pull_requests(
                    owner, repo, state="all", limit=50, page=page
                )
                for pr in page_prs:
                    head = pr.get("head") or {}
                    if str(head.get("ref") or "") != branch:
                        continue
                    same_repo = _same_gitea_repo_from_pr_head(head, owner=owner, repo=repo)
                    if same_repo is None:
                        return tool_error(
                            tool="gitea_delete_branch",
                            code="POLICY_DENIED",
                            message=(
                                f"pull request head repository for branch {branch!r} "
                                "could not be verified"
                            ),
                            source="gitea",
                        )
                    if same_repo is False:
                        continue
                    if pr.get("merged") is True:
                        continue
                    if pr.get("state") == "closed":
                        if not default_branch:
                            return tool_error(
                                tool="gitea_delete_branch",
                                code="POLICY_DENIED",
                                message="repository default branch is unavailable; refusing deletion",
                                source="gitea",
                            )
                        root_tree_matches = await _gitea_root_tree_matches_ref(
                            client,
                            owner,
                            repo,
                            left_ref=expected_head_sha,
                            right_ref=default_branch,
                        )
                        if root_tree_matches is True:
                            closed_unmerged_pr_cleanup.append(
                                {
                                    "number": pr.get("number"),
                                    "state": "closed",
                                    "merged": False,
                                    "default_branch": default_branch,
                                    "head_sha": expected_head_sha,
                                    "root_tree_equivalent_to_default": True,
                                }
                            )
                            continue
                        if root_tree_matches is False:
                            return tool_error(
                                tool="gitea_delete_branch",
                                code="POLICY_DENIED",
                                message=(
                                    f"branch {branch!r} is the head of a closed unmerged "
                                    f"pull request whose root tree differs from default "
                                    f"branch {default_branch!r}; refusing deletion without "
                                    "an explicit cleanup decision"
                                ),
                                details={
                                    "branch": branch,
                                    "pull_number": pr.get("number"),
                                    "default_branch": default_branch,
                                    "expected_head_sha": expected_head_sha,
                                    "root_tree_equivalent_to_default": False,
                                    "mutation_occurred": False,
                                },
                                source="gitea",
                            )
                        return tool_error(
                            tool="gitea_delete_branch",
                            code="POLICY_DENIED",
                            message=(
                                f"could not prove root tree equivalence between branch "
                                f"{branch!r} and default branch {default_branch!r}; "
                                "refusing deletion"
                            ),
                            retryable=True,
                            details={
                                "branch": branch,
                                "pull_number": pr.get("number"),
                                "default_branch": default_branch,
                                "left_ref": expected_head_sha,
                                "right_ref": default_branch,
                                "mutation_occurred": False,
                            },
                            source="gitea",
                        )
                    return tool_error(
                        tool="gitea_delete_branch",
                        code="POLICY_DENIED",
                        message=f"branch {branch!r} is still the head of an unmerged pull request",
                        source="gitea",
                    )
                if len(page_prs) < 50:
                    break
                if page >= 20:
                    return tool_error(
                        tool="gitea_delete_branch",
                        code="POLICY_DENIED",
                        message=(
                            f"branch {branch!r} PR scan exceeded {20 * 50} records; "
                            "exhaustive PR coverage is unproven, refusing deletion"
                        ),
                        source="gitea",
                    )
                page += 1

            permissions = metadata.get("permissions") or {}
            if not permissions.get("push"):
                return tool_error(
                    tool="gitea_delete_branch",
                    code="AUTH_ERROR",
                    message="Configured Gitea identity does not have push access to repository",
                    source="gitea",
                )

            identity = await _require_gitea_identity(client)
            if identity is None:
                return tool_error(
                    tool="gitea_delete_branch",
                    code="AUTH_ERROR",
                    message="Authenticated Gitea identity and caller fingerprint are required to delete a branch",
                    source="gitea",
                )
            username, fingerprint = identity
            correlation_id = uuid.uuid4().hex
            audit_metadata: dict[str, Any] = {
                "owner": owner,
                "repo": repo,
                "branch": branch,
                "expected_head_sha": expected_head_sha,
                "gitea_username": username,
                "caller_fingerprint": fingerprint,
                "correlation_id": correlation_id,
            }
            if closed_unmerged_pr_cleanup:
                audit_metadata["closed_unmerged_pr_cleanup"] = closed_unmerged_pr_cleanup
            audit_logger = _get_gitea_audit_logger()
            try:
                audit_logger.append_required(
                    McpAuditEvent(
                        event_type="mcp.gitea_destructive_intent",
                        tool="gitea_delete_branch",
                        action="delete_branch",
                        decision="allow",
                        reason="branch deletion",
                        metadata=audit_metadata,
                    )
                )
            except AuditWriteError:
                return tool_error(
                    tool="gitea_delete_branch",
                    code="AUDIT_UNAVAILABLE",
                    message="Audit log unavailable; destructive operation refused",
                    source="gitea",
                )

            git_base = configured_gitea_git_base()
            await asyncio.to_thread(
                delete_remote_branch_with_lease,
                owner=owner,
                repo=repo,
                branch=branch,
                expected_sha=expected_head_sha,
                username=username,
                token=token,
                git_base=git_base,
            )
            try:
                audit_logger = _get_gitea_audit_logger()
                audit_logger.append(
                    McpAuditEvent(
                        event_type="mcp.gitea_destructive_success",
                        tool="gitea_delete_branch",
                        action="delete_branch",
                        decision="allow",
                        reason="branch deletion",
                        metadata=audit_metadata,
                    )
                )
            except Exception:
                pass  # best-effort
    except ManagedGitError as exc:
        return tool_error(
            tool="gitea_delete_branch",
            code="GIT_PUSH_FAILED",
            message=str(exc),
            retryable=True,
            source="gitea",
        )
    except Exception as exc:
        return _remote_api_error("gitea_delete_branch", "gitea", exc)

    result: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "deleted": True,
        "head_sha": expected_head_sha,
        "lease_guarded": True,
        "verified_absent": True,
    }
    if closed_unmerged_pr_cleanup:
        result["closed_unmerged_pr_cleanup"] = closed_unmerged_pr_cleanup
    return tool_success(
        "gitea_delete_branch",
        result=result,
        source="gitea",
    )


async def gitea_list_action_runs(
    owner: str, repo: str, status: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """List Gitea Actions workflow runs. Optionally filter by status (completed, running/in_progress, waiting)."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_action_runs",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with _server_gitea_client()(token) as client:
            data = await client.list_action_runs(owner, repo, status=status, limit=limit)
    except Exception as exc:
        return _remote_api_error("gitea_list_action_runs", "gitea", exc)
    return tool_success("gitea_list_action_runs", result=data, source="gitea")


async def gitea_get_action_run(owner: str, repo: str, run_id: int) -> dict[str, Any]:
    """Get details of a specific Gitea Actions workflow run by ID."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_action_run",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with _server_gitea_client()(token) as client:
            data = await client.get_action_run(owner, repo, run_id)
    except Exception as exc:
        return _remote_api_error("gitea_get_action_run", "gitea", exc)
    return tool_success("gitea_get_action_run", result=data, source="gitea")


async def gitea_list_action_run_jobs(owner: str, repo: str, run_id: int) -> dict[str, Any]:
    """List jobs and steps for a Gitea Actions workflow run."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_action_run_jobs",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with _server_gitea_client()(token) as client:
            data = await client.list_action_run_jobs(owner, repo, run_id)
    except Exception as exc:
        return _remote_api_error("gitea_list_action_run_jobs", "gitea", exc)
    return tool_success("gitea_list_action_run_jobs", result=data, source="gitea")


async def gitea_list_action_jobs(
    owner: str,
    repo: str,
    status: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """List repository-wide Gitea Actions jobs. Optionally filter by status
    (pending, queued, running/in_progress, failure, success, skipped)."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_action_jobs",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(page, "page")
        validate_pagination(limit, "limit", max_value=50)
        async with _server_gitea_client()(token) as client:
            data = await client.list_action_jobs(
                owner,
                repo,
                status=status,
                page=page,
                limit=limit,
            )
    except Exception as exc:
        return _remote_api_error("gitea_list_action_jobs", "gitea", exc)
    return tool_success("gitea_list_action_jobs", result=data, source="gitea")


def _validate_action_log_tail_lines(tail_lines: int) -> int:
    if (
        isinstance(tail_lines, bool)
        or not isinstance(tail_lines, int)
        or tail_lines < 1
        or tail_lines > GITEA_ACTION_JOB_LOG_MAX_TAIL_LINES
    ):
        raise ValueError(f"tail_lines must be an integer from 1 to {GITEA_ACTION_JOB_LOG_MAX_TAIL_LINES}")
    return tail_lines


def _tail_action_log(data: dict[str, Any], tail_lines: int) -> dict[str, Any]:
    logs = str(data.get("logs") or "")
    lines = logs.splitlines()
    data = dict(data)
    data["lines_total_after_byte_limit"] = len(lines)
    if len(lines) > tail_lines:
        data["logs"] = "\n".join(lines[-tail_lines:])
        data["lines_returned"] = tail_lines
        data["line_truncated"] = True
    else:
        data["lines_returned"] = len(lines)
        data["line_truncated"] = False
    return data


async def gitea_get_action_job_logs(
    owner: str,
    repo: str,
    job_id: int,
    max_bytes: int = GITEA_ACTION_JOB_LOG_DEFAULT_MAX_BYTES,
    tail_lines: int = GITEA_ACTION_JOB_LOG_DEFAULT_TAIL_LINES,
) -> dict[str, Any]:
    """Download a bounded, redacted tail of one Gitea Actions job log."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_action_job_logs",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        tail_lines = _validate_action_log_tail_lines(tail_lines)
        async with _server_gitea_client()(token) as client:
            data = await client.get_action_job_logs(
                owner,
                repo,
                job_id,
                max_bytes=max_bytes,
            )
            data = _tail_action_log(data, tail_lines)
    except Exception as exc:
        return _remote_api_error("gitea_get_action_job_logs", "gitea", exc)
    return tool_success("gitea_get_action_job_logs", result=data, source="gitea")


async def gitea_list_workflows(owner: str, repo: str) -> dict[str, Any]:
    """List Gitea Actions workflow files in a repository."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_workflows",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with _server_gitea_client()(token) as client:
            data = await client.list_workflows(owner, repo)
    except Exception as exc:
        return _remote_api_error("gitea_list_workflows", "gitea", exc)
    return tool_success("gitea_list_workflows", result=data, source="gitea")


# ── GitHub tools ─────────────────────────────────────────────────


async def gitea_materialize_task_candidate(
    project: str,
    task_id: str,
    owner: str,
    repo: str,
    destination_branch: str,
    expected_diff_sha256: str,
    base_ref: str | None = None,
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
) -> dict[str, Any]:
    """Materialize BASE_HEAD + supervisor diff and atomically record its receipt.

    When a legacy/external task has trusted evidence but is missing the
    control-plane delivery contract, callers may provide the exact base_ref
    and immutable scope/check contract here.  The contract is then persisted
    by the same control-plane primitive used during normal task creation
    before materialization proceeds.
    """
    try:
        info = _server_workspace_registry().project_info(project)
        agent_client = _server_agent_client()
        contract_recorded = False
        if (
            base_ref is not None
            or allowed_files is not None
            or forbidden_files is not None
            or required_checks is not None
        ):
            if not base_ref:
                raise CandidateError(
                    "base_ref is required when seeding a delivery contract",
                    code="INVALID_INPUT",
                )
            await asyncio.to_thread(
                record_task_delivery_contract,
                project=project,
                task_id=task_id,
                base_ref=base_ref,
                allowed_files=allowed_files or [],
                forbidden_files=forbidden_files or [],
                required_checks=required_checks or [],
            )
            contract_recorded = True

        def _verify(staging, expected_sha, checks):
            return verify_candidate_via_docker(
                staging_root=staging,
                expected_sha=expected_sha,
                required_checks=checks,
            )

        receipt = await asyncio.to_thread(
            materialize_task_candidate,
            project_root=info["root"],
            project=project,
            task_id=task_id,
            destination_owner=owner,
            destination_repo=repo,
            destination_branch=destination_branch,
            expected_diff_sha256=expected_diff_sha256,
            job_result=lambda jid: agent_client.job_result(jid, redact_output=True),
            verify_candidate=_verify,
        )
    except CandidateError as exc:
        return tool_error(
            tool="gitea_materialize_task_candidate",
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            hint=exc.hint,
            details=exc.details,
            source="gitea",
        )
    except Exception:
        return tool_error(
            tool="gitea_materialize_task_candidate",
            code="INVALID_INPUT",
            message=f"Unknown registered project or unavailable candidate evidence: {project!r}",
            source="gitea",
        )
    verifier_evidence = receipt.get("verifier_receipt")
    if not isinstance(verifier_evidence, dict):
        verifier_evidence = {}
    return tool_success(
        "gitea_materialize_task_candidate",
        result={
            "project": project,
            "task_id": task_id,
            "owner": owner,
            "repo": repo,
            "branch": destination_branch,
            "base_head": receipt["base_head"],
            "implementation_diff_sha256": receipt["implementation_diff_sha256"],
            "candidate_head_sha": receipt["candidate_head_sha"],
            "created_at": receipt["created_at"],
            "delivery_contract_recorded": contract_recorded,
            "verifier_receipt_sha256": receipt.get("verifier_receipt_sha256"),
            "verifier_receipt_schema_version": receipt.get(
                "verifier_receipt_schema_version"
            ),
            "check_count": verifier_evidence.get("check_count"),
            "verifier_image": verifier_evidence.get("verifier_image"),
        },
        source="gitea",
    )


async def gitea_push_local_ref(
    project: str,
    task_id: str,
    owner: str,
    repo: str,
    destination_branch: str,
    expected_sha: str,
) -> dict[str, Any]:
    """Push only the exact receipt-bound task candidate from trusted staging."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_push_local_ref",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        info = _server_workspace_registry().project_info(project)
        receipt, staging = await asyncio.to_thread(
            validate_task_candidate_for_push,
            project_root=info["root"],
            project=project,
            task_id=task_id,
            destination_owner=owner,
            destination_repo=repo,
            destination_branch=destination_branch,
            expected_sha=expected_sha,
        )
    except CandidateError as exc:
        return tool_error(
            tool="gitea_push_local_ref",
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            hint=exc.hint,
            details=exc.details,
            source="gitea",
        )
    except Exception:
        return tool_error(
            tool="gitea_push_local_ref",
            code="INVALID_INPUT",
            message=f"Unknown registered project: {project!r}",
            source="gitea",
        )

    # Fail closed before any remote access.  Push is only authorized when the
    # receipt returned by validate_task_candidate_for_push -- which revalidated
    # verifier evidence, its canonical digest, the exact candidate head and the
    # immutable required checks -- derives as trusted here from scratch.  A
    # stored or caller-supplied naked checks_verified bool is never read.
    checks_verified = verifier_receipt_is_trusted(receipt)
    if not checks_verified:
        return tool_error(
            tool="gitea_push_local_ref",
            code="CANDIDATE_RECEIPT_INVALID",
            message="candidate receipt is missing trusted verifier evidence",
            hint="Re-materialize the candidate so push can revalidate the digest-bound verifier receipt.",
            details={"mutation_occurred": False},
            source="gitea",
        )

    try:
        git_base = configured_gitea_git_base()
        async with _server_gitea_client()(token) as client:
            user = await client.get_user()
            metadata = await client.get_repo(owner, repo)
            permissions = metadata.get("permissions") or {}
            if not permissions.get("push"):
                return tool_error(
                    tool="gitea_push_local_ref",
                    code="AUTH_ERROR",
                    message="Configured Gitea identity does not have push access to repository",
                    source="gitea",
                )
            default_branch = str(metadata.get("default_branch") or "").strip()
            if not default_branch:
                return tool_error(
                    tool="gitea_push_local_ref",
                    code="POLICY_DENIED",
                    message="Repository default branch is unavailable; refusing trusted push",
                    source="gitea",
                )
            if destination_branch == default_branch:
                return tool_error(
                    tool="gitea_push_local_ref",
                    code="POLICY_DENIED",
                    message="Trusted candidate push to the repository default branch is not allowed",
                    source="gitea",
                )

            remote_branch: dict[str, Any] | None = None
            try:
                remote_branch = await client.get_branch(owner, repo, destination_branch)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
            if remote_branch is not None and remote_branch.get("protected") is not False:
                return tool_error(
                    tool="gitea_push_local_ref",
                    code="POLICY_DENIED",
                    message="Trusted candidate push to a protected or unverifiable branch is not allowed",
                    source="gitea",
                )

            username = str(user.get("login") or user.get("username") or "").strip()
            if not username:
                return tool_error(
                    tool="gitea_push_local_ref",
                    code="AUTH_ERROR",
                    message="Configured Gitea identity has no usable username",
                    source="gitea",
                )
            await asyncio.to_thread(
                push_trusted_staging_sha,
                staging_root=staging,
                owner=owner,
                repo=repo,
                destination_branch=destination_branch,
                expected_sha=receipt["candidate_head_sha"],
                username=username,
                token=token,
                git_base=git_base,
            )
            try:
                remote_branch = await client.get_branch(owner, repo, destination_branch)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                remote_branch = None
    except ManagedGitError as exc:
        return tool_error(
            tool="gitea_push_local_ref",
            code="GIT_PUSH_FAILED",
            message=str(exc),
            source="gitea",
        )
    except Exception as exc:
        return _remote_api_error("gitea_push_local_ref", "gitea", exc)

    expected = receipt["candidate_head_sha"]
    remote_ref = f"refs/heads/{destination_branch}"
    commit = (remote_branch or {}).get("commit") or {}
    observed_sha = str(commit.get("id") or "").lower() or None
    verification = {
        "remote_ref": remote_ref,
        "expected_sha": expected,
        "remote_observed_sha": observed_sha,
        "owner": owner,
        "repo": repo,
        "branch": destination_branch,
    }
    if observed_sha == expected:
        return tool_success(
            "gitea_push_local_ref",
            result={
                "project": project,
                "task_id": task_id,
                "owner": owner,
                "repo": repo,
                "branch": destination_branch,
                "remote_ref": remote_ref,
                "sha": expected,
                "remote_observed_sha": observed_sha,
                "verified": True,
                "checks_verified": checks_verified,
            },
            source="gitea",
        )
    return tool_error(
        tool="gitea_push_local_ref",
        code="CHECK_FAILED",
        message="Remote branch does not resolve to candidate_head_sha after push",
        retryable=True,
        hint="Fetch the remote ref and compare it with error.details.expected_sha before retrying the trusted push.",
        details=verification,
        source="gitea",
    )


def _contract_lines(value: str) -> list[str]:
    lines: list[str] = []
    for raw in str(value or "").replace(",", "\n").splitlines():
        item = raw.strip()
        if item:
            lines.append(item)
    return lines


async def gitea_push_verified_commit(
    project: str,
    owner: str,
    repo: str,
    destination_branch: str,
    expected_base_sha: str,
    expected_head_sha: str,
    allowed_files: str,
    required_checks: str,
) -> dict[str, Any]:
    """Verify and push one exact commit from a registered clean workspace."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_push_verified_commit",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    allowed = _contract_lines(allowed_files)
    checks = [line.strip() for line in str(required_checks or "").splitlines() if line.strip()]
    if not allowed:
        return tool_error(
            tool="gitea_push_verified_commit",
            code="INVALID_INPUT",
            message="allowed_files must contain at least one path pattern",
            source="gitea",
        )
    if not checks:
        return tool_error(
            tool="gitea_push_verified_commit",
            code="INVALID_INPUT",
            message="required_checks must contain at least one verification command",
            source="gitea",
        )

    try:
        info = _server_workspace_registry().project_info(project)
        project_root = info["root"]
        proof_before = await asyncio.to_thread(
            verify_registered_delivery_workspace,
            project_root=project_root,
            expected_base_sha=expected_base_sha,
            expected_head_sha=expected_head_sha,
            allowed_files=allowed,
        )
        await asyncio.to_thread(
            verify_workspace_via_docker,
            workspace_root=Path(project_root),
            expected_sha=proof_before["head_sha"],
            required_checks=checks,
        )
        proof_after = await asyncio.to_thread(
            verify_registered_delivery_workspace,
            project_root=project_root,
            expected_base_sha=expected_base_sha,
            expected_head_sha=expected_head_sha,
            allowed_files=allowed,
        )
        if proof_after != proof_before:
            return tool_error(
                tool="gitea_push_verified_commit",
                code="WORKSPACE_CONTENDED",
                message="delivery workspace changed during isolated verification",
                retryable=True,
                source="gitea",
                details={
                    "phase": "candidate_checkout",
                    "mutation_occurred": False,
                    "expected_base_sha": expected_base_sha,
                    "expected_head_sha": expected_head_sha,
                    "allowed_files": allowed,
                    "required_checks": checks,
                    "branch": destination_branch,
                },
            )
    except (VerifiedWorkspaceError, CandidateVerificationError) as exc:
        return _delivery_verification_error(
            tool="gitea_push_verified_commit",
            project=project,
            owner=owner,
            repo=repo,
            destination_branch=destination_branch,
            expected_base_sha=expected_base_sha,
            expected_head_sha=expected_head_sha,
            allowed=allowed,
            checks=checks,
            exc=exc,
        )
    except Exception as exc:
        return tool_error(
            tool="gitea_push_verified_commit",
            code="CANDIDATE_VERIFICATION_FAILED",
            message=f"isolated delivery verification failed: {type(exc).__name__}",
            retryable=False,
            source="gitea",
            details={
                "phase": "push_preflight",
                "mutation_occurred": False,
                "expected_base_sha": expected_base_sha,
                "expected_head_sha": expected_head_sha,
                "allowed_files": allowed,
                "required_checks": checks,
                "branch": destination_branch,
            },
        )

    try:
        destination_branch = validate_feature_branch(destination_branch)
        expected_head = validate_expected_sha(expected_head_sha)
        git_base = configured_gitea_git_base()
        async with _server_gitea_client()(token) as client:
            user = await client.get_user()
            metadata = await client.get_repo(owner, repo)
            permissions = metadata.get("permissions") or {}
            if not permissions.get("push"):
                return tool_error(
                    tool="gitea_push_verified_commit",
                    code="AUTH_ERROR",
                    message="Configured Gitea identity does not have push access to repository",
                    source="gitea",
                )
            default_branch = str(metadata.get("default_branch") or "").strip()
            if not default_branch or destination_branch == default_branch:
                return tool_error(
                    tool="gitea_push_verified_commit",
                    code="POLICY_DENIED",
                    message="Trusted delivery push to the repository default branch is not allowed",
                    source="gitea",
                )
            try:
                remote_branch = await client.get_branch(owner, repo, destination_branch)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                remote_branch = None
            if remote_branch is not None and remote_branch.get("protected") is not False:
                return tool_error(
                    tool="gitea_push_verified_commit",
                    code="POLICY_DENIED",
                    message="Trusted delivery push to a protected or unverifiable branch is not allowed",
                    source="gitea",
                )
            username = str(user.get("login") or user.get("username") or "").strip()
            if not username:
                return tool_error(
                    tool="gitea_push_verified_commit",
                    code="AUTH_ERROR",
                    message="Configured Gitea identity has no usable username",
                    source="gitea",
                )
            await asyncio.to_thread(
                push_exact_sha,
                project_root=project_root,
                owner=owner,
                repo=repo,
                destination_branch=destination_branch,
                expected_sha=expected_head,
                username=username,
                token=token,
                git_base=git_base,
            )
            remote_branch = await client.get_branch(owner, repo, destination_branch)
    except ManagedGitError as exc:
        return tool_error(
            tool="gitea_push_verified_commit",
            code="GIT_PUSH_FAILED",
            message=str(exc),
            retryable=True,
            source="gitea",
        )
    except Exception as exc:
        return _remote_api_error("gitea_push_verified_commit", "gitea", exc)

    commit = (remote_branch or {}).get("commit") or {}
    observed_sha = str(commit.get("id") or commit.get("sha") or "").lower() or None
    if observed_sha != expected_head:
        return tool_error(
            tool="gitea_push_verified_commit",
            code="CHECK_FAILED",
            message="Remote branch does not resolve to expected_head_sha after trusted push",
            retryable=True,
            details={
                "phase": "push_preflight",
                "mutation_occurred": True,
                "expected_head_sha": expected_head,
                "observed_head_sha": observed_sha,
                "branch": destination_branch,
            },
            source="gitea",
        )
    return tool_success(
        "gitea_push_verified_commit",
        result={
            "project": project,
            "owner": owner,
            "repo": repo,
            "branch": destination_branch,
            "base_sha": proof_after["base_sha"],
            "head_sha": expected_head,
            "changed_files": proof_after["changed_files"],
            "allowed_files": proof_after["allowed_files"],
            "required_checks": checks,
            "clean": True,
            "scope_verified": True,
            "checks_verified": True,
            "remote_observed_sha": observed_sha,
            "verified": True,
        },
        source="gitea",
    )


async def github_get_repo(owner: str, repo: str) -> dict[str, Any]:
    """Get GitHub repository metadata (login, visibility, default branch, permissions, counters, topics)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_get_repo",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        async with _server_github_client()(token) as client:
            data = await client.get_repo(owner, repo)
    except Exception as exc:
        return _remote_api_error("github_get_repo", "github", exc)
    return tool_success("github_get_repo", result=_minimize_github_repo(data), source="github")


async def github_list_branches(owner: str, repo: str, per_page: int = 30) -> dict[str, Any]:
    """List branches in a GitHub repository."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_list_branches",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        validate_pagination(per_page, "per_page")
        async with _server_github_client()(token) as client:
            raw = await client.list_branches(owner, repo, per_page=per_page)
            data = normalize_list_response(
                raw,
                meta=list_pagination_meta(len(raw), per_page),
            )
    except Exception as exc:
        return _remote_api_error("github_list_branches", "github", exc)
    return tool_success("github_list_branches", result=data, source="github")


async def github_list_commits(
    owner: str, repo: str, sha: str | None = None, per_page: int = 30
) -> dict[str, Any]:
    """List commits in a GitHub repository."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_list_commits",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        validate_pagination(per_page, "per_page")
        async with _server_github_client()(token) as client:
            raw = await client.list_commits(owner, repo, sha=sha, per_page=per_page)
            data = normalize_list_response(
                raw,
                meta=list_pagination_meta(len(raw), per_page),
            )
    except Exception as exc:
        return _remote_api_error("github_list_commits", "github", exc)
    return tool_success("github_list_commits", result=data, source="github")


async def github_get_file(
    owner: str, repo: str, path: str = "", branch: str | None = None
) -> dict[str, Any]:
    """Get a file or directory from a GitHub repository. Omit path (or
    pass "") to list the repository root."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_get_file",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        async with _server_github_client()(token) as client:
            data = await client.get_file(owner, repo, path, branch=branch)
    except Exception as exc:
        return _remote_api_error("github_get_file", "github", exc)
    return tool_success("github_get_file", result=data, source="github")


async def github_list_issues(
    owner: str, repo: str, state: str = "open", per_page: int = 30
) -> dict[str, Any]:
    """List issues in a GitHub repository. State: open, closed, all."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_list_issues",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        validate_pagination(per_page, "per_page")
        async with _server_github_client()(token) as client:
            raw = await client.list_issues(owner, repo, state=state, per_page=per_page)
            data = normalize_list_response(
                [minimize_issue_payload(i, provider="github") for i in raw],
                meta=list_pagination_meta(len(raw), per_page),
            )
    except Exception as exc:
        return _remote_api_error("github_list_issues", "github", exc)
    return tool_success("github_list_issues", result=data, source="github")


async def github_get_issue(owner: str, repo: str, issue_number: int) -> dict[str, Any]:
    """Get details of a specific GitHub issue by number."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_get_issue",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        async with _server_github_client()(token) as client:
            raw = await client.get_issue(owner, repo, issue_number)
            data = minimize_issue_payload(raw, provider="github")
    except Exception as exc:
        return _remote_api_error("github_get_issue", "github", exc)
    return tool_success("github_get_issue", result=data, source="github")


async def github_list_pull_requests(
    owner: str, repo: str, state: str = "open", per_page: int = 30
) -> dict[str, Any]:
    """List pull requests in a GitHub repository. State: open, closed, all."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_list_pull_requests",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        validate_pagination(per_page, "per_page")
        async with _server_github_client()(token) as client:
            raw = await client.list_pull_requests(owner, repo, state=state, per_page=per_page)
            data = normalize_list_response(
                [minimize_issue_payload(i, provider="github") for i in raw],
                meta=list_pagination_meta(len(raw), per_page),
            )
    except Exception as exc:
        return _remote_api_error("github_list_pull_requests", "github", exc)
    return tool_success("github_list_pull_requests", result=data, source="github")


async def github_get_pull_request(owner: str, repo: str, pull_number: int) -> dict[str, Any]:
    """Get details of a specific GitHub pull request by number."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_get_pull_request",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        async with _server_github_client()(token) as client:
            raw = await client.get_pull_request(owner, repo, pull_number)
            data = minimize_issue_payload(raw, provider="github")
    except Exception as exc:
        return _remote_api_error("github_get_pull_request", "github", exc)
    return tool_success("github_get_pull_request", result=data, source="github")

def register_all() -> None:
    register_tool("gitea_get_repo")(gitea_get_repo)
    register_tool("gitea_list_branches")(gitea_list_branches)
    register_tool("gitea_list_commits")(gitea_list_commits)
    register_tool("gitea_get_file")(gitea_get_file)
    register_tool("gitea_list_issues")(gitea_list_issues)
    register_tool("gitea_get_issue")(gitea_get_issue)
    register_tool("gitea_list_pull_requests")(gitea_list_pull_requests)
    register_tool("gitea_get_pull_request")(gitea_get_pull_request)
    register_tool("gitea_create_pull_request")(gitea_create_pull_request)
    register_tool("gitea_merge_pull_request")(gitea_merge_pull_request)
    register_tool("gitea_close_pull_request")(gitea_close_pull_request)
    register_tool("gitea_delete_branch")(gitea_delete_branch)
    register_tool("gitea_materialize_task_candidate")(gitea_materialize_task_candidate)
    register_tool("gitea_push_local_ref")(gitea_push_local_ref)
    register_tool("gitea_push_verified_commit")(gitea_push_verified_commit)
    register_tool("gitea_list_action_runs")(gitea_list_action_runs)
    register_tool("gitea_get_action_run")(gitea_get_action_run)
    register_tool("gitea_list_action_run_jobs")(gitea_list_action_run_jobs)
    register_tool("gitea_list_action_jobs")(gitea_list_action_jobs)
    register_tool("gitea_get_action_job_logs")(gitea_get_action_job_logs)
    register_tool("gitea_list_workflows")(gitea_list_workflows)
    register_tool("github_get_repo")(github_get_repo)
    register_tool("github_list_branches")(github_list_branches)
    register_tool("github_list_commits")(github_list_commits)
    register_tool("github_get_file")(github_get_file)
    register_tool("github_list_issues")(github_list_issues)
    register_tool("github_get_issue")(github_get_issue)
    register_tool("github_list_pull_requests")(github_list_pull_requests)
    register_tool("github_get_pull_request")(github_get_pull_request)
