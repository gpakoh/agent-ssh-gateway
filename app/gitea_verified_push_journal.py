"""Caller-bound, opt-in receipt for verified Git push.

The journal records observations, never authorizes a new mutation. The
same key is single-use, even after cancellation or a process restart.
Only the trusted server's existing private audit volume is used.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.gitea_workflow_dispatch import (
    HASH,
    KEY,
    SHA,
    DispatchRefused,
    digest,
    operation_lock,
    save_receipt,
)

_MAX_BYTES = 8192
_AUDIT_ID = re.compile(r"[0-9a-f]{32}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_OUTCOMES = {"completed", "completed_after_ambiguous_response", "already_published"}
_FIELDS = {
    "version", "actor", "contract_sha256", "expected_head_sha", "phase",
    "audit_intent_id", "terminal",
}
_TERMINAL_FIELDS = {
    "ok", "error_code", "outcome", "remote_observed_sha",
    "mutation_started", "mutation_occurred", "audit_outcome_persisted",
}


class PushJournalError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _unavailable() -> PushJournalError:
    return PushJournalError("RECEIPT_UNAVAILABLE", "Verified push journal is unavailable")


def _private_directory(root: Path) -> None:
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise _unavailable()


def _valid_optional_bool(value: Any) -> bool:
    return value is None or type(value) is bool


def _read(root: Path, key: str) -> dict[str, Any] | None:
    """Atomically replaced private records; absent is only a local observation."""
    try:
        _private_directory(root)
        fd = os.open(
            root / (digest(key) + ".json"),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unavailable() from exc
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > _MAX_BYTES
            ):
                raise _unavailable()
            raw = stream.read(_MAX_BYTES + 1)
        record = json.loads(raw)
        if (
            len(raw) > _MAX_BYTES
            or not isinstance(record, dict)
            or set(record) != _FIELDS
            or type(record["version"]) is not int
            or record["version"] != 1
            or not all(
                isinstance(record[k], str) and HASH.fullmatch(record[k])
                for k in ("actor", "contract_sha256")
            )
            or not isinstance(record["expected_head_sha"], str)
            or not SHA.fullmatch(record["expected_head_sha"])
            or record["phase"] not in {"started", "dispatching", "terminal"}
            or (
                record["audit_intent_id"] is not None
                and (
                    not isinstance(record["audit_intent_id"], str)
                    or not _AUDIT_ID.fullmatch(record["audit_intent_id"])
                )
            )
            or (record["phase"] == "dispatching" and record["audit_intent_id"] is None)
            or (record["phase"] == "started" and record["audit_intent_id"] is not None)
        ):
            raise _unavailable()
        terminal = record["terminal"]
        if record["phase"] == "terminal":
            if (
                not isinstance(terminal, dict)
                or set(terminal) != _TERMINAL_FIELDS
                or type(terminal["ok"]) is not bool
                or (
                    terminal["error_code"] is not None
                    and (
                        not isinstance(terminal["error_code"], str)
                        or not _CODE.fullmatch(terminal["error_code"])
                    )
                )
                or terminal["outcome"] not in _OUTCOMES | {None}
                or terminal["remote_observed_sha"] not in {None, record["expected_head_sha"]}
                or not all(
                    _valid_optional_bool(terminal[k])
                    for k in ("mutation_started", "mutation_occurred", "audit_outcome_persisted")
                )
            ):
                raise _unavailable()
        elif terminal is not None:
            raise _unavailable()
        return record
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise _unavailable() from exc


def _binding(record: dict[str, Any], actor: str, contract_sha: str) -> None:
    if record["actor"] != actor:
        raise PushJournalError("AUTH_ERROR", "Verified push operation belongs to another caller")
    if record["contract_sha256"] != contract_sha:
        raise PushJournalError("OPERATION_KEY_CONFLICT", "Operation key is bound to another request")


def _view(record: dict[str, Any] | None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "authorizes_mutation": False,
        "replay_allowed": False,
        "current_remote_state_verified": False,
    }
    if record is None:
        return {**view, "state": "not_found"}
    view.update(
        state=record["phase"],
        contract_sha256=record["contract_sha256"],
        expected_head_sha=record["expected_head_sha"],
        audit_intent_id=record["audit_intent_id"],
        terminal_observation=record["phase"] == "terminal",
    )
    if record["terminal"] is not None:
        view.update(record["terminal"])
    return view


def _terminal(result: dict[str, Any], head: str) -> dict[str, Any]:
    """Persist fixed, typed observations only; never raw error or check output."""
    ok = result.get("ok") is True
    error = result.get("error") or {}
    details = (result.get("result") if ok else error.get("details")) or {}
    outcome = details.get("outcome")
    outcome = outcome if isinstance(outcome, str) and outcome in _OUTCOMES else None
    error_code = error.get("code")
    error_code = error_code if isinstance(error_code, str) and _CODE.fullmatch(error_code) else None
    started = details.get("mutation_started")
    if ok and outcome in {"completed", "completed_after_ambiguous_response"}:
        started = True
    return {
        "ok": ok,
        "error_code": error_code,
        "outcome": outcome,
        "remote_observed_sha": head if details.get("remote_observed_sha") == head else None,
        "mutation_started": started if type(started) is bool else None,
        "mutation_occurred": details.get("mutation_occurred")
        if type(details.get("mutation_occurred")) is bool else None,
        "audit_outcome_persisted": details.get("audit_outcome_persisted")
        if type(details.get("audit_outcome_persisted")) is bool else None,
    }


def _annotate(result: dict[str, Any], persisted: bool) -> dict[str, Any]:
    if result.get("ok") is True:
        result["result"]["operation_receipt_persisted"] = persisted
    else:
        error = result["error"]
        if not isinstance(error.get("details"), dict):
            error["details"] = {}
        error["details"]["operation_receipt_persisted"] = persisted
    return result


async def verified_push_with_receipt(
    *,
    root: Path,
    operation_key: str,
    actor: str,
    contract: list[Any],
    expected_head_sha: str,
    receipt_only: bool,
    invoke: Callable[[Callable[[str], None]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    if not isinstance(operation_key, str) or not KEY.fullmatch(operation_key):
        raise PushJournalError("INVALID_INPUT", "Expected a bounded operation key")
    if not isinstance(actor, str) or not HASH.fullmatch(actor):
        raise PushJournalError("AUTH_ERROR", "Authenticated caller is required")
    if not isinstance(expected_head_sha, str) or not SHA.fullmatch(expected_head_sha):
        raise PushJournalError("INVALID_INPUT", "Expected an exact lowercase head SHA")
    contract_sha = digest(contract)
    if receipt_only:
        old = _read(root, operation_key)
        if old is not None:
            _binding(old, actor, contract_sha)
        return _view(old)

    try:
        with operation_lock(root, operation_key) as path:
            old = _read(root, operation_key)
            if old is not None:
                _binding(old, actor, contract_sha)
                raise PushJournalError(
                    "OPERATION_KEY_ALREADY_USED",
                    "Operation key has already been used; read its receipt, do not replay",
                    {"operation_receipt": _view(old)},
                )
            record: dict[str, Any] = {
                "version": 1,
                "actor": actor,
                "contract_sha256": contract_sha,
                "expected_head_sha": expected_head_sha,
                "phase": "started",
                "audit_intent_id": None,
                "terminal": None,
            }
            # The journal directory itself must survive a crash before the
            # initial record can be relied on as a replay fence.
            parent = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            save_receipt(path, record)

            def before_dispatch(audit_id: str) -> None:
                if not _AUDIT_ID.fullmatch(audit_id):
                    raise _unavailable()
                next_record = {**record, "phase": "dispatching", "audit_intent_id": audit_id}
                save_receipt(path, next_record)
                record.update(next_record)

            # CancelledError is deliberately not converted to a terminal
            # result. The last durable phase is left for read-only recovery.
            result = await invoke(before_dispatch)
            details = (
                result.get("result") if result.get("ok") is True
                else (result.get("error") or {}).get("details")
            ) or {}
            audit_id = details.get("audit_intent_id")
            # Even a failure to persist the pre-dispatch phase may have
            # a durable audit intent. Retain its exact ID if available.
            terminal = {
                **record,
                "phase": "terminal",
                "audit_intent_id": record["audit_intent_id"] or (
                    audit_id if isinstance(audit_id, str) and _AUDIT_ID.fullmatch(audit_id) else None
                ),
                "terminal": _terminal(result, expected_head_sha),
            }
            try:
                save_receipt(path, terminal)
            except Exception:
                # Persistence failure cannot reverse or reclassify the
                # already observed remote effect.
                return _annotate(result, False)
            return _annotate(result, True)
    except PushJournalError:
        raise
    except DispatchRefused as exc:
        if exc.code == "DISPATCH_BUSY":
            raise PushJournalError("OPERATION_BUSY", "Operation key is active; read its receipt") from exc
        raise _unavailable() from exc
    except OSError as exc:
        raise _unavailable() from exc
