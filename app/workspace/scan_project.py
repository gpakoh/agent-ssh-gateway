"""Scan a project directory for destructive command patterns."""

from __future__ import annotations

import time
from pathlib import Path

from app.command_policy import scan_command
from app.workspace.registry import get_registry

EXCLUDE_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox",
    ".eggs", "eggs", "dist", "build", ".egg-info",
    ".hg", ".svn", ".bzr", ".terraform", ".serverless",
    ".next", ".nuxt", "target", "vendor",
})

MAX_FILES = 100
MAX_FILE_BYTES = 200 * 1024
TIMEOUT_S = 30


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:512]


def scan_project(
    project_id: str,
    *,
    pattern: str = "*",
    max_files: int = MAX_FILES,
    _root_override: Path | None = None,
) -> dict:
    """Scan a project for destructive command patterns.

    Returns:
        dict with keys: project_id, root, files_scanned,
        findings (list per file), total_findings, truncated, elapsed_ms
    """
    start = time.monotonic()
    if _root_override is not None:
        root = _root_override.resolve()
    else:
        registry = get_registry()
        info = registry.project_info(project_id)
        root = Path(info["root"]).resolve()
    findings_by_file: dict[str, list[dict]] = {}
    files_scanned = 0
    truncated = False
    total_findings = 0

    for path in sorted(root.rglob(pattern)):
        if time.monotonic() - start > TIMEOUT_S:
            truncated = True
            break

        try:
            rel = path.relative_to(root)
        except ValueError:
            continue

        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue

        raw = path.read_bytes()
        if _is_binary(raw):
            continue

        text = raw.decode("utf-8", errors="replace")
        rel_str = str(rel)
        file_findings: list[dict] = []
        lines = text.splitlines()

        for lineno, line in enumerate(lines, 1):
            report = scan_command(line)
            for f in report.findings:
                file_findings.append({
                    "line": lineno,
                    "content": line.strip(),
                    "pattern_name": f.pattern_name,
                    "severity": f.severity,
                    "reason": f.reason,
                    "suggestion": f.suggestion,
                    "confidence": f.confidence,
                })

        if file_findings:
            findings_by_file[rel_str] = file_findings
            total_findings += len(file_findings)

        files_scanned += 1
        if files_scanned >= max_files:
            truncated = True
            break

    elapsed = (time.monotonic() - start) * 1000

    return {
        "project_id": project_id,
        "root": str(root),
        "files_scanned": files_scanned,
        "findings": findings_by_file,
        "total_findings": total_findings,
        "truncated": truncated,
        "elapsed_ms": round(elapsed, 1),
    }
