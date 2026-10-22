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


# Stable, read-only Kanban representation of the authoritative Gitea threads.
# This projection never creates a second status store or treats a merged PR
# as a closed cross-project dependency.
_BOARD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("open", "Входящие"),
    ("accepted", "В работе"),
    ("waiting_review", "Ожидает проверки"),
    ("resolved", "Решено"),
)


def board_from_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a bounded set of verified coordination threads into Kanban columns.

    Ordering follows Gitea's returned issue order. In particular, this does
    not infer completion from Gitea issue state or Git branch/PR status.
    """
    grouped: dict[str, list[dict[str, Any]]] = {state: [] for state, _ in _BOARD_COLUMNS}
    for item in threads:
        status = item.get("coord_state")
        if status not in grouped:
            raise ValueError("Gitea thread has an invalid coordination state")
        grouped[status].append({
            "number": item["number"],
            "title": item["title"],
            "url": item["url"],
            "source_project": item["source_project"],
            "request_key": item["request_key"],
            "updated_at": item["updated_at"],
            "gitea_issue_state": item["state"],
        })
    return [
        {"state": state, "label": label, "count": len(grouped[state]), "cards": grouped[state]}
        for state, label in _BOARD_COLUMNS
    ]


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


def _same_issue_payload(
    raw: dict[str, Any], *, title: str, source_project: str, request_key: str, text: str
) -> bool:
    if raw.get("title") != title:
        return False
    body = raw.get("body")
    if not isinstance(body, str):
        return False
    marker = _parse(body, "issue")
    return (
        marker is not None
        and marker["key"] == request_key
        and marker["source"] == source_project
        and body.partition("\n")[2] == text
    )


def _same_event_payload(entry: dict[str, Any], *, state: str, text: str) -> bool:
    # Caller fingerprint is audit identity from the ORIGINAL write. Session
    # rotation/new chats must not invalidate idempotent replay of the same key.
    return entry["state"] == state and entry["body"] == text


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
        if not _same_issue_payload(
            previous, title=title, source_project=source_project,
            request_key=request_key, text=body,
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
                observed = await client.get_issue(owner, repo, found[0]["number"])
                if _same_issue_payload(
                    observed, title=title, source_project=source_project,
                    request_key=request_key, text=body,
                ):
                    return {"thread": found[0], "created": True, "reconciled": True}
        except Exception:
            pass
        raise CoordinationOutcomeUnknown("issue write outcome uncertain; do not replay with a new key") from exc
    item = _issue(result)
    if item is None or not _same_issue_payload(
        result, title=title, source_project=source_project,
        request_key=request_key, text=body,
    ):
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
        if len(existing) != 1 or not _same_event_payload(existing[0], state=state, text=text):
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
            if len(found) == 1 and _same_event_payload(found[0], state=state, text=text):
                return {"number": number, "event": found[0], "created": True, "reconciled": True}
        except Exception:
            pass
        raise CoordinationOutcomeUnknown("comment write outcome uncertain; do not replay with a new key") from exc
    marker = _parse(result.get("body"), "event")
    if (
        marker is None or marker["key"] != request_key or marker["state"] != state
        or result.get("body", "").partition("\n")[2] != text
    ):
        raise CoordinationOutcomeUnknown("comment created but result could not be verified")
    return {"number": number, "event_id": result.get("id"), "created": True, "state": state}
