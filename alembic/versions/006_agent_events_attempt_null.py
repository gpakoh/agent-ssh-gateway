"""Make agent_events.attempt_id nullable for pre-execution lifecycle events.

A pending job that is cancelled before any execution attempt begins has no
attempt_id.  The ``cancelled`` event written in that path must carry
``attempt_id=NULL`` in PostgreSQL so that (a) the column semantics stay
correct — attempt_id identifies an *execution* attempt, not a job-level
event — and (b) history queries survive restarts.

Revision ID: 006_agent_events_attempt_null
Revises: 005_agent_events
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "006_agent_events_attempt_null"
down_revision: str | None = "005_agent_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table: str) -> bool:
    from sqlalchemy import text

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
    if not _table_exists("agent_events"):
        return
    op.alter_column(
        "agent_events",
        "attempt_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )


def downgrade() -> None:
    if not _table_exists("agent_events"):
        return
    # Existing NULL rows would block the NOT NULL constraint.  In practice
    # no pre-execution cancelled events exist yet (this migration ships in
    # the same release), so the unconditional ALTER is safe.
    op.alter_column(
        "agent_events",
        "attempt_id",
        existing_type=sa.String(length=36),
        nullable=False,
        server_default="",
    )
