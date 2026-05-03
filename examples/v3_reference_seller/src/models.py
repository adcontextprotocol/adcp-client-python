"""SQLAlchemy models for the v3 reference seller.

The schema is **3.0-compliant on the wire, 3.1-ready in architecture
and storage**. Adopters fork this file and extend the columns with
their own seller-side audit / contract / billing fields.

Four tables make up the spine:

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
  delivery target).
* :class:`MediaBuy` — terminal artifact of ``create_media_buy``.
  Idempotency-keyed for replay safety.

Admin API and protocol-side audit log live in separate tables
(:mod:`audit` ships :class:`AuditEvent`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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

    All downstream tables (buyer agents, accounts, media buys, audit
    events) FK back to :attr:`Tenant.id` so a single Postgres
    instance hosts multiple tenants without per-tenant table sharding.
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
# Account — 3.1-ready buyer account
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


# ---------------------------------------------------------------------------
# MediaBuy — terminal artifact of create_media_buy
# ---------------------------------------------------------------------------


class MediaBuy(Base):
    """Terminal artifact of ``create_media_buy``.

    Idempotency-keyed for replay safety — the framework's idempotency
    middleware caches by ``(scope_key, idempotency_key)`` and replays
    the same response. This row is what the platform method returns
    on the canonical insert.

    Row state mirrors the spec's :class:`MediaBuyStatus` literal.
    """

    __tablename__ = "media_buys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )

    #: Wire ``media_buy_id`` returned to the buyer.
    media_buy_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    #: The buyer's idempotency key for ``create_media_buy``.
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    brand_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    request_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    #: Per-buy invoice override. When the buyer supplies
    #: ``CreateMediaBuyRequest.invoice_recipient`` (a
    #: :class:`adcp.types.BusinessEntity`), the seller persists the
    #: full payload here — bank details included — so invoicing can
    #: route to a recipient different from the account default. The
    #: column is response-projected through
    #: :func:`adcp.decisioning.project_business_entity_for_response`
    #: before serialization (write-only ``bank``).
    invoice_recipient: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="media_buys_idem_uk"),
        Index("media_buys_tenant_idx", "tenant_id"),
        Index("media_buys_account_idx", "account_id"),
    )


# ---------------------------------------------------------------------------
# Creative — seller-side view of buyer-uploaded creatives
# ---------------------------------------------------------------------------


class Creative(Base):
    """Seller-side projection of a buyer-uploaded creative.

    Populated by ``sync_creatives``; surfaced by ``list_creatives``.
    Idempotency is keyed on ``(tenant_id, creative_id)`` so a buyer
    re-syncing the same creative under the same wire id updates the
    existing row in place.

    The full creative manifest (assets, format parameters, tags) is
    persisted in ``manifest_json`` — production adopters split the hot
    fields (format_id, status) into typed columns and route the rest
    to a creative-management service.
    """

    __tablename__ = "creatives"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"cr_{uuid.uuid4().hex[:12]}"
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )

    #: Wire ``creative_id`` provided by the buyer.
    creative_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Format reference — stored as the structured object
    #: ``{agent_url, id}`` from the spec. We persist the JSON shape so
    #: adopters can layer on parameterized template formats without a
    #: column migration.
    format_id: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    #: Spec ``CreativeStatus`` — pending_review / approved / rejected /
    #: archived / processing.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="approved")

    #: Full creative manifest (assets, tags, ext) — projection-time
    #: shape kept opaque so spec evolution doesn't force migrations.
    manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "creative_id", name="creatives_tenant_creative_uk"),
        Index("creatives_tenant_idx", "tenant_id"),
        Index("creatives_account_idx", "account_id"),
    )


# ---------------------------------------------------------------------------
# PerformanceFeedback — buyer-supplied performance signal
# ---------------------------------------------------------------------------


class PerformanceFeedback(Base):
    """Persisted record of a ``provide_performance_feedback`` call.

    Buyer-supplied attribution / measurement signals route into this
    table for downstream optimization. ``value`` carries the full
    request payload (performance_index, metric_type, package_id,
    creative_id, measurement_period) so adopters can backfill new
    dimensions without column migrations.
    """

    __tablename__ = "performance_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    media_buy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("media_buys.id", ondelete="CASCADE"), nullable=False
    )

    #: Spec ``MetricType`` — overall_performance / conversion_rate /
    #: ctr / brand_safety / etc.
    feedback_type: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Full request payload (performance_index, period bounds, source).
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("performance_feedback_tenant_idx", "tenant_id"),
        Index("performance_feedback_media_buy_idx", "media_buy_id"),
    )


__all__ = [
    "Account",
    "Base",
    "BuyerAgent",
    "Creative",
    "MediaBuy",
    "PerformanceFeedback",
    "Tenant",
]
