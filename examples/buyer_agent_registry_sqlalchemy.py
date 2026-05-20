"""Example: SQLAlchemy-backed :class:`BuyerAgentRegistry` for v3 sellers.

The reference adopter pattern for the v3-identity Tier 2 commercial
identity layer. Wraps a SQLAlchemy session over a ``buyer_agents``
table behind the
:class:`adcp.decisioning.BuyerAgentRegistry` Protocol so the framework
can gate every request on the seller's commercial allowlist
(allowlist + onboarding state + billing capabilities) BEFORE the
platform method runs.

**Why this exists.** AdCP v3 introduced a three-way identity split
(agent / operator / brand). The cryptographic side (HTTP-Signatures
verification, brand.json resolution, JWKS) is in
:mod:`adcp.signing`. The commercial side — "is this counterparty on
our allowlist? are they suspended? are they billable agent-direct?"
— is the registry. Pre-trust beta sellers (today: salesagent,
trafficker-driven sellers) implement it bearer-only against their
existing key table; production-target sellers run signing-only.

Adopters fork this file — replace the model class with their own
SQLAlchemy schema (salesagent's ``Principal`` table, or a
``buyer_agents`` table fitted to your app), and the wrapper functions
keep the same shape.

**Two factory shapes, one wrapper.**

* :func:`build_signing_only_registry` — production-target. The
  framework hands the wrapper a verified ``agent_url``; it looks up
  by that string. Bearer / API-key traffic is rejected at the
  registry layer with no extra config.
* :func:`build_bearer_only_registry` — pre-trust beta. The wrapper
  receives an :class:`ApiKeyCredential` or :class:`OAuthCredential`
  and matches against the existing key table.

For the migration window, :func:`adcp.decisioning.mixed_registry`
combines both — see the Tier 2 RFC for the migration play-by-play.

**Security model.**

* The framework has already cryptographically validated signed
  traffic before calling :meth:`resolve_by_agent_url`; the wrapper
  does NOT re-verify the signature. The wrapper's job is the
  commercial lookup only.
* :attr:`BuyerAgent.billing_capabilities` is enforced by the
  framework's ``sync_accounts`` shim. Adopters store the set as a
  ``billing_capabilities`` column (JSON, ARRAY, or comma-separated
  string + parser) and project it to ``frozenset[BillingMode]``
  inside the wrapper.
* Suspension / blocking is a database-side state. Updating
  ``status="suspended"`` in the row cuts the agent off on the next
  request — no cache invalidation, no restart.

Run::

    uv run python examples/buyer_agent_registry_sqlalchemy.py

The script seeds three buyer agents (active, suspended, mixed-billing
agency) and exercises both registry shapes against an in-memory
SQLite DB.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

try:
    from sqlalchemy import String, create_engine, select
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
    from sqlalchemy.pool import StaticPool
except ImportError as exc:
    raise SystemExit(
        "This example requires SQLAlchemy. Install with `uv pip install sqlalchemy`."
    ) from exc

from adcp.decisioning import (
    ApiKeyCredential,
    BillingMode,
    BuyerAgent,
    BuyerAgentDefaultTerms,
    BuyerAgentRegistry,
    BuyerAgentStatus,
    OAuthCredential,
    bearer_only_registry,
    mixed_registry,
    signing_only_registry,
)

# ---------------------------------------------------------------------------
# ORM model — one row per recognized buyer agent
# ---------------------------------------------------------------------------


class _Base(DeclarativeBase):
    """Local declarative base — adopters wire into their own."""


class BuyerAgentRow(_Base):
    """SQLAlchemy model for the seller's commercial allowlist.

    Real adopters extend this with seller-side columns (audit
    timestamps, internal tenant ids, contract refs, account-manager
    pointers). The fields below are the minimum the framework needs.
    """

    __tablename__ = "buyer_agents"

    #: AdCP v3 canonical identifier — verified at signature time for
    #: signed traffic, used as the bearer-table FK pointer for
    #: bearer traffic.
    agent_url: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)

    #: Lifecycle: active / suspended / blocked. Adopters update
    #: in-place to suspend / unblock.
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")

    #: JSON-encoded set of permitted ``BillingMode`` values. Stored as
    #: a JSON-string for backend portability (Postgres ARRAY would be
    #: cleaner on Postgres; this stays SQLite-friendly). The wrapper
    #: deserializes via ``json.loads`` and projects into a
    #: ``frozenset``.
    billing_capabilities_json: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=lambda: json.dumps(["operator"]),
    )

    #: Optional bearer-token id for pre-trust beta auth. Adopters in
    #: signing-only posture leave NULL.
    api_key_id: Mapped[str | None] = mapped_column(String, nullable=True)

    #: Default account terms applied when accounts are provisioned
    #: under this agent (rate card, payment terms, etc.). JSON-string
    #: for portability; structure mirrors
    #: :class:`BuyerAgentDefaultTerms`. Adopters with structured
    #: validation can swap the column for a Pydantic-validated JSON
    #: type.
    default_terms_json: Mapped[str | None] = mapped_column(String, nullable=True)


_ALLOWED_STATUSES = {"active", "suspended", "blocked"}


def _row_to_buyer_agent(row: BuyerAgentRow) -> BuyerAgent:
    """Project an ORM row to the framework :class:`BuyerAgent` shape.

    The wrapper boundary — anything stored as JSON / strings on the
    DB side gets projected to typed framework objects here. Validates
    ``status`` against the framework enum so a row with a typo'd
    state (``"ACTIVE"``, ``"deleted"``) raises at projection time
    instead of silently flowing through to the dispatch gate.
    """
    if row.status not in _ALLOWED_STATUSES:
        raise ValueError(
            f"BuyerAgentRow {row.agent_url!r} has invalid status "
            f"{row.status!r}; expected one of {sorted(_ALLOWED_STATUSES)!r}. "
            "Adopters with custom statuses extend the SDK's "
            "BuyerAgentStatus literal."
        )
    capabilities = cast(
        "frozenset[BillingMode]",
        frozenset(json.loads(row.billing_capabilities_json)),
    )
    terms: BuyerAgentDefaultTerms | None = None
    if row.default_terms_json:
        terms_dict = json.loads(row.default_terms_json)
        terms = BuyerAgentDefaultTerms(
            rate_card=terms_dict.get("rate_card"),
            payment_terms=terms_dict.get("payment_terms"),
            credit_limit=terms_dict.get("credit_limit"),
            billing_entity=terms_dict.get("billing_entity"),
        )
    return BuyerAgent(
        agent_url=row.agent_url,
        display_name=row.display_name,
        status=cast("BuyerAgentStatus", row.status),
        billing_capabilities=capabilities,
        default_account_terms=terms,
    )


# ---------------------------------------------------------------------------
# Registry factory wrappers
# ---------------------------------------------------------------------------


def build_signing_only_registry(session_factory: Any) -> BuyerAgentRegistry:
    """Wrap a SQLAlchemy session factory into a signing-only registry.

    Production-target shape: signed traffic resolves cryptographically;
    bearer traffic is refused at the registry layer.

    :param session_factory: Callable returning a new
        :class:`sqlalchemy.orm.Session`. Adopters using
        ``sessionmaker(bind=engine)`` pass that directly.
    """

    async def lookup(agent_url: str) -> BuyerAgent | None:
        # Run the sync DB call on a thread so the framework's async
        # dispatch loop isn't blocked. Production adopters using
        # ``AsyncSession`` + ``create_async_engine`` skip the
        # ``to_thread`` and ``await session.execute(...)`` directly.
        def _query() -> BuyerAgent | None:
            with session_factory() as session:
                row = session.execute(
                    select(BuyerAgentRow).where(BuyerAgentRow.agent_url == agent_url)
                ).scalar_one_or_none()
                return _row_to_buyer_agent(row) if row else None

        return await asyncio.to_thread(_query)

    return signing_only_registry(lookup)


def build_bearer_only_registry(session_factory: Any) -> BuyerAgentRegistry:
    """Wrap a SQLAlchemy session factory into a bearer-only registry.

    Pre-trust beta shape: API-key / OAuth traffic resolves against the
    seller's existing key table (here projected onto
    :attr:`BuyerAgentRow.api_key_id`); signed traffic is refused.
    """

    async def lookup(
        cred: ApiKeyCredential | OAuthCredential,
    ) -> BuyerAgent | None:
        def _query() -> BuyerAgent | None:
            with session_factory() as session:
                if isinstance(cred, ApiKeyCredential):
                    row = session.execute(
                        select(BuyerAgentRow).where(BuyerAgentRow.api_key_id == cred.key_id)
                    ).scalar_one_or_none()
                else:
                    # OAuth — adopters typically map ``client_id`` to
                    # an internal id. This example reuses ``api_key_id``
                    # for both; richer schemas split into separate
                    # ``oauth_client_id`` / ``api_key_id`` columns.
                    row = session.execute(
                        select(BuyerAgentRow).where(BuyerAgentRow.api_key_id == cred.client_id)
                    ).scalar_one_or_none()
                return _row_to_buyer_agent(row) if row else None

        return await asyncio.to_thread(_query)

    return bearer_only_registry(lookup)


def build_mixed_registry(session_factory: Any) -> BuyerAgentRegistry:
    """Migration-window registry — both methods wired against the
    same SQLAlchemy session factory."""

    async def by_agent_url(agent_url: str) -> BuyerAgent | None:
        def _query() -> BuyerAgent | None:
            with session_factory() as session:
                row = session.execute(
                    select(BuyerAgentRow).where(BuyerAgentRow.agent_url == agent_url)
                ).scalar_one_or_none()
                return _row_to_buyer_agent(row) if row else None

        return await asyncio.to_thread(_query)

    async def by_credential(
        cred: ApiKeyCredential | OAuthCredential,
    ) -> BuyerAgent | None:
        def _query() -> BuyerAgent | None:
            key = cred.key_id if isinstance(cred, ApiKeyCredential) else cred.client_id
            with session_factory() as session:
                row = session.execute(
                    select(BuyerAgentRow).where(BuyerAgentRow.api_key_id == key)
                ).scalar_one_or_none()
                return _row_to_buyer_agent(row) if row else None

        return await asyncio.to_thread(_query)

    return mixed_registry(
        resolve_by_agent_url=by_agent_url,
        resolve_by_credential=by_credential,
    )


# ---------------------------------------------------------------------------
# Demo: seed three agents and exercise every posture
# ---------------------------------------------------------------------------


def _seed(session_factory: Any) -> None:
    """Populate the demo DB with three representative buyer agents."""
    with session_factory() as session:
        session.add_all(
            [
                BuyerAgentRow(
                    agent_url="https://acme-buyer.example/",
                    display_name="Acme Buyer Agent",
                    status="active",
                    billing_capabilities_json=json.dumps(["operator"]),
                    api_key_id="acme-key-1",
                ),
                BuyerAgentRow(
                    agent_url="https://suspended-buyer.example/",
                    display_name="Suspended Buyer",
                    status="suspended",
                    billing_capabilities_json=json.dumps(["operator"]),
                ),
                BuyerAgentRow(
                    agent_url="https://hybrid-agency.example/",
                    display_name="Hybrid Agency",
                    status="active",
                    # Multi-mode: agency direct-billed for owned brands,
                    # operator-passthrough for agency-mediated brands.
                    billing_capabilities_json=json.dumps(["operator", "agent"]),
                    default_terms_json=json.dumps(
                        {
                            "rate_card": "agency-2026",
                            "payment_terms": "NET30",
                        }
                    ),
                ),
            ]
        )
        session.commit()


async def _demo() -> None:
    # ``StaticPool`` + ``check_same_thread=False`` so the in-memory
    # SQLite DB is shared across the connections that
    # :func:`asyncio.to_thread` opens — without this, each lookup
    # would land on a fresh in-memory DB and the seeded rows
    # would be invisible.
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    _Base.metadata.create_all(engine)

    def session_factory() -> Session:
        return Session(engine)

    _seed(session_factory)

    print("== signing-only registry ==")
    signing = build_signing_only_registry(session_factory)
    hit = await signing.resolve_by_agent_url("https://acme-buyer.example/")
    print(f"  acme: {hit!r}")
    miss = await signing.resolve_by_agent_url("https://unknown.example/")
    print(f"  unknown: {miss!r}")
    bearer_refused = await signing.resolve_by_credential(
        ApiKeyCredential(kind="api_key", key_id="acme-key-1")
    )
    print(f"  bearer refused: {bearer_refused!r}")  # None — production posture

    print("\n== bearer-only registry ==")
    bearer = build_bearer_only_registry(session_factory)
    hit = await bearer.resolve_by_credential(ApiKeyCredential(kind="api_key", key_id="acme-key-1"))
    print(f"  acme via key: {hit!r}")
    signed_refused = await bearer.resolve_by_agent_url("https://acme-buyer.example/")
    print(f"  signed refused: {signed_refused!r}")  # None — pre-trust posture

    print("\n== mixed registry ==")
    mixed = build_mixed_registry(session_factory)
    via_url = await mixed.resolve_by_agent_url("https://hybrid-agency.example/")
    print(f"  hybrid via url: {via_url!r}")
    print(f"  billing_capabilities: {sorted(via_url.billing_capabilities)}")
    print(f"  default_terms: {via_url.default_account_terms!r}")

    print("\n== suspended agent ==")
    suspended = await signing.resolve_by_agent_url("https://suspended-buyer.example/")
    print(f"  status: {suspended.status!r}")
    print("  → framework dispatch raises " "AdcpError(code='AGENT_SUSPENDED', recovery='terminal')")


if __name__ == "__main__":
    asyncio.run(_demo())
