"""Read-only Gitea REST API client for fleet MCP adapter (incl. Actions/CI)."""

from __future__ import annotations

import os
from typing import Any

import httpx

MAX_LIMIT = 50
MAX_FILE_SIZE = 256 * 1024
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

API_BASE = os.environ.get("GITEA_API_BASE", "https://git.xloud.ru/api/v1")

ALLOWED_ENDPOINTS = frozenset({
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
})


class GiteaClient:
    """Stateless async HTTP client for Gitea REST API (read-only)."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("GITEA_TOKEN is required")
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/json",
                "User-Agent": "agent-ssh-gateway-mcp/1.0",
            },
            timeout=REQUEST_TIMEOUT,
        )

    async def _get(
        self, endpoint: str,
        params: dict[str, Any] | None = None,
        **path_params: Any,
    ) -> Any:
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"Endpoint not allowed: {endpoint}")
        path = endpoint.format(**path_params)
        resp = await self._client.get(path, params=params)
        if resp.status_code in (401, 403):
            detail = resp.json().get("message", "unauthorized")
            raise PermissionError(f"gitea api {path}: {detail}")
        resp.raise_for_status()
        return resp.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self._get("/repos/{owner}/{repo}", owner=owner, repo=repo)

    async def list_branches(
        self, owner: str, repo: str, limit: int = 30,
    ) -> list[dict[str, Any]]:
        limit = min(limit, MAX_LIMIT)
        return await self._get(
            "/repos/{owner}/{repo}/branches",
            params={"limit": limit}, owner=owner, repo=repo,
        )

    async def list_commits(
        self, owner: str, repo: str,
        sha: str | None = None, limit: int = 30,
    ) -> list[dict[str, Any]]:
        limit = min(limit, MAX_LIMIT)
        params: dict[str, Any] = {"limit": limit}
        if sha:
            params["sha"] = sha
        return await self._get(
            "/repos/{owner}/{repo}/commits",
            params=params, owner=owner, repo=repo,
        )

    async def get_file(
        self, owner: str, repo: str,
        path: str, branch: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {}
        if branch:
            params["ref"] = branch
        result = await self._get(
            "/repos/{owner}/{repo}/contents/{path}",
            params=params, owner=owner, repo=repo, path=path,
        )
        if isinstance(result, dict) and "content" in result:
            import base64
            raw = base64.b64decode(result["content"])
            if len(raw) > MAX_FILE_SIZE:
                result["content"] = (
                    f"[truncated {len(raw)} bytes > {MAX_FILE_SIZE} limit]"
                )
                result["truncated"] = True
        return result

    async def list_issues(
        self, owner: str, repo: str,
        state: str = "open", limit: int = 30,
    ) -> list[dict[str, Any]]:
        limit = min(limit, MAX_LIMIT)
        return await self._get(
            "/repos/{owner}/{repo}/issues",
            params={"state": state, "limit": limit},
            owner=owner, repo=repo,
        )

    async def get_issue(
        self, owner: str, repo: str, issue_number: int,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/issues/{number}",
            owner=owner, repo=repo, number=issue_number,
        )

    async def list_pull_requests(
        self, owner: str, repo: str,
        state: str = "open", limit: int = 30,
    ) -> list[dict[str, Any]]:
        limit = min(limit, MAX_LIMIT)
        return await self._get(
            "/repos/{owner}/{repo}/pulls",
            params={"state": state, "limit": limit},
            owner=owner, repo=repo,
        )

    async def get_pull_request(
        self, owner: str, repo: str, pull_number: int,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/pulls/{number}",
            owner=owner, repo=repo, number=pull_number,
        )

    # ── Gitea Actions (CI/CD) ──────────────────────────────────────

    async def list_action_runs(
        self, owner: str, repo: str,
        status: str | None = None, limit: int = 10,
    ) -> dict[str, Any]:
        limit = min(limit, MAX_LIMIT)
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self._get(
            "/repos/{owner}/{repo}/actions/runs",
            params=params, owner=owner, repo=repo,
        )

    async def get_action_run(
        self, owner: str, repo: str, run_id: int,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/actions/runs/{run_id}",
            owner=owner, repo=repo, run_id=run_id,
        )

    async def list_action_run_jobs(
        self, owner: str, repo: str, run_id: int,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
            owner=owner, repo=repo, run_id=run_id,
        )

    async def list_workflows(
        self, owner: str, repo: str,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/actions/workflows",
            owner=owner, repo=repo,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GiteaClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
