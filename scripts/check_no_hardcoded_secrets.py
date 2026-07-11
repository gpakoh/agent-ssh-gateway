#!/usr/bin/env python3
"""Check tracked config/compose files for hardcoded credential values.

Exit codes:
  0 — no secrets found (all values are placeholders or env refs)
  1 — hardcoded secret detected
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TRACKED_PATTERNS = (
    "docker-compose*.yml",
    "docker/*.yml",
    "examples/**/*.yml",
    "docs/**/*.md",
    "scripts/**/*.py",
)

SUSPICIOUS_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<key>POSTGRES_PASSWORD|PGPASSWORD|DATABASE_URL
               |REDIS_URL|API_KEY|JWT_SECRET|AGENT_TOKEN
               |ENCRYPTION_KEY|SECRET_KEY|MCP_PUBLIC_TOKEN)
    [:=]\s*
    (?P<value>.+)
    $
    """,
    re.VERBOSE | re.MULTILINE,
)

PLACEHOLDER_PATTERNS = (
    r"^\$\{.*:?.*\}$",
    r"^<.*>$",
    r"^`+.*`*$",
    r"^change-me",
    r"^example",
    r"^placeholder",
    r"^dummy",
    r"^test-",
    r"^secret-42$",
    r"^ws-secret-99$",
    r"^test-jwt-secret-for-testing-only$",
    r"^super-secret(-password)?$",
    r"^secret123$",
    r"^secret$",
    r"^my-secret(-password)?$",
    r"^password1$",
    r"^password2$",
    r"^hunter2$",
    r"^db_secret_123$",
    r"^redis://redis:6379/0$",
    r"^wrongpass",
    r"^test123$",
    r"^sshpass123$",
    r"^test-secret(-key)?$",
)


def _git_ls_files(pattern: str) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", pattern],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return [REPO_ROOT / p for p in result.stdout.strip().splitlines() if p]


def _is_placeholder(value: str) -> bool:
    stripped = value.strip().strip("\"'")
    for pat in PLACEHOLDER_PATTERNS:
        if re.match(pat, stripped):
            return True
    return False


def _check_file(filepath: Path) -> list[str]:
    hits: list[str] = []
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return hits

    for m in SUSPICIOUS_LINE_RE.finditer(text):
        value = m.group("value")
        if _is_placeholder(value):
            continue
        rel = filepath.relative_to(REPO_ROOT)
        hits.append(f"{rel}: {m.group('key')}={value[:16]}...")
    return hits


def main() -> int:
    files: list[Path] = []
    for pat in TRACKED_PATTERNS:
        files.extend(_git_ls_files(pat))

    seen = set()
    unique_files: list[Path] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)

    failures: list[str] = []
    for fp in sorted(unique_files):
        failures.extend(_check_file(fp))

    if failures:
        print("HARDCODED SECRETS DETECTED (use env refs or placeholders):")
        for hit in sorted(failures):
            print(f"  {hit}")
        return 1

    print("No hardcoded secrets found in tracked config files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
