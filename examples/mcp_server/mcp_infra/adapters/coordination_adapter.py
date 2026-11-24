"""Project-scoped Py Coordination MCP tools using Gitea issues as durable state."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any

from pydantic import StrictInt
from tool_results import tool_error, tool_success

from app.coordination_close import CloseRefused, close_thread
from examples.mcp_server.coordination import (
    CoordinationOutcomeUnknown,
    board_from_threads,
    create_thread,
    list_threads_preview,
    open_thread,
    preview_thread_state,
    resolve_coordination_repo,
    validate_project,
    write_event,
)
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.adapters import remote
from examples.mcp_server.mcp_infra.tool_registry import register_tool


def _owner() -> str:
    owner = os.environ.get("MCP_COORD_GITEA_OWNER", "gpakoh").strip()
    return validate_project(owner)


def _registered(project: str) -> str:
    name = validate_project(project)
    try:
        metadata = server_attr("_get_workspace_registry")().project_info(name)
    except Exception as exc:
        raise ValueError("project is not registered in SSH Gateway") from exc
    return resolve_coordination_repo(name, _owner(), metadata)


def _principal() -> str | None:
    # Gitea sees the shared service account; this adds an opaque, verified
    # Gateway caller pseudonym without leaking the bearer token itself.
    fp = remote._caller_fingerprint()
    return hashlib.sha256(fp.encode()).hexdigest()[:16] if fp else None


def _fail(tool: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, RuntimeError) and str(exc) == "GITEA_TOKEN is not configured":
        return tool_error(tool=tool, code="DEPENDENCY_MISSING", message="GITEA_TOKEN is not configured", source="gitea")
    if isinstance(exc, CoordinationOutcomeUnknown):
        return tool_error(
            tool=tool, code="MUTATION_OUTCOME_UNKNOWN", message=str(exc),
            hint="Reopen the target thread or inbox using the original request_key before retrying.",
            retryable=False, source="gitea",
        )
    return remote._remote_api_error(tool, "gitea", exc)


async def _connection(tool: str, project: str, *, write: bool) -> tuple[str, str]:
    # Pure validation occurs before any network request or write.
    repo = _registered(project)
    owner = _owner()
    if write and _principal() is None:
        raise PermissionError("authenticated Gateway caller identity required for coordination writes")
    if not os.environ.get("GITEA_TOKEN"):
        raise RuntimeError("GITEA_TOKEN is not configured")
    return owner, repo


async def _verified_preview(
    client: Any, owner: str, repo: str, all_threads: list[dict[str, Any]], limit: int,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], list[int], int]:
    """Backfill unreadable threads from a capped lookahead, never invent states.

    A long/unreadable first thread should not prevent the next healthy thread
    from occupying a requested preview slot. Bounded to at most 2 * limit
    issue-comment probes; remaining threads and all ambiguous states stay
    outside the verified result.
    """
    verified: list[dict[str, Any]] = []
    incomplete_numbers: list[int] = []
    examined = 0
    for item in all_threads[offset : offset + 2 * limit]:
        if len(verified) == limit:
            break
        examined += 1
        state, incomplete = await preview_thread_state(
            client, owner, repo, item["number"]
        )
        if incomplete or state is None:
            incomplete_numbers.append(item["number"])
            continue
        verified.append({**item, "coord_state": state})
    return verified, incomplete_numbers, examined


_SUMMARY_MAX_THREADS = 50
_SUMMARY_READ_TIMEOUT = 8.0  # Total optional summary budget, not per issue.
_RESUME_PR_LIMIT = 10
_RESUME_RUN_LIMIT = 20
_RESUME_READ_TIMEOUT = 8.0


def _resume_sha(value: Any) -> str | None:
    if isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def _resume_ref(value: Any) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable():
        return value
    return None


async def _resume_read(client: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    # Read-only provider errors are isolated; never serialize exception bodies.
    # asyncio cancellation still propagates to the request owner.
    try:
        return await asyncio.wait_for(getattr(client, method)(*args, **kwargs), _RESUME_READ_TIMEOUT)
    except Exception:
        return None


async def _project_resume(client: Any, owner: str, repo: str) -> dict[str, Any]:
    """Bounded observations from the same registered repository and read scope."""
    result: dict[str, Any] = {
        "repository": f"{owner}/{repo}",
        "repo_state": "unavailable", "open_issues_count": None,
        "default_branch": None, "default_head_sha": None, "default_head_state": "unavailable",
        "open_prs_state": "unavailable", "open_prs": [], "open_pr_limit": _RESUME_PR_LIMIT,
        "ci_state": "unavailable", "ci_run_limit": _RESUME_RUN_LIMIT,
        "ci_window_truncated": None, "default_head_runs": [],
        "atomic_snapshot": False, "authorizes_mutation": False,
        "note": "Read-only observations, not merge/release evidence. Unmatched CI is unknown; read exact PR/run/jobs before acting. Local workspace and handoff require their own scoped tools.",
    }
    raw_repo = await _resume_read(client, "get_repo", owner, repo)
    if not isinstance(raw_repo, dict):
        return result
    if raw_repo.get("full_name") != result["repository"]:
        result["repo_state"] = "identity_unverified"
        return result
    result["repo_state"] = "observed"
    count = raw_repo.get("open_issues_count")
    if type(count) is int and count >= 0:
        result["open_issues_count"] = count
    branch = _resume_ref(raw_repo.get("default_branch"))
    result["default_branch"] = branch

    async def read_branch() -> Any:
        if branch is None:
            return None
        return await _resume_read(client, "get_branch", owner, repo, branch)

    raw_branch, raw_prs, raw_ci = await asyncio.gather(
        read_branch(),
        _resume_read(client, "list_pull_requests", owner, repo, state="open", limit=_RESUME_PR_LIMIT),
        _resume_read(client, "list_action_runs", owner, repo, limit=_RESUME_RUN_LIMIT),
    )
    if isinstance(raw_branch, dict) and raw_branch.get("name") == branch:
        commit = raw_branch.get("commit")
        sha = _resume_sha(commit.get("id")) if isinstance(commit, dict) else None
        if sha is not None:
            result["default_head_sha"] = sha
            result["default_head_state"] = "observed"

    prs: list[dict[str, Any]] = []
    if isinstance(raw_prs, list):
        result["open_prs_state"] = "partial" if len(raw_prs) >= _RESUME_PR_LIMIT else "observed"
        seen_prs: set[int] = set()
        for pr in raw_prs[:_RESUME_PR_LIMIT]:
            if not isinstance(pr, dict):
                result["open_prs_state"] = "partial"
                continue
            head, base = pr.get("head"), pr.get("base")
            number = pr.get("number")
            if (type(number) is not int or number < 1 or number in seen_prs
                    or pr.get("state") != "open" or not isinstance(head, dict) or not isinstance(base, dict)):
                result["open_prs_state"] = "partial"
                continue
            head_sha, base_sha = _resume_sha(head.get("sha")), _resume_sha(base.get("sha"))
            head_ref, base_ref = _resume_ref(head.get("ref")), _resume_ref(base.get("ref"))
            if None in (head_sha, base_sha, head_ref, base_ref):
                result["open_prs_state"] = "partial"
                continue
            base_repo = base.get("repo")
            if isinstance(base_repo, dict) and base_repo.get("full_name") != result["repository"]:
                result["open_prs_state"] = "partial"
                continue
            seen_prs.add(number)
            prs.append({"number": number, "head_sha": head_sha, "head_ref": head_ref,
                        "base_sha": base_sha, "base_ref": base_ref, "runs": []})
    result["open_prs"] = prs

    if not isinstance(raw_ci, dict) or not isinstance(raw_ci.get("workflow_runs"), list):
        return result
    runs = raw_ci["workflow_runs"]
    result["ci_state"] = "observed"
    total = raw_ci.get("total_count")
    if type(total) is int and total >= len(runs):
        result["ci_window_truncated"] = total > min(len(runs), _RESUME_RUN_LIMIT)
    elif len(runs) >= _RESUME_RUN_LIMIT:
        result["ci_window_truncated"] = True
    if len(runs) > _RESUME_RUN_LIMIT:
        result["ci_state"] = "partial"
    seen_runs: set[int] = set()
    for run in runs[:_RESUME_RUN_LIMIT]:
        if not isinstance(run, dict):
            result["ci_state"] = "partial"
            continue
        repository = run.get("repository")
        run_id, attempt = run.get("id"), run.get("run_attempt")
        sha = _resume_sha(run.get("head_sha"))
        status, conclusion, event = run.get("status"), run.get("conclusion"), run.get("event")
        if (not isinstance(repository, dict) or repository.get("full_name") != result["repository"]
                or type(run_id) is not int or run_id < 1 or run_id in seen_runs
                or type(attempt) is not int or attempt < 1 or sha is None
                or not isinstance(status, str) or len(status) > 32 or not status.isprintable()
                or (conclusion is not None and (not isinstance(conclusion, str) or len(conclusion) > 32 or not conclusion.isprintable()))):
            result["ci_state"] = "partial"
            continue
        seen_runs.add(run_id)
        receipt = {"id": run_id, "head_sha": sha, "run_attempt": attempt, "status": status, "conclusion": conclusion}
        if event == "push" and sha == result["default_head_sha"] and run.get("head_branch") == branch:
            result["default_head_runs"].append(receipt)
        elif event == "pull_request":
            for pr in prs:
                if pr["head_sha"] == sha:
                    pr["runs"].append(receipt)
    return result


async def _verified_global_summary(
    client: Any, owner: str, repo: str, all_threads: list[dict[str, Any]],
    source_scan_truncated: bool,
) -> dict[str, Any]:
    """Opt-in bounded projection, never infer a state for unreadable history.

    Counts are verified lower bounds unless every Gitea Issue in the complete
    read window has an unambiguous event history. The read is not an atomic
    Gitea snapshot and cannot authorize a write or close lease.
    """
    counts = {"open": 0, "accepted": 0, "waiting_review": 0, "resolved": 0}
    unknown: list[int] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SUMMARY_READ_TIMEOUT
    examined = 0
    deadline_reached = False
    timed_out_thread = None
    probe_errors: list[int] = []
    for item in all_threads[:_SUMMARY_MAX_THREADS]:
        remaining = deadline - loop.time()
        if remaining <= 0:
            deadline_reached = True
            break
        number = item["number"]
        examined += 1
        try:
            state, incomplete = await asyncio.wait_for(
                preview_thread_state(client, owner, repo, number), timeout=remaining,
            )
            if state is not None and state not in counts:
                raise ValueError("invalid coordination state in verified summary")
        except TimeoutError:
            unknown.append(number)
            timed_out_thread = number
            deadline_reached = True
            break
        except Exception:
            # A read-only projection must not lose earlier verified evidence
            # or the independent resume result because one provider read fails.
            # Caller cancellation (BaseException) still propagates.
            unknown.append(number)
            probe_errors.append(number)
            continue
        if incomplete or state is None:
            unknown.append(number)
        else:
            counts[state] += 1
    exact = (
        not source_scan_truncated
        and examined == len(all_threads)
        and not unknown
    )
    return {
        "state_counts": {
            key: {"count": count, "count_exact": exact}
            for key, count in counts.items()
        },
        "count_exact": exact,
        "verified_thread_count": sum(counts.values()),
        "observed_thread_count": len(all_threads),
        "examined_thread_count": examined,
        "unexamined_thread_count": len(all_threads) - examined,
        "unverified_thread_numbers": unknown,
        "scan_deadline_reached": deadline_reached,
        "timed_out_thread_number": timed_out_thread,
        "probe_error_thread_numbers": probe_errors,
        "source_scan_truncated": source_scan_truncated,
        "note": "Non-atomic read-only observation; counts are lower bounds if not exact",
    }


async def coord_inbox(
    project: str, limit: int = 25, offset: int = 0, include_summary: bool = False
) -> dict[str, Any]:
    """Show open/closed project threads and the latest verified coordination state."""
    tool = "coord_inbox"
    try:
        if type(include_summary) is not bool:
            raise ValueError("include_summary must be a boolean")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("limit must be an integer from 1 to 30")
        if type(offset) is not int or not 0 <= offset < 500:
            raise ValueError("offset must be an integer from 0 to 499")
        owner, repo = await _connection(tool, project, write=False)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            all_threads, scan_truncated = await list_threads_preview(client, owner, repo)
            threads, incomplete_thread_numbers, examined = await _verified_preview(
                client, owner, repo, all_threads, limit, offset
            )
            summary = (
                await _verified_global_summary(client, owner, repo, all_threads, scan_truncated)
                if include_summary else None
            )
    except Exception as exc:
        return _fail(tool, exc)
    next_offset = offset + examined if offset + examined < len(all_threads) else None
    truncated = scan_truncated or offset > 0 or next_offset is not None or bool(incomplete_thread_numbers)
    return tool_success(
        tool,
        result={
            "project": repo, "threads": threads, "count": len(threads),
            "truncated": truncated, "count_exact": not truncated,
            "offset": offset, "next_offset": next_offset,
            "source_scan_truncated": scan_truncated,
            "examined_thread_count": examined,
            "incomplete_thread_numbers": incomplete_thread_numbers,
            **({"summary": summary} if include_summary else {}),
        },
        source="gitea",
    )


async def coord_board(
    project: str, limit: int = 25, offset: int = 0, include_summary: bool = False
) -> dict[str, Any]:
    """Read the project's Gitea-backed Kanban columns without changing issues."""
    tool = "coord_board"
    try:
        if type(include_summary) is not bool:
            raise ValueError("include_summary must be a boolean")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("limit must be an integer from 1 to 30")
        if type(offset) is not int or not 0 <= offset < 500:
            raise ValueError("offset must be an integer from 0 to 499")
        owner, repo = await _connection(tool, project, write=False)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            all_threads, scan_truncated = await list_threads_preview(client, owner, repo)
            enriched, incomplete_thread_numbers, examined = await _verified_preview(
                client, owner, repo, all_threads, limit, offset
            )
            columns = board_from_threads(enriched)
            if include_summary:
                summary, resume = await asyncio.gather(
                    _verified_global_summary(client, owner, repo, all_threads, scan_truncated),
                    _project_resume(client, owner, repo),
                )
            else:
                summary, resume = None, None
    except Exception as exc:
        return _fail(tool, exc)
    next_offset = offset + examined if offset + examined < len(all_threads) else None
    truncated = scan_truncated or offset > 0 or next_offset is not None or bool(incomplete_thread_numbers)
    # Counts are exact only after every thread/state was verified. Otherwise
    # they are lower bounds across the observed sample, not global Kanban totals.
    for column in columns:
        column["count_exact"] = not truncated
    return tool_success(
        tool, result={
            "project": repo, "columns": columns, "count": len(enriched),
            "truncated": truncated, "count_exact": not truncated,
            "offset": offset, "next_offset": next_offset,
            "source_scan_truncated": scan_truncated,
            "examined_thread_count": examined,
            "incomplete_thread_numbers": incomplete_thread_numbers,
            **({"summary": summary, "resume": resume} if include_summary else {}),
            "note": "Gitea Issues is authoritative; resolved does not automatically close an Issue",
        }, source="gitea",
    )


async def coord_open(project: str, issue_number: int) -> dict[str, Any]:
    """Read recent bounded Gitea evidence; partial display never grants a close lease."""
    tool = "coord_open"
    try:
        owner, repo = await _connection(tool, project, write=False)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            thread = await open_thread(
                client, owner, repo, issue_number, allow_partial=True, recent_preview=True
            )
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(tool, result={"project": repo, "thread": thread}, source="gitea")


async def coord_create(
    source_project: str, target_project: str, title: str, message: str, request_key: str
) -> dict[str, Any]:
    """Address an issue to a registered project's inbox; request_key is reusable."""
    tool = "coord_create"
    try:
        source = validate_project(source_project)
        _registered(source)  # Registration check; issue marker keeps project_id, not repo slug.
        owner, target = await _connection(tool, target_project, write=True)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            repo = await client.get_repo(owner, target)
            if (
                repo.get("full_name") != f"{owner}/{target}"
                or (repo.get("permissions") or {}).get("push") is not True
            ):
                raise PermissionError("Gitea target repository identity or write access is unverified")
            outcome = await create_thread(
                client, owner, target,
                source_project=source, title=title, body=message, request_key=request_key,
                actor=_principal() or "",
            )
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(
        tool, result={"target_project": target_project, "gitea_repo": f"{owner}/{target}", **outcome},
        source="gitea",
    )


async def _mutate(
    tool: str, project: str, issue_number: int, message: str,
    request_key: str, state: str
) -> dict[str, Any]:
    try:
        owner, repo = await _connection(tool, project, write=True)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            meta = await client.get_repo(owner, repo)
            if (
                meta.get("full_name") != f"{owner}/{repo}"
                or (meta.get("permissions") or {}).get("push") is not True
            ):
                raise PermissionError("Gitea target repository identity or write access is unverified")
            outcome = await write_event(
                client, owner, repo, issue_number,
                text=message, request_key=request_key, state=state,
                actor=_principal() or "",
            )
    except Exception as exc:
        return _fail(tool, exc)
    return tool_success(tool, result={"project": repo, **outcome}, source="gitea")


async def coord_reply(project: str, issue_number: int, message: str, request_key: str) -> dict[str, Any]:
    """Append a response without changing the coordination state."""
    return await _mutate("coord_reply", project, issue_number, message, request_key, "message")


async def coord_accept(project: str, issue_number: int, message: str, request_key: str) -> dict[str, Any]:
    """Acknowledge an incoming request in its original thread."""
    return await _mutate("coord_accept", project, issue_number, message, request_key, "accepted")


async def coord_update(
    project: str, issue_number: int, state: str, message: str, request_key: str
) -> dict[str, Any]:
    """Record accepted/waiting_review/resolved with verification evidence."""
    if state not in {"accepted", "waiting_review", "resolved"}:
        return tool_error(tool="coord_update", code="INVALID_INPUT", message="invalid coordination transition", source="gitea")
    return await _mutate("coord_update", project, issue_number, message, request_key, state)


async def coord_close(
    project: str, issue_number: StrictInt, resolution_event_id: StrictInt,
    expected_state_sha256: str, request_key: str, reason: str,
) -> dict[str, Any]:
    """Explicitly close one reviewed, resolved coordination Issue.

    Requires repo+admin scope and an existing trusted resolved report. Read
    coord_open.close_state_sha256, review the report/discussion, then supply
    that exact lease and resolution_event_id. reason records the operator's
    explicit close decision. No automatic close follows merge or resolved.

    Existing comments are retained without duplication. Attempts are journaled
    under the configured audit directory; retries after dispatch are read-only.
    Reopened/changed Issues are never automatically closed again. Gitea lacks
    Issue CAS here: pre/post reads detect drift but cannot prevent remote edits
    in the HTTP window. Unknown outcomes require read-only reconciliation.
    """
    tool = "coord_close"

    def audit(phase: str, why: str, metadata: dict[str, Any]) -> None:
        from examples.mcp_server.mcp_audit import McpAuditEvent

        logger = remote._get_gitea_audit_logger()
        event = McpAuditEvent(
            event_type="mcp.gitea_destructive_" + phase, tool=tool,
            action="close_coordination_issue", decision="allow", reason=why,
            metadata=metadata,
        )
        if phase == "intent":
            try:
                logger.append_required(event)
            except Exception as exc:
                raise CloseRefused("AUDIT_UNAVAILABLE", "Audit unavailable; Issue close refused") from exc
        else:
            logger.append(event)

    try:
        owner, repo = await _connection(tool, project, write=True)
        async with server_attr("GiteaClient")(os.environ["GITEA_TOKEN"]) as client:
            meta = await client.get_repo(owner, repo)
            if meta.get("full_name") != f"{owner}/{repo}" or (meta.get("permissions") or {}).get("push") is not True:
                raise PermissionError("Repository write permission or identity not verified")
            outcome = await close_thread(
                client, owner, repo, issue_number, resolution_event_id=resolution_event_id,
                expected_state_sha256=expected_state_sha256, request_key=request_key,
                reason=reason, actor=_principal() or "",
                journal_dir=Path(os.environ.get("MCP_AUDIT_LOG_PATH", "logs/mcp_audit.jsonl")).parent / "coordination-close",
                audit=audit,
            )
    except CloseRefused as exc:
        return tool_error(tool=tool, code=exc.code, message=str(exc), source="gitea")
    except CoordinationOutcomeUnknown as exc:
        return _fail(tool, exc)
    except PermissionError:
        return tool_error(tool=tool, code="AUTH_ERROR", message="Caller or repository write permission denied", source="gitea")
    except ValueError:
        return tool_error(tool=tool, code="INVALID_INPUT", message="Invalid close request or coordination thread", source="gitea")
    except Exception:
        return tool_error(tool=tool, code="CLOSE_UNAVAILABLE", message="Issue close is unavailable; inspect the thread before retrying", source="gitea")
    return tool_success(tool, result={"project": repo, **outcome}, source="gitea")


def register_all() -> None:
    register_tool("coord_inbox")(coord_inbox)
    register_tool("coord_board")(coord_board)
    register_tool("coord_open")(coord_open)
    register_tool("coord_create")(coord_create)
    register_tool("coord_reply")(coord_reply)
    register_tool("coord_accept")(coord_accept)
    register_tool("coord_update")(coord_update)
    register_tool("coord_close")(coord_close)
