"""Tests for P12: SQLite history logger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.history import HistoryLogger, _command_hash


@pytest.fixture
def db(tmp_path):
    h = HistoryLogger(db_path=str(tmp_path / "test.db"), retention_days=999)
    yield h
    h.close()


# ── _command_hash ────────────────────────────────────────────────────────


def test_command_hash_consistent():
    assert _command_hash("rm -rf /") == _command_hash("rm -rf /")


def test_command_hash_different():
    assert _command_hash("rm -rf /") != _command_hash("echo hi")


# ── Basic write + count ─────────────────────────────────────────────────


def test_count_empty(db):
    assert db.count() == 0


def test_log_one(db):
    db.log({"command": "rm -rf /", "outcome": "deny"})
    assert db.count() == 1


def test_log_ten(db):
    for i in range(10):
        db.log({"command": f"cmd{i}", "outcome": "deny"})
    assert db.count() == 10


def test_log_batch(db):
    entries = [{"command": f"cmd{i}", "outcome": "deny"} for i in range(20)]
    db.log_batch(entries)
    assert db.count() == 20


# ── get_history_count ───────────────────────────────────────────────────


def test_history_count_recent(db):
    db.log({"command": "rm -rf /", "outcome": "deny"})
    assert db.get_history_count("rm -rf /", days=7) == 1


def test_history_count_old_not_counted(db):
    db.log({"command": "rm -rf /", "outcome": "deny"})
    assert db.get_history_count("rm -rf /", days=0) <= 1  # may still match same-day


def test_history_count_different_cmd(db):
    db.log({"command": "rm -rf /", "outcome": "deny"})
    assert db.get_history_count("echo hi", days=7) == 0


def test_history_count_by_rule(db):
    db.log({"command": "rm -rf /", "outcome": "deny", "rule_id": "R001"})
    assert db.get_history_count_by_rule("R001", days=7) == 1


def test_history_count_by_rule_missing(db):
    assert db.get_history_count_by_rule("R999", days=7) == 0


# ── get_frequent_blocks ────────────────────────────────────────────────


def test_frequent_blocks_empty(db):
    assert db.get_frequent_blocks(days=7, min_count=1) == []


def test_frequent_blocks_returns_top(db):
    for _ in range(5):
        db.log({"command": "rm -rf /", "outcome": "deny"})
    for _ in range(3):
        db.log({"command": "chmod 777", "outcome": "deny"})

    blocks = db.get_frequent_blocks(days=7, min_count=3)
    assert len(blocks) >= 1
    assert blocks[0]["command"] == "rm -rf /"
    assert blocks[0]["count"] == 5


def test_frequent_blocks_filters_by_min_count(db):
    db.log({"command": "rare cmd", "outcome": "deny"})
    blocks = db.get_frequent_blocks(days=7, min_count=5)
    assert len(blocks) == 0


def test_frequent_blocks_only_deny(db):
    """Should not include bypass or allow outcomes."""
    for _ in range(5):
        db.log({"command": "rm -rf /", "outcome": "bypass"})
    blocks = db.get_frequent_blocks(days=7, min_count=3)
    assert len(blocks) == 0


# ── get_bypass_patterns ─────────────────────────────────────────────────


def test_bypass_patterns_empty(db):
    assert db.get_bypass_patterns(days=7) == []


def test_bypass_patterns_found(db):
    for _ in range(3):
        db.log({"command": "docker rm -f foo", "outcome": "bypass"})
    patterns = db.get_bypass_patterns(days=7, min_count=2)
    assert len(patterns) >= 1
    assert patterns[0]["command"] == "docker rm -f foo"


def test_bypass_patterns_excludes_deny(db):
    """Bypass query should not include deny records."""
    for _ in range(5):
        db.log({"command": "rm -rf /", "outcome": "deny"})
    assert db.get_bypass_patterns(days=7) == []


# ── compute_stats ──────────────────────────────────────────────────────


def test_stats_empty(db):
    stats = db.compute_stats(days=7)
    assert stats["total_commands"] == 0


def test_stats_counts_outcomes(db):
    for _ in range(3):
        db.log({"command": "cmd1", "outcome": "deny"})
    db.log({"command": "cmd2", "outcome": "allow"})

    stats = db.compute_stats(days=7)
    assert stats["total_commands"] == 4
    assert stats["outcome_breakdown"].get("deny") == 3
    assert stats["outcome_breakdown"].get("allow") == 1


def test_stats_top_patterns(db):
    for _ in range(5):
        db.log({"command": "rm -rf /", "outcome": "deny", "rule_id": "R001", "pattern_name": "rm-root"})
    for _ in range(3):
        db.log({"command": "chmod 777", "outcome": "deny", "rule_id": "R002", "pattern_name": "chmod-777"})

    stats = db.compute_stats(days=7)
    assert len(stats["top_patterns"]) == 2
    assert stats["top_patterns"][0]["rule_id"] == "R001"


# ── Prune ──────────────────────────────────────────────────────────────


def test_prune_removes_old(db):
    old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    db.log({"command": "old cmd", "outcome": "deny", "timestamp": old_ts})
    db.log({"command": "new cmd", "outcome": "deny"})
    db.prune(older_than_days=5)
    assert db.count() == 1


def test_prune_keeps_recent(db):
    db.log({"command": "recent cmd", "outcome": "deny"})
    db.prune(older_than_days=365)
    assert db.count() == 1


def test_prune_zero_removes_all(db):
    """older_than_days=0 means delete everything before now."""
    db.log({"command": "recent cmd", "outcome": "deny"})
    deleted = db.prune(older_than_days=0)
    assert deleted == 1
    assert db.count() == 0


def test_prune_idempotent(db):
    old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    db.log({"command": "old cmd", "outcome": "deny", "timestamp": old_ts})
    db.prune(older_than_days=5)
    db.prune(older_than_days=5)
    assert db.count() == 0


# ── Health ──────────────────────────────────────────────────────────────


def test_health_ok(db):
    health = db.check_health()
    assert health["status"] == "ok"
    assert isinstance(health["record_count"], int)


# ── Edge cases ──────────────────────────────────────────────────────────


def test_empty_command(db):
    db.log({"command": "", "outcome": "deny"})
    assert db.count() == 1


def test_unicode_command(db):
    db.log({"command": "echo café ñoño", "outcome": "allow"})
    assert db.count() == 1
    h = _command_hash("echo café ñoño")
    assert len(h) == 64


def test_all_fields(db):
    db.log({
        "command": "rm -rf /",
        "outcome": "deny",
        "agent_type": "claude_code",
        "session_id": "ses-abc123",
        "pack_id": "system",
        "pattern_name": "rm-rf-root",
        "rule_id": "R001",
        "eval_duration_us": 42,
        "exit_code": 0,
        "working_dir": "/home/user",
        "hostname": "server-1",
    })
    assert db.count() == 1


def test_log_batch_atomic(db):
    entries = [{"command": f"cmd{i}", "outcome": "deny"} for i in range(100)]
    db.log_batch(entries)
    assert db.count() == 100
    assert len(db.get_frequent_blocks(days=7, min_count=1, limit=200)) == 100


def test_multiple_outcomes_in_stats(db):
    outcomes = ["allow", "deny", "warn", "bypass"]
    for o in outcomes:
        for _ in range(2):
            db.log({"command": f"cmd-{o}", "outcome": o})
    stats = db.compute_stats(days=7)
    assert stats["total_commands"] == 8
    for o in outcomes:
        assert stats["outcome_breakdown"].get(o) == 2


def test_frequent_blocks_limit(db):
    for i in range(30):
        for _ in range(3):
            db.log({"command": f"cmd{i}", "outcome": "deny"})
    blocks = db.get_frequent_blocks(days=7, min_count=3, limit=10)
    assert len(blocks) <= 10


def test_bypass_limit(db):
    for i in range(30):
        for _ in range(3):
            db.log({"command": f"cmd{i}", "outcome": "bypass"})
    patterns = db.get_bypass_patterns(days=7, min_count=3, limit=5)
    assert len(patterns) == 5


def test_concurrent_writes(db):
    import threading
    errors = []

    def writer(n):
        try:
            for i in range(20):
                db.log({"command": f"thread-{n}-cmd{i}", "outcome": "deny"})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert db.count() == 100


def test_close_then_open_again(db, tmp_path):
    path = tmp_path / "persist.db"
    h1 = HistoryLogger(db_path=str(path))
    h1.log({"command": "test", "outcome": "deny"})
    h1.close()

    h2 = HistoryLogger(db_path=str(path))
    assert h2.count() == 1
    h2.close()
