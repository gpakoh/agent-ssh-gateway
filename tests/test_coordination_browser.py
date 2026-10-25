"""Browser Coordination board: admin authorization and read-only Gitea access."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.auth_middleware import AuthIdentity
from app.routers import coordination as route
from examples.mcp_server import coordination as core


def identity(token_type: str, role: str | None = None) -> AuthIdentity:
    return AuthIdentity(
        token_type=token_type, token="masked", name="caller",
        scopes=("*",), role=role,
    )


def test_browser_assets_never_embed_secrets_or_untrusted_html() -> None:
    from pathlib import Path

    script = Path("app/static/coordination.js").read_text(encoding="utf-8")
    html = Path("app/static/coordination.html").read_text(encoding="utf-8")
    assert "innerHTML" not in script
    assert "textContent" in script
    assert "credentials:\"same-origin\"" in script
    assert "GITEA_TOKEN" not in script
    assert "<script src=\"/static/coordination.js\" defer></script>" in html
    assert "<script>" not in html


def test_browser_route_requires_admin_role(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.auth_middleware import require_any_auth

    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[require_any_auth] = lambda: identity("web-ui", "viewer")
    with TestClient(app) as client:
        denied = client.get("/coordination")
        assert denied.status_code == 403
        denied_api = client.get("/api/coordination/board?project=agent-ssh-gateway")
        assert denied_api.status_code == 403

    app.dependency_overrides[require_any_auth] = lambda: identity("web-ui", "admin")
    with TestClient(app) as client:
        allowed = client.get("/coordination")
        assert allowed.status_code == 200
        assert "Py Coordination" in allowed.text
        assert allowed.headers["Cache-Control"] == "no-store"


def test_board_refuses_auth_disabled_mode(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(route, "settings", SimpleNamespace(api_auth_enabled=False))
    with pytest.raises(HTTPException) as error:
        route._admin_only(identity("master"))
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_read_only_gitea_principal_must_match_event_writer(monkeypatch) -> None:
    monkeypatch.setenv("COORDINATION_GITEA_WRITER_LOGIN", "gpakoh")
    async with route._ReadOnlyGitea("dummy", "https://git.xloud.ru/api/v1") as client:
        monkeypatch.setattr(client, "_get", AsyncMock(return_value={"login": "foreign-user"}))
        with pytest.raises(ValueError, match="differs"):
            await client.get_user()
        client._get.assert_awaited_once_with("user")


def test_browser_access_is_admin_only() -> None:
    assert route._admin_only(identity("master")).token_type == "master"
    assert route._admin_only(identity("web-ui", "admin")).role == "admin"
    for who in (
        identity("web-ui", "viewer"), identity("web-ui", "operator"),
        identity("agent"), identity("agent", "admin"),
    ):
        with pytest.raises(HTTPException) as exc:
            route._admin_only(who)
        assert exc.value.status_code == 403


def test_project_requires_registration_and_rejects_candidate(monkeypatch) -> None:
    class Registry:
        def project_info(self, project):
            if project == "not-registered":
                from app.workspace.registry import WorkspacePolicyError
                raise WorkspacePolicyError("no such project")
            return {"type": "candidate-clone" if project == "candidate-test" else "source"}

    monkeypatch.setattr(route, "get_registry", lambda: Registry())
    assert route._source_project("agent-ssh-gateway") == "agent-ssh-gateway"
    for value, status in (("../escape", 400), ("not-registered", 404), ("candidate-test", 404)):
        with pytest.raises(HTTPException) as e:
            route._source_project(value)
        assert e.value.status_code == status


def test_read_only_credential_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("COORDINATION_GITEA_READ_TOKEN", raising=False)
    with pytest.raises(HTTPException) as e:
        route._source_config()
    assert e.value.status_code == 503
    monkeypatch.setenv("COORDINATION_GITEA_READ_TOKEN", "dummy")
    monkeypatch.setenv("COORDINATION_GITEA_API_BASE", "http://127.0.0.1/api/v1")
    with pytest.raises(HTTPException) as e:
        route._source_config()
    assert e.value.status_code == 503
    monkeypatch.setenv("COORDINATION_GITEA_API_BASE", "https://git.xloud.ru/api/v1")
    assert route._source_config() == ("dummy", "gpakoh", "https://git.xloud.ru/api/v1")


class FakeGitea:
    instances: list[FakeGitea] = []

    def __init__(self, token, base):
        assert token == "fake-read-token"
        assert base == "https://git.xloud.ru/api/v1"
        self.requests = 0
        self.instances.append(self)
        self.issues = [
            {
                "number": 634, "title": "Cleanup blocker",
                "html_url": "https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/634",
                "body": core._header(kind="issue", key="browser-issue-634",
                                     source="business-analytics-service", state="open") + "\nEvidence",
                "state": "open", "updated_at": "2026-10-23T08:00:00Z",
            },
            {
                "number": 635, "title": "An ordinary pull request",
                "body": "not a coordination issue",
                "pull_request": {"url": "https://git.xloud.ru/api/v1/pulls/635"},
            },
        ]
        self.comments = [{
            "id": 1, "body": core._header(kind="event", key="browser-event-635",
                                         state="accepted") + "\nWorking",
            "user": {"login": "gpakoh"}, "created_at": "2026-10-23T08:15:00Z",
        }]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def list_issues(self, owner, repo, *, state, limit, page):
        self.requests += 1
        assert owner == "gpakoh" and repo == "agent-ssh-gateway"
        return self.issues[(page - 1) * limit:page * limit]

    async def get_issue(self, owner, repo, number):
        self.requests += 1
        return next(x for x in self.issues if x["number"] == number)

    async def list_issue_comments(self, owner, repo, number, *, limit, page):
        self.requests += 1
        return self.comments[(page - 1) * limit:page * limit]

    async def get_user(self):
        self.requests += 1
        return {"login": "gpakoh"}


@pytest.mark.asyncio
async def test_browser_board_and_thread_are_gitea_backed(monkeypatch) -> None:
    class Registry:
        def project_info(self, project):
            return {"type": "source"}

    monkeypatch.setattr(route, "get_registry", lambda: Registry())
    monkeypatch.setenv("COORDINATION_GITEA_READ_TOKEN", "fake-read-token")
    monkeypatch.setenv("COORDINATION_GITEA_API_BASE", "https://git.xloud.ru/api/v1")
    monkeypatch.setattr(route, "_ReadOnlyGitea", FakeGitea)
    response = await route.browser_coordination_board(
        project="agent-ssh-gateway", limit=20, _identity=identity("web-ui", "admin")
    )
    data = __import__("json").loads(response.body)
    assert response.headers["Cache-Control"] == "no-store"
    assert data["read_only"] is True
    assert data["source_origin"] == "https://git.xloud.ru"
    assert data["count"] == 1 and data["truncated"] is False
    assert [c["count"] for c in data["columns"]] == [0, 1, 0, 0]
    assert data["columns"][1]["cards"][0]["number"] == 634
    thread_response = await route.browser_coordination_thread(
        project="agent-ssh-gateway", issue_number=634, _identity=identity("web-ui", "admin")
    )
    thread_data = __import__("json").loads(thread_response.body)
    assert thread_data["thread"]["coord_state"] == "accepted"
    assert thread_data["thread"]["history"][0]["body"] == "Working"


@pytest.mark.asyncio
async def test_board_missing_credential_does_not_call_gitea(monkeypatch) -> None:
    class Registry:
        def project_info(self, project):
            return {"type": "source"}
    monkeypatch.setattr(route, "get_registry", lambda: Registry())
    monkeypatch.delenv("COORDINATION_GITEA_READ_TOKEN", raising=False)
    fake = AsyncMock()
    monkeypatch.setattr(route, "_ReadOnlyGitea", fake)
    with pytest.raises(HTTPException) as error:
        await route.browser_coordination_board(
            project="agent-ssh-gateway", limit=20, _identity=identity("master")
        )
    assert error.value.status_code == 503
    fake.assert_not_called()


@pytest.mark.asyncio
async def test_gitea_error_is_sanitized(monkeypatch) -> None:
    class Registry:
        def project_info(self, project):
            return {"type": "source"}
    monkeypatch.setattr(route, "get_registry", lambda: Registry())
    monkeypatch.setenv("COORDINATION_GITEA_READ_TOKEN", "fake-read-token")
    monkeypatch.setenv("COORDINATION_GITEA_API_BASE", "https://git.xloud.ru/api/v1")

    class Denied(FakeGitea):
        async def list_issues(self, owner, repo, *, state, limit, page):
            request = httpx.Request("GET", "https://git.xloud.ru/api/v1/repos/gpakoh/agent-ssh-gateway/issues")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("token leak", request=request, response=response)

    monkeypatch.setattr(route, "_ReadOnlyGitea", Denied)
    with pytest.raises(HTTPException) as error:
        await route.browser_coordination_board(
            project="agent-ssh-gateway", limit=20, _identity=identity("master")
        )
    assert error.value.status_code == 503
    assert "token leak" not in str(error.value.detail)


@pytest.mark.asyncio
async def test_browser_has_no_mutation_transport(monkeypatch) -> None:
    async with route._ReadOnlyGitea("dummy", "https://git.xloud.ru/api/v1") as client:
        assert not hasattr(client, "post")
        assert not hasattr(client, "create_coord_issue")
        assert not hasattr(client, "create_coord_comment")
