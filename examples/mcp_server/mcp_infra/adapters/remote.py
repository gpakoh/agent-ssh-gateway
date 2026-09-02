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
from typing import Any

import httpx
from tool_results import tool_error, tool_success, validate_pagination

from examples.mcp_client_remote.fleet.github_client import (
    normalize_list_response,
)
from examples.mcp_client_remote.fleet.shared import (
    list_pagination_meta,
    minimize_issue_payload,
)
from examples.mcp_server.candidate_verifier import verify_candidate_via_docker
from examples.mcp_server.managed_git import (
    ManagedGitError,
    configured_gitea_git_base,
    push_trusted_staging_sha,
)
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.tool_registry import register_tool
from examples.mcp_server.task_candidate import (
    CandidateError,
    materialize_task_candidate,
    validate_task_candidate_for_push,
)


def _server_gitea_client():
    return server_attr("GiteaClient")


def _server_github_client():
    return server_attr("GitHubClient")

def _server_agent_client():
    return server_attr("get_agent_client")()


def _server_workspace_registry():
    return server_attr("_get_workspace_registry")()



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
    owner: str, repo: str, path: str = "", branch: str | None = None
) -> dict[str, Any]:
    """Get a file or directory from a Gitea repository. Omit path (or
    pass "") to list the repository root."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_file",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with _server_gitea_client()(token) as client:
            data = await client.get_file(owner, repo, path, branch=branch)
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
    except Exception as exc:
        return _remote_api_error("gitea_create_pull_request", "gitea", exc)
    return tool_success("gitea_create_pull_request", result=data, source="gitea")


async def gitea_merge_pull_request(
    owner: str,
    repo: str,
    pull_number: int,
    expected_head_sha: str,
    method: str = "merge",
) -> dict[str, Any]:
    """Merge an open, mergeable PR only when its expected head has green CI."""
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
    if method != "merge":
        return tool_error(
            tool="gitea_merge_pull_request",
            code="INVALID_INPUT",
            message="only merge method 'merge' is allowed",
            source="gitea",
        )

    try:
        async with _server_gitea_client()(token) as client:
            pr = await client.get_pull_request(owner, repo, pull_number)
            head = pr.get("head") or {}
            base = pr.get("base") or {}
            actual_head_sha = str(head.get("sha") or "").lower()
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

            actions = await client.list_action_runs(owner, repo, status=None, limit=50)
            matching_runs = [
                run
                for run in actions.get("workflow_runs", [])
                if run.get("event") == "pull_request" and run.get("head_sha") == expected_head_sha
            ]
            latest_run = (
                max(matching_runs, key=lambda run: int(run.get("id") or -1))
                if matching_runs
                else None
            )
            if (
                not latest_run
                or latest_run.get("status") != "completed"
                or latest_run.get("conclusion") != "success"
            ):
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="CI_NOT_GREEN",
                    message="latest pull_request CI for expected_head_sha is not successful",
                    retryable=True,
                    source="gitea",
                )

            await client.merge_pull_request(
                owner,
                repo,
                pull_number,
                expected_head_sha=expected_head_sha,
                method=method,
            )
            merged_pr = await client.get_pull_request(owner, repo, pull_number)
            if merged_pr.get("merged") is not True:
                return tool_error(
                    tool="gitea_merge_pull_request",
                    code="MERGE_NOT_CONFIRMED",
                    message="Gitea accepted the merge request but merged=true was not observed",
                    retryable=True,
                    source="gitea",
                )
            data = {
                "number": pull_number,
                "merged": True,
                "head_sha": expected_head_sha,
                "base": base_ref,
                "method": method,
                "merge_commit_sha": merged_pr.get("merge_commit_sha"),
                "html_url": merged_pr.get("html_url"),
            }
    except Exception as exc:
        return _remote_api_error("gitea_merge_pull_request", "gitea", exc)
    return tool_success("gitea_merge_pull_request", result=data, source="gitea")


async def gitea_close_pull_request(
    owner: str,
    repo: str,
    pull_number: int,
    expected_head_sha: str,
) -> dict[str, Any]:
    """Close an open PR protected by an exact head-SHA check.

    Never merges and never deletes branches: the only mutation is a
    single state=closed PATCH issued after a fresh GET confirms the PR
    is still open and its head still equals expected_head_sha. Any head
    mismatch fails closed with zero writes. A PR already closed whose
    head still matches is an idempotent success (already_closed=true).
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

    already_closed = False
    try:
        async with _server_gitea_client()(token) as client:
            pr = await client.get_pull_request(owner, repo, pull_number)
            head = pr.get("head") or {}
            base = pr.get("base") or {}
            actual_head_sha = str(head.get("sha") or "").lower()
            base_ref = str(base.get("ref") or "")
            html_url = pr.get("html_url")

            if actual_head_sha != expected_head_sha:
                return tool_error(
                    tool="gitea_close_pull_request",
                    code="HEAD_MISMATCH",
                    message="pull request head changed; re-read the PR before closing",
                    source="gitea",
                )
            if pr.get("state") == "closed":
                already_closed = True
            elif pr.get("state") != "open":
                return tool_error(
                    tool="gitea_close_pull_request",
                    code="PR_NOT_OPEN",
                    message=f"pull request #{pull_number} is not open",
                    source="gitea",
                )
            else:
                await client.close_pull_request(owner, repo, pull_number)
            data = {
                "number": pull_number,
                "closed": True,
                "already_closed": already_closed,
                "head_sha": expected_head_sha,
                "base": base_ref,
                "html_url": html_url,
            }
    except Exception as exc:
        return _remote_api_error("gitea_close_pull_request", "gitea", exc)
    return tool_success("gitea_close_pull_request", result=data, source="gitea")


async def gitea_list_action_runs(
    owner: str, repo: str, status: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """List Gitea Actions workflow runs. Optionally filter by status (completed, running, waiting)."""
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
) -> dict[str, Any]:
    """Materialize BASE_HEAD + supervisor diff and atomically record its receipt."""
    try:
        info = _server_workspace_registry().project_info(project)
        agent_client = _server_agent_client()

        def _verify(staging, expected_sha, checks):
            verify_candidate_via_docker(
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
    register_tool("gitea_materialize_task_candidate")(gitea_materialize_task_candidate)
    register_tool("gitea_push_local_ref")(gitea_push_local_ref)
    register_tool("gitea_list_action_runs")(gitea_list_action_runs)
    register_tool("gitea_get_action_run")(gitea_get_action_run)
    register_tool("gitea_list_action_run_jobs")(gitea_list_action_run_jobs)
    register_tool("gitea_list_workflows")(gitea_list_workflows)
    register_tool("github_get_repo")(github_get_repo)
    register_tool("github_list_branches")(github_list_branches)
    register_tool("github_list_commits")(github_list_commits)
    register_tool("github_get_file")(github_get_file)
    register_tool("github_list_issues")(github_list_issues)
    register_tool("github_get_issue")(github_get_issue)
    register_tool("github_list_pull_requests")(github_list_pull_requests)
    register_tool("github_get_pull_request")(github_get_pull_request)
