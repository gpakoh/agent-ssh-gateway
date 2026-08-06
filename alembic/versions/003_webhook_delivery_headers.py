"""Add headers_json column to webhook_deliveries.

The delivery worker previously computed the HMAC signature, timestamp,
event-id, delivery-id, and user-configured custom headers for every
outbound webhook POST, then discarded all of them before sending --
enqueue()/WebhookDelivery had no column to carry them from the emitter
to the worker, so every delivery went out with only a bare
Content-Type header. This column lets the computed headers survive the
outbox round-trip so they're actually sent on the wire.

Idempotent: skips if the column already exists (deployments that
auto-created the table via Base.metadata.create_all after this model
change already have it).

Revision ID: 003_webhook_delivery_headers
Revises: 002_audit_log
Create Date: 2026-08-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_webhook_delivery_headers"
down_revision: str | None = "002_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table "
            "AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    if _column_exists("webhook_deliveries", "headers_json"):
        return
    op.add_column(
        "webhook_deliveries", sa.Column("headers_json", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    if _column_exists("webhook_deliveries", "headers_json"):
        op.drop_column("webhook_deliveries", "headers_json")
