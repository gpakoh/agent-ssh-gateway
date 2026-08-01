"""Create audit_log table for persistent audit trail.

Creates the audit_log table (metadata-only + command execution details).
Idempotent: skips creation if the table already exists (deployments that
auto-created it via Base.metadata.create_all keep working).

Revision ID: 002_audit_log
Revises: 001_tz_aware_timestamps
Create Date: 2026-08-01
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_audit_log"
down_revision: str | None = "001_tz_aware_timestamps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table: str) -> bool:
    """Check if a table exists in the public schema."""
    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": table},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    """Create the audit_log table if it does not exist."""
    if _table_exists("audit_log"):
        return
    op.create_table(
        "audit_log",
        op.Column("id", sa.String(36), primary_key=True),
        op.Column("event_id", sa.String(64), nullable=False),
        op.Column("event_type", sa.String(64), nullable=False),
        op.Column("session_id", sa.String(36), nullable=True),
        op.Column("actor_type", sa.String(64), nullable=True),
        op.Column("actor_name", sa.String(255), nullable=True),
        op.Column("actor_fingerprint", sa.String(64), nullable=True),
        op.Column("request_id", sa.String(64), nullable=True),
        op.Column("source_ip", sa.String(64), nullable=True),
        op.Column("route", sa.String(255), nullable=True),
        op.Column("tool", sa.String(128), nullable=True),
        op.Column("action", sa.String(512), nullable=True),
        op.Column("target_type", sa.String(64), nullable=True),
        op.Column("target_id", sa.String(512), nullable=True),
        op.Column("policy", sa.String(128), nullable=True),
        op.Column("profile", sa.String(128), nullable=True),
        op.Column("decision", sa.String(16), nullable=True),
        op.Column("reason", sa.Text(), nullable=True),
        op.Column("error_code", sa.String(64), nullable=True),
        op.Column("metadata_json", sa.JSON(), nullable=True),
        op.Column("command", sa.Text(), nullable=True),
        op.Column("exit_code", sa.Integer(), nullable=True),
        op.Column("duration_ms", sa.Integer(), nullable=True),
        op.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_event_id", "audit_log", ["event_id"])
    op.create_index("ix_audit_log_event_type", "audit_log", ["event_type"])
    op.create_index("ix_audit_log_session_id", "audit_log", ["session_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    """Drop the audit_log table."""
    if _table_exists("audit_log"):
        op.drop_table("audit_log")
