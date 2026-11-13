"""Keep the Gateway backlog on Gitea Issues instead of a duplicate local TODO.

This contract is intentionally hermetic and dependency-free: it runs in the
lightweight CI Change scope job, including for documentation-only updates.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROADMAP = ROOT / "docs" / "roadmap.md"
LEGACY_TODO = ROOT / "TODO.md"
ISSUES_URL = "https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues"


class TestBoardSingleSource(unittest.TestCase):
    def test_no_repo_owned_todo_backlog(self) -> None:
        self.assertFalse(
            LEGACY_TODO.exists(),
            "TODO.md duplicates the canonical Gitea Issues backlog; use coord_board",
        )

    def test_roadmap_points_to_canonical_gitea_issues(self) -> None:
        content = ROADMAP.read_text(encoding="utf-8")
        self.assertIn(ISSUES_URL, content)
        self.assertIn('coord_board(project="agent-ssh-gateway")', content)
        self.assertIn("Issue state is", content)
        self.assertNotIn("](../TODO.md)", content)


if __name__ == "__main__":
    unittest.main()
