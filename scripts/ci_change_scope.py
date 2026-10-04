"""Classify exact CI changes without installing application dependencies.

Only allowlisted documentation can skip heavy jobs. Unknown event/history
runs the full pipeline; a checkout/event identity mismatch fails the job.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT_DOCS = frozenset(
    {
        "README.md",
        "TODO.md",
        "CHANGELOG.md",
        "SECURITY.md",
        "SSH_GATEWAY_GUIDE.md",
        "HEALTH_PATH_AUDIT.md",
        "deploy.example.md",
    }
)
DOC_SUFFIXES = frozenset(
    {".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".pdf"}
)
SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Scope:
    full_ci: bool
    reason: str
    head: str
    base: str = ""


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


def documentation(path: str) -> bool:
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if parts[-1].lower() == "agents.md":
        return False
    return path in ROOT_DOCS or (
        parts[0] == "docs" and len(parts) > 1 and Path(path).suffix.lower() in DOC_SUFFIXES
    )


def classify(root: Path, event_name: str, event: dict) -> Scope:
    head = git(root, "rev-parse", "HEAD").decode().strip()
    if event_name == "pull_request":
        pr = event.get("pull_request")
        if not isinstance(pr, dict):
            return Scope(True, "missing pull request metadata", head)
        base_event, head_event = pr.get("base"), pr.get("head")
        if not isinstance(base_event, dict) or not isinstance(head_event, dict):
            return Scope(True, "missing pull request revisions", head)
        before, after = base_event.get("sha"), head_event.get("sha")
    elif event_name == "push":
        before, after = event.get("before"), event.get("after")
    else:
        return Scope(True, "unsupported event", head)
    if not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in (before, after)):
        return Scope(True, "missing exact event revisions", head)
    if after != head:
        raise ValueError("checkout HEAD does not match the event head")
    if before == "0" * 40:
        return Scope(True, "new branch has no comparison base", head)
    try:
        base = (
            git(root, "merge-base", before, head).decode().strip()
            if event_name == "pull_request"
            else before
        )
        # Disable rename detection: a code -> docs rename includes the deleted
        # code path and cannot escape the full pipeline. Raw NUL records also
        # preserve filenames containing whitespace/newlines and file modes.
        raw = git(root, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, head, "--")
    except (subprocess.SubprocessError, OSError):
        return Scope(True, "comparison history unavailable", head)
    records = raw.split(b"\0")
    if records[-1] != b"" or (len(records) - 1) % 2:
        return Scope(True, "unrecognized diff records", head, base)
    if len(records) == 1:
        return Scope(True, "empty diff", head, base)
    try:
        for index in range(0, len(records) - 1, 2):
            metadata = records[index].decode("ascii").split()
            path = records[index + 1].decode("utf-8")
            if (
                len(metadata) != 5
                or metadata[0] not in {":100644", ":000000"}
                or metadata[1] not in {"100644", "000000"}
                or metadata[4] not in {"A", "M", "D"}
                or not documentation(path)
            ):
                return Scope(True, "non-documentation path or file mode", head, base)
    except UnicodeError:
        return Scope(True, "unrecognized filename encoding", head, base)
    return Scope(False, "all changed paths are allowlisted documentation", head, base)


def main() -> None:
    root = Path.cwd()
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError):
        event = {}
    if not isinstance(event, dict):
        event = {}
    scope = classify(root, os.environ.get("GITHUB_EVENT_NAME", ""), event)
    if not scope.full_ci:
        # A lightweight check on the actual diff, rather than unconditional
        # success. Whitespace errors fail the docs job and the workflow.
        git(root, "diff", "--check", scope.base, scope.head, "--")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"full_ci={str(scope.full_ci).lower()}\n")
    print(json.dumps(scope.__dict__, sort_keys=True))


if __name__ == "__main__":
    main()
