"""Bounded recovery of a transient null coordination timeline page."""

import asyncio
from typing import Any

import pytest

from app.coordination_logic import (
    CoordinationOutcomeUnknown,
    _issue_timeline_comment_pages,
    _timeline_comment_pages,
)

ISSUE = {
    "id": 4, "number": 5, "title": "task", "body": "body", "state": "open",
    "updated_at": "2026-11-27T00:00:00Z", "comments": 1,
}
COMMENT = {"id": 51, "type": "comment", "body": "event", "user": {"login": "bot"}}
SERVICES = [{"id": i, "type": "label"} for i in range(1, 51)]


class Client:
    def __init__(self, rows: list[Any], versions: tuple | list = ()) -> None:
        self.rows = iter(rows)
        self.versions = iter(versions)
        self.pages: list[int] = []
        self.reads = 0

    async def list_issue_timeline(
        self, owner: str, repo: str, number: int, *, limit: int, page: int,
    ) -> Any:
        self.pages.append(page)
        return next(self.rows)

    async def get_issue(self, owner: str, repo: str, number: int) -> Any:
        self.reads += 1
        return next(self.versions)


def run(client: Client, issue: dict[str, Any] | None = None, partial: bool = False,
        max_pages: int = 3) -> tuple:
    return asyncio.run(_issue_timeline_comment_pages(
        client, "o", "r", 5, ISSUE if issue is None else issue,
        allow_partial=partial, max_pages=max_pages,
    ))


def test_old_rejects_transient_null() -> None:
    client = Client([None, [COMMENT]])

    async def fetch(page: int) -> Any:
        return await client.list_issue_timeline("o", "r", 5, limit=50, page=page)

    with pytest.raises(CoordinationOutcomeUnknown):
        asyncio.run(_timeline_comment_pages(fetch, allow_partial=False))
    assert client.pages == [1]


@pytest.mark.parametrize("rows,pages", [
    ([None, [COMMENT]], [1, 1]),
    ([SERVICES, None, [COMMENT]], [1, 2, 2]),
])
def test_transient(rows: list[Any], pages: list[int]) -> None:
    client = Client(rows, [ISSUE, ISSUE])
    assert run(client)[:3] == ([COMMENT], False, None)
    assert client.pages == pages and client.reads == 2


@pytest.mark.parametrize("rows,versions", [
    ([None, None], [ISSUE]),
    ([None, SERVICES, None], [ISSUE]),
    ([None, [COMMENT]], [ISSUE, {**ISSUE, "updated_at": "changed"}]),
    ([None, []], [ISSUE]),
    ([None], [{**ISSUE, "comments": 2}]),
    ([SERVICES, None, [SERVICES[0]]], [ISSUE]),
])
def test_fail_closed(rows: list[Any], versions: list[dict[str, Any]]) -> None:
    client = Client(rows, versions)
    with pytest.raises(CoordinationOutcomeUnknown):
        run(client)
    assert client.reads <= 2


@pytest.mark.parametrize("issue", [
    {**ISSUE, "comments": None}, {**ISSUE, "comments": True},
    {**ISSUE, "id": None}, {**ISSUE, "updated_at": ""},
])
def test_unknown_identity_no_retry(issue: dict[str, Any]) -> None:
    client = Client([None])
    assert run(client, issue, partial=True)[1:3] == (True, "unverifiable_null_timeline_page")
    assert client.pages == [1] and client.reads == 0


@pytest.mark.parametrize("rows,issue,expected", [
    ([[COMMENT]], ISSUE, [COMMENT]),
    ([None], {**ISSUE, "comments": 0}, []),
])
def test_normal_unchanged(rows: list[Any], issue: dict[str, Any], expected: list) -> None:
    client = Client(rows)
    assert run(client, issue)[:3] == (expected, False, None)
    assert client.pages == [1] and client.reads == 0


def test_preview_drift_no_complete_result() -> None:
    client = Client([None, [COMMENT]], [ISSUE, {**ISSUE, "state": "closed"}])
    assert run(client, partial=True)[1:3] == (True, "issue_changed_during_scan")
