"""Read-only Gitea REST API client for fleet MCP adapter (incl. Actions/CI)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .shared import validate_repo_owner_or_name, validate_repo_path

MAX_LIMIT = 50
MAX_FILE_SIZE = 256 * 1024
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

API_BASE = os.environ.get("GITEA_API_BASE", "https://git.example.com/api/v1")
GITEA_FORWARDED_HOST = os.environ.get("GITEA_FORWARDED_HOST", "")
GITEA_FORWARDED_PROTO = os.environ.get("GITEA_FORWARDED_PROTO", "https")

ALLOWED_ENDPOINTS = frozenset(
    {
        "/repos/{owner}/{repo}",
        "/repos/{owner}/{repo}/branches",
        "/repos/{owner}/{repo}/commits",
        "/repos/{owner}/{repo}/contents/{path}",
        "/repos/{owner}/{repo}/issues",
        "/repos/{owner}/{repo}/issues/{number}",
        "/repos/{owner}/{repo}/pulls",
        "/repos/{owner}/{repo}/pulls/{number}",
        "/repos/{owner}/{repo}/actions/runs",
        "/repos/{owner}/{repo}/actions/runs/{run_id}",
        "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
        "/repos/{owner}/{repo}/actions/workflows",
    }
)


class GiteaClient:
    """Stateless async HTTP client for Gitea REST API (read-only)."""

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
            proxy=None,
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
            # e.g. http://192.168.1.103:3005/...) — FastMCP's tool-call
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

    async def get_file(
        self,
        owner: str,
        repo: str,
        path: str,
        branch: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {}
        if branch:
            params["ref"] = branch
        result = await self._get(
            "/repos/{owner}/{repo}/contents/{path}",
            params=params,
            owner=owner,
            repo=repo,
            path=path,
        )
        if isinstance(result, dict) and "content" in result:
            import base64

            raw = base64.b64decode(result["content"])
            if len(raw) > MAX_FILE_SIZE:
                result["content"] = f"[truncated {len(raw)} bytes > {MAX_FILE_SIZE} limit]"
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
    ) -> list[dict[str, Any]]:
        limit = min(limit, MAX_LIMIT)
        return await self._get(
            "/repos/{owner}/{repo}/pulls",
            params={"state": state, "limit": limit},
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

    # ── Gitea Actions (CI/CD) ──────────────────────────────────────

    async def list_action_runs(
        self,
        owner: str,
        repo: str,
        status: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        limit = min(limit, MAX_LIMIT)
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self._get(
            "/repos/{owner}/{repo}/actions/runs",
            params=params,
            owner=owner,
            repo=repo,
        )

    async def get_action_run(
        self,
        owner: str,
        repo: str,
        run_id: int,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/actions/runs/{run_id}",
            owner=owner,
            repo=repo,
            run_id=run_id,
        )

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
