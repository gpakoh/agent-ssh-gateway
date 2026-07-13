"""Convert timestamp columns from without time zone to with time zone.

All existing data is treated as UTC. The USING clause ensures safe
conversion: existing naive UTC timestamps become timezone-aware UTC.

Revision ID: 001_tz_aware_timestamps
Revises: None (first Alembic migration)
Create Date: 2026-07-13
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "001_tz_aware_timestamps"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# All (table, column) pairs that need conversion.
# Derived from information_schema inspection of the live gateway database.
COLUMNS_TO_MIGRATE: list[tuple[str, str]] = [
    ("ssh_sessions", "connected_at"),
    ("ssh_sessions", "last_activity"),
    ("ssh_sessions", "expires_at"),
    ("event_hooks", "created_at"),
    ("event_hooks", "updated_at"),
    ("webhook_deliveries", "next_retry_at"),
    ("webhook_deliveries", "leased_at"),
    ("webhook_deliveries", "created_at"),
    ("webhook_deliveries", "updated_at"),
]

# Conditional columns — only migrate if the table exists (lazy-created by known_hosts.py).
CONDITIONAL_COLUMNS: list[tuple[str, str]] = [
    ("ssh_host_keys", "updated_at"),
]


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


def _column_is_tz_naive(table: str, column: str) -> bool:
    """Check if a column is timestamp without time zone via information_schema."""
    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = :table "
            "AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    row = result.fetchone()
    return row is not None and row[0] == "timestamp without time zone"


def upgrade() -> None:
    """Convert timestamp without time zone → timestamp with time zone.

    Uses AT TIME ZONE 'UTC' to interpret existing naive values as UTC
    and produce proper TIMESTAMPTZ values. This is safe because all
    application code uses datetime.now(UTC).
    """
    for table, column in COLUMNS_TO_MIGRATE:
        if _column_is_tz_naive(table, column):
            op.execute(
                text(
                    f"ALTER TABLE {table} ALTER COLUMN {column} "
                    f"TYPE timestamp with time zone "
                    f"USING {column} AT TIME ZONE 'UTC'"
                )
            )

    # Conditional: ssh_host_keys is lazy-created by known_hosts.py and may not exist yet.
    for table, column in CONDITIONAL_COLUMNS:
        if _table_exists(table) and _column_is_tz_naive(table, column):
            op.execute(
                text(
                    f"ALTER TABLE {table} ALTER COLUMN {column} "
                    f"TYPE timestamp with time zone "
                    f"USING {column} AT TIME ZONE 'UTC'"
                )
            )


def downgrade() -> None:
    """Revert timestamp with time zone → timestamp without time zone.

    Strips timezone info. Existing UTC values are preserved as naive.
    """
    for table, column in COLUMNS_TO_MIGRATE:
        op.execute(
            text(
                f"ALTER TABLE {table} ALTER COLUMN {column} "
                f"TYPE timestamp without time zone "
                f"USING {column} AT TIME ZONE 'UTC'"
            )
        )

    # Conditional: only downgrade if the table exists.
    for table, column in CONDITIONAL_COLUMNS:
        if _table_exists(table):
            op.execute(
                text(
                    f"ALTER TABLE {table} ALTER COLUMN {column} "
                    f"TYPE timestamp without time zone "
                    f"USING {column} AT TIME ZONE 'UTC'"
                )
            )
