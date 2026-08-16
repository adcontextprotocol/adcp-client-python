"""require globally unique buyer-agent bearer identifiers

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-29

``api_key_id`` is a bearer credential, so it must identify exactly one
commercial identity and tenant. The preflight deliberately stops the
migration before changing indexes when legacy duplicates exist; operators
must rotate or remove duplicates rather than letting the database choose an
arbitrary owner.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not context.is_offline_mode():
        connection = op.get_bind()
        duplicate_count = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM ("
                "SELECT api_key_id FROM buyer_agents "
                "WHERE api_key_id IS NOT NULL "
                "GROUP BY api_key_id HAVING COUNT(*) > 1"
                ") AS duplicate_credentials"
            )
        ).scalar_one()
        if duplicate_count:
            raise RuntimeError(
                "Cannot enforce buyer_agents.api_key_id uniqueness: "
                f"found {duplicate_count} duplicated credential identifier(s). "
                "Rotate or remove duplicate bearer credentials, then rerun the migration."
            )

    # Create first so a concurrent/legacy duplicate makes the migration fail
    # while the old lookup index remains available. PostgreSQL then rolls the
    # transaction back; offline SQL retains the same safe ordering.
    op.create_index(
        "buyer_agents_api_key_uidx",
        "buyer_agents",
        ["api_key_id"],
        unique=True,
        postgresql_where=sa.text("api_key_id IS NOT NULL"),
        sqlite_where=sa.text("api_key_id IS NOT NULL"),
    )
    op.drop_index("buyer_agents_api_key_idx", table_name="buyer_agents")


def downgrade() -> None:
    op.drop_index("buyer_agents_api_key_uidx", table_name="buyer_agents")
    op.create_index(
        "buyer_agents_api_key_idx",
        "buyer_agents",
        ["api_key_id"],
        unique=False,
        postgresql_where=sa.text("api_key_id IS NOT NULL"),
        sqlite_where=sa.text("api_key_id IS NOT NULL"),
    )
