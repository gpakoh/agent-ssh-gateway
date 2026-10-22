"""Gitea-backed Py Coordination threads.

Gitea issues and comments are authoritative; this module stores no secondary
thread state. Coordination markers are data, not executable instructions.
"""

from __future__ import annotations

import re
from typing import Any

_MARKER = "py-coordination/v1"
_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{7,99}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_HEADER = re.compile(
    r"^<!-- py-coordination/v1 (?:issue|event) key=([a-z0-9._-]+)"
    r"(?: source=([A-Za-z0-9_.-]+))?(?: state=([a-z_]+))?(?: actor=([0-9a-f]{16}))? -->$"
)
_STATES = frozenset({"open", "accepted", "waiting_review", "resolved", "message"})
_TRANSITIONS = {
    "open": frozenset({"accepted", "message"}),
    "accepted": frozenset({"waiting_review", "message"}),
    "waiting_review": frozenset({"accepted", "resolved", "message"}),
    "resolved": frozenset(),
}
_PAGE_SIZE = 50
_MAX_PAGES = 10


class CoordinationOutcomeUnknown(RuntimeError):
    """A Gitea mutation crossed the call boundary without proven outcome."""


def validate_key(value: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError("request_key must be 8..100 lowercase alphanumeric/._- characters")
    return value


def validate_project(value: str) -> str:
    if not isinstance(value, str) or not _PROJECT.fullmatch(value):
        raise ValueError("project must be a registered repository name")
    return value


def _text(value: str, *, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > limit or any(ord(c) == 0 for c in value):
        raise ValueError(f"{name} must contain 1..{limit} characters without NUL")
    return value


def _header(*, kind: str, key: str, source: str = "", state: str = "", actor: str = "") -> str:
    return (
        f"<!-- {_MARKER} {kind} key={key}"
        + (f" source={source}" if source else "")
        + (f" state={state}" if state else "")
        + (f" actor={actor}" if actor else "")
        + " -->"
    )


def _parse(body: Any, kind: str) -> dict[str, str] | None:
    if not isinstance(body, str):
        return None
    line = body.split("\n", 1)[0]
    matched = _HEADER.fullmatch(line)
    if matched is None or f" {_MARKER} {kind} " not in line:
        return None
    key, source, state, actor = matched.groups()
    if _KEY.fullmatch(key) is None:
        return None
    if kind == "issue" and (not source or state != "open"):
        return None
    if kind == "event" and (source or state not in _STATES):
        return None
    return {"key": key, "source": source or "", "state": state or "", "actor": actor or ""}


def _issue(issue: dict[str, Any]) -> dict[str, Any] | None:
    if issue.get("pull_request") is not None:
        return None
    marker = _parse(issue.get("body"), "issue")
    if marker is None:
        return None
    number = issue.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    return {
        "number": number,
        "title": issue.get("title", ""),
        "url": issue.get("html_url"),
        "source_project": marker["source"],
        "request_key": marker["key"],
        "created_by": marker["actor"],
        "state": issue.get("state"),
        "updated_at": issue.get("updated_at"),
    }


async def _pages(fetch: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        rows = await fetch(page)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Gitea collection returned invalid rows")
        result.extend(rows)
        if len(rows) < _PAGE_SIZE:
            return result
    # Incomplete lookups must not authorize potentially duplicate writes.
    raise CoordinationOutcomeUnknown("collection exceeds bounded 500-row reconciliation window")


async def list_threads(client: Any, owner: str, repo: str) -> list[dict[str, Any]]:
    rows = await _pages(
        lambda page: client.list_issues(owner, repo, state="all", limit=_PAGE_SIZE, page=page)
    )
    return [item for row in rows if (item := _issue(row)) is not None]


async def open_thread(client: Any, owner: str, repo: str, number: int) -> dict[str, Any]:
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("issue_number must be positive")
    issue = await client.get_issue(owner, repo, number)
    item = _issue(issue)
    if item is None:
        raise ValueError("issue is not a Py Coordination thread")
    comments = await _pages(
        lambda page: client.list_issue_comments(owner, repo, number, limit=_PAGE_SIZE, page=page)
    )
    trusted_identity = await client.get_user()
    trusted_login = trusted_identity.get("login")
    if not isinstance(trusted_login, str) or not trusted_login:
        raise ValueError("Gitea coordination service identity is unavailable")
    history: list[dict[str, Any]] = []
    for comment in comments:
        body = comment.get("body", "")
        author = (comment.get("user") or {}).get("login")
        marker = _parse(body, "event") if author == trusted_login else None
        state = marker["state"] if marker else None
        history.append(
            {
                "id": comment.get("id"),
                "body": body.split("\n", 1)[-1] if marker else body,
                "state": state,
                "request_key": marker["key"] if marker else None,
                "caller": marker["actor"] if marker else None,
                "created_at": comment.get("created_at"),
                "author": author,
            }
        )
    item["history"] = history
    item["coord_state"] = next(
        (entry["state"] for entry in reversed(history) if entry["state"] not in (None, "message")), "open"
    )
    return item


async def create_thread(
    client: Any, owner: str, repo: str, *,
    source_project: str, title: str, body: str, request_key: str, actor: str = ""
) -> dict[str, Any]:
    source_project = validate_project(source_project)
    title = _text(title, name="title", limit=160)
    body = _text(body, name="body", limit=6000)
    request_key = validate_key(request_key)
    existing = [x for x in await list_threads(client, owner, repo) if x["request_key"] == request_key]
    if existing:
        if len(existing) != 1:
            raise CoordinationOutcomeUnknown("request_key matches more than one Gitea issue")
        previous = await client.get_issue(owner, repo, existing[0]["number"])
        if previous.get("title") != title or previous.get("body") != (
            _header(kind="issue", key=request_key, source=source_project, state="open", actor=actor)
            + "\n" + body
        ):
            raise ValueError("request_key reused with different issue content")
        return {"thread": existing[0], "created": False}
    payload = _header(kind="issue", key=request_key, source=source_project, state="open", actor=actor) + "\n" + body
    try:
        result = await client.create_coord_issue(owner, repo, title=title, body=payload)
    except (PermissionError, ValueError):
        raise
    except Exception as exc:
        # Network errors and unreadable success bodies may mean the issue was
        # created. Reconcile by idempotency marker; never blindly replay POST.
        try:
            found = [x for x in await list_threads(client, owner, repo) if x["request_key"] == request_key]
            if len(found) == 1:
                return {"thread": found[0], "created": True, "reconciled": True}
        except Exception:
            pass
        raise CoordinationOutcomeUnknown("issue write outcome uncertain; do not replay with a new key") from exc
    item = _issue(result)
    if item is None or item["request_key"] != request_key:
        raise CoordinationOutcomeUnknown("issue created but result could not be verified")
    return {"thread": item, "created": True}


async def write_event(
    client: Any, owner: str, repo: str, number: int, *,
    text: str, request_key: str, state: str, actor: str = ""
) -> dict[str, Any]:
    text = _text(text, name="message", limit=6000)
    request_key = validate_key(request_key)
    if state not in _STATES:
        raise ValueError("unknown coordination state")
    thread = await open_thread(client, owner, repo, number)
    existing = [e for e in thread["history"] if e["request_key"] == request_key]
    if existing:
        if (
            existing[0]["state"] != state
            or existing[0]["body"] != text
            or existing[0]["caller"] != actor
        ):
            raise ValueError("request_key reused with different event content")
        return {"number": number, "event": existing[0], "created": False}
    if state not in _TRANSITIONS.get(thread["coord_state"], frozenset()):
        raise ValueError("invalid coordination state transition")
    # A proposed resolution is still a comment: the creator must confirm
    # evidence before a Gitea issue is closed by an authorized owner.
    payload = _header(kind="event", key=request_key, state=state, actor=actor) + "\n" + text
    try:
        result = await client.create_coord_comment(owner, repo, number, body=payload)
    except (PermissionError, ValueError):
        raise
    except Exception as exc:
        try:
            refreshed = await open_thread(client, owner, repo, number)
            found = [e for e in refreshed["history"] if e["request_key"] == request_key]
            if len(found) == 1:
                return {"number": number, "event": found[0], "created": True, "reconciled": True}
        except Exception:
            pass
        raise CoordinationOutcomeUnknown("comment write outcome uncertain; do not replay with a new key") from exc
    marker = _parse(result.get("body"), "event")
    if marker is None or marker["key"] != request_key or marker["state"] != state:
        raise CoordinationOutcomeUnknown("comment created but result could not be verified")
    return {"number": number, "event_id": result.get("id"), "created": True, "state": state}
