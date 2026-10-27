"""Explicit Issue close, bound to a reviewed resolution and durable attempt.

Gitea remains authoritative for reports and thread state. Local receipts contain
operation hashes only: an interrupted attempt can reconcile but cannot resend a
PATCH. File locks coordinate MCP workers sharing the configured audit volume.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.coordination_logic import (
    CoordinationOutcomeUnknown,
    _text,
    open_thread,
    validate_key,
    validate_project,
)


class CloseRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _regular(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise CloseRefused("RECEIPT_UNAVAILABLE", "Close receipt is not a private regular file")


@contextmanager
def _issue_lock(root: Path, owner: str, repo: str, number: int):
    """Nonblocking OS lock: never stall the async MCP loop on another writer."""
    target = root / _hash([owner, repo, number])
    for directory in (root, target):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CloseRefused("RECEIPT_UNAVAILABLE", "Close receipt directory is not private")
    fd = os.open(target / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        _regular(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CloseRefused("CLOSE_BUSY", "Another close is active for this Issue") from exc
        yield target
    finally:
        os.close(fd)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        _regular(stream.fileno())
        raw = stream.read(8193)
    try:
        result = json.loads(raw)
    except ValueError as exc:
        raise CloseRefused("RECEIPT_UNAVAILABLE", "Close receipt is unreadable") from exc
    if (
        len(raw) > 8192
        or not isinstance(result, dict)
        or result.get("version") != 1
        or result.get("phase") not in {"dispatched", "confirmed", "rejected"}
        or any(
            re.fullmatch(r"[0-9a-f]{64}", str(result.get(key, ""))) is None
            for key in ("payload_sha256", "content_sha256", "report_sha256")
        )
        or type(result.get("resolution_event_id")) is not int
        or result["resolution_event_id"] < 1
        or re.fullmatch(r"[0-9a-f]{16}", str(result.get("actor", ""))) is None
    ):
        raise CloseRefused("RECEIPT_UNAVAILABLE", "Close receipt is invalid")
    return result


def _save(path: Path, receipt: dict[str, Any]) -> None:
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _report(thread: dict[str, Any], event_id: int) -> dict[str, Any]:
    states = [event for event in thread["history"] if event["state"] not in (None, "message")]
    if (
        thread["coord_state"] != "resolved"
        or not states
        or states[-1]["id"] != event_id
        or states[-1]["state"] != "resolved"
        or not states[-1]["body"].strip()
    ):
        raise CloseRefused(
            "RESOLUTION_REQUIRED", "Review the latest trusted resolved report before closing"
        )
    return states[-1]


def _confirmed(thread: dict[str, Any], receipt: dict[str, Any], event_id: int) -> bool:
    _report(thread, event_id)
    return (
        thread["state"] == "closed" and thread["close_content_sha256"] == receipt["content_sha256"]
    )


async def close_thread(
    client: Any,
    owner: str,
    repo: str,
    number: int,
    *,
    resolution_event_id: int,
    expected_state_sha256: str,
    request_key: str,
    reason: str,
    actor: str,
    journal_dir: Path,
    audit: Any,
) -> dict[str, Any]:
    """Close once; every replay of a dispatched attempt is read-only.

    Gitea does not provide Issue compare-and-swap here. Re-read immediately
    before PATCH and verify content afterwards; concurrent remote edits can
    make the outcome unknown even when the server has closed the Issue.
    """
    validate_project(owner)
    validate_project(repo)
    request_key = validate_key(request_key)
    reason = _text(reason, name="reason", limit=500)
    for label, value in (("issue_number", number), ("resolution_event_id", resolution_event_id)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{label} must be a positive integer")
    if (
        not isinstance(expected_state_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_state_sha256) is None
    ):
        raise ValueError("expected_state_sha256 must be a 64-character lowercase SHA-256")
    if not isinstance(actor, str) or re.fullmatch(r"[0-9a-f]{16}", actor) is None:
        raise PermissionError("Authenticated caller required")
    payload_hash = _hash(
        [owner, repo, number, resolution_event_id, expected_state_sha256, request_key, reason]
    )
    with _issue_lock(journal_dir, owner, repo, number) as directory:
        path = directory / (_hash(request_key) + ".json")
        prior = _read(path)
        if prior and prior.get("payload_sha256") != payload_hash:
            raise CloseRefused(
                "IDEMPOTENCY_CONFLICT", "request_key was used with a different close payload"
            )
        # All permission and report checks run on replays too. Session rotation
        # is allowed; the original actor remains on the durable attempt.
        thread = await open_thread(client, owner, repo, number)
        report = _report(thread, resolution_event_id)
        result = {
            "number": number,
            "url": thread["url"],
            "resolution_event_id": resolution_event_id,
            "request_key": request_key,
            "closed": True,
            "verified": True,
        }
        if prior:
            if _confirmed(thread, prior, resolution_event_id):
                if prior["phase"] != "confirmed":
                    _save(path, {**prior, "phase": "confirmed"})
                return {**result, "already_closed": True, "reconciled": True}
            raise CoordinationOutcomeUnknown(
                "Previous close is not confirmed; replay is read-only, inspect the thread"
            )
        if thread["close_state_sha256"] != expected_state_sha256:
            raise CloseRefused(
                "STATE_MISMATCH", "Issue or discussion changed; reopen and review the thread"
            )
        paths = list(directory.glob("*.json"))
        if len(paths) > 100:
            raise CloseRefused("RECEIPT_UNAVAILABLE", "Close reconciliation window exceeded")
        for other_path in paths:
            other = _read(other_path)
            if other and other.get("phase") == "dispatched":
                if not _confirmed(thread, other, other["resolution_event_id"]):
                    raise CoordinationOutcomeUnknown(
                        "An earlier close is unresolved; inspect the thread before any new operation"
                    )
                _save(other_path, {**other, "phase": "confirmed"})
        receipt = {
            "version": 1,
            "payload_sha256": payload_hash,
            "content_sha256": thread["close_content_sha256"],
            "resolution_event_id": resolution_event_id,
            "report_sha256": _hash(report),
            "actor": actor,
            "phase": "confirmed" if thread["state"] == "closed" else "dispatched",
        }
        if thread["state"] == "closed":
            _save(path, receipt)
            return {**result, "already_closed": True, "reconciled": False}
        if thread["state"] != "open":
            raise CloseRefused("STATE_MISMATCH", "Issue is neither open nor closed")
        metadata = {
            "owner": owner,
            "repo": repo,
            "issue_number": number,
            "actor": actor,
            "gitea_user": report["author"],
            "resolution_event_id": resolution_event_id,
            "request_key": request_key,
            "payload_sha256": payload_hash,
            "state_sha256": expected_state_sha256,
        }
        audit("intent", reason, metadata)
        fresh = await open_thread(client, owner, repo, number)
        _report(fresh, resolution_event_id)
        if fresh["close_state_sha256"] != expected_state_sha256:
            raise CloseRefused("STATE_MISMATCH", "Issue changed before close; review it again")
        # Persist BEFORE the HTTP boundary. A crash/cancellation now leaves an
        # uncertain attempt which may only reconcile, never automatically PATCH.
        _save(path, receipt)
        mutation_error = False
        try:
            await client.close_coord_issue(owner, repo, number)
        except PermissionError:
            receipt["phase"] = "rejected"
            _save(path, receipt)
            raise
        except Exception:
            mutation_error = True
        try:
            observed = await open_thread(client, owner, repo, number)
            confirmed = _confirmed(observed, receipt, resolution_event_id)
        except Exception:
            confirmed = False
        if not confirmed:
            raise CoordinationOutcomeUnknown(
                "Close outcome is not confirmed; inspect the thread, do not resend PATCH"
            )
        receipt["phase"] = "confirmed"
        _save(path, receipt)
        try:
            audit("success", reason, metadata)
        except Exception:
            pass  # strict pre-write intent is durable; post-write audit is best effort
        return {**result, "already_closed": False, "reconciled": mutation_error}
