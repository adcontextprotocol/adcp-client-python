"""SQLAlchemy models for the v3 reference seller.

The reference seller demonstrates the **translator pattern**: AdCP
wire on the inside, a real upstream ad server (the JS mock-server
shipped in ``@adcp/client``, GAM-flavored) on the outside. Ad-ops
data — orders / line items / creatives / delivery — lives upstream.
The local Postgres only stores the *commercial-identity* layer:
which buyer agent is allowed to talk to us, which account they map
to upstream, what billing terms apply.

Three tables make up the spine:

* :class:`Tenant` — multi-tenant root. The
  :class:`adcp.server.SubdomainTenantMiddleware` resolves
  ``Host: <subdomain>.example.com`` to a row here and threads the
  tenant id onto the request scope.
* :class:`BuyerAgent` — Tier 2 commercial-identity record. The
  framework's :class:`adcp.decisioning.BuyerAgentRegistry` reads
  this row before every dispatch to gate on
  ``status`` + ``billing_capabilities``.
* :class:`Account` — buyer-side account under a recognized agent.
  Carries 3.1-ready columns ``billing_entity`` (write-only bank
  details — projection-guarded) and ``reporting_bucket`` (offline
  delivery target). The ``ext`` column maps the AdCP account to the
  upstream ad server's ``network_code`` + ``advertiser_id`` — this
  is the translation seam.

Admin API and protocol-side audit log live in separate tables
(:mod:`audit` ships :class:`AuditEvent`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Project-local declarative base.

    Adopters with an existing SQLAlchemy app integrate by either
    re-using their own ``Base`` (replace this import) or composing
    their models alongside via metadata-merging at boot.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Tenant — multi-tenant root
# ---------------------------------------------------------------------------


class Tenant(Base):
    """Operator-owned tenant. One row per ``<subdomain>.example.com``.

    The ``host`` column is the lookup key for
    :class:`adcp.server.SubdomainTenantRouter` — the middleware reads
    the request's ``Host`` header (lower-cased, port-stripped) and
    finds the matching row.

    All downstream tables (buyer agents, accounts, audit events) FK
    back to :attr:`Tenant.id` so a single Postgres instance hosts
    multiple tenants without per-tenant table sharding.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"t_{uuid.uuid4().hex[:12]}"
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
    )

    # Operator-side branding / config the framework doesn't model.
    ext: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended', 'archived')", name="tenants_status_ck"),
        Index("tenants_host_idx", "host"),
    )

    buyer_agents: Mapped[list[BuyerAgent]] = relationship(
        "BuyerAgent", back_populates="tenant", cascade="all, delete-orphan"
    )
    accounts: Mapped[list[Account]] = relationship(
        "Account", back_populates="tenant", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# BuyerAgent — Tier 2 commercial identity
# ---------------------------------------------------------------------------


class BuyerAgent(Base):
    """Tier 2 commercial-identity record per recognized buyer agent.

    Tenant-scoped so the same ``agent_url`` can have different
    commercial postures across tenants (e.g., active under one tenant,
    suspended under another). The reference
    :class:`adcp.decisioning.PgBuyerAgentRegistry` looks up rows by
    ``agent_url`` only — for tenant-scoped enforcement, this seller
    layers its own
    :class:`buyer_registry.TenantScopedBuyerAgentRegistry` on top.

    ``billing_capabilities`` is the framework-enforced
    ``frozenset[BillingMode]`` from the spec — stored as a native
    JSON array column and projected to ``frozenset`` at the
    registry seam.
    """

    __tablename__ = "buyer_agents"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"ba_{uuid.uuid4().hex[:12]}"
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    #: AdCP v3 canonical identifier — the buyer agent's well-known
    #: URL. Used as the dispatch key for HTTP-Signatures-verified
    #: traffic (the framework calls
    #: ``BuyerAgentRegistry.resolve_by_agent_url`` with this value).
    agent_url: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Lifecycle: active / suspended / blocked. The framework's
    #: dispatch layer rejects suspended (transient) and blocked
    #: (terminal) before the platform method runs.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    #: List of permitted ``BillingMode`` values. Default ``["operator"]``
    #: is the pre-trust passthrough posture — agents under this row
    #: get operator-billed accounts only.
    billing_capabilities: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: ["operator"]
    )

    #: Pre-trust beta API key. Adopters running signing-only auth
    #: leave NULL.
    api_key_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Default account terms (rate card, payment terms, credit limit,
    #: billing entity FK).
    default_terms: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    #: Pre-RFC allowlist of brand domains. Layered with the (Tier 3)
    #: ``BrandAuthorizationResolver`` once spec #3690 lands.
    allowed_brands: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    #: Adopter passthrough — internal ids, contract refs, etc.
    ext: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'blocked')", name="buyer_agents_status_ck"
        ),
        UniqueConstraint("tenant_id", "agent_url", name="buyer_agents_tenant_agent_uk"),
        Index("buyer_agents_tenant_idx", "tenant_id"),
        Index(
            "buyer_agents_api_key_idx",
            "api_key_id",
            postgresql_where=(api_key_id.is_not(None)),  # type: ignore[has-type]
        ),
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="buyer_agents")


# ---------------------------------------------------------------------------
# Account — 3.1-ready buyer account; carries upstream routing in ext
# ---------------------------------------------------------------------------


class Account(Base):
    """Buyer-side account under a recognized agent.

    Carries the spec 3.1-ready columns:

    * ``billing_entity`` — full :class:`adcp.types.BusinessEntity`
      blob including bank details (write-only on response per spec).
      The seller projects through
      :func:`adcp.types.project_account_for_response` before
      serializing on the wire.
    * ``reporting_bucket`` — offline-reporting delivery target.

    ``ext`` carries the **translator pattern routing** — for the
    reference seller this is ``{"network_code": "...",
    "advertiser_id": "..."}``, the keys the upstream JS mock-server
    requires on the ``X-Network-Code`` header and order body
    respectively. Adopters with their own upstream replace these
    keys with their ad server's identifiers (GAM ``networkCode`` +
    ``advertiserId``, FreeWheel ``customerId`` + ``advertiserId``,
    etc.).

    ``billing`` carries the spec ``BillingParty`` enum (operator /
    agent / advertiser); the framework's ``sync_accounts`` dispatch
    rejects mismatches against
    :attr:`BuyerAgent.billing_capabilities` before the platform
    method runs.
    """

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"a_{uuid.uuid4().hex[:12]}"
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    buyer_agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("buyer_agents.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: Wire ``account_id`` — what the buyer puts in
    #: ``request.account.account_id``. Stable across renames.
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    #: Billing party — operator / agent / advertiser.
    billing: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Full BusinessEntity JSON including bank details (write-only on
    #: response). Adopters MUST project through
    #: :func:`adcp.types.project_account_for_response` before
    #: serializing on any response payload.
    billing_entity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    #: ReportingBucket JSON — offline reporting target.
    reporting_bucket: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    rate_card: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(32), nullable=True)
    credit_limit: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Translator-pattern routing — ``{"network_code": "...",
    #: "advertiser_id": "..."}``. Read onto ``ctx.account.metadata``
    #: by :func:`platform._make_account_store` so platform method
    #: bodies pass ``network_code`` to :mod:`upstream` helpers without
    #: a second query.
    ext: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'pending_approval', 'suspended', 'closed')",
            name="accounts_status_ck",
        ),
        UniqueConstraint("tenant_id", "account_id", name="accounts_tenant_acct_uk"),
        Index("accounts_tenant_idx", "tenant_id"),
        Index("accounts_buyer_agent_idx", "buyer_agent_id"),
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="accounts")


__all__ = [
    "Account",
    "Base",
    "BuyerAgent",
    "Tenant",
]
