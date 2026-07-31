"""Scan a project directory for destructive command patterns."""

from __future__ import annotations

import json
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


_SARIF_LEVEL_MAP = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}


def _build_sarif(
    project_id: str,
    root: str,
    files_scanned: int,
    findings_by_file: dict[str, list[dict]],
    total_findings: int,
    truncated: bool,
    elapsed_ms: float,
) -> str:
    """Build a SARIF v2.1.0 report string."""
    rules: dict[str, dict] = {}
    results: list[dict] = []

    for file_path, file_findings in sorted(findings_by_file.items()):
        for finding in file_findings:
            pname = finding["pattern_name"]
            if pname not in rules:
                rules[pname] = {
                    "id": pname,
                    "shortDescription": {"text": finding["reason"]},
                    "fullDescription": {"text": finding["reason"]},
                    "defaultConfiguration": {
                        "level": _SARIF_LEVEL_MAP.get(finding["severity"], "warning"),
                    },
                    "properties": {
                        "severity": finding["severity"],
                        "confidence": finding["confidence"],
                    },
                }
            results.append({
                "ruleId": pname,
                "level": _SARIF_LEVEL_MAP.get(finding["severity"], "warning"),
                "message": {"text": finding["content"]},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": file_path},
                        "region": {"startLine": finding["line"]},
                    }
                }],
                "properties": {
                    "suggestion": finding.get("suggestion"),
                    "confidence": finding["confidence"],
                },
            })

    sarif_doc = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "agent-ssh-gateway scan_project",
                    "informationUri": "https://github.com/gpakoh/agent-ssh-gateway",
                    "rules": sorted(rules.values(), key=lambda r: r["id"]),
                }
            },
            "results": results,
            "properties": {
                "project_id": project_id,
                "files_scanned": files_scanned,
                "total_findings": total_findings,
                "truncated": truncated,
                "elapsed_ms": round(elapsed_ms, 1),
            },
        }],
    }
    return json.dumps(sarif_doc, indent=2, ensure_ascii=False)


def _scan(
    project_id: str,
    root: Path,
    pattern: str,
    max_files: int,
) -> dict:
    """Core scanning logic — collect findings grouped by file."""
    findings_by_file: dict[str, list[dict]] = {}
    files_scanned = 0
    truncated = False
    total_findings = 0
    start = time.monotonic()

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
                    "suggestions": f.suggestions,
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


def scan_project(
    project_id: str,
    *,
    pattern: str = "*",
    max_files: int = MAX_FILES,
    _root_override: Path | None = None,
    fmt: str = "dict",
) -> dict | str:
    """Scan a project for destructive command patterns.

    Args:
        project_id: Registered project name.
        pattern: Glob pattern to filter files.
        max_files: Maximum files to scan.
        fmt: Output format — ``"dict"`` (default), ``"json"``, or ``"sarif"``.

    Returns:
        dict when fmt="dict", str (JSON) when fmt="json" or fmt="sarif".
    """
    if _root_override is not None:
        root = _root_override.resolve()
    else:
        registry = get_registry()
        info = registry.project_info(project_id)
        root = Path(info["root"]).resolve()

    data = _scan(project_id, root, pattern, max_files)

    if fmt == "sarif":
        return _build_sarif(
            project_id=data["project_id"],
            root=data["root"],
            files_scanned=data["files_scanned"],
            findings_by_file=data["findings"],
            total_findings=data["total_findings"],
            truncated=data["truncated"],
            elapsed_ms=data["elapsed_ms"],
        )

    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)

    return data
