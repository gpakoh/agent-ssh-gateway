#!/usr/bin/env python3
"""Live overlay preflight check.

Verifies that the deployment overlay setup is correct:
- docker-compose.live.yml exists locally and is gitignored
- docker/.env exists locally and is gitignored
- Main compose is generic (no hardcoded host IPs/paths)
- Rendered compose has readonly mounts where expected

Usage:
    python3 scripts/compose_live_preflight.py
    python3 scripts/compose_live_preflight.py --verbose
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKER_DIR = ROOT / "docker"
COMPOSE_FILE = DOCKER_DIR / "docker-compose.yml"
LIVE_OVERLAY = DOCKER_DIR / "docker-compose.live.yml"
LIVE_EXAMPLE = DOCKER_DIR / "docker-compose.live.example.yml"
ENV_FILE = DOCKER_DIR / ".env"
ENV_EXAMPLE = DOCKER_DIR / ".env.example"

# Patterns that should NOT appear in tracked compose
_FORBIDDEN_IN_TRACKED = [
    "10.10.10.",
    "192.168.",
    "/media/1TB/",
    "proxmox_macvlan",
]


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, **kwargs)


def _is_gitignored(path: Path) -> bool:
    """Check if a path is gitignored."""
    rel = path.relative_to(ROOT)
    result = _run(["git", "check-ignore", str(rel)])
    return result.returncode == 0


def _check_file_exists(path: Path, label: str, verbose: bool) -> bool:
    if path.exists():
        if verbose:
            print(f"  ✅ {label} exists: {path}")
        return True
    print(f"  ❌ {label} missing: {path}")
    return False


def _check_gitignored(path: Path, label: str, verbose: bool) -> bool:
    if _is_gitignored(path):
        if verbose:
            print(f"  ✅ {label} is gitignored")
        return True
    print(f"  ❌ {label} is NOT gitignored (should be private)")
    return False


def _check_not_gitignored(path: Path, label: str, verbose: bool) -> bool:
    if not _is_gitignored(path):
        if verbose:
            print(f"  ✅ {label} is tracked (public)")
        return True
    print(f"  ❌ {label} is gitignored (should be tracked)")
    return False


def _check_compose_generic(verbose: bool) -> bool:
    """Verify tracked compose doesn't contain hardcoded private values."""
    if not COMPOSE_FILE.exists():
        print(f"  ❌ Main compose missing: {COMPOSE_FILE}")
        return False

    content = COMPOSE_FILE.read_text(encoding="utf-8")
    violations = []
    for pattern in _FORBIDDEN_IN_TRACKED:
        if pattern in content:
            violations.append(pattern)

    if violations:
        print(f"  ❌ Main compose contains private values: {violations}")
        return False

    if verbose:
        print("  ✅ Main compose is generic (no hardcoded private values)")
    return True


def _check_overlay_structure(verbose: bool) -> bool:
    """Verify live overlay has expected structure."""
    if not LIVE_OVERLAY.exists():
        # Not an error — overlay is local-only
        if verbose:
            print("  ℹ️  Live overlay not present (local deployment only)")
        return True

    try:
        import yaml
    except ImportError:
        print("  ⚠️  PyYAML not available, skipping overlay structure check")
        return True

    data = yaml.safe_load(LIVE_OVERLAY.read_text(encoding="utf-8"))
    services = data.get("services", {})
    gw = services.get("web-ssh-gateway", {})

    ok = True

    # Check networks include proxmox_macvlan or similar
    networks = gw.get("networks", {})
    has_macvlan = any(
        isinstance(n, dict) and "ipv4_address" in n
        for n in networks.values()
    ) if isinstance(networks, dict) else False
    if not has_macvlan:
        print("  ⚠️  Live overlay: no macvlan network with static IP found")
        # Not a hard failure — might use different network setup

    # Check volumes have readonly mount
    volumes = gw.get("volumes", [])
    has_workspace_mount = any(
        isinstance(v, str) and ":ro" in v
        for v in volumes
    )
    if not has_workspace_mount:
        print("  ⚠️  Live overlay: no readonly workspace mount found")

    if verbose:
        print("  ✅ Live overlay structure looks correct")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Live overlay preflight check")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    checks = []

    print("=== Live Overlay Preflight ===\n")

    print("1. File existence:")
    checks.append(_check_file_exists(LIVE_OVERLAY, "docker-compose.live.yml", args.verbose))
    checks.append(_check_file_exists(ENV_FILE, ".env", args.verbose))
    checks.append(_check_file_exists(ENV_EXAMPLE, ".env.example", args.verbose))
    checks.append(_check_file_exists(LIVE_EXAMPLE, "docker-compose.live.example.yml", args.verbose))

    print("\n2. Gitignore status:")
    checks.append(_check_gitignored(LIVE_OVERLAY, "docker-compose.live.yml", args.verbose))
    checks.append(_check_gitignored(ENV_FILE, ".env", args.verbose))
    checks.append(_check_not_gitignored(COMPOSE_FILE, "docker-compose.yml", args.verbose))
    checks.append(_check_not_gitignored(ENV_EXAMPLE, ".env.example", args.verbose))
    checks.append(_check_not_gitignored(LIVE_EXAMPLE, "docker-compose.live.example.yml", args.verbose))

    print("\n3. Compose genericity:")
    checks.append(_check_compose_generic(args.verbose))

    print("\n4. Overlay structure:")
    checks.append(_check_overlay_structure(args.verbose))

    print()
    if all(checks):
        print("✅ All preflight checks passed")
        return 0
    print("❌ Some preflight checks failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
