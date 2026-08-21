"""Create agent_events table for the Agent Events Observability v2 layer.

Append-only event log keyed by a monotonically increasing sequence,
partitioned logically by job_id/attempt_id for replay and resume.

Idempotent: skips creation if the table already exists (deployments that
auto-created it via Base.metadata.create_all keep working).

Revision ID: 005_agent_events
Revises: 004_persistent_session_ownership
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "005_agent_events"
down_revision: str | None = "004_persistent_session_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": table},
    )
    return result.fetchone() is not None


def _create_agent_events() -> None:
    op.create_table(
        "agent_events",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "sequence",
            sa.BigInteger(),
            sa.Sequence("agent_events_sequence_seq"),
            nullable=False,
        ),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=text("'{}'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("sequence", name="uq_agent_events_sequence"),
    )
    op.create_index("ix_agent_events_job_id", "agent_events", ["job_id"])
    op.create_index("ix_agent_events_job_seq", "agent_events", ["job_id", "sequence"])
    op.create_index("ix_agent_events_job_attempt", "agent_events", ["job_id", "attempt_id"])
    op.create_index(
        "ix_agent_events_job_type_created",
        "agent_events",
        ["job_id", "event_type", text("created_at DESC")],
    )


def upgrade() -> None:
    if not _table_exists("agent_events"):
        _create_agent_events()


def downgrade() -> None:
    if _table_exists("agent_events"):
        op.drop_table("agent_events")
    # CREATE SEQUENCE is not OWNED BY the column, so drop_table leaves it behind.
    op.execute(text("DROP SEQUENCE IF EXISTS agent_events_sequence_seq"))
