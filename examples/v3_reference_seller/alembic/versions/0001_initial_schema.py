"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-03

Generated from the dev schema — review before applying to production.
Captures all five tables declared by the v3 reference seller:
  tenants, buyer_agents, accounts, media_buys, audit_events.

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("ext", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'archived')",
            name="tenants_status_ck",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host"),
    )
    op.create_index("tenants_host_idx", "tenants", ["host"])

    op.create_table(
        "buyer_agents",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_url", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("billing_capabilities", sa.JSON(), nullable=False),
        sa.Column("api_key_id", sa.String(128), nullable=True),
        sa.Column("default_terms", sa.JSON(), nullable=True),
        sa.Column("allowed_brands", sa.JSON(), nullable=True),
        sa.Column("ext", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'blocked')",
            name="buyer_agents_status_ck",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "agent_url", name="buyer_agents_tenant_agent_uk"),
    )
    op.create_index("buyer_agents_tenant_idx", "buyer_agents", ["tenant_id"])
    op.create_index(
        "buyer_agents_api_key_idx",
        "buyer_agents",
        ["api_key_id"],
        postgresql_where=sa.text("api_key_id IS NOT NULL"),
    )

    op.create_table(
        "accounts",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("buyer_agent_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("billing", sa.String(32), nullable=True),
        sa.Column("billing_entity", sa.JSON(), nullable=True),
        sa.Column("reporting_bucket", sa.JSON(), nullable=True),
        sa.Column("rate_card", sa.String(64), nullable=True),
        sa.Column("payment_terms", sa.String(32), nullable=True),
        sa.Column("credit_limit", sa.JSON(), nullable=True),
        sa.Column("sandbox", sa.Boolean(), nullable=False),
        sa.Column("ext", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'pending_approval', 'suspended', 'closed')",
            name="accounts_status_ck",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["buyer_agent_id"], ["buyer_agents.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "account_id", name="accounts_tenant_acct_uk"),
    )
    op.create_index("accounts_tenant_idx", "accounts", ["tenant_id"])
    op.create_index("accounts_buyer_agent_idx", "accounts", ["buyer_agent_id"])

    op.create_table(
        "media_buys",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("media_buy_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("brand_domain", sa.String(255), nullable=True),
        sa.Column("total_budget", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_snapshot", sa.JSON(), nullable=True),
        sa.Column("response_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("media_buy_id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="media_buys_idem_uk"),
    )
    op.create_index("media_buys_tenant_idx", "media_buys", ["tenant_id"])
    op.create_index("media_buys_account_idx", "media_buys", ["account_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("caller_identity", sa.String(512), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("error_message", sa.String(255), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "audit_events_tenant_idx", "audit_events", ["tenant_id", "occurred_at"]
    )
    op.create_index(
        "audit_events_operation_idx", "audit_events", ["operation", "occurred_at"]
    )
    op.create_index(
        "audit_events_caller_idx", "audit_events", ["caller_identity", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("media_buys")
    op.drop_table("accounts")
    op.drop_table("buyer_agents")
    op.drop_table("tenants")
