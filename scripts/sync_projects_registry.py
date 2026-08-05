#!/usr/bin/env python3
"""Sync projects.yaml with repos that exist in Gitea AND are cloned locally.

Only ADDS new entries — existing hand-curated entries (description, type,
tags, parent) are never modified or removed. A repo is added only when
both conditions hold:
  - it exists in Gitea under GITEA_OWNER, and
  - a local git clone of it is found under one of the scan roots (matched
    by the repo name in the clone's remote URL, not by directory name).

Repos that exist in Gitea but have no local clone are skipped (there is
nothing to browse). Local clones pointing at a different owner/host
(third-party dependencies vendored under the same tree) are skipped too.

Env vars (all required except SCAN_ROOTS):
    GITEA_API_BASE   e.g. https://gitea.example.com/api/v1
    GITEA_TOKEN      Gitea API token, repo:read scope is enough
    GITEA_OWNER      Gitea username/org to list repos for
    SCAN_ROOTS       Comma-separated extra directories to scan for local
                      git clones, in addition to projects.yaml's own
                      registry_root (optional)

Usage:
    GITEA_API_BASE=... GITEA_TOKEN=... GITEA_OWNER=... \\
        python3 scripts/sync_projects_registry.py [--apply] [--yaml PATH]

Without --apply, prints a dry-run summary only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = REPO_ROOT / "projects.yaml"

# Directory name suffixes that mark a backup/scratch copy rather than the
# canonical clone of a project -- never auto-registered.
_SKIP_DIR_SUFFIXES = (".bak", "-bak", "-backup", "-cleaned", "-old", ".orig")

_SCP_STYLE_RE = re.compile(r"^[\w.-]+@[\w.-]+:(.+)$")
_URL_STYLE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?[^/]+/(.+)$")


def _fetch_gitea_repos(api_base: str, token: str, owner: str) -> dict[str, str]:
    """Return {repo_name: description} for every repo owned by `owner`."""
    repos: dict[str, str] = {}
    page = 1
    while True:
        req = urllib.request.Request(
            f"{api_base.rstrip('/')}/repos/search?limit=50&page={page}&uid={_owner_id(api_base, token, owner)}",
            headers={"Authorization": f"token {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        batch = data.get("data", [])
        if not batch:
            break
        for r in batch:
            repos[r["name"]] = r.get("description") or ""
        if len(batch) < 50:
            break
        page += 1
    return repos


def _owner_id(api_base: str, token: str, owner: str) -> int:
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/users/{owner}",
        headers={"Authorization": f"token {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["id"]


def _repo_name_from_remote(url: str, owner: str) -> str | None:
    """Extract the repo name from a remote URL, or None if it doesn't
    belong to `owner` (third-party / unrelated remote)."""
    if not url:
        return None
    m = _SCP_STYLE_RE.match(url)
    path = m.group(1) if m else url
    m2 = _URL_STYLE_RE.match(path)
    if m2:
        path = m2.group(1)
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) < 2 or parts[-2] != owner:
        return None
    return parts[-1]


def _find_local_clones(scan_roots: list[Path], owner: str, max_depth: int = 4) -> dict[str, Path]:
    """Return {repo_name: absolute_path} for every local git clone under
    scan_roots whose remote belongs to `owner`.

    A single directory's remotes can name more than one repo (e.g. a
    monorepo that still carries a stray remote from before a subproject
    was split out into its own clone elsewhere) -- every name is
    considered as a *candidate* rather than stopping at the first one
    found, and candidates are resolved globally: when more than one
    directory claims the same repo name, the one whose own basename
    matches the name wins (a nested clone with the right name beats a
    parent directory that merely has a leftover remote pointing at it).
    """
    candidates: dict[str, list[Path]] = {}
    for root in scan_roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, _filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth >= max_depth:
                dirnames[:] = []
                continue
            if ".git" not in dirnames:
                continue
            dirnames[:] = [d for d in dirnames if d != ".git"]  # don't descend into it
            repo_dir = Path(dirpath)
            if repo_dir.name.endswith(_SKIP_DIR_SUFFIXES):
                continue
            remotes = subprocess.run(
                ["git", "-C", str(repo_dir), "remote", "-v"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            urls = {line.split()[1] for line in remotes.splitlines() if len(line.split()) >= 2}
            names = {_repo_name_from_remote(url, owner) for url in urls}
            for name in names:
                if name is None:
                    continue
                candidates.setdefault(name, []).append(repo_dir)

    found: dict[str, Path] = {}
    for name, dirs in candidates.items():
        basename_matches = [d for d in dirs if d.name == name]
        pool = basename_matches or dirs
        found[name] = min(pool, key=lambda d: len(d.parts))
    return found


def _load_yaml_entries(yaml_path: Path) -> tuple[str, list[str]]:
    """Return (raw_text, list_of_existing_project_keys) without a YAML
    dependency -- projects.yaml's structure is simple enough to scan by
    line prefix, and this avoids adding a new dependency for a script."""
    text = yaml_path.read_text(encoding="utf-8")
    keys = re.findall(r"^  ([A-Za-z0-9_.-]+):\s*$", text, flags=re.MULTILINE)
    return text, keys


def _infer_type(local_path: Path) -> str:
    if (local_path / "pyproject.toml").exists() or (local_path / "requirements.txt").exists():
        return "python-service"
    if (local_path / "package.json").exists():
        return "node-service"
    if (local_path / "Dockerfile").exists():
        return "service"
    return "unknown"


def _find_parent(root_rel: str, existing_keys: list[str], text: str) -> str | None:
    for key in existing_keys:
        m = re.search(rf"^  {re.escape(key)}:\n(?:    .+\n)*    root: (.+)$", text, flags=re.MULTILINE)
        if not m:
            continue
        existing_root = m.group(1).strip()
        if root_rel != existing_root and root_rel.startswith(existing_root + "/"):
            return key
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Write changes to projects.yaml (default: dry run)")
    ap.add_argument("--yaml", type=Path, default=DEFAULT_YAML, help="Path to projects.yaml")
    args = ap.parse_args()

    api_base = os.environ["GITEA_API_BASE"]
    token = os.environ["GITEA_TOKEN"]
    owner = os.environ["GITEA_OWNER"]

    text, existing_keys = _load_yaml_entries(args.yaml)
    registry_root_m = re.search(r"^registry_root:\s*(.+)$", text, flags=re.MULTILINE)
    if not registry_root_m:
        print("projects.yaml has no registry_root -- aborting", file=sys.stderr)
        return 1
    registry_root = Path(registry_root_m.group(1).strip())

    scan_roots = [registry_root]
    if os.environ.get("SCAN_ROOTS"):
        scan_roots.extend(Path(p.strip()) for p in os.environ["SCAN_ROOTS"].split(",") if p.strip())

    gitea_repos = _fetch_gitea_repos(api_base, token, owner)
    local_clones = _find_local_clones(scan_roots, owner)

    # existing entries' `root:` values, so we never re-add an existing project
    existing_roots = set(re.findall(r"^    root: (.+)$", text, flags=re.MULTILINE))

    new_entries: list[str] = []
    skipped_no_local: list[str] = []
    for repo_name, description in sorted(gitea_repos.items()):
        if repo_name in existing_keys:
            continue
        local_path = local_clones.get(repo_name)
        if local_path is None:
            skipped_no_local.append(repo_name)
            continue
        try:
            root_rel = str(local_path.relative_to(registry_root))
        except ValueError:
            skipped_no_local.append(f"{repo_name} (outside registry_root: {local_path})")
            continue
        if root_rel in existing_roots:
            continue

        key = repo_name.lower()
        parent = _find_parent(root_rel, existing_keys, text)
        entry_type = _infer_type(local_path)
        desc_escaped = description.replace('"', '\\"') if description else key
        lines = [
            f"  {key}:",
            f"    root: {root_rel}",
        ]
        if parent:
            lines.append(f"    parent: {parent}")
        lines.append(f"    type: {entry_type}")
        lines.append(f'    description: "{desc_escaped}"')
        lines.append("    tags: []")
        new_entries.append("\n".join(lines))

    print(f"Gitea repos: {len(gitea_repos)}")
    print(f"Local clones matched: {len(local_clones)}")
    print(f"Already registered: {len(existing_keys)}")
    print(f"New entries to add: {len(new_entries)}")
    for e in new_entries:
        print("  + " + e.splitlines()[0].strip())
    if skipped_no_local:
        print(f"Skipped (no local clone found): {len(skipped_no_local)}")
        for s in skipped_no_local:
            print("  - " + s)

    if not new_entries:
        print("Nothing to do.")
        return 0

    if not args.apply:
        print("\nDry run only -- pass --apply to write projects.yaml")
        return 0

    updated = text.rstrip("\n") + "\n\n" + "\n\n".join(new_entries) + "\n"
    args.yaml.write_text(updated, encoding="utf-8")
    print(f"\nWrote {len(new_entries)} new entries to {args.yaml}")
    print("Restart agent-ssh-gateway-mcp.service to pick up the changes "
          "(the registry is loaded once per process).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
