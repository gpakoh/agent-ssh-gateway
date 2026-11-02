"""Keep the active TODO count invariant testable without application dependencies.

This module intentionally uses only the Python standard library: both the
lightweight CI scope gate and the full pytest suite execute these same checks.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TODO = Path(__file__).resolve().parents[1] / "TODO.md"
_OPEN_TASK = re.compile(r"^- \[ \] \*\*(P[123]-[0-9]+):", re.MULTILINE)
_CLOSED_TASK = re.compile(r"^- \[[xX]\] \*\*P[123]-", re.MULTILINE)
_TOTAL = re.compile(
    r"\*\*Open: ([0-9]+) \(P1 ([0-9]+) / P2 ([0-9]+) / P3 ([0-9]+)\)\.\*\*"
)


def validate_todo_counts(content: str) -> None:
    """Validate the repo-owned local backlog, excluding linked Gitea Issues."""
    open_ids = _OPEN_TASK.findall(content)
    assert open_ids, "TODO has no recognized open task identifiers"
    assert len(open_ids) == len(set(open_ids)), "TODO contains duplicate open task IDs"
    assert not _CLOSED_TASK.search(content), (
        "Completed items belong in the closeout evidence, not the active checklist"
    )

    match = _TOTAL.search(content)
    assert match is not None, "TODO is missing the authoritative open-count summary"
    total, p1, p2, p3 = map(int, match.groups())
    counts = {
        priority: sum(task.startswith(priority) for task in open_ids)
        for priority in ("P1", "P2", "P3")
    }
    assert total == len(open_ids)
    assert (p1, p2, p3) == (counts["P1"], counts["P2"], counts["P3"])
    for priority in ("P1", "P2", "P3"):
        heading = re.search(
            rf"^## {priority} — .* \(([0-9]+)\)$", content, re.MULTILINE
        )
        assert heading is not None, f"missing {priority} heading"
        assert int(heading.group(1)) == counts[priority]


class TestTodoCountConsistency(unittest.TestCase):
    def test_active_todo_counts_and_unique_ids_match_headings(self) -> None:
        validate_todo_counts(TODO.read_text(encoding="utf-8"))

    def test_each_todo_priority_has_open_entries(self) -> None:
        open_ids = _OPEN_TASK.findall(TODO.read_text(encoding="utf-8"))
        for priority in ("P1", "P2", "P3"):
            with self.subTest(priority=priority):
                self.assertTrue(any(task.startswith(priority) for task in open_ids))

    def test_docs_only_header_regression_must_fail(self) -> None:
        original = TODO.read_text(encoding="utf-8")
        match = _TOTAL.search(original)
        self.assertIsNotNone(match)
        assert match is not None
        invalid = original[: match.start()] + (
            "**Local engineering TODO: 30 open (P1 8 / P2 13 / P3 9).**"
        ) + original[match.end() :]
        with self.assertRaisesRegex(AssertionError, "authoritative open-count"):
            validate_todo_counts(invalid)

    def test_docs_only_wrong_total_must_fail(self) -> None:
        original = TODO.read_text(encoding="utf-8")
        match = _TOTAL.search(original)
        self.assertIsNotNone(match)
        assert match is not None
        count, p1, p2, p3 = map(int, match.groups())
        invalid = original[: match.start()] + (
            f"**Open: {count + 1} (P1 {p1} / P2 {p2} / P3 {p3}).**"
        ) + original[match.end() :]
        with self.assertRaises(AssertionError):
            validate_todo_counts(invalid)


if __name__ == "__main__":
    unittest.main()
