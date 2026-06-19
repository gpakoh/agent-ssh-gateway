"""Read-only GitHub REST API client for fleet MCP adapter."""

from __future__ import annotations

import os
from typing import Any

import httpx

# ── Guardrails ────────────────────────────────────────────────────────
MAX_PER_PAGE = 50
MAX_FILE_SIZE = 256 * 1024  # 256 KB
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

ALLOWED_ENDPOINTS = frozenset({
    "/repos/{owner}/{repo}",
    "/repos/{owner}/{repo}/branches",
    "/repos/{owner}/{repo}/commits",
    "/repos/{owner}/{repo}/contents/{path}",
    "/repos/{owner}/{repo}/issues",
    "/repos/{owner}/{repo}/issues/{number}",
    "/repos/{owner}/{repo}/pulls",
    "/repos/{owner}/{repo}/pulls/{number}",
})

API_BASE = "https://api.github.com"


class GitHubClient:
    """Stateless async HTTP client for GitHub REST API (read-only)."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "agent-ssh-gateway-mcp/1.0",
            },
            timeout=REQUEST_TIMEOUT,
        )

    async def _get(
        self, endpoint: str,
        params: dict[str, Any] | None = None,
        **path_params: Any,
    ) -> Any:
        """Build URL, validate endpoint, perform GET, parse JSON."""
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"Endpoint not allowed: {endpoint}")

        path = endpoint.format(**path_params)
        resp = await self._client.get(path, params=params)

        if resp.status_code in (401, 403):
            detail = resp.json().get("message", "unauthorized")
            raise PermissionError(f"github api {path}: {detail}")

        resp.raise_for_status()
        return resp.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self._get("/repos/{owner}/{repo}", owner=owner, repo=repo)

    async def list_branches(
        self, owner: str, repo: str, per_page: int = 30
    ) -> list[dict[str, Any]]:
        per_page = min(per_page, MAX_PER_PAGE)
        return await self._get(
            "/repos/{owner}/{repo}/branches",
            params={"per_page": per_page},
            owner=owner, repo=repo,
        )

    async def list_commits(
        self, owner: str, repo: str,
        sha: str | None = None, per_page: int = 30,
    ) -> list[dict[str, Any]]:
        per_page = min(per_page, MAX_PER_PAGE)
        params: dict[str, Any] = {"per_page": per_page}
        if sha:
            params["sha"] = sha
        return await self._get(
            "/repos/{owner}/{repo}/commits",
            params=params,
            owner=owner, repo=repo,
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
            params=params,
            owner=owner, repo=repo, path=path,
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
        state: str = "open", per_page: int = 30,
    ) -> list[dict[str, Any]]:
        per_page = min(per_page, MAX_PER_PAGE)
        return await self._get(
            "/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": per_page},
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
        state: str = "open", per_page: int = 30,
    ) -> list[dict[str, Any]]:
        per_page = min(per_page, MAX_PER_PAGE)
        return await self._get(
            "/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": per_page},
            owner=owner, repo=repo,
        )

    async def get_pull_request(
        self, owner: str, repo: str, pull_number: int,
    ) -> dict[str, Any]:
        return await self._get(
            "/repos/{owner}/{repo}/pulls/{number}",
            owner=owner, repo=repo, number=pull_number,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
