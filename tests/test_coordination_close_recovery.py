"""Close receipt regression tests with a stateful, network-free Gitea fake."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.coordination_close import CloseRefused, close_thread
from app.coordination_logic import CoordinationOutcomeUnknown, open_thread

OWNER, REPO, NUMBER = "owner", "repo", 5
KEY = "close-recovery-001"
EVENT_ID = 51
ACTOR = "0123456789abcdef"
ISSUE = {
    "id": 4, "number": NUMBER, "title": "task",
    "body": "<!-- py-coordination/v1 issue key=original-task source=repo state=open -->\nTask",
    "state": "open", "updated_at": "2026-11-27T00:00:00Z", "comments": 1,
    "html_url": "https://example.invalid/owner/repo/issues/5",
}
EVENT = {
    "id": EVENT_ID, "type": "comment", "user": {"login": "bot"},
    "body": "<!-- py-coordination/v1 event key=resolution-001 state=resolved -->\nVerified result",
}


class Client:
    def __init__(self, journal: Path, *, faults: dict[int, str] | None = None,
                 mode: str = "ok") -> None:
        self.issue = deepcopy(ISSUE)
        self.events = [deepcopy(EVENT)]
        self.journal = journal
        self.faults = dict(faults or {})
        self.mode = mode
        self.timeline_calls = 0
        self.patches = 0
        self.audit_events: list[str] = []

    async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        assert (owner, repo, number) == (OWNER, REPO, NUMBER)
        return deepcopy(self.issue)

    async def list_issue_timeline(self, owner: str, repo: str, number: int,
                                  *, limit: int, page: int) -> Any:
        assert (owner, repo, number, limit, page) == (OWNER, REPO, NUMBER, 50, 1)
        self.timeline_calls += 1
        fault = self.faults.get(self.timeline_calls)
        if fault == "null":
            return None
        if fault == "drift":
            self.issue["updated_at"] = "changed"
            return None
        if fault == "empty":
            return []
        return deepcopy(self.events)

    async def get_user(self) -> dict[str, str]:
        return {"login": "bot"}

    def audit(self, phase: str, reason: str, metadata: dict[str, Any]) -> None:
        self.audit_events.append(phase)

    def receipts(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text()) for p in self.journal.glob("*/*.json")]

    async def close_coord_issue(self, owner: str, repo: str, number: int) -> None:
        assert (owner, repo, number) == (OWNER, REPO, NUMBER)
        self.patches += 1
        assert [r["phase"] for r in self.receipts()] == ["dispatched"]
        assert self.audit_events == ["intent"]
        if self.mode == "denied":
            raise PermissionError("denied")
        if self.mode == "no-effect":
            raise TimeoutError("lost without effect")
        self.issue.update(state="closed", updated_at="2026-11-27T00:00:01Z")
        if self.mode == "edit":
            self.events[0]["body"] += " changed after close"
        if self.mode == "lost":
            raise TimeoutError("lost after effect")


def lease() -> str:
    client = Client(Path("unused"))
    return asyncio.run(open_thread(client, OWNER, REPO, NUMBER))["close_state_sha256"]


def submit(client: Client, *, key: str = KEY, expected: str | None = None) -> dict[str, Any]:
    return asyncio.run(close_thread(
        client, OWNER, REPO, NUMBER, resolution_event_id=EVENT_ID,
        expected_state_sha256=expected or lease(), request_key=key,
        reason="verified result", actor=ACTOR, journal_dir=client.journal, audit=client.audit,
    ))


@pytest.mark.parametrize("call", [1, 2, 3])
def test_transient_null_at_each_close_scan(tmp_path: Path, call: int) -> None:
    client = Client(tmp_path / "journal", faults={call: "null"})
    result = submit(client)
    assert result["verified"] and client.patches == 1
    assert client.receipts()[0]["phase"] == "confirmed"
    assert client.timeline_calls == 4
    assert client.audit_events == ["intent", "success"]


def test_lost_patch_then_transient_null_reconciles(tmp_path: Path) -> None:
    client = Client(tmp_path / "journal", faults={3: "null"}, mode="lost")
    result = submit(client)
    assert result["reconciled"] and client.patches == 1
    assert client.receipts()[0]["phase"] == "confirmed"
    assert submit(client)["already_closed"] and client.patches == 1


@pytest.mark.parametrize("faults", [{1: "null", 2: "null"}, {2: "null", 3: "null"},
                                     {1: "drift"}, {2: "null", 3: "empty"}])
def test_invalid_preclose_read_blocks_patch(tmp_path: Path, faults: dict[int, str]) -> None:
    client = Client(tmp_path / "journal", faults=faults)
    with pytest.raises(CoordinationOutcomeUnknown):
        submit(client)
    assert client.patches == 0
    assert client.receipts() == []


def test_postclose_persistent_null_reconciles_later_without_patch(tmp_path: Path) -> None:
    client = Client(tmp_path / "journal", faults={3: "null", 4: "null"})
    with pytest.raises(CoordinationOutcomeUnknown):
        submit(client)
    assert client.patches == 1 and client.receipts()[0]["phase"] == "dispatched"
    client.faults.clear()
    assert submit(client)["reconciled"]
    assert client.patches == 1 and client.receipts()[0]["phase"] == "confirmed"


def test_timeout_without_effect_never_resends_even_new_key(tmp_path: Path) -> None:
    client = Client(tmp_path / "journal", mode="no-effect")
    with pytest.raises(CoordinationOutcomeUnknown):
        submit(client)
    for key in (KEY, "close-recovery-002"):
        with pytest.raises(CoordinationOutcomeUnknown):
            submit(client, key=key)
    assert client.patches == 1 and client.receipts()[0]["phase"] == "dispatched"


def test_edited_report_after_close_is_not_confirmed(tmp_path: Path) -> None:
    client = Client(tmp_path / "journal", mode="edit")
    with pytest.raises(CoordinationOutcomeUnknown):
        submit(client)
    with pytest.raises(CoordinationOutcomeUnknown):
        submit(client)
    assert client.patches == 1 and client.receipts()[0]["phase"] == "dispatched"


def test_permission_denial_is_durable(tmp_path: Path) -> None:
    client = Client(tmp_path / "journal", mode="denied")
    with pytest.raises(PermissionError):
        submit(client)
    assert client.receipts()[0]["phase"] == "rejected"
    with pytest.raises(CoordinationOutcomeUnknown):
        submit(client)
    assert client.patches == 1


def test_stale_lease_never_dispatches(tmp_path: Path) -> None:
    client = Client(tmp_path / "journal")
    with pytest.raises(CloseRefused, match="changed"):
        submit(client, expected="0" * 64)
    assert client.patches == 0 and client.receipts() == []


@pytest.mark.parametrize("effect_committed", [False, True])
def test_cancel_during_patch_keeps_dispatch_fence(
    tmp_path: Path, effect_committed: bool
) -> None:
    """Real asyncio task cancellation at the mutation boundary, not a fake timeout."""

    async def scenario() -> Client:
        entered = asyncio.Event()

        class CancelClient(Client):
            async def close_coord_issue(self, owner: str, repo: str, number: int) -> None:
                assert (owner, repo, number) == (OWNER, REPO, NUMBER)
                self.patches += 1
                assert [r["phase"] for r in self.receipts()] == ["dispatched"]
                if effect_committed:
                    self.issue.update(state="closed", updated_at="2026-11-27T00:00:01Z")
                entered.set()
                await asyncio.Event().wait()

        client = CancelClient(tmp_path / "journal", faults={3: "null"})
        expected = (await open_thread(Client(Path("unused")), OWNER, REPO, NUMBER))[
            "close_state_sha256"
        ]
        task = asyncio.create_task(close_thread(
            client, OWNER, REPO, NUMBER, resolution_event_id=EVENT_ID,
            expected_state_sha256=expected, request_key=KEY,
            reason="verified result", actor=ACTOR, journal_dir=client.journal,
            audit=client.audit,
        ))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert client.patches == 1
        assert client.receipts()[0]["phase"] == "dispatched"
        assert client.audit_events == ["intent"]
        return client

    client = asyncio.run(scenario())
    # A separate event loop reacquires the real file lock and reads the receipt.
    if effect_committed:
        result = submit(client)
        assert result["reconciled"] and result["already_closed"]
        assert client.receipts()[0]["phase"] == "confirmed"
    else:
        for key in (KEY, "close-recovery-002"):
            with pytest.raises(CoordinationOutcomeUnknown):
                submit(client, key=key)
        assert client.receipts()[0]["phase"] == "dispatched"
    assert client.patches == 1
    assert client.audit_events == ["intent"]
