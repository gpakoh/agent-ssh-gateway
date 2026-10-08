"""Gitea REST client: read APIs plus a narrow, explicitly-allowed PR write surface."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from .shared import (
    minimize_action_run_payload,
    validate_repo_owner_or_name,
    validate_repo_path,
)

MAX_LIMIT = 50
MAX_FILE_SIZE = 256 * 1024
# Default bound, in bytes, applied to a file's *decoded* content before it is
# returned to a caller. Centralized so both the low-level client and the MCP
# tool can opt in to a smaller, less chat-spammy cap than MAX_FILE_SIZE.
DEFAULT_GET_FILE_MAX_CONTENT_BYTES = 16 * 1024
DEFAULT_ACTION_JOB_LOG_MAX_BYTES = 64 * 1024
MAX_ACTION_JOB_LOG_BYTES = 256 * 1024
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Gitea's Actions run-list API accepts `in_progress`, while the operator-facing
# tool historically documented `running`. Normalize that alias locally so callers
# do not get an opaque remote 400 for a supported semantic state.
_ACTION_RUN_STATUS_ALIASES = {
    "running": "in_progress",
}
_ALLOWED_ACTION_RUN_STATUS_FILTERS = frozenset({"completed", "in_progress", "waiting"})

# Job-level statuses accepted by the repo-wide /actions/jobs filter. Unlike
# the run-list filter, "running" is the only alias and it maps forward to the
# remote's in_progress spelling; every other value must match exactly.
_ACTION_JOB_STATUS_ALIASES = {
    "running": "in_progress",
}
_ALLOWED_ACTION_JOB_STATUS_FILTERS = frozenset(
    {"pending", "queued", "in_progress", "failure", "success", "skipped"}
)

API_BASE = os.environ.get("GITEA_API_BASE", "https://git.example.com/api/v1")
GITEA_FORWARDED_HOST = os.environ.get("GITEA_FORWARDED_HOST", "")
GITEA_FORWARDED_PROTO = os.environ.get("GITEA_FORWARDED_PROTO", "https")

ALLOWED_ENDPOINTS = frozenset(
    {
        "/user",
        "/repos/{owner}/{repo}",
        "/repos/{owner}/{repo}/branches",
        "/repos/{owner}/{repo}/branches/{branch}",
        "/repos/{owner}/{repo}/commits",
        "/repos/{owner}/{repo}/compare/{basehead}",
        "/repos/{owner}/{repo}/contents",
        "/repos/{owner}/{repo}/contents/{path}",
        "/repos/{owner}/{repo}/issues",
        "/repos/{owner}/{repo}/issues/{number}",
        "/repos/{owner}/{repo}/pulls",
        "/repos/{owner}/{repo}/pulls/{number}",
        "/repos/{owner}/{repo}/actions/runs",
        "/repos/{owner}/{repo}/actions/runs/{run_id}",
        "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
        "/repos/{owner}/{repo}/actions/jobs",
        "/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
        "/repos/{owner}/{repo}/actions/workflows",
    }
)

# Keep write access on a separate, deliberately tiny allowlist. The MCP write
# surface exposes only explicit PR lifecycle operations; it is not a generic
# Gitea mutation client.
ALLOWED_WRITE_ENDPOINTS = frozenset(
    {
        "/repos/{owner}/{repo}/pulls",
        "/repos/{owner}/{repo}/pulls/{number}/merge",
    }
)
# PATCH is not a generic mutation tool either: the close operation may
# touch exactly one endpoint, and only to set state=closed.
ALLOWED_CLOSE_ENDPOINTS = frozenset(
    {
        "/repos/{owner}/{repo}/pulls/{number}",
    }
)
# Actions writes are isolated from PR writes: only rerunning one exact existing
# workflow run is allowed, never workflow dispatch/edit/delete or a generic POST.
ALLOWED_ACTION_WRITE_ENDPOINTS = frozenset(
    {
        "/repos/{owner}/{repo}/actions/runs/{run_id}/rerun",
    }
)
# Repository-governance writes stay separate from PR/Actions writes. Branch
# creation is the only allowed POST and repository PATCH is reserved solely
# for changing ``default_branch``.
ALLOWED_BRANCH_WRITE_ENDPOINTS = frozenset(
    {
        "/repos/{owner}/{repo}/branches",
    }
)
ALLOWED_REPO_ADMIN_ENDPOINTS = frozenset(
    {
        "/repos/{owner}/{repo}",
    }
)
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_PR_TITLE = 200
MAX_PR_BODY = 20_000


class GiteaMutationOutcomeUnknown(RuntimeError):
    """The HTTP mutation boundary was crossed but its response was unusable."""


class GiteaActionRunResponseError(ValueError):
    pass


def _validate_branch_name(value: str, label: str) -> str:
    value = value.strip()
    if (
        not value
        or not _BRANCH_RE.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith("/")
    ):
        raise ValueError(f"Invalid {label} branch name: {value!r}")
    return value


def _validate_compare_basehead(value: str) -> str:
    """Validate and URL-encode Gitea's single-segment base...head parameter."""
    value = value.strip()
    separator = "..." if "..." in value else ".." if ".." in value else ""
    if not separator:
        raise ValueError("Invalid compare basehead: expected base...head")
    parts = value.split(separator)
    if len(parts) != 2:
        raise ValueError("Invalid compare basehead: expected exactly two refs")
    base = _validate_branch_name(parts[0], "compare base")
    head = _validate_branch_name(parts[1], "compare head")
    return quote(f"{base}{separator}{head}", safe="")


def _normalize_action_run_status_filter(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = _ACTION_RUN_STATUS_ALIASES.get(status.strip(), status.strip())
    if normalized not in _ALLOWED_ACTION_RUN_STATUS_FILTERS:
        allowed = ", ".join(sorted(_ALLOWED_ACTION_RUN_STATUS_FILTERS | set(_ACTION_RUN_STATUS_ALIASES)))
        raise ValueError(f"status must be one of: {allowed}")
    return normalized


def _normalize_action_job_status_filter(status: str | None) -> str | None:
    if status is None:
        return None
    if not isinstance(status, str):
        raise ValueError(f"status must be a string, got {type(status).__name__}")
    stripped = status.strip()
    normalized = _ACTION_JOB_STATUS_ALIASES.get(stripped, stripped)
    if normalized not in _ALLOWED_ACTION_JOB_STATUS_FILTERS:
        allowed = ", ".join(sorted(_ALLOWED_ACTION_JOB_STATUS_FILTERS | set(_ACTION_JOB_STATUS_ALIASES)))
        raise ValueError(f"status must be one of: {allowed}")
    return normalized


def _validate_positive_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_action_job_log_max_bytes(max_bytes: int) -> int:
    max_bytes = _validate_positive_int(max_bytes, "max_bytes")
    if max_bytes > MAX_ACTION_JOB_LOG_BYTES:
        raise ValueError(f"max_bytes must be <= {MAX_ACTION_JOB_LOG_BYTES}")
    return max_bytes


def _action_job_str_or_none(value: Any, label: str) -> str | None:
    """Require an emitted job string field to be exactly str or null."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"action job {label} must be a string or null")
    return value


def _action_job_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"action job {label} must be a positive integer")
    return value


def _action_job_optional_nonneg_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"action job {label} must be an integer >= 0 or null")
    return value


def _action_job_head_sha(value: Any) -> str | None:
    """Validate head_sha without normalizing or coercing remote input.

    None/absent is allowed. Any present value must be exactly a lowercase
    40-hex SHA-1; empty string, uppercase, non-string, and otherwise
    malformed remote values fail closed instead of silently becoming a
    different SHA. We never lowercase or coerce it ourselves.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("action job head_sha must be a string or null")
    if not _SHA1_RE.fullmatch(value):
        raise ValueError("action job head_sha must be a lowercase 40-character SHA-1")
    return value


def _validate_action_run_list_limit(limit: int) -> int:
    limit = _validate_positive_int(limit, "limit")
    if limit > MAX_LIMIT:
        raise ValueError(f"limit must be <= {MAX_LIMIT}")
    return limit


def _validate_action_job_limit(limit: int) -> int:
    limit = _validate_positive_int(limit, "limit")
    if limit > MAX_LIMIT:
        raise ValueError(f"limit must be <= {MAX_LIMIT}")
    return limit


def minimize_action_job_payload(job: Any) -> dict[str, Any]:
    """Validate and minimize a Gitea Actions job to a strict 14-field allowlist.

    Malformed emitted scalars fail closed with ValueError instead of being
    coerced to None: any job whose id/run_id/run_attempt/runner_id/string
    fields or head_sha do not match the contract is a remote-shape bug we
    refuse to hide from callers.
    """
    if not isinstance(job, dict):
        raise ValueError("action job payload must be an object")
    return {
        "id": _action_job_positive_int(job.get("id"), "id"),
        "run_id": _action_job_positive_int(job.get("run_id"), "run_id"),
        "run_attempt": _action_job_positive_int(job.get("run_attempt"), "run_attempt"),
        "head_branch": _action_job_str_or_none(job.get("head_branch"), "head_branch"),
        "head_sha": _action_job_head_sha(job.get("head_sha")),
        "name": _action_job_str_or_none(job.get("name"), "name"),
        "status": _action_job_str_or_none(job.get("status"), "status"),
        "conclusion": _action_job_str_or_none(job.get("conclusion"), "conclusion"),
        "runner_id": _action_job_optional_nonneg_int(job.get("runner_id"), "runner_id"),
        "runner_name": _action_job_str_or_none(job.get("runner_name"), "runner_name"),
        "started_at": _action_job_str_or_none(job.get("started_at"), "started_at"),
        "completed_at": _action_job_str_or_none(job.get("completed_at"), "completed_at"),
        "url": _action_job_str_or_none(job.get("url"), "url"),
        "run_url": _action_job_str_or_none(job.get("run_url"), "run_url"),
    }


def normalize_action_jobs_response(data: Any) -> dict[str, Any]:
    """Return the minimized repo-wide jobs response, failing closed on shape."""
    if not isinstance(data, dict):
        raise ValueError("actions jobs response must be an object")
    total_count = data.get("total_count")
    if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
        raise ValueError("total_count must be an integer >= 0")
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list")
    return {
        "total_count": total_count,
        "jobs": [minimize_action_job_payload(job) for job in jobs],
    }


def _redact_action_job_log(text: str) -> tuple[str, bool]:
    """Redact common secret-bearing log fragments before returning CI logs."""
    patterns = (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASS|API_KEY|JWT|BEARER|AUTH)[A-Z0-9_]*)\s*=\s*([^\s]+)"
        ),
        re.compile(r"(?i)(Authorization:\s*)(?:Bearer|token)\s+[^\s]+"),
        re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"),
    )
    redacted = text
    redacted = patterns[0].sub(r"\1=<redacted>", redacted)
    redacted = patterns[1].sub(r"\1<redacted>", redacted)
    redacted = patterns[2].sub("https://<redacted>@", redacted)
    return redacted, redacted != text


class GiteaClient:
    """Stateless async Gitea client with a separately allowlisted PR write."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("GITEA_TOKEN is required")
        headers: dict[str, str] = {
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "User-Agent": "agent-ssh-gateway-mcp/1.0",
        }
        if GITEA_FORWARDED_HOST:
            headers["X-Forwarded-Host"] = GITEA_FORWARDED_HOST
            headers["X-Forwarded-Proto"] = GITEA_FORWARDED_PROTO
            headers["Host"] = GITEA_FORWARDED_HOST
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

    async def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        **path_params: Any,
    ) -> Any:
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"Endpoint not allowed: {endpoint}")
        # Validate every path-template placeholder centrally — owner/repo
        # must never contain "/" (path-segment injection past the intended
        # /repos/{owner}/{repo}/... structure — see shared.py's docstring),
        # {path} legitimately contains "/" but never "..".
        if "owner" in path_params:
            validate_repo_owner_or_name(path_params["owner"], label="owner")
        if "repo" in path_params:
            validate_repo_owner_or_name(path_params["repo"], label="repo")
        if "path" in path_params:
            validate_repo_path(path_params["path"])
        if "branch" in path_params or "basehead" in path_params:
            path_params = dict(path_params)
        if "branch" in path_params:
            branch = _validate_branch_name(str(path_params["branch"]), "branch")
            path_params["branch"] = quote(branch, safe="")
        if "basehead" in path_params:
            path_params["basehead"] = _validate_compare_basehead(str(path_params["basehead"]))
        path = endpoint.format(**path_params)
        resp = await self._client.get(path, params=params)
        if resp.status_code in (401, 403):
            detail = resp.json().get("message", "unauthorized")
            raise PermissionError(f"gitea api {path}: {detail}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # httpx.HTTPStatusError's own message embeds the fully-resolved
            # absolute URL (scheme + internal host + port from API_BASE,
            # e.g. http://192.0.2.103:3005/...) — FastMCP's tool-call
            # error handler sends str(exception) straight to the external
            # MCP client verbatim (mcp/server/lowlevel/server.py's
            # `except Exception as e: return self._make_error_result(str(e))`),
            # so any failed call (e.g. a 404 for a typo'd repo name — an
            # everyday mistake, not a rare failure) leaked internal
            # infrastructure topology that GITEA_FORWARDED_HOST/PROTO exist
            # specifically to hide from *successful* responses. Re-raise
            # with only the already-sanitized endpoint path, never the
            # resolved base URL.
            raise httpx.HTTPStatusError(
                f"gitea api {path}: {resp.status_code} {resp.reason_phrase}",
                request=exc.request,
                response=exc.response,
            ) from None
        return resp.json()

    async def _get_text(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        **path_params: Any,
    ) -> str:
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"Endpoint not allowed: {endpoint}")
        if "owner" in path_params:
            validate_repo_owner_or_name(path_params["owner"], label="owner")
        if "repo" in path_params:
            validate_repo_owner_or_name(path_params["repo"], label="repo")
        if "job_id" in path_params:
            path_params = dict(path_params)
            path_params["job_id"] = _validate_positive_int(path_params["job_id"], "job_id")
        path = endpoint.format(**path_params)
        resp = await self._client.get(path, params=params)
        if resp.status_code in (401, 403):
            detail = resp.json().get("message", "unauthorized")
            raise PermissionError(f"gitea api {path}: {detail}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                f"gitea api {path}: {resp.status_code} {resp.reason_phrase}",
                request=exc.request,
                response=exc.response,
            ) from None
        return resp.text

    async def get_user(self) -> dict[str, Any]:
        return await self._get("/user")

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        **path_params: Any,
    ) -> Any:
        """POST to the tiny mutation allowlist used by explicit write tools."""
        if endpoint not in ALLOWED_WRITE_ENDPOINTS:
            raise ValueError(f"Write endpoint not allowed: {endpoint}")
        if "owner" in path_params:
            validate_repo_owner_or_name(path_params["owner"], label="owner")
        if "repo" in path_params:
            validate_repo_owner_or_name(path_params["repo"], label="repo")
        path = endpoint.format(**path_params)
        resp = await self._client.post(path, json=payload)
        if resp.status_code in (401, 403):
            detail = resp.json().get("message", "unauthorized")
            raise PermissionError(f"gitea api {path}: {detail}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                f"gitea api {path}: {resp.status_code} {resp.reason_phrase}",
                request=exc.request,
                response=exc.response,
            ) from None
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            # A successful HTTP status means the write may already be durable.
            # Decoding its body is observational only and must never turn that
            # irreversible boundary into an apparent input error/retry signal.
            raise GiteaMutationOutcomeUnknown(
                "gitea mutation returned an undecodable success response"
            ) from exc

    async def _post_action(
        self,
        endpoint: str,
        **path_params: Any,
    ) -> Any:
        """POST one narrowly allowlisted Actions lifecycle operation."""
        if endpoint not in ALLOWED_ACTION_WRITE_ENDPOINTS:
            raise ValueError(f"Actions write endpoint not allowed: {endpoint}")
        if "owner" in path_params:
            validate_repo_owner_or_name(path_params["owner"], label="owner")
        if "repo" in path_params:
            validate_repo_owner_or_name(path_params["repo"], label="repo")
        if "run_id" in path_params:
            path_params = dict(path_params)
            path_params["run_id"] = _validate_positive_int(path_params["run_id"], "run_id")
        path = endpoint.format(**path_params)
        resp = await self._client.post(path)
        if resp.status_code in (401, 403):
            detail = resp.json().get("message", "unauthorized")
            raise PermissionError(f"gitea api {path}: {detail}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                f"gitea api {path}: {resp.status_code} {resp.reason_phrase}",
                request=exc.request,
                response=exc.response,
            ) from None
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise GiteaMutationOutcomeUnknown(
                "gitea actions mutation returned an undecodable success response"
            ) from exc

    async def _post_branch(
        self,
        endpoint: str,
        payload: dict[str, Any],
        **path_params: Any,
    ) -> Any:
        """POST one narrowly allowlisted branch-creation operation."""
        if endpoint not in ALLOWED_BRANCH_WRITE_ENDPOINTS:
            raise ValueError(f"Branch write endpoint not allowed: {endpoint}")
        if "owner" in path_params:
            validate_repo_owner_or_name(path_params["owner"], label="owner")
        if "repo" in path_params:
            validate_repo_owner_or_name(path_params["repo"], label="repo")
        path = endpoint.format(**path_params)
        resp = await self._client.post(path, json=payload)
        if resp.status_code in (401, 403):
            detail = resp.json().get("message", "unauthorized")
            raise PermissionError(f"gitea api {path}: {detail}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                f"gitea api {path}: {resp.status_code} {resp.reason_phrase}",
                request=exc.request,
                response=exc.response,
            ) from None
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise GiteaMutationOutcomeUnknown(
                "gitea branch mutation returned an undecodable success response"
            ) from exc

    async def _patch_repo_admin(
        self,
        endpoint: str,
        payload: dict[str, Any],
        **path_params: Any,
    ) -> Any:
        """PATCH the narrowly allowlisted repository-properties endpoint."""
        if endpoint not in ALLOWED_REPO_ADMIN_ENDPOINTS:
            raise ValueError(f"Repository admin endpoint not allowed: {endpoint}")
        if "owner" in path_params:
            validate_repo_owner_or_name(path_params["owner"], label="owner")
        if "repo" in path_params:
            validate_repo_owner_or_name(path_params["repo"], label="repo")
        path = endpoint.format(**path_params)
        resp = await self._client.patch(path, json=payload)
        if resp.status_code in (401, 403):
            detail = resp.json().get("message", "unauthorized")
            raise PermissionError(f"gitea api {path}: {detail}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                f"gitea api {path}: {resp.status_code} {resp.reason_phrase}",
                request=exc.request,
                response=exc.response,
            ) from None
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise GiteaMutationOutcomeUnknown(
                "gitea repository admin mutation returned an undecodable success response"
            ) from exc

    async def _patch(
        self,
        endpoint: str,
        payload: dict[str, Any],
        **path_params: Any,
    ) -> Any:
        """PATCH the single narrowly-allowlisted PR-state endpoint."""
        if endpoint not in ALLOWED_CLOSE_ENDPOINTS:
            raise ValueError(f"Write endpoint not allowed: {endpoint}")
        if "owner" in path_params:
            validate_repo_owner_or_name(path_params["owner"], label="owner")
        if "repo" in path_params:
            validate_repo_owner_or_name(path_params["repo"], label="repo")
        path = endpoint.format(**path_params)
        resp = await self._client.patch(path, json=payload)
        if resp.status_code in (401, 403):
            detail = resp.json().get("message", "unauthorized")
            raise PermissionError(f"gitea api {path}: {detail}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                f"gitea api {path}: {resp.status_code} {resp.reason_phrase}",
                request=exc.request,
                response=exc.response,
            ) from None
        if not resp.content:
            return {}
        return resp.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self._get("/repos/{owner}/{repo}", owner=owner, repo=repo)

    async def list_branches(
        self,
        owner: str,
        repo: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        limit = min(limit, MAX_LIMIT)
        return await self._get(
            "/repos/{owner}/{repo}/branches",
            params={"limit": limit},
            owner=owner,
            repo=repo,
        )

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
        """Get one branch including Gitea's effective protection fields."""
        return await self._get(
            "/repos/{owner}/{repo}/branches/{branch}",
            owner=owner,
            repo=repo,
            branch=branch,
        )

    async def create_branch_at_ref(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
        source_sha: str,
    ) -> dict[str, Any]:
        """Create exactly one branch from an exact commit SHA/ref."""
        branch = _validate_branch_name(branch, "new")
        source_sha = source_sha.strip()
        if not _SHA1_RE.fullmatch(source_sha):
            raise ValueError("source_sha must be a lowercase 40-character SHA-1")
        data = await self._post_branch(
            "/repos/{owner}/{repo}/branches",
            {"new_branch_name": branch, "old_ref_name": source_sha},
            owner=owner,
            repo=repo,
        )
        if not isinstance(data, dict):
            raise GiteaMutationOutcomeUnknown(
                "gitea branch mutation returned a non-object response"
            )
        return data

    async def set_default_branch(
        self,
        owner: str,
        repo: str,
        *,
        branch: str,
    ) -> dict[str, Any]:
        """Set only the repository ``default_branch`` property."""
        branch = _validate_branch_name(branch, "default")
        data = await self._patch_repo_admin(
            "/repos/{owner}/{repo}",
            {"default_branch": branch},
            owner=owner,
            repo=repo,
        )
        if not isinstance(data, dict):
            raise GiteaMutationOutcomeUnknown(
                "gitea repository admin mutation returned a non-object response"
            )
        return data

    async def list_commits(
        self,
        owner: str,
        repo: str,
        sha: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        limit = min(limit, MAX_LIMIT)
        params: dict[str, Any] = {"limit": limit}
        if sha:
            params["sha"] = sha
        return await self._get(
            "/repos/{owner}/{repo}/commits",
            params=params,
            owner=owner,
            repo=repo,
        )

    async def compare_commits(
        self,
        owner: str,
        repo: str,
        *,
        base: str,
        head: str,
    ) -> dict[str, Any]:
        """Compare two refs as base...head using Gitea's JSON compare API."""
        base = _validate_branch_name(base, "compare base")
        head = _validate_branch_name(head, "compare head")
        return await self._get(
            "/repos/{owner}/{repo}/compare/{basehead}",
            owner=owner,
            repo=repo,
            basehead=f"{base}...{head}",
        )

    async def get_file(
        self,
        owner: str,
        repo: str,
        path: str = "",
        branch: str | None = None,
        *,
        include_content: bool = True,
        max_content_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Get a file, a directory listing, or (path="") the repo root
        listing. validate_repo_path() rejects an empty path, so root
        listing must go through the path-less endpoint variant instead
        of substituting {path} at all -- P2 audit finding: there was
        previously no way to list a repo's top level through this tool.

        Content contract (keeps large base64 blobs out of chat by default):
          * include_content=False drops the "content" field and sets
            "content_omitted": true, keeping all metadata (path, name, sha,
            download_url, html_url, last_commit_sha).
          * max_content_bytes (int > 0) bounds the *decoded* content: larger
            files come back as a "[truncated N bytes > M limit]" marker with
            "truncated": true and "content_bytes": original length.
          * max_content_bytes=None falls back to MAX_FILE_SIZE (256 KiB), so
            existing callers keep their previous behavior.
        """
        params: dict[str, str] = {}
        if branch:
            params["ref"] = branch
        if path:
            result = await self._get(
                "/repos/{owner}/{repo}/contents/{path}",
                params=params,
                owner=owner,
                repo=repo,
                path=path,
            )
        else:
            result = await self._get(
                "/repos/{owner}/{repo}/contents",
                params=params,
                owner=owner,
                repo=repo,
            )
        if isinstance(result, dict) and "content" in result:
            if not include_content:
                result.pop("content", None)
                result["content_omitted"] = True
                return result
            import base64

            raw = base64.b64decode(result["content"])
            limit = max_content_bytes if max_content_bytes is not None else MAX_FILE_SIZE
            if limit <= 0:
                raise ValueError(
                    f"max_content_bytes must be a positive int or None, got {max_content_bytes}"
                )
            if len(raw) > limit:
                result["content"] = f"[truncated {len(raw)} bytes > {limit} limit]"
                result["content_bytes"] = len(raw)
                result["truncated"] = True
        return result

    async def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        limit = min(limit, MAX_LIMIT)
        return await self._get(
            "/repos/{owner}/{repo}/issues",
            params={"state": state, "limit": limit},
            owner=owner,
            repo=repo,
        )

    async def get_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/issues/{number}",
            owner=owner,
            repo=repo,
            number=issue_number,
        )

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 30,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        limit = min(limit, MAX_LIMIT)
        page = _validate_positive_int(page, "page")
        return await self._get(
            "/repos/{owner}/{repo}/pulls",
            params={"state": state, "limit": limit, "page": page},
            owner=owner,
            repo=repo,
        )

    async def get_pull_request(
        self,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/pulls/{number}",
            owner=owner,
            repo=repo,
            number=pull_number,
        )

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str = "",
    ) -> dict[str, Any]:
        """Create a same-repository pull request; merge remains out of scope."""
        title = title.strip()
        body = body.strip()
        if not title or len(title) > MAX_PR_TITLE:
            raise ValueError(f"title must be 1..{MAX_PR_TITLE} characters")
        if len(body) > MAX_PR_BODY:
            raise ValueError(f"body exceeds {MAX_PR_BODY} characters")
        head = _validate_branch_name(head, "head")
        base = _validate_branch_name(base, "base")
        if head == base:
            raise ValueError("head and base branches must differ")
        return await self._post(
            "/repos/{owner}/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
            owner=owner,
            repo=repo,
        )

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        *,
        expected_head_sha: str,
        method: str = "merge",
    ) -> dict[str, Any]:
        """Merge one PR with an optimistic-lock check on its head commit."""
        if pull_number < 1:
            raise ValueError("pull_number must be >= 1")
        expected_head_sha = expected_head_sha.strip().lower()
        if not _SHA1_RE.fullmatch(expected_head_sha):
            raise ValueError("expected_head_sha must be a 40-character SHA-1")
        if method not in {"merge", "squash"}:
            raise ValueError("merge method must be one of: merge, squash")
        return await self._post(
            "/repos/{owner}/{repo}/pulls/{number}/merge",
            {"Do": method, "head_commit_id": expected_head_sha},
            owner=owner,
            repo=repo,
            number=pull_number,
        )

    async def close_pull_request(
        self,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict[str, Any]:
        """Close one PR (state=closed) without merging it or deleting branches."""
        if pull_number < 1:
            raise ValueError("pull_number must be >= 1")
        return await self._patch(
            "/repos/{owner}/{repo}/pulls/{number}",
            {"state": "closed"},
            owner=owner,
            repo=repo,
            number=pull_number,
        )

    # ── Gitea Actions (CI/CD) ──────────────────────────────────────

    async def list_action_runs(
        self,
        owner: str,
        repo: str,
        status: str | None = None,
        limit: int = 10,
        page: int | None = None,
    ) -> dict[str, Any]:
        limit = _validate_action_run_list_limit(limit)
        if page is not None:
            page = _validate_positive_int(page, "page")
        params: dict[str, Any] = {"limit": limit}
        if page is not None:
            params["page"] = page
        normalized_status = _normalize_action_run_status_filter(status)
        if normalized_status:
            params["status"] = normalized_status
        data = await self._get(
            "/repos/{owner}/{repo}/actions/runs",
            params=params,
            owner=owner,
            repo=repo,
        )
        if not isinstance(data, dict):
            raise GiteaActionRunResponseError(
                "gitea action runs response must contain a workflow_runs list"
            )
        runs = data.get("workflow_runs")
        if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
            raise GiteaActionRunResponseError(
                "gitea action runs response must contain a workflow_runs list"
            )
        data["workflow_runs"] = [minimize_action_run_payload(r) for r in runs]
        return data

    async def get_action_run(
        self,
        owner: str,
        repo: str,
        run_id: int,
    ) -> dict[str, Any]:
        run = await self._get(
            "/repos/{owner}/{repo}/actions/runs/{run_id}",
            owner=owner,
            repo=repo,
            run_id=run_id,
        )
        return minimize_action_run_payload(run)

    async def rerun_action_run(
        self,
        owner: str,
        repo: str,
        run_id: int,
    ) -> dict[str, Any]:
        """Rerun one existing workflow run via Gitea's dedicated endpoint."""
        run_id = _validate_positive_int(run_id, "run_id")
        run = await self._post_action(
            "/repos/{owner}/{repo}/actions/runs/{run_id}/rerun",
            owner=owner,
            repo=repo,
            run_id=run_id,
        )
        return minimize_action_run_payload(run)

    async def list_action_run_jobs(
        self,
        owner: str,
        repo: str,
        run_id: int,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
            owner=owner,
            repo=repo,
            run_id=run_id,
        )

    async def list_action_jobs(
        self,
        owner: str,
        repo: str,
        status: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List repository-wide Gitea Actions jobs (read-only).

        ``status`` accepts exactly pending, queued, in_progress, failure,
        success, skipped (plus the running alias mapped to in_progress).
        A non-string status, or an unknown status, is rejected here with
        ValueError before any HTTP request is made. ``page``/``limit`` must
        be positive ints (bools rejected) and limit may not exceed 50.
        The response is expected to be an object with a non-negative
        ``total_count`` and a ``jobs`` list; each job is minimized to a
        strict allowlist and malformed emitted scalars fail closed.
        """
        normalized_status = _normalize_action_job_status_filter(status)
        page = _validate_positive_int(page, "page")
        limit = _validate_action_job_limit(limit)
        params: dict[str, Any] = {"page": page, "limit": limit}
        if normalized_status:
            params["status"] = normalized_status
        data = await self._get(
            "/repos/{owner}/{repo}/actions/jobs",
            params=params,
            owner=owner,
            repo=repo,
        )
        return normalize_action_jobs_response(data)

    async def get_action_job_logs(
        self,
        owner: str,
        repo: str,
        job_id: int,
        *,
        max_bytes: int = DEFAULT_ACTION_JOB_LOG_MAX_BYTES,
    ) -> dict[str, Any]:
        max_bytes = _validate_action_job_log_max_bytes(max_bytes)
        job_id = _validate_positive_int(job_id, "job_id")
        text = await self._get_text(
            "/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
            owner=owner,
            repo=repo,
            job_id=job_id,
        )
        text, redacted = _redact_action_job_log(text)
        encoded = text.encode("utf-8", "replace")
        total_bytes = len(encoded)
        truncated = total_bytes > max_bytes
        if truncated:
            encoded = encoded[-max_bytes:]
            text = encoded.decode("utf-8", "replace")
        return {
            "job_id": job_id,
            "logs": text,
            "bytes_returned": len(encoded),
            "bytes_total_after_redaction": total_bytes,
            "truncated": truncated,
            "truncation": "tail" if truncated else None,
            "redacted": redacted,
        }

    async def list_workflows(
        self,
        owner: str,
        repo: str,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/actions/workflows",
            owner=owner,
            repo=repo,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GiteaClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
