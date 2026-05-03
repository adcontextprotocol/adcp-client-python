"""DecisioningPlatform impl for the v3 reference seller.

Sales-non-guaranteed specialism with seven implemented methods:

* :meth:`get_products` — read inventory catalog
* :meth:`create_media_buy` — terminal artifact insert; idempotency-keyed
* :meth:`update_media_buy` — patch (status / pause / spend cap)
* :meth:`sync_creatives` — accept creative manifests
* :meth:`get_media_buy_delivery` — read delivery actuals
* :meth:`sync_accounts` — upsert accounts; billing_entity stored with bank, stripped on response
* :meth:`list_accounts` — list accounts projected through the write-only guard

All seven run against the SQLAlchemy models in :mod:`models`. The
platform reads the resolved :class:`adcp.decisioning.BuyerAgent`
from :attr:`RequestContext.buyer_agent` (set by the framework's
dispatch gate) and the :class:`adcp.decisioning.Account` from
:attr:`RequestContext.account` (set by the platform's
``AccountStore``) — both already filtered to the active tenant via
:func:`adcp.server.current_tenant`.

This file is the bulk of what an adopter customizes. Everything
else (auth verifier, registry, audit sink, tenant routing) is
boilerplate the seller wires once and forgets.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from adcp.decisioning import (
    Account,
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    ExplicitAccounts,
    project_account_for_response,
    project_business_entity_for_response,
)
from adcp.decisioning.specialisms import SalesPlatform
from adcp.server import current_tenant
from adcp.types import (
    Account as AdcpAccount,
    CreateMediaBuyRequest,
    CreateMediaBuySuccessResponse,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetProductsRequest,
    GetProductsResponse,
    ListAccountsRequest,
    ListAccountsResponse,
    Product,
    SyncAccountsRequest,
    SyncAccountsSuccessResponse,
    SyncCreativesRequest,
    SyncCreativesSuccessResponse,
    UpdateMediaBuyRequest,
    UpdateMediaBuySuccessResponse,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from adcp.decisioning import RequestContext

from .models import Account as AccountRow
from .models import BuyerAgent as BuyerAgentRow
from .models import MediaBuy as MediaBuyRow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AccountStore — explicit (wire ref drives lookup)
# ---------------------------------------------------------------------------


def _make_account_store(sessionmaker: async_sessionmaker) -> ExplicitAccounts:
    """Adopter ``AccountStore`` — resolves
    ``request.account.account_id`` against the ``accounts`` table.

    The framework calls this BEFORE the platform method runs.
    Returns the typed :class:`Account` dataclass that lands on
    :attr:`RequestContext.account`.

    Tenant scoping happens implicitly: the request's tenant is
    pinned by :class:`SubdomainTenantMiddleware`, threads onto
    :attr:`ToolContext.tenant_id`, and we filter accounts by it
    here.
    """

    async def loader(account_id: str) -> Account[dict[str, Any]]:
        # Read tenant from the contextvar set by the middleware.
        from adcp.server import current_tenant

        tenant = current_tenant()
        if tenant is None:
            raise AdcpError(
                "AUTH_INVALID",
                message=(
                    "AccountStore.resolve called without a tenant context. "
                    "Wire the SubdomainTenantMiddleware before serve()."
                ),
                recovery="terminal",
            )
        async with sessionmaker() as session:
            result = await session.execute(
                select(AccountRow).where(
                    AccountRow.tenant_id == tenant.id,
                    AccountRow.account_id == account_id,
                )
            )
            row = result.scalar_one_or_none()
        if row is None or row.status != "active":
            raise AdcpError(
                "ACCOUNT_NOT_FOUND",
                message=f"No active account {account_id!r} under tenant {tenant.id!r}.",
                recovery="terminal",
                field="account.account_id",
            )
        return Account(
            id=row.id,
            name=row.name,
            status=row.status,
            metadata={
                "tenant_id": row.tenant_id,
                "buyer_agent_id": row.buyer_agent_id,
                "account_id": row.account_id,
                "billing": row.billing,
                "sandbox": row.sandbox,
            },
        )

    return ExplicitAccounts(loader=loader)


# ---------------------------------------------------------------------------
# Platform — sales-non-guaranteed
# ---------------------------------------------------------------------------


class V3ReferenceSeller(DecisioningPlatform, SalesPlatform):
    """Sales-non-guaranteed seller against the v3 reference schema.

    Every method body reads :attr:`RequestContext.buyer_agent` (the
    Tier 2 commercial-identity record) and :attr:`account` (the
    resolved account for this request). Both are populated by the
    framework's dispatch layer before the method runs.
    """

    capabilities = DecisioningCapabilities(
        specialisms=("sales-non-guaranteed",),
        channels=("display", "video"),
        pricing_models=("cpm",),
    )

    def __init__(self, *, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker
        self.accounts = _make_account_store(sessionmaker)

    # ----- get_products ----------------------------------------------------

    async def get_products(
        self, req: GetProductsRequest, ctx: RequestContext
    ) -> GetProductsResponse:
        """Static product catalog for the reference seller. Real
        adopters query a CMS / forecasting service."""
        del req, ctx  # this reference impl ignores brief / context
        return GetProductsResponse(
            products=[
                Product(
                    product_id="display-run-of-network",
                    name="Display run-of-network",
                    delivery_type="non_guaranteed",
                    creative_policy={
                        "co_branding": "neither",
                        "landing_page": "any",
                    },
                    # Conformant CpmPricingOption shape: discriminator
                    # ``pricing_model`` (not ``type``), required
                    # ``pricing_option_id``, ``fixed_price`` (not
                    # ``rate``). See the spec's
                    # ``pricing_options/cpm_option.json``.
                    pricing_options=[
                        {
                            "pricing_option_id": "ron-cpm-5usd",
                            "pricing_model": "cpm",
                            "currency": "USD",
                            "fixed_price": 5.00,
                        }
                    ],
                )
            ]
        )

    # ----- create_media_buy ------------------------------------------------

    async def create_media_buy(
        self, req: CreateMediaBuyRequest, ctx: RequestContext
    ) -> CreateMediaBuySuccessResponse:
        """Insert the canonical media-buy row.

        Idempotency-keyed: the framework's outer middleware caches by
        ``(scope_key, idempotency_key)`` and serves the cached
        response on retry. We additionally enforce uniqueness at the
        DB level via ``UniqueConstraint(tenant_id, idempotency_key)``
        so a misconfigured cache can't double-insert.
        """
        if ctx.buyer_agent is None or ctx.account is None:
            raise AdcpError(
                "INTERNAL_ERROR",
                message="Dispatch should have populated buyer_agent and account.",
                recovery="terminal",
            )
        # The (tenant_id, idempotency_key) unique constraint already
        # enforces replay safety; the public id just needs to be
        # globally unique. Don't derive from the idempotency key —
        # a 16-hex prefix of a UUID v4 collides at scale, throwing
        # IntegrityError on the unique constraint over media_buy_id.
        media_buy_id = f"mb_{uuid.uuid4().hex}"
        # CreateMediaBuyRequest fields:
        #   total_budget: TotalBudget | None  (with .amount + .currency)
        #   start_time: StartTiming           (root: 'asap' | AwareDatetime)
        #   end_time:   AwareDatetime
        # Project at the seam — the SQL columns are flat float / str /
        # datetime so the platform owns the unwrapping.
        budget_amount = req.total_budget.amount if req.total_budget else None
        budget_currency = req.total_budget.currency if req.total_budget else None
        start_dt = _project_start_time(req.start_time)
        row = MediaBuyRow(
            tenant_id=ctx.account.metadata["tenant_id"],
            account_id=ctx.account.id,
            media_buy_id=media_buy_id,
            idempotency_key=req.idempotency_key,
            status="active",
            brand_domain=getattr(req.brand, "domain", None) if req.brand else None,
            total_budget=budget_amount,
            currency=budget_currency,
            start_time=start_dt,
            end_time=req.end_time,
            request_snapshot=req.model_dump(mode="json"),
        )
        async with self._sessionmaker() as session, session.begin():
            session.add(row)
        logger.info(
            "Created media buy %s for account=%s buyer=%s",
            media_buy_id,
            ctx.account.id,
            ctx.buyer_agent.agent_url,
        )
        return CreateMediaBuySuccessResponse(
            media_buy_id=media_buy_id,
            packages=[],
            status="active",
        )

    # ----- update_media_buy ------------------------------------------------

    async def update_media_buy(
        self, media_buy_id: str, patch: UpdateMediaBuyRequest, ctx: RequestContext
    ) -> UpdateMediaBuySuccessResponse:
        """Patch a media buy's status / pause flag.

        Tenant + account scoped — the SQL UPDATE includes both in the
        WHERE clause so a misrouted request can't mutate rows
        belonging to another tenant.
        """
        if ctx.account is None:
            raise AdcpError(
                "INTERNAL_ERROR",
                message="Dispatch should have populated account.",
                recovery="terminal",
            )
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(
                select(MediaBuyRow).where(
                    MediaBuyRow.tenant_id == ctx.account.metadata["tenant_id"],
                    MediaBuyRow.account_id == ctx.account.id,
                    MediaBuyRow.media_buy_id == media_buy_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise AdcpError(
                    "MEDIA_BUY_NOT_FOUND",
                    message=f"No media buy {media_buy_id!r} under this account.",
                    recovery="terminal",
                )
            if patch.paused is True and row.status == "active":
                row.status = "paused"
            elif patch.paused is False and row.status == "paused":
                row.status = "active"
            row.updated_at = datetime.now(timezone.utc)
        return UpdateMediaBuySuccessResponse(
            media_buy_id=row.media_buy_id,
            status=row.status,  # type: ignore[arg-type]
            packages=[],
        )

    # ----- sync_creatives --------------------------------------------------

    async def sync_creatives(
        self, req: SyncCreativesRequest, ctx: RequestContext
    ) -> SyncCreativesSuccessResponse:
        """Accept creative manifests — the reference impl persists
        nothing; production adopters route to their creative review
        pipeline here."""
        del req, ctx
        return SyncCreativesSuccessResponse(creatives=[])

    # ----- get_media_buy_delivery ------------------------------------------

    async def get_media_buy_delivery(
        self, req: GetMediaBuyDeliveryRequest, ctx: RequestContext
    ) -> GetMediaBuyDeliveryResponse:
        """Stub delivery — production adopters wire their real
        delivery / pacing query."""
        del req, ctx
        return GetMediaBuyDeliveryResponse(media_buys=[])

    # ----- sync_accounts ---------------------------------------------------

    async def sync_accounts(
        self, req: SyncAccountsRequest, ctx: RequestContext
    ) -> SyncAccountsSuccessResponse:
        """Upsert advertiser accounts and return results with bank details stripped.

        Persists the full :attr:`billing_entity` (including bank coordinates) to the
        ``accounts`` table for invoicing, then echoes it back with ``bank`` cleared —
        the AdCP 3.1 write-only guard.  Production adopters add approval workflows,
        credit-limit checks, and dry_run rollback here.

        Note: ``dry_run`` is intentionally ignored in this reference impl — the
        session always commits.  Production adopters should branch on ``req.dry_run``
        before ``session.begin()`` and call ``await session.rollback()`` (or avoid
        ``session.begin()`` entirely) when it is ``True``.
        """
        if ctx.buyer_agent is None:
            raise AdcpError(
                "INTERNAL_ERROR",
                message="Dispatch should have populated buyer_agent.",
                recovery="terminal",
            )
        tenant = current_tenant()
        if tenant is None:
            raise AdcpError(
                "AUTH_INVALID",
                message="sync_accounts called without a tenant context.",
                recovery="terminal",
            )

        results: list[dict[str, Any]] = []
        async with self._sessionmaker() as session, session.begin():
            # ctx.buyer_agent.ext doesn't carry the DB surrogate id — _row_to_agent
            # in buyer_registry.py discards row.id.  Re-query to get the FK.
            ba_result = await session.execute(
                select(BuyerAgentRow).where(
                    BuyerAgentRow.tenant_id == tenant.id,
                    BuyerAgentRow.agent_url == ctx.buyer_agent.agent_url,
                )
            )
            ba_row = ba_result.scalar_one_or_none()
            if ba_row is None:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"BuyerAgent row for {ctx.buyer_agent.agent_url!r} "
                        f"not found under tenant {tenant.id!r}."
                    ),
                    recovery="terminal",
                )

            for acct in req.accounts:
                # Natural upsert key — brand.domain and operator both forbid colons
                # (domain-pattern validation), so the colon separator is safe.
                # Adopters with an existing account-ID scheme should replace this
                # derivation with their own stable key (e.g. a UUID assigned on
                # first creation and stored in a separate column).
                wire_acct_id = f"{acct.brand.domain}:{acct.operator}"

                acct_result = await session.execute(
                    select(AccountRow).where(
                        AccountRow.tenant_id == tenant.id,
                        AccountRow.buyer_agent_id == ba_row.id,
                        AccountRow.account_id == wire_acct_id,
                    )
                )
                row = acct_result.scalar_one_or_none()

                billing_json = (
                    acct.billing_entity.model_dump(mode="json", exclude_none=True)
                    if acct.billing_entity
                    else None
                )
                # Prefer the legal entity name for the human-readable account label;
                # fall back to the brand domain when billing_entity is absent.
                acct_name = (
                    acct.billing_entity.legal_name
                    if acct.billing_entity and acct.billing_entity.legal_name
                    else acct.brand.domain
                )
                # Normalise None → False for the NOT NULL column; preserve the
                # normalised value in the response so DB and wire stay consistent.
                sandbox_val = acct.sandbox if acct.sandbox is not None else False

                if row is None:
                    action = "created"
                    row = AccountRow(
                        tenant_id=tenant.id,
                        buyer_agent_id=ba_row.id,
                        account_id=wire_acct_id,
                        name=acct_name,
                        status="active",
                        billing=acct.billing.value if acct.billing else None,
                        billing_entity=billing_json,
                        sandbox=sandbox_val,
                    )
                    session.add(row)
                    status_str = "active"
                else:
                    action = "updated"
                    row.billing_entity = billing_json
                    row.name = acct_name
                    if acct.billing:
                        row.billing = acct.billing.value
                    row.sandbox = sandbox_val
                    row.updated_at = datetime.now(timezone.utc)
                    status_str = row.status

                # Strip write-only bank details before echoing in the response.
                # Entity-level helper used here because acct.billing_entity is a
                # raw BusinessEntity from the wire — not a full stored Account.
                # Use project_account_for_response when you have an Account object
                # from storage (as in list_accounts below).
                safe_be = (
                    project_business_entity_for_response(acct.billing_entity)
                    if acct.billing_entity is not None
                    else None
                )
                results.append(
                    {
                        "account_id": wire_acct_id,
                        "name": acct_name,
                        "brand": acct.brand.model_dump(mode="json"),
                        "operator": acct.operator,
                        "action": action,
                        "status": status_str,
                        "billing": acct.billing.value if acct.billing else None,
                        "billing_entity": (
                            safe_be.model_dump(mode="json", exclude_none=True)
                            if safe_be is not None
                            else None
                        ),
                        "sandbox": sandbox_val,
                    }
                )

        # dry_run is intentionally always False here — we always commit.
        # Echoing req.dry_run=True would lie to the caller about what was persisted.
        return SyncAccountsSuccessResponse(accounts=results, dry_run=False)

    # ----- list_accounts ---------------------------------------------------

    async def list_accounts(
        self, req: ListAccountsRequest, ctx: RequestContext
    ) -> ListAccountsResponse:
        """List accounts for the authenticated agent, bank details stripped (write-only).

        Applies optional ``status`` and ``sandbox`` filters from the request.
        Production adopters add pagination, ownership scoping, and field-level
        access control here.
        """
        if ctx.buyer_agent is None:
            raise AdcpError(
                "INTERNAL_ERROR",
                message="Dispatch should have populated buyer_agent.",
                recovery="terminal",
            )
        tenant = current_tenant()
        if tenant is None:
            raise AdcpError(
                "AUTH_INVALID",
                message="list_accounts called without a tenant context.",
                recovery="terminal",
            )

        async with self._sessionmaker() as session:
            # ctx.buyer_agent.ext doesn't carry the DB surrogate id — re-query.
            ba_result = await session.execute(
                select(BuyerAgentRow).where(
                    BuyerAgentRow.tenant_id == tenant.id,
                    BuyerAgentRow.agent_url == ctx.buyer_agent.agent_url,
                )
            )
            ba_row = ba_result.scalar_one_or_none()
            if ba_row is None:
                return ListAccountsResponse(accounts=[])

            query = select(AccountRow).where(
                AccountRow.tenant_id == tenant.id,
                AccountRow.buyer_agent_id == ba_row.id,
            )
            if req.status is not None:
                query = query.where(AccountRow.status == req.status.value)
            if req.sandbox is not None:
                query = query.where(AccountRow.sandbox == req.sandbox)

            acct_result = await session.execute(query)
            rows = list(acct_result.scalars().all())

        accounts = []
        for row in rows:
            # model_validate coerces the billing_entity JSON dict → BusinessEntity
            # so project_account_for_response receives a real Pydantic object and
            # can call model_copy() safely.
            wire_acct = AdcpAccount.model_validate(
                {
                    "account_id": row.account_id,
                    "name": row.name,
                    "status": row.status,
                    "billing": row.billing,
                    "billing_entity": row.billing_entity,
                    "rate_card": row.rate_card,
                    "payment_terms": row.payment_terms,
                    "credit_limit": row.credit_limit,
                    "sandbox": row.sandbox,
                    "ext": row.ext,
                    "reporting_bucket": row.reporting_bucket,
                }
            )
            accounts.append(project_account_for_response(wire_acct))

        return ListAccountsResponse(accounts=accounts)


def _project_start_time(value: Any) -> datetime:
    """Project :class:`StartTiming` (root: ``'asap'`` | :class:`AwareDatetime`)
    into a flat timezone-aware datetime for SQL storage.

    The spec lets buyers send either ``'asap'`` or an ISO 8601 datetime;
    this seller normalizes ``'asap'`` to ``now()`` so the column is
    always populated. Adopters who need to preserve the literal flag
    add a separate ``start_immediately`` boolean column and project
    here.
    """
    root = getattr(value, "root", value)
    if root == "asap":
        return datetime.now(timezone.utc)
    if isinstance(root, datetime):
        return root if root.tzinfo else root.replace(tzinfo=timezone.utc)
    raise AdcpError(
        "INVALID_REQUEST",
        message=f"Unrecognized StartTiming value {root!r}.",
        recovery="terminal",
        field="start_time",
    )


__all__ = ["V3ReferenceSeller"]
