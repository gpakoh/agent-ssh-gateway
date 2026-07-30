"""P12: SQLite-based persistent decision history log.

Stores policy decisions in SQLite for querying, trending, and analysis.
Simplified port of DCG src/history/ (~5600 lines).

Schema: commands table with indexes on timestamp, outcome, command_hash.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Defaults
_DEFAULT_DB_PATH = "./data/history/history.db"
_DEFAULT_MAX_SIZE_MB = 500
_DEFAULT_RETENTION_DAYS = 90
_DEFAULT_PRUNE_INTERVAL = 100  # check prune every N writes

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT '',
    session_id TEXT,
    command TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('allow','deny','warn','bypass')),
    pack_id TEXT,
    pattern_name TEXT,
    rule_id TEXT,
    eval_duration_us INTEGER DEFAULT 0,
    exit_code INTEGER,
    working_dir TEXT DEFAULT '',
    hostname TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_commands_timestamp ON commands(timestamp);
CREATE INDEX IF NOT EXISTS idx_commands_outcome ON commands(outcome);
CREATE INDEX IF NOT EXISTS idx_commands_command_hash ON commands(command_hash);
CREATE INDEX IF NOT EXISTS idx_commands_rule_id ON commands(rule_id) WHERE rule_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commands_outcome_timestamp ON commands(outcome, timestamp);
"""


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


class HistoryError(Exception):
    """History database error."""


class HistoryLogger:
    """SQLite-backed persistent policy decision log.

    Thread-safe. Auto-creates DB and schema on init.
    Prunes old entries periodically based on retention_days.
    """

    def __init__(
        self,
        db_path: str | Path = _DEFAULT_DB_PATH,
        max_size_mb: int = _DEFAULT_MAX_SIZE_MB,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        prune_interval: int = _DEFAULT_PRUNE_INTERVAL,
    ):
        self._db_path = Path(db_path)
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._retention_days = retention_days
        self._prune_interval = prune_interval
        self._write_count = 0
        self._lock = threading.Lock()

        # Ensure parent directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._db_path), timeout=10, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

        # Apply size limit
        if self._max_size_bytes > 0:
            page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
            max_pages = max(1, self._max_size_bytes // page_size)
            self._conn.execute(f"PRAGMA max_page_count={max_pages}")

        logger.info(
            "HistoryLogger opened at %s (retention=%dd, max=%dMB)",
            self._db_path, retention_days, max_size_mb,
        )

    # ── Public query API ────────────────────────────────────────────────

    def count(self) -> int:
        """Total number of command records."""
        row = self._conn.execute("SELECT COUNT(*) FROM commands").fetchone()
        return row[0] if row else 0

    def get_history_count(self, command: str, days: int = 7) -> int:
        """How many times a specific command was blocked in the last N days."""
        h = _command_hash(command)
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) FROM commands WHERE command_hash=? AND timestamp>=?",
            (h, since),
        ).fetchone()
        return row[0] if row else 0

    def get_history_count_by_rule(self, rule_id: str, days: int = 7) -> int:
        """How many times a specific rule triggered in the last N days."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) FROM commands WHERE rule_id=? AND timestamp>=?",
            (rule_id, since),
        ).fetchone()
        return row[0] if row else 0

    def get_frequent_blocks(
        self, days: int = 7, min_count: int = 3, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Most frequently blocked commands."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            """
            SELECT command, command_hash, outcome, COUNT(*) as cnt,
                   COUNT(DISTINCT session_id) as sessions
            FROM commands
            WHERE timestamp>=? AND outcome='deny'
            GROUP BY command_hash
            HAVING cnt>=?
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (since, min_count, limit),
        ).fetchall()
        return [
            {
                "command": r[0],
                "command_hash": r[1],
                "outcome": r[2],
                "count": r[3],
                "sessions": r[4],
            }
            for r in rows
        ]

    def get_bypass_patterns(
        self, days: int = 7, min_count: int = 2, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Commands that were bypassed most frequently."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            """
            SELECT command, command_hash, COUNT(*) as cnt,
                   COUNT(DISTINCT session_id) as sessions
            FROM commands
            WHERE timestamp>=? AND outcome='bypass'
            GROUP BY command_hash
            HAVING cnt>=?
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (since, min_count, limit),
        ).fetchall()
        return [
            {
                "command": r[0],
                "command_hash": r[1],
                "count": r[2],
                "sessions": r[3],
            }
            for r in rows
        ]

    def compute_stats(self, days: int = 7) -> dict[str, Any]:
        """Aggregate statistics for the last N days."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        total = self._conn.execute(
            "SELECT COUNT(*) FROM commands WHERE timestamp>=?", (since,),
        ).fetchone()[0]

        outcome_counts: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT outcome, COUNT(*) FROM commands WHERE timestamp>=? GROUP BY outcome",
            (since,),
        ).fetchall():
            outcome_counts[row[0]] = row[1]

        top_patterns: list[dict[str, Any]] = []
        for row in self._conn.execute(
            """
            SELECT rule_id, pattern_name, COUNT(*) as cnt
            FROM commands
            WHERE timestamp>=? AND rule_id IS NOT NULL
            GROUP BY rule_id
            ORDER BY cnt DESC
            LIMIT 10
            """,
            (since,),
        ).fetchall():
            top_patterns.append({"rule_id": row[0], "pattern_name": row[1], "count": row[2]})

        return {
            "period_days": days,
            "total_commands": total,
            "outcome_breakdown": outcome_counts,
            "top_patterns": top_patterns,
        }

    # ── Write ────────────────────────────────────────────────────────────

    def log(self, entry: dict[str, Any]) -> None:
        """Log a single command decision."""
        with self._lock:
            self._log_inner(entry)

    def log_batch(self, entries: list[dict[str, Any]]) -> None:
        """Log multiple command decisions atomically."""
        with self._lock:
            for entry in entries:
                self._log_inner(entry)

    def _log_inner(self, entry: dict[str, Any]) -> None:
        command = entry.get("command", "")
        cmd_hash = _command_hash(command)

        try:
            self._conn.execute(
                """
                INSERT INTO commands
                    (timestamp, agent_type, session_id, command, command_hash,
                     outcome, pack_id, pattern_name, rule_id,
                     eval_duration_us, exit_code, working_dir, hostname)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.get("timestamp", datetime.now(UTC).isoformat()),
                    entry.get("agent_type", ""),
                    entry.get("session_id"),
                    command,
                    cmd_hash,
                    entry.get("outcome", "deny"),
                    entry.get("pack_id"),
                    entry.get("pattern_name"),
                    entry.get("rule_id"),
                    entry.get("eval_duration_us", 0),
                    entry.get("exit_code"),
                    entry.get("working_dir", ""),
                    entry.get("hostname", ""),
                ),
            )
            self._conn.commit()

            # Periodic prune check
            self._write_count += 1
            if self._write_count % self._prune_interval == 0:
                self._auto_prune()

        except sqlite3.Error as exc:
            logger.warning("History write failed: %s", exc)

    def _auto_prune(self) -> None:
        if self._retention_days <= 0:
            return
        cutoff = (datetime.now(UTC) - timedelta(days=self._retention_days)).isoformat()
        try:
            deleted = self._conn.execute(
                "DELETE FROM commands WHERE timestamp<?", (cutoff,),
            ).rowcount
            if deleted > 0:
                self._conn.commit()
                logger.info("Pruned %d history records older than %d days", deleted, self._retention_days)
        except sqlite3.Error as exc:
            logger.warning("History prune failed: %s", exc)

    def prune(self, older_than_days: int) -> int:
        """Explicitly prune records older than N days. Returns count deleted."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            deleted = self._conn.execute(
                "DELETE FROM commands WHERE timestamp<?", (cutoff,),
            ).rowcount
            if deleted > 0:
                self._conn.commit()
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return deleted

    def vacuum(self) -> None:
        """Recover disk space."""
        with self._lock:
            self._conn.execute("VACUUM")

    def close(self) -> None:
        """Close database connection."""
        with self._lock:
            self._conn.close()

    # ── Health ───────────────────────────────────────────────────────────

    def check_health(self) -> dict[str, Any]:
        """Basic health check."""
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM commands").fetchone()
            count = row[0] if row else -1
            size = self._db_path.stat().st_size if self._db_path.exists() else 0
            return {
                "status": "ok",
                "record_count": count,
                "db_size_bytes": size,
                "db_path": str(self._db_path),
            }
        except sqlite3.Error as exc:
            return {"status": "error", "error": str(exc)}
