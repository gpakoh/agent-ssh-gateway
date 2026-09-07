"""Structured TODO/backlog writer for operator findings.

The tool is intentionally narrow: it edits only TODO.md inside a registered
workspace/candidate and returns host-path-free evidence. Remote publication is
left to the existing verified-delivery path, which can then enforce TODO-only
scope with exact base/head SHAs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.workspace.edit import project_file_write
from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry, get_registry

TODO_PATH = "TODO.md"
DEFAULT_SECTION_TITLE = "Runtime/tooling intake"
TODO_MAX_BYTES = 250_000
_ALLOWED_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
_ITEM_RE = re.compile(r"(?m)^(?P<number>\d+)\. ⬜ \*\*(?P<title>.+?)\*\*")
_HEADING_RE = re.compile(r"(?m)^## .+$")
_KEY_RE = re.compile(r"<!--\s*gateway-todo-key:\s*(?P<key>[a-z0-9-]+)\s*-->")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class TodoBacklogError(ValueError):
    """Raised when a TODO entry cannot be created or updated safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_INPUT",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class TodoItemMatch:
    start: int
    end: int
    number: int
    title: str
    key: str | None


def _clean_required(name: str, value: str, *, max_len: int = 4_000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if not text:
        raise TodoBacklogError(f"{name} is required")
    if len(text.encode("utf-8")) > max_len:
        raise TodoBacklogError(f"{name} exceeds {max_len} UTF-8 bytes")
    return text


def _clean_optional(value: str, *, max_len: int = 4_000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text.encode("utf-8")) > max_len:
        raise TodoBacklogError(f"optional field exceeds {max_len} UTF-8 bytes")
    return text


def _entry_date(value: str | None) -> str:
    if value is None or not str(value).strip():
        return date.today().isoformat()
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise TodoBacklogError("entry_date must use YYYY-MM-DD format")
    return text


def normalize_failure_key(value: str) -> str:
    """Return a stable key for title/failure-mode dedupe."""
    cleaned = _SLUG_RE.sub("-", str(value).strip().lower()).strip("-")
    if not cleaned:
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
        return f"finding-{digest}"
    return cleaned[:120].strip("-") or "finding"


def _heading(section_title: str, when: str) -> str:
    title = _clean_required("section_title", section_title, max_len=120)
    if "\n" in title or title.startswith("#"):
        raise TodoBacklogError("section_title must be one plain heading fragment")
    return f"## {title} — {when}"


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _format_field(label: str, value: str) -> list[str]:
    lines = _split_lines(value)
    if not lines:
        return [f"   **{label}:** n/a"]
    if len(lines) == 1:
        return [f"   **{label}:** {lines[0]}"]
    return [f"   **{label}:**", *[f"   - {line}" for line in lines]]


def _format_entry(
    *,
    number: int,
    title: str,
    key: str,
    severity: str,
    observed_behavior: str,
    reproduction_steps: str,
    expected_behavior: str,
    impact: str,
    acceptance_criteria: str,
    related_evidence: str,
) -> str:
    rendered_title = title if title.endswith((".", "!", "?")) else f"{title}."
    lines = [f"{number}. ⬜ **{rendered_title}**", f"   <!-- gateway-todo-key: {key} -->"]
    lines.extend(_format_field("Severity", severity))
    lines.append("")
    lines.extend(_format_field("Observed behavior", observed_behavior))
    lines.append("")
    lines.extend(_format_field("Reproduction", reproduction_steps))
    lines.append("")
    lines.extend(_format_field("Expected behavior", expected_behavior))
    lines.append("")
    lines.extend(_format_field("Impact", impact))
    lines.append("")
    lines.extend(_format_field("Acceptance", acceptance_criteria))
    lines.append("")
    lines.extend(_format_field("Related evidence", related_evidence))
    return "\n".join(lines).rstrip() + "\n"


def _format_update(
    *,
    when: str,
    observed_behavior: str,
    reproduction_steps: str,
    expected_behavior: str,
    impact: str,
    acceptance_criteria: str,
    related_evidence: str,
) -> str:
    lines = [f"   **Update {when}:**"]
    lines.extend(_format_field("Observed behavior", observed_behavior))
    lines.extend(_format_field("Reproduction", reproduction_steps))
    lines.extend(_format_field("Expected behavior", expected_behavior))
    lines.extend(_format_field("Impact", impact))
    lines.extend(_format_field("Acceptance", acceptance_criteria))
    if related_evidence:
        lines.extend(_format_field("Related evidence", related_evidence))
    return "\n".join(lines).rstrip() + "\n"


def _next_boundary(content: str, start: int) -> int:
    candidates: list[int] = []
    next_item = _ITEM_RE.search(content, start + 1)
    if next_item:
        candidates.append(next_item.start())
    next_heading = _HEADING_RE.search(content, start + 1)
    if next_heading:
        candidates.append(next_heading.start())
    return min(candidates) if candidates else len(content)


def _iter_items(content: str) -> list[TodoItemMatch]:
    matches: list[TodoItemMatch] = []
    for match in _ITEM_RE.finditer(content):
        body_end = _next_boundary(content, match.start())
        body = content[match.start():body_end]
        key_match = _KEY_RE.search(body)
        key = key_match.group("key") if key_match else None
        matches.append(
            TodoItemMatch(
                start=match.start(),
                end=body_end,
                number=int(match.group("number")),
                title=match.group("title").strip().rstrip("."),
                key=key,
            )
        )
    return matches


def _find_existing(content: str, key: str) -> TodoItemMatch | None:
    for item in _iter_items(content):
        if item.key == key:
            return item
        if normalize_failure_key(item.title) == key:
            return item
    return None


def _ensure_section(content: str, heading: str) -> tuple[str, int, int]:
    target = re.compile(rf"(?m)^{re.escape(heading)}$")
    match = target.search(content)
    if not match:
        prefix = content.rstrip()
        if prefix:
            prefix += "\n\n"
        prefix += f"{heading}\n\n"
        return prefix, len(prefix), len(prefix)

    section_body_start = match.end()
    next_heading = _HEADING_RE.search(content, section_body_start + 1)
    section_end = next_heading.start() if next_heading else len(content)
    return content, section_body_start, section_end


def _next_item_number(section_body: str) -> int:
    numbers = [int(match.group("number")) for match in _ITEM_RE.finditer(section_body)]
    return max(numbers, default=0) + 1


def _read_todo(project_id: str, registry: WorkspaceRegistry) -> str:
    todo_path = registry._policy.validate_read(project_id, TODO_PATH)
    if not todo_path.exists():
        return "# Agent SSH Gateway — TODO\n"
    if not todo_path.is_file():
        raise TodoBacklogError("TODO.md is not a regular file")
    size = todo_path.stat().st_size
    if size > TODO_MAX_BYTES:
        raise TodoBacklogError(f"TODO.md exceeds {TODO_MAX_BYTES} bytes")
    try:
        return todo_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise TodoBacklogError("TODO.md must be UTF-8 text") from exc
    except OSError as exc:
        raise WorkspacePolicyError("Could not read TODO.md") from exc


def upsert_todo_backlog_entry(
    *,
    project_id: str,
    title: str,
    severity: str,
    observed_behavior: str,
    reproduction_steps: str,
    expected_behavior: str,
    impact: str,
    acceptance_criteria: str,
    related_evidence: str = "",
    section_title: str = DEFAULT_SECTION_TITLE,
    failure_mode: str = "",
    entry_date: str | None = None,
    update_existing: bool = False,
    registry: WorkspaceRegistry | None = None,
    safe: bool = False,
) -> dict[str, Any]:
    """Create or update one structured TODO entry in TODO.md.

    Duplicate detection is based on ``failure_mode`` when supplied, otherwise
    on the normalized title. By default a duplicate fails with ALREADY_EXISTS;
    callers must set ``update_existing=True`` to append dated evidence to the
    existing item instead of creating a second checkbox.
    """
    title = " ".join(_clean_required("title", title, max_len=180).split())
    severity = _clean_required("severity", severity, max_len=2).upper()
    if severity not in _ALLOWED_SEVERITIES:
        raise TodoBacklogError("severity must be one of P0, P1, P2, P3")
    observed_behavior = _clean_required("observed_behavior", observed_behavior)
    reproduction_steps = _clean_required("reproduction_steps", reproduction_steps)
    expected_behavior = _clean_required("expected_behavior", expected_behavior)
    impact = _clean_required("impact", impact)
    acceptance_criteria = _clean_required("acceptance_criteria", acceptance_criteria)
    related_evidence = _clean_optional(related_evidence)
    when = _entry_date(entry_date)
    key_material = failure_mode or title
    key = normalize_failure_key(key_material)
    registry = registry or get_registry()
    original = _read_todo(project_id, registry)
    existing = _find_existing(original, key)

    if existing is not None and not update_existing:
        raise TodoBacklogError(
            "matching TODO entry already exists; set update_existing=true to append evidence",
            code="ALREADY_EXISTS",
            details={
                "matched_title": existing.title,
                "failure_key": key,
                "action": "duplicate_refused",
            },
        )

    if existing is not None:
        update = _format_update(
            when=when,
            observed_behavior=observed_behavior,
            reproduction_steps=reproduction_steps,
            expected_behavior=expected_behavior,
            impact=impact,
            acceptance_criteria=acceptance_criteria,
            related_evidence=related_evidence,
        )
        new_content = original[: existing.end].rstrip() + "\n\n" + update + original[existing.end:]
        action = "updated"
        matched_title = existing.title
        item_number = existing.number
    else:
        heading = _heading(section_title, when)
        content, section_start, section_end = _ensure_section(original, heading)
        section_body = content[section_start:section_end]
        item_number = _next_item_number(section_body)
        entry = _format_entry(
            number=item_number,
            title=title,
            key=key,
            severity=severity,
            observed_behavior=observed_behavior,
            reproduction_steps=reproduction_steps,
            expected_behavior=expected_behavior,
            impact=impact,
            acceptance_criteria=acceptance_criteria,
            related_evidence=related_evidence,
        )
        new_content = content[:section_end].rstrip() + "\n\n" + entry + content[section_end:]
        action = "created"
        matched_title = None

    if new_content == original:
        raise TodoBacklogError("TODO.md would not change", code="ALREADY_EXISTS")
    if len(new_content.encode("utf-8")) > TODO_MAX_BYTES:
        raise TodoBacklogError(f"TODO.md after update exceeds {TODO_MAX_BYTES} bytes")

    write_result = project_file_write(
        project_id=project_id,
        relative_path=TODO_PATH,
        content=new_content,
        max_bytes=TODO_MAX_BYTES,
        registry=registry,
        safe=safe,
    )
    result: dict[str, Any] = {
        "project_id": project_id,
        "path": TODO_PATH,
        "action": action,
        "failure_key": key,
        "title": title,
        "matched_title": matched_title,
        "item_number": item_number,
        "entry_date": when,
        "todo_only": True,
        "post_write": write_result.get("post_write"),
    }
    if safe and "receipt" in write_result:
        result["receipt"] = write_result["receipt"]
    return result


__all__ = [
    "DEFAULT_SECTION_TITLE",
    "TODO_PATH",
    "TodoBacklogError",
    "normalize_failure_key",
    "upsert_todo_backlog_entry",
]
