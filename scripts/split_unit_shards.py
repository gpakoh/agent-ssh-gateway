#!/usr/bin/env python3
"""Split the unit test suite into deterministic, balanced shards.

Each shard is a disjoint set of test FILES (a file is never split across
shards), so module-level fixtures and inter-test ordering inside a file
are preserved exactly as in a serial run. Files are assigned by greedy
bin-packing on collected test counts with a measured runtime cost hint for
one slow module. This balances expected runtime without splitting a file.

The partition is a pure function of the repo state: identical input
(checked-out tree + same Python) always yields identical output, so the
parallel shard jobs need no coordination. The greedy loop sorts files by
(descending estimated work, path) and assigns each to the currently least
loaded shard, which makes the result independent of pytest's collection
order -- the same sort discipline that fixed the earlier non-deterministic
FORBIDDEN_KEYS parametrize order.

Usage:
    split_unit_shards.py --total N --shard K
        Print the test files belonging to shard K (0-based), one per line.

Environment: uses the same marker filter as Makefile `test-unit`, so only
tests a shard job will actually run are counted.

Note: pytest --collect-only output goes to stdout; warnings/errors go to
stderr and are intentionally not captured by this script.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

UNIT_MARKER_FILTER = "not host_smoke and not e2e and not integration and not smoke"

# Gitea Actions #26755: this file accounted for roughly 11 min (Python 3.11)
# and 7 min (Python 3.12), versus a few minutes for other files in its
# count-balanced shard. A conservative virtual-test weight allows the costly
# file to occupy one shard without splitting module fixtures or dropping tests.
# It affects placement only; all collected nodes are still executed once.
UNIT_FILE_COST_MULTIPLIERS: dict[str, int] = {
    "tests/test_agent_tools_proxy_worktree.py": 12,
}


def collect_unit_files() -> dict[str, int]:
    """Return {test_file_path: number of collected unit tests}."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-m",
        UNIT_MARKER_FILTER,
        "-p",
        "no:cacheprovider",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"pytest --collect-only failed with rc={proc.returncode}")

    counts: dict[str, int] = defaultdict(int)
    for line in proc.stdout.splitlines():
        if "::" not in line:
            continue
        test_node = line.strip()
        if not test_node or test_node.startswith(("=", " ")):
            continue
        file_part = test_node.split("::", 1)[0]
        counts[file_part] += 1

    if not counts:
        raise SystemExit("no unit tests collected; refusing to build empty shards")

    repo_root = Path(__file__).resolve().parent.parent
    for path in counts:
        try:
            resolved = (repo_root / path).resolve()
            resolved.relative_to(repo_root.resolve())
        except (OSError, ValueError):
            raise SystemExit(f"collected node outside repo tree: {path}") from None
        if resolved.suffix != ".py":
            raise SystemExit(f"collected node is not a .py file: {path}")
    return dict(counts)


def assign_shards(counts: dict[str, int], total: int) -> list[list[str]]:
    """Greedy bin-pack files by estimated work, preserving complete file units."""

    def estimated_work(item: tuple[str, int]) -> int:
        path, count = item
        return count * UNIT_FILE_COST_MULTIPLIERS.get(path, 1)

    ordered = sorted(counts.items(), key=lambda item: (-estimated_work(item), item[0]))
    shards: list[list[str]] = [[] for _ in range(total)]
    loads = [0] * total
    for item in ordered:
        path, _ = item
        target = min(range(total), key=lambda i: (loads[i], i))
        shards[target].append(path)
        loads[target] += estimated_work(item)
    return shards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--total", type=int, required=True)
    parser.add_argument("--shard", type=int, required=True)
    args = parser.parse_args()

    if args.total < 1:
        raise SystemExit("--total must be >= 1")
    if not 0 <= args.shard < args.total:
        raise SystemExit(f"--shard must be in 0..{args.total - 1}")

    counts = collect_unit_files()
    shards = assign_shards(counts, args.total)
    for path in shards[args.shard]:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())