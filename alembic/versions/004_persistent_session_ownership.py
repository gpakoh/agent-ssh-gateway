"""Persist SSH session ownership metadata across gateway restarts.

Revision ID: 004_persistent_session_ownership
Revises: 003_webhook_delivery_headers
Create Date: 2026-08-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "004_persistent_session_ownership"
down_revision: str | None = "003_webhook_delivery_headers"
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


def _create_ssh_sessions() -> None:
    op.create_table(
        "ssh_sessions",
        sa.Column("session_id", sa.String(length=36), primary_key=True),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True, server_default="22"),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=True),
        sa.Column("private_key_encrypted", sa.Text(), nullable=True),
        sa.Column("key_passphrase_encrypted", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True, server_default=sa.true()),
        sa.Column("reconnect_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("owner_type", sa.String(length=32), nullable=False, server_default="master"),
        sa.Column("owner_name", sa.String(length=255), nullable=True),
        sa.Column("owner_token_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("tenant_labels", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
    )


def upgrade() -> None:
    if not _table_exists("ssh_sessions"):
        _create_ssh_sessions()
        return

    if not _column_exists("ssh_sessions", "owner_type"):
        op.add_column(
            "ssh_sessions",
            sa.Column(
                "owner_type",
                sa.String(length=32),
                nullable=False,
                server_default="master",
            ),
        )
    if not _column_exists("ssh_sessions", "owner_name"):
        op.add_column(
            "ssh_sessions", sa.Column("owner_name", sa.String(length=255), nullable=True)
        )
    if not _column_exists("ssh_sessions", "owner_token_fingerprint"):
        op.add_column(
            "ssh_sessions",
            sa.Column("owner_token_fingerprint", sa.String(length=64), nullable=True),
        )
    if not _column_exists("ssh_sessions", "source_ip"):
        op.add_column(
            "ssh_sessions", sa.Column("source_ip", sa.String(length=64), nullable=True)
        )
    if not _column_exists("ssh_sessions", "tenant_labels"):
        op.add_column(
            "ssh_sessions", sa.Column("tenant_labels", sa.JSON(), nullable=True)
        )

    # Rows created before this revision have no trustworthy ownership
    # fingerprint. They must never be revived as master-owned sessions after
    # upgrade; fail closed and require an explicit reconnect under the new
    # schema instead.
    op.execute(
        text(
            "UPDATE ssh_sessions SET is_active = false "
            "WHERE is_active = true AND owner_token_fingerprint IS NULL"
        )
    )


def downgrade() -> None:
    if not _table_exists("ssh_sessions"):
        return
    for column in (
        "tenant_labels",
        "source_ip",
        "owner_token_fingerprint",
        "owner_name",
        "owner_type",
    ):
        if _column_exists("ssh_sessions", column):
            op.drop_column("ssh_sessions", column)
