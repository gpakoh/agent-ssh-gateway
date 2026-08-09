"""Read-only GitHub REST API client for fleet MCP adapter."""

from __future__ import annotations

from typing import Any

import httpx

from .shared import validate_repo_owner_or_name, validate_repo_path

# ── Guardrails ────────────────────────────────────────────────────────
MAX_PER_PAGE = 50
MAX_FILE_SIZE = 256 * 1024  # 256 KB
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def normalize_list_response(
    value: Any,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a bare list in a stable dict for MCP tool output.

    MCP protocol expects tool results to be JSON objects (dicts), not bare arrays.
    This helper normalises list data to ``{"items": [...], "count": N}``.

    If *value* is already a dict with an ``items`` key, it is returned as-is
    (with count ensured).  If it is a plain dict, it is returned unchanged.
    Otherwise returns an empty result.
    """
    if isinstance(value, dict):
        if "items" in value:
            if "count" not in value:
                value["count"] = len(value["items"])
            if meta:
                value.update(meta)
            return value
        if meta:
            value.update(meta)
        return value

    if isinstance(value, list):
        result: dict[str, Any] = {"items": value, "count": len(value)}
        if meta:
            result.update(meta)
        return result

    return {"items": [], "count": 0, "error": "unexpected response type"}


ALLOWED_ENDPOINTS = frozenset(
    {
        "/repos/{owner}/{repo}",
        "/repos/{owner}/{repo}/branches",
        "/repos/{owner}/{repo}/commits",
        "/repos/{owner}/{repo}/contents",
        "/repos/{owner}/{repo}/contents/{path}",
        "/repos/{owner}/{repo}/issues",
        "/repos/{owner}/{repo}/issues/{number}",
        "/repos/{owner}/{repo}/pulls",
        "/repos/{owner}/{repo}/pulls/{number}",
    }
)

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
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        **path_params: Any,
    ) -> Any:
        """Build URL, validate endpoint, perform GET, parse JSON."""
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
            raise PermissionError(f"github api {path}: {detail}")

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Same fix as gitea_client.py: httpx.HTTPStatusError's own
            # message embeds the fully-resolved absolute URL, and FastMCP
            # sends str(exception) straight to the external MCP client
            # verbatim on any tool failure. API_BASE is public here, but
            # sanitize anyway for consistency and to avoid ever depending
            # on that staying true.
            raise httpx.HTTPStatusError(
                f"github api {path}: {resp.status_code} {resp.reason_phrase}",
                request=exc.request,
                response=exc.response,
            ) from None
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
            owner=owner,
            repo=repo,
        )

    async def list_commits(
        self,
        owner: str,
        repo: str,
        sha: str | None = None,
        per_page: int = 30,
    ) -> list[dict[str, Any]]:
        per_page = min(per_page, MAX_PER_PAGE)
        params: dict[str, Any] = {"per_page": per_page}
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
        path: str = "",
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Get a file, a directory listing, or (path="") the repo root
        listing. validate_repo_path() rejects an empty path, so root
        listing must go through the path-less endpoint variant instead
        of substituting {path} at all -- P2 audit finding: there was
        previously no way to list a repo's top level through this tool.
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
        per_page: int = 30,
    ) -> list[dict[str, Any]]:
        per_page = min(per_page, MAX_PER_PAGE)
        return await self._get(
            "/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": per_page},
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
        per_page: int = 30,
    ) -> list[dict[str, Any]]:
        per_page = min(per_page, MAX_PER_PAGE)
        return await self._get(
            "/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": per_page},
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

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
