"""Regression tests for deterministic CI unit shard allocation.

The pinned cost hint is for distribution only: it must never skip or repeat
any collected test file and never change pytest marker selection.
"""

from scripts.split_unit_shards import assign_shards

SLOW_PROXY_FILE = "tests/test_agent_tools_proxy_worktree.py"


def test_slow_proxy_worktree_has_a_dedicated_shard_for_measured_cost() -> None:
    # Proxy worktree takes ~7-11 minutes while a similar-count group of
    # ordinary unit tests takes much less (Gitea Actions #26755).
    counts = {
        SLOW_PROXY_FILE: 250,
        "tests/test_a.py": 600,
        "tests/test_b.py": 600,
        "tests/test_c.py": 600,
        "tests/test_d.py": 600,
        "tests/test_e.py": 600,
    }

    shards = assign_shards(counts, total=3)

    assert [shard for shard in shards if SLOW_PROXY_FILE in shard] == [[SLOW_PROXY_FILE]]
    assert sorted(path for shard in shards for path in shard) == sorted(counts)
    assert all(shard for shard in shards)


def test_default_unit_files_keep_count_based_deterministic_assignment() -> None:
    counts = {
        "tests/test_c.py": 1,
        "tests/test_b.py": 2,
        "tests/test_a.py": 3,
    }

    assert assign_shards(counts, total=2) == [
        ["tests/test_a.py"],
        ["tests/test_b.py", "tests/test_c.py"],
    ]
    assert assign_shards(dict(reversed(list(counts.items()))), total=2) == assign_shards(
        counts, total=2
    )


def test_weighted_partition_preserves_each_test_file_exactly_once() -> None:
    counts = {SLOW_PROXY_FILE: 200}
    counts.update({f"tests/test_{n}.py": (n % 7) + 1 for n in range(1, 41)})

    shards = assign_shards(counts, total=6)

    assert len(shards) == 6
    assert sorted(file for shard in shards for file in shard) == sorted(counts)
    assert all(shard for shard in shards)
    assert shards == assign_shards(dict(reversed(list(counts.items()))), total=6)
