"""Keep the repo-owned active TODO counters aligned with open checklist entries."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TODO = Path(__file__).resolve().parents[1] / "TODO.md"
_OPEN_TASK = re.compile(r"^- \[ \] \*\*(P[123]-[0-9]+):", re.MULTILINE)
_CLOSED_TASK = re.compile(r"^- \[[xX]\] \*\*P[123]-", re.MULTILINE)
_TOTAL = re.compile(
    r"\*\*Open: ([0-9]+) \(P1 ([0-9]+) / P2 ([0-9]+) / P3 ([0-9]+)\)\.\*\*"
)


def test_active_todo_counts_and_unique_ids_match_headings() -> None:
    content = TODO.read_text(encoding="utf-8")
    open_ids = _OPEN_TASK.findall(content)
    assert open_ids, "TODO has no recognized open task identifiers"
    assert len(open_ids) == len(set(open_ids)), "TODO contains duplicate open task IDs"
    assert not _CLOSED_TASK.search(content), (
        "Completed items belong in the closeout evidence, not the active checklist"
    )

    match = _TOTAL.search(content)
    assert match is not None, "TODO is missing the authoritative open-count summary"
    total, p1, p2, p3 = map(int, match.groups())
    counts = {priority: sum(task.startswith(priority) for task in open_ids)
              for priority in ("P1", "P2", "P3")}
    assert total == len(open_ids)
    assert (p1, p2, p3) == (counts["P1"], counts["P2"], counts["P3"])
    for priority in ("P1", "P2", "P3"):
        heading = re.search(
            rf"^## {priority} — .* \(([0-9]+)\)$", content, re.MULTILINE
        )
        assert heading is not None, f"missing {priority} heading"
        assert int(heading.group(1)) == counts[priority]


@pytest.mark.parametrize("priority", ["P1", "P2", "P3"])
def test_each_todo_priority_has_open_entries(priority: str) -> None:
    ids = _OPEN_TASK.findall(TODO.read_text(encoding="utf-8"))
    assert any(task.startswith(priority) for task in ids)
