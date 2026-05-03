"""broadening cycle: invoice_recipient, creatives, performance_feedback

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-03

Adds the three schema additions introduced alongside the v3 broadening
cycle (PR #408):

  - media_buys.invoice_recipient  — JSON, nullable; per-buy invoice
                                    override (bank details write-only
                                    on response, durable on storage)
  - creatives                     — seller-side creative registry;
                                    idempotency-keyed on
                                    (tenant_id, creative_id)
  - performance_feedback          — buyer-supplied performance signals
                                    FK'd to media_buys.id

Downgrade reverses all three steps safely (performance_feedback first
to satisfy FK order, then creatives, then the column drop).

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add invoice_recipient to media_buys.
    op.add_column(
        "media_buys",
        sa.Column("invoice_recipient", sa.JSON(), nullable=True),
    )

    # 2. Create creatives table.
    op.create_table(
        "creatives",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("creative_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("format_id", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "creative_id", name="creatives_tenant_creative_uk"
        ),
    )
    op.create_index("creatives_tenant_idx", "creatives", ["tenant_id"])
    op.create_index("creatives_account_idx", "creatives", ["account_id"])

    # 3. Create performance_feedback table.
    op.create_table(
        "performance_feedback",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("media_buy_id", sa.BigInteger(), nullable=False),
        sa.Column("feedback_type", sa.String(64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["media_buy_id"], ["media_buys.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "performance_feedback_tenant_idx", "performance_feedback", ["tenant_id"]
    )
    op.create_index(
        "performance_feedback_media_buy_idx",
        "performance_feedback",
        ["media_buy_id"],
    )


def downgrade() -> None:
    op.drop_table("performance_feedback")
    op.drop_table("creatives")
    op.drop_column("media_buys", "invoice_recipient")
