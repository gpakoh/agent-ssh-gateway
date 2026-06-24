"""Agent Handoff v2 — .ai-bridge task management for parallel agent execution."""

from __future__ import annotations

import re
from typing import Any

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{10,120}$")

TASKS_REL_DIR = ".ai-bridge/tasks"
ARCHIVE_REL_DIR = ".ai-bridge/archive"


def validate_task_id(task_id: str) -> None:
    """Raise ValueError if task_id is malformed."""
    if not TASK_ID_RE.match(task_id):
        raise ValueError(
            f"Invalid task_id: {task_id!r}. Must match {TASK_ID_RE.pattern}"
        )


def _task_dir(task_id: str) -> str:
    return f"{TASKS_REL_DIR}/{task_id}"


def _archive_dir(task_id: str) -> str:
    return f"{ARCHIVE_REL_DIR}/{task_id}"


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"
