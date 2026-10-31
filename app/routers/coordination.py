"""Admin-only, read-only browser adapter for durable Gitea coordination Issues.

The authoritative projection lives in examples.mcp_server.coordination and
Gitea remains the single data store. A dedicated read credential is required.
The browser never receives any Gitea or MCP access token.
"""

from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse

from app.auth_middleware import AuthIdentity, require_any_auth
from app.config import settings
from app.coordination_logic import (
    board_from_threads,
    list_threads_preview,
    open_thread,
    preview_thread_state,
    resolve_coordination_repo,
)
from app.workspace.registry import WorkspacePolicyError, get_registry

router = APIRouter(tags=["coordination"])
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_DEFAULT_BASE_URL = "https://git.xloud.ru/api/v1"


def _admin_only(identity: AuthIdentity = Depends(require_any_auth)) -> AuthIdentity:
    """Only master API or authenticated web UI admin can read private boards."""
    if not settings.api_auth_enabled:
        raise HTTPException(status_code=503, detail="Authenticated board is disabled")
    if identity.token_type == "master":
        return identity
    if identity.token_type == "web-ui" and identity.role == "admin":
        return identity
    raise HTTPException(status_code=403, detail="Board requires administrator permission")


def _source_project(project: str) -> str:
    if not isinstance(project, str) or not _PROJECT_RE.fullmatch(project):
        raise HTTPException(status_code=400, detail="Invalid registered project name")
    try:
        info = get_registry().project_info(project)
    except (WorkspacePolicyError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Project is not registered") from exc
    if info.get("type") == "candidate-clone":
        raise HTTPException(status_code=404, detail="Project is not a source project")
    try:
        owner = os.environ.get("COORDINATION_GITEA_OWNER", "gpakoh")
        return resolve_coordination_repo(project, owner, info)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Unverified project repository mapping") from exc


def _source_config() -> tuple[str, str, str]:
    token = os.environ.get("COORDINATION_GITEA_READ_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="Coordination read credential is not configured")
    owner = os.environ.get("COORDINATION_GITEA_OWNER", "gpakoh")
    if not _PROJECT_RE.fullmatch(owner):
        raise HTTPException(status_code=503, detail="Invalid coordination owner configuration")
    writer = os.environ.get("COORDINATION_GITEA_WRITER_LOGIN", "gpakoh")
    if not _PROJECT_RE.fullmatch(writer):
        raise HTTPException(status_code=503, detail="Invalid coordination writer configuration")
    base = os.environ.get("COORDINATION_GITEA_API_BASE", _DEFAULT_BASE_URL).rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or not parsed.path.endswith("/api/v1")
    ):
        raise HTTPException(status_code=503, detail="Invalid coordination API origin configuration")
    return token, owner, base


class _ReadOnlyGitea:
    """Narrow GET-only transport; no generic Gitea mutation methods."""

    def __init__(self, token: str, base_url: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url + "/",
            headers={"Authorization": f"token {token}", "Accept": "application/json"},
            timeout=httpx.Timeout(8.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> _ReadOnlyGitea:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: int | str) -> object:
        # This helper is only called with fixed, known relative routes.
        response = await self._http.get(path.lstrip("/"), params=params or None)
        response.raise_for_status()
        return response.json()

    async def list_issues(
        self, owner: str, repo: str, *, state: str, limit: int, page: int
    ) -> object:
        return await self._get(
            f"repos/{owner}/{repo}/issues", state=state, limit=limit, page=page
        )

    async def get_issue(self, owner: str, repo: str, number: int) -> object:
        return await self._get(f"repos/{owner}/{repo}/issues/{number}")

    async def list_issue_comments(
        self, owner: str, repo: str, number: int, *, limit: int, page: int
    ) -> object:
        return await self._get(
            f"repos/{owner}/{repo}/issues/{number}/comments", limit=limit, page=page
        )

    async def get_user(self) -> object:
        # A distinct read-only PAT must belong to the SAME Gitea principal
        # that wrote the coordination events; otherwise marker provenance
        # would be silently lost on the board.
        user = await self._get("user")
        expected = os.environ.get("COORDINATION_GITEA_WRITER_LOGIN", "gpakoh")
        if not isinstance(user, dict) or user.get("login") != expected:
            raise ValueError("Gitea read principal differs from coordination writer")
        return user


def _upstream_error(exc: Exception) -> HTTPException:
    # Do not serialize token, upstream URLs, API response bodies or stack traces.
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        return HTTPException(status_code=503, detail="Coordination read credential was rejected")
    return HTTPException(status_code=502, detail="Gitea coordination read is unavailable")


@router.get("/coordination", include_in_schema=False)
async def browser_coordination(_identity: AuthIdentity = Depends(_admin_only)) -> FileResponse:
    return FileResponse(
        "app/static/coordination.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/coordination/board")
async def browser_coordination_board(
    project: str = Query(...),
    limit: int = Query(20, ge=1, le=25),
    _identity: AuthIdentity = Depends(_admin_only),
) -> Response:
    repo = _source_project(project)
    token, owner, base = _source_config()
    try:
        async with asyncio.timeout(30):
            async with _ReadOnlyGitea(token, base) as client:
                issues, scanned_truncated = await list_threads_preview(client, owner, repo)
                cards = []
                incomplete_thread_numbers: list[int] = []
                for issue in issues[:limit]:
                    state, incomplete = await preview_thread_state(
                        client, owner, repo, issue["number"]
                    )
                    if incomplete or state is None:
                        incomplete_thread_numbers.append(issue["number"])
                        continue
                    cards.append({**issue, "coord_state": state})
                columns = board_from_threads(cards)
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise _upstream_error(exc) from exc
    data = {
        "project": repo,
        "columns": columns,
        "count": len(cards),
        "truncated": scanned_truncated or len(issues) > limit
        or bool(incomplete_thread_numbers),
        "incomplete_thread_numbers": incomplete_thread_numbers,
        "source_origin": f"{urlsplit(base).scheme}://{urlsplit(base).netloc}",
        "read_only": True,
    }
    from fastapi.responses import JSONResponse

    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.get("/api/coordination/thread")
async def browser_coordination_thread(
    project: str = Query(...),
    issue_number: int = Query(..., ge=1),
    _identity: AuthIdentity = Depends(_admin_only),
) -> Response:
    repo = _source_project(project)
    token, owner, base = _source_config()
    try:
        async with asyncio.timeout(30):
            async with _ReadOnlyGitea(token, base) as client:
                detail = await open_thread(client, owner, repo, issue_number, allow_partial=True)
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise _upstream_error(exc) from exc
    from fastapi.responses import JSONResponse

    return JSONResponse({"thread": detail, "read_only": True}, headers={"Cache-Control": "no-store"})
