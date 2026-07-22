#!/usr/bin/env python3
"""Public-repo hygiene scanner.

Checks tracked public-facing files for infrastructure topology hints:
non-local IP literals, non-example URLs/domains, and host-specific filesystem
paths. The scanner prints only file/line/category, never the matched value.

Use ``# public-hygiene: allow`` on a line for rare intentional examples.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOW_MARKER = "public-hygiene: allow"

TRACKED_PATHS = (
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "SSH_GATEWAY_GUIDE.md",
    "deploy.example.md",
    ".env.example",
    "app/api_help.py",
    "app/static/app.js",
    "app/static/index.html",
    "docker/*.yml",
    "docker-compose*.yml",
    "docs/**/*.md",
    "scripts/**/*.py",
)

IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w.])")
URL_RE = re.compile(r"\bhttps?://[^\s)'\"<>]+")
SSH_HOST_RE = re.compile(r"\b(?:git|ssh)@([^:\s]+):")
INTERNAL_PATH_RE = re.compile(
    r"(?:^|[\s`'\"])(?:/media/|/mnt/|/root/\.ssh/|/etc/agent-|/etc/.+\.env\b)"
)

ALLOWED_EXACT_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "example.com",
    "gateway.example.com",
    "auth.example.com",
    "mcp.example.com",
    "gitea.example.internal",
    "api.telegram.org",
    "github.com",
    "img.shields.io",
    "modelcontextprotocol.io",
}

ALLOWED_HOST_SUFFIXES = (
    ".example.com",
    ".example.invalid",
    ".localhost",
)

GENERIC_CIDR_KEYS = (
    "ALLOWED_TARGET_CIDRS",
    "DENIED_TARGET_CIDRS",
    "ALLOWED_CLIENT_CIDRS",
    "TRUSTED_PROXY_CIDRS",
)

GENERIC_BIND_KEYS = (
    "UVICORN_HOST=0.0.0.0",
    "--host 0.0.0.0",
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    category: str
    detail: str

    def render(self) -> str:
        rel = self.path.relative_to(REPO_ROOT)
        return f"{rel}:{self.line}: {self.category} ({self.detail})"


def _git_ls_files(pattern: str) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", pattern],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    return [REPO_ROOT / item for item in result.stdout.splitlines() if item]


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for pattern in TRACKED_PATHS:
        files.extend(_git_ls_files(pattern))
    return sorted(set(files))


def _line_is_allowed(line: str) -> bool:
    if ALLOW_MARKER in line:
        return True
    if any(key in line for key in GENERIC_CIDR_KEYS):
        return True
    if any(key in line for key in GENERIC_BIND_KEYS):
        return True
    return False


def _is_public_hygiene_ip(token: str) -> bool:
    address_text = token.split("/", 1)[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        return False

    if address.is_loopback or address.is_unspecified:
        return False

    return True


def _host_is_allowed(host: str) -> bool:
    host = host.strip().lower().strip("[]")
    if not host:
        return True
    if host in ALLOWED_EXACT_HOSTS:
        return True
    if host.endswith(ALLOWED_HOST_SUFFIXES):
        return True
    try:
        return not _is_public_hygiene_ip(host)
    except ValueError:
        return False


def _url_host(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or ""


def _scan_line(path: Path, line_no: int, line: str) -> list[Finding]:
    if _line_is_allowed(line):
        return []

    findings: list[Finding] = []

    for match in IPV4_RE.finditer(line):
        if _is_public_hygiene_ip(match.group(0)):
            findings.append(Finding(path, line_no, "ip-literal", "replace with placeholder"))

    for match in URL_RE.finditer(line):
        host = _url_host(match.group(0))
        if not _host_is_allowed(host):
            findings.append(Finding(path, line_no, "non-example-url-host", "use example domain/localhost"))

    for match in SSH_HOST_RE.finditer(line):
        host = match.group(1)
        if not _host_is_allowed(host):
            findings.append(Finding(path, line_no, "non-example-ssh-host", "use example domain"))

    if INTERNAL_PATH_RE.search(line):
        findings.append(Finding(path, line_no, "host-specific-path", "use <repo-root> or placeholder"))

    return findings


def _scan_file(path: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        findings.extend(_scan_line(path, line_no, line))
    return findings


def main() -> int:
    findings: list[Finding] = []
    for path in _iter_files():
        findings.extend(_scan_file(path))

    if findings:
        print("PUBLIC HYGIENE FINDINGS (values intentionally omitted):")
        for finding in findings:
            print(f"  {finding.render()}")
        print("\nUse placeholders or add an explicit '# public-hygiene: allow' comment for generic examples.")
        return 1

    print("Public hygiene scan passed: no public-repo topology hints found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
