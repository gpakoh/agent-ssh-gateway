"""Regression cases for the full coordination read/write call chain."""

import asyncio
from copy import deepcopy
from typing import Any

import pytest

from app.coordination_logic import (
    CoordinationOutcomeUnknown,
    open_thread,
    preview_thread_state,
    write_event,
)

OWNER, REPO, NUMBER = "owner", "repo", 5
KEY = "recovery-event-001"
BODY = "checkpoint"
ACTOR = "0123456789abcdef"
ISSUE = {
    "id": 4,
    "number": NUMBER,
    "title": "task",
    "body": "<!-- py-coordination/v1 issue key=original-task source=repo state=open -->\nTask",
    "state": "open",
    "updated_at": "2026-11-27T00:00:00Z",
    "comments": 1,
}
COMMENT = {"id": 51, "type": "comment", "body": "prior", "user": {"login": "bot"}}
SERVICES = [{"id": i, "type": "label"} for i in range(1, 51)]
EVENT = {
    **COMMENT,
    "body": f"<!-- py-coordination/v1 event key={KEY} state=message actor={ACTOR} -->\n{BODY}",
}


class Client:
    """Stateful fake API: the POST itself is counted, including ambiguous outcomes."""

    def __init__(
        self,
        rows: list[Any],
        versions: list[dict[str, Any]] | None = None,
        *,
        timeout_after_post: bool = False,
        timeout_without_effect: bool = False,
    ) -> None:
        self.rows = list(rows)
        self.versions = list(versions or [])
        self.issue = deepcopy(ISSUE)
        self.comments = [deepcopy(COMMENT)]
        self.pages: list[int] = []
        self.reads = 0
        self.posts: list[str] = []
        self.timeout_after_post = timeout_after_post
        self.timeout_without_effect = timeout_without_effect

    async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        assert (owner, repo, number) == (OWNER, REPO, NUMBER)
        self.reads += 1
        return deepcopy(self.versions.pop(0) if self.versions else self.issue)

    async def list_issue_timeline(
        self, owner: str, repo: str, number: int, *, limit: int, page: int
    ) -> Any:
        assert (owner, repo, number, limit) == (OWNER, REPO, NUMBER, 50)
        self.pages.append(page)
        return deepcopy(self.rows.pop(0) if self.rows else self.comments)

    async def get_user(self) -> dict[str, str]:
        return {"login": "bot"}

    async def create_coord_comment(
        self, owner: str, repo: str, number: int, *, body: str
    ) -> dict[str, Any]:
        assert (owner, repo, number) == (OWNER, REPO, NUMBER)
        self.posts.append(body)
        if self.timeout_without_effect:
            raise TimeoutError("response unavailable")
        comment = {**COMMENT, "id": 52, "body": body}
        self.comments.append(comment)
        self.issue["comments"] = len(self.comments)
        self.issue["updated_at"] = "2026-11-27T00:00:01Z"
        if self.timeout_after_post:
            raise TimeoutError("response lost after commit")
        return deepcopy(comment)


def submit(client: Client) -> dict[str, Any]:
    return asyncio.run(write_event(
        client, OWNER, REPO, NUMBER, text=BODY, request_key=KEY,
        state="message", actor=ACTOR,
    ))


def preview(client: Client) -> dict[str, Any]:
    return asyncio.run(open_thread(
        client, OWNER, REPO, NUMBER, allow_partial=True, recent_preview=True,
    ))


@pytest.mark.parametrize(
    "rows,pages", [([None, [COMMENT]], [1, 1]), ([SERVICES, None, [COMMENT]], [1, 2, 2])]
)
def test_write_recovers_one_read_then_posts_once(rows: list[Any], pages: list[int]) -> None:
    client = Client(rows)
    result = submit(client)
    assert result == {"number": NUMBER, "event_id": 52, "created": True, "state": "message"}
    assert client.pages == pages
    assert client.reads == 3
    assert len(client.posts) == 1


def test_existing_event_after_recovery_never_posts() -> None:
    client = Client([None, [EVENT]])
    result = submit(client)
    assert result["created"] is False
    assert result["event"]["id"] == 51
    assert client.posts == []


def test_post_timeout_reconciles_via_recovered_read_without_second_post() -> None:
    client = Client([[COMMENT], None], timeout_after_post=True)
    result = submit(client)
    assert result["reconciled"] is True
    assert result["event"]["id"] == 52
    assert client.pages == [1, 1, 1]
    assert len(client.posts) == 1


def test_post_timeout_without_effect_never_retries_post() -> None:
    client = Client([[COMMENT], None, [COMMENT]], timeout_without_effect=True)
    with pytest.raises(CoordinationOutcomeUnknown, match="do not replay"):
        submit(client)
    assert len(client.posts) == 1


@pytest.mark.parametrize("rows,versions", [
    ([None, None], None),
    ([None, SERVICES, None], None),
    ([None, []], None),
    ([None], [ISSUE, {**ISSUE, "comments": 2}]),
    ([None, [COMMENT]], [ISSUE, ISSUE, {**ISSUE, "updated_at": "changed"}]),
    ([SERVICES, None, [SERVICES[0]]], None),
    ([None, [{**COMMENT, "user": None}]], None),
    ([None, [{**COMMENT, "id": True}]], None),
])
def test_invalid_recovery_never_posts(rows: list[Any], versions: list[dict[str, Any]] | None) -> None:
    client = Client(rows, versions)
    with pytest.raises(CoordinationOutcomeUnknown):
        submit(client)
    assert client.posts == []


def test_recovered_preview_preserves_exact_close_digest() -> None:
    recovered = preview(Client([None, [COMMENT]]))
    normal = preview(Client([[COMMENT]]))
    assert recovered["history_scan_complete"] is True
    assert recovered["history"] == normal["history"]
    assert recovered["close_state_sha256"] == normal["close_state_sha256"]
    assert recovered["close_content_sha256"] == normal["close_content_sha256"]


@pytest.mark.parametrize("rows,versions", [
    ([None, None], None),
    ([None, []], None),
    ([None, [COMMENT]], [ISSUE, ISSUE, {**ISSUE, "state": "closed"}]),
])
def test_unverified_preview_has_no_close_lease(rows: list[Any], versions: list[dict[str, Any]] | None) -> None:
    result = preview(Client(rows, versions))
    assert result["history_scan_complete"] is False
    assert result["coord_state"] is None
    assert "close_state_sha256" not in result
    assert "close_content_sha256" not in result
    assert result["history_older_cursor"] is None


def test_board_preview_recovers_known_state() -> None:
    client = Client([None, [EVENT]])
    assert asyncio.run(preview_thread_state(client, OWNER, REPO, NUMBER)) == ("open", False)
    assert client.posts == []


def test_recovered_existing_key_with_other_payload_is_rejected() -> None:
    client = Client([None, [{**EVENT, "body": EVENT["body"].replace(BODY, "different")}]])
    with pytest.raises(ValueError, match="reused with different event content"):
        submit(client)
    assert client.posts == []


def test_incomplete_recovery_cannot_begin_signed_review() -> None:
    client = Client([None, None])
    with pytest.raises(ValueError, match="complete counted history scan"):
        asyncio.run(open_thread(
            client, OWNER, REPO, NUMBER, allow_partial=True, recent_preview=True,
            history_cursor="review", review_key=b"k" * 32,
        ))
    assert client.posts == []


def test_recovered_resolved_review_has_same_lease_as_normal() -> None:
    event = {**EVENT, "body": EVENT["body"].replace("state=message", "state=resolved")}

    def review(client: Client) -> dict[str, Any]:
        return asyncio.run(open_thread(
            client, OWNER, REPO, NUMBER, allow_partial=True, recent_preview=True,
            history_cursor="review", review_key=b"k" * 32,
        ))

    recovered = review(Client([None, [event]]))
    normal = review(Client([[event]]))
    assert recovered["history_review_complete"] is True
    assert recovered["review_coord_state"] == "resolved"
    assert recovered["review_resolution_event_id"] == 51
    assert recovered["review_close_state_sha256"] == normal["review_close_state_sha256"]
