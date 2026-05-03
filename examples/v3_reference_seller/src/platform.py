"""DecisioningPlatform impl for the v3 reference seller.

Sales-non-guaranteed specialism with the full Sales surface:

Required (every sales-* specialism):

* :meth:`get_products` — read inventory catalog
* :meth:`create_media_buy` — terminal artifact insert; idempotency-keyed
* :meth:`update_media_buy` — patch (status / pause / spend cap /
  invoice recipient)
* :meth:`sync_creatives` — accept creative manifests, persist to
  ``creatives`` table
* :meth:`get_media_buy_delivery` — read delivery actuals

Optional (v6.0 rc.1+ — present for sales-non-guaranteed):

* :meth:`get_media_buys` — list buys for the resolved account with
  cursor-friendly limit/offset paging
* :meth:`provide_performance_feedback` — persist buyer-supplied
  performance signals
* :meth:`list_creative_formats` — static catalog of accepted formats
* :meth:`list_creatives` — seller-side view of buyer-uploaded
  creatives

Account ops (3.1-readiness anchor):

* :meth:`sync_accounts` — upsert with full :class:`BusinessEntity`
  payload (bank details persisted; never echoed)
* :meth:`list_accounts` — projected through
  :func:`adcp.decisioning.project_account_for_response` so bank
  details never leak on response

All methods run against the SQLAlchemy models in :mod:`models`. The
platform reads :attr:`RequestContext.buyer_agent` and
:attr:`account` from the typed request context, both populated by
the framework's dispatch layer before the method runs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import func, select

from adcp.decisioning import (
    Account,
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    ExplicitAccounts,
    MockAdServer,
    project_account_for_response,
    project_business_entity_for_response,
)
from adcp.decisioning.specialisms import SalesPlatform
from adcp.server import current_tenant
from adcp.types import (
    Account as AccountWire,
)
from adcp.types import (
    BusinessEntity,
    CreateMediaBuyRequest,
    CreateMediaBuySuccessResponse,
    Format,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetMediaBuysRequest,
    GetMediaBuysResponse,
    GetProductsRequest,
    GetProductsResponse,
    ListAccountsRequest,
    ListAccountsResponse,
    ListCreativeFormatsRequest,
    ListCreativeFormatsResponse,
    ListCreativesRequest,
    ListCreativesResponse,
    Product,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackSuccessResponse,
    SyncAccountsRequest,
    SyncAccountsSuccessResponse,
    SyncCreativeResult,
    SyncCreativesRequest,
    SyncCreativesSuccessResponse,
    UpdateMediaBuyRequest,
    UpdateMediaBuySuccessResponse,
)
from adcp.types import (
    MediaBuy as MediaBuyWire,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from adcp.decisioning import RequestContext

from .models import Account as AccountRow
from .models import BuyerAgent as BuyerAgentRow
from .models import Creative as CreativeRow
from .models import MediaBuy as MediaBuyRow
from .models import PerformanceFeedback as PerformanceFeedbackRow

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
        # Required by the spec whenever ``media_buy`` is in
        # ``supported_protocols`` (per
        # ``protocol/get-adcp-capabilities-response.json``,
        # ``account.supported_billing`` ``minItems: 1``). The
        # framework projects this into ``account.supported_billing``
        # on the auto-generated ``get_adcp_capabilities`` response.
        # This reference seller invoices the operator (agency / brand
        # buying direct) and supports agent-consolidated billing for
        # platforms acting on behalf of multiple advertisers.
        supported_billing=("operator", "agent"),
    )

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker,
        mock_ad_server: MockAdServer | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._mock_ad_server = mock_ad_server
        self.accounts = _make_account_store(sessionmaker)

    def _record(self, method: str, args: dict[str, Any]) -> None:
        """Record an outbound upstream call on the wired
        :class:`MockAdServer`, if any.

        Anti-façade contract — storyboard runners assert traffic
        counts via ``GET /_debug/traffic``. Methods that return spec-
        valid envelopes without recording at least one upstream call
        are textbook façade adapters.
        """
        if self._mock_ad_server is not None:
            self._mock_ad_server.record_call(method, args)

    # ----- get_products ----------------------------------------------------

    async def get_products(
        self, req: GetProductsRequest, ctx: RequestContext
    ) -> GetProductsResponse:
        """Static product catalog for the reference seller. Real
        adopters query a CMS / forecasting service."""
        del req, ctx  # this reference impl ignores brief / context
        self._record("products.list", {})
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

        :attr:`CreateMediaBuyRequest.invoice_recipient` is persisted
        as a flat JSON column on the row (full
        :class:`BusinessEntity` payload, bank details included). The
        seller projects through
        :func:`project_business_entity_for_response` only when
        echoing on a response — the SQL column is the durable
        invoicing record.
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
        invoice_recipient_payload: dict[str, Any] | None = None
        if req.invoice_recipient is not None:
            # Persist full payload (bank included) — write-only on
            # response, not on storage.
            invoice_recipient_payload = req.invoice_recipient.model_dump(
                mode="json", exclude_none=True
            )
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
            invoice_recipient=invoice_recipient_payload,
            request_snapshot=req.model_dump(mode="json"),
        )
        async with self._sessionmaker() as session, session.begin():
            session.add(row)
        self._record(
            "media_buy.create",
            {"media_buy_id": media_buy_id, "account_id": ctx.account.id},
        )
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
        """Patch a media buy's status / pause flag / invoice recipient.

        Tenant + account scoped — the SQL UPDATE includes both in the
        WHERE clause so a misrouted request can't mutate rows
        belonging to another tenant. ``invoice_recipient`` overrides
        replace the full :class:`BusinessEntity` payload (bank
        included) when present on the patch — 3.1-ready for per-buy
        invoice override semantics.
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
            patch_invoice = getattr(patch, "invoice_recipient", None)
            if patch_invoice is not None:
                row.invoice_recipient = patch_invoice.model_dump(mode="json", exclude_none=True)
            row.updated_at = datetime.now(timezone.utc)
        self._record(
            "media_buy.update",
            {"media_buy_id": media_buy_id, "status": row.status},
        )
        return UpdateMediaBuySuccessResponse(
            media_buy_id=row.media_buy_id,
            status=row.status,  # type: ignore[arg-type]
            packages=[],
        )

    # ----- sync_creatives --------------------------------------------------

    async def sync_creatives(
        self, req: SyncCreativesRequest, ctx: RequestContext
    ) -> SyncCreativesSuccessResponse:
        """Accept creative manifests and persist to the ``creatives``
        table.

        Idempotency-keyed on ``(tenant_id, creative_id)`` — re-syncing
        the same wire id under the same tenant updates the existing
        row in place (UPSERT). Auto-approves on ingest; production
        adopters route to a creative-review pipeline that flips
        ``status`` to ``pending_review`` and signs back via
        :meth:`adcp.decisioning.RequestContext.publish_status_change`.
        """
        if ctx.account is None:
            raise AdcpError(
                "INTERNAL_ERROR",
                message="Dispatch should have populated account.",
                recovery="terminal",
            )
        tenant_id = ctx.account.metadata["tenant_id"]
        results: list[SyncCreativeResult] = []
        async with self._sessionmaker() as session, session.begin():
            for creative in req.creatives:
                manifest_json = creative.model_dump(mode="json", exclude_none=True)
                format_id_payload = manifest_json.get("format_id") or {}
                # Look up by natural key first so we know whether
                # this is a create or update for the response action
                # field — UPSERT alone collapses both to one path.
                existing_q = await session.execute(
                    select(CreativeRow).where(
                        CreativeRow.tenant_id == tenant_id,
                        CreativeRow.creative_id == creative.creative_id,
                    )
                )
                existing = existing_q.scalar_one_or_none()
                if existing is None:
                    session.add(
                        CreativeRow(
                            tenant_id=tenant_id,
                            account_id=ctx.account.id,
                            creative_id=creative.creative_id,
                            name=creative.name,
                            format_id=format_id_payload,
                            status=(creative.status or "approved"),
                            manifest_json=manifest_json,
                        )
                    )
                    action: Literal["created", "updated"] = "created"
                else:
                    existing.name = creative.name
                    existing.format_id = format_id_payload
                    existing.manifest_json = manifest_json
                    if creative.status is not None:
                        existing.status = creative.status
                    existing.updated_at = datetime.now(timezone.utc)
                    action = "updated"
                results.append(
                    SyncCreativeResult.model_validate(
                        {
                            "creative_id": creative.creative_id,
                            "action": action,
                            "status": creative.status or "approved",
                        }
                    )
                )
        self._record("creative.upload", {"count": len(req.creatives) if req.creatives else 0})
        return SyncCreativesSuccessResponse(creatives=results)

    # ----- get_media_buy_delivery ------------------------------------------

    async def get_media_buy_delivery(
        self, req: GetMediaBuyDeliveryRequest, ctx: RequestContext
    ) -> GetMediaBuyDeliveryResponse:
        """Stub delivery — production adopters wire their real
        delivery / pacing query."""
        del req, ctx
        self._record("delivery.read", {})
        return GetMediaBuyDeliveryResponse(media_buys=[])

    # ----- get_media_buys --------------------------------------------------

    async def get_media_buys(
        self, req: GetMediaBuysRequest, ctx: RequestContext
    ) -> GetMediaBuysResponse:
        """List media buys for the resolved account.

        Filters by ``(tenant_id, account_id)`` from the resolved
        :class:`Account`. Pagination is offset/limit on the request's
        :class:`PaginationRequest` — adopters with billions of buys
        upgrade to seek-pagination on ``(created_at, id)``.
        """
        if ctx.account is None:
            raise AdcpError(
                "INTERNAL_ERROR",
                message="Dispatch should have populated account.",
                recovery="terminal",
            )
        limit = 50
        offset = 0
        if req.pagination is not None:
            limit = getattr(req.pagination, "limit", None) or 50
            offset = getattr(req.pagination, "offset", None) or 0
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(MediaBuyRow)
                .where(
                    MediaBuyRow.tenant_id == ctx.account.metadata["tenant_id"],
                    MediaBuyRow.account_id == ctx.account.id,
                )
                .order_by(MediaBuyRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = list(result.scalars())
        media_buys: list[MediaBuyWire] = []
        for row in rows:
            invoice_recipient: BusinessEntity | None = None
            if row.invoice_recipient is not None:
                # Project bank details out before echoing on response.
                entity = BusinessEntity.model_validate(row.invoice_recipient)
                invoice_recipient = project_business_entity_for_response(entity)
            media_buys.append(
                MediaBuyWire.model_validate(
                    {
                        "media_buy_id": row.media_buy_id,
                        "status": row.status,
                        "currency": row.currency or "USD",
                        "total_budget": row.total_budget or 0.0,
                        "start_time": row.start_time,
                        "end_time": row.end_time,
                        "created_at": row.created_at,
                        "updated_at": row.updated_at,
                        "packages": [],
                        "invoice_recipient": invoice_recipient,
                    }
                )
            )
        self._record(
            "media_buys.list",
            {"account_id": ctx.account.id, "limit": limit, "offset": offset},
        )
        # Pydantic re-validates each item against the response-specific
        # ``MediaBuy`` shape. Passing the public-API ``MediaBuy``
        # instances we built above ensures field drift surfaces here
        # rather than at the wire boundary.
        return GetMediaBuysResponse.model_validate(
            {"media_buys": [m.model_dump(mode="python", exclude_none=True) for m in media_buys]}
        )

    # ----- provide_performance_feedback ------------------------------------

    async def provide_performance_feedback(
        self, req: ProvidePerformanceFeedbackRequest, ctx: RequestContext
    ) -> ProvidePerformanceFeedbackSuccessResponse:
        """Persist buyer-supplied performance signal.

        Looks up the media buy by ``(tenant_id, media_buy_id)`` —
        rejects with ``MEDIA_BUY_NOT_FOUND`` if the buyer's id doesn't
        resolve under this tenant. Production adopters route the
        feedback into their optimization / pacing service from here.
        """
        if ctx.account is None:
            raise AdcpError(
                "INTERNAL_ERROR",
                message="Dispatch should have populated account.",
                recovery="terminal",
            )
        tenant_id = ctx.account.metadata["tenant_id"]
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(
                select(MediaBuyRow).where(
                    MediaBuyRow.tenant_id == tenant_id,
                    MediaBuyRow.media_buy_id == req.media_buy_id,
                )
            )
            mb = result.scalar_one_or_none()
            if mb is None:
                raise AdcpError(
                    "MEDIA_BUY_NOT_FOUND",
                    message=f"No media buy {req.media_buy_id!r} under this tenant.",
                    recovery="terminal",
                    field="media_buy_id",
                )
            metric_type = (
                (
                    req.metric_type.value
                    if hasattr(req.metric_type, "value")
                    else str(req.metric_type)
                )
                if req.metric_type is not None
                else "overall_performance"
            )
            session.add(
                PerformanceFeedbackRow(
                    tenant_id=tenant_id,
                    media_buy_id=mb.id,
                    feedback_type=metric_type,
                    value=req.model_dump(mode="json", exclude_none=True),
                )
            )
        self._record(
            "performance.feedback",
            {"media_buy_id": req.media_buy_id, "feedback_type": metric_type},
        )
        return ProvidePerformanceFeedbackSuccessResponse.model_validate({"success": True})

    # ----- list_creative_formats -------------------------------------------

    async def list_creative_formats(
        self, req: ListCreativeFormatsRequest, ctx: RequestContext
    ) -> ListCreativeFormatsResponse:
        """Static catalog of accepted formats.

        Real adopters drive this from a creative-format registry
        keyed on the seller's actual placement / template inventory.
        """
        del req, ctx
        agent_url = "https://reference.adcp.org"
        formats = [
            Format.model_validate(
                {
                    "format_id": {"agent_url": agent_url, "id": "display_300x250"},
                    "name": "Display 300x250 (medium rectangle)",
                    "description": "IAB standard 300x250 display banner.",
                }
            ),
            Format.model_validate(
                {
                    "format_id": {"agent_url": agent_url, "id": "display_728x90"},
                    "name": "Display 728x90 (leaderboard)",
                    "description": "IAB standard 728x90 display banner.",
                }
            ),
            Format.model_validate(
                {
                    "format_id": {"agent_url": agent_url, "id": "video_16x9_30s"},
                    "name": "Video 16:9 30s",
                    "description": "Standard 30-second 16:9 video creative.",
                }
            ),
        ]
        self._record("creatives.formats", {})
        return ListCreativeFormatsResponse(formats=formats)

    # ----- list_creatives --------------------------------------------------

    async def list_creatives(
        self, req: ListCreativesRequest, ctx: RequestContext
    ) -> ListCreativesResponse:
        """List the seller's view of buyer-uploaded creatives for the
        resolved account.

        Sourced from the ``creatives`` table populated by
        :meth:`sync_creatives`. Pagination is offset/limit; adopters
        with millions of creatives per buyer upgrade to seek-pagination.
        """
        if ctx.account is None:
            raise AdcpError(
                "INTERNAL_ERROR",
                message="Dispatch should have populated account.",
                recovery="terminal",
            )
        limit = 50
        offset = 0
        if req.pagination is not None:
            limit = getattr(req.pagination, "limit", None) or 50
            offset = getattr(req.pagination, "offset", None) or 0
        async with self._sessionmaker() as session:
            count_q = await session.execute(
                select(func.count())
                .select_from(CreativeRow)
                .where(
                    CreativeRow.tenant_id == ctx.account.metadata["tenant_id"],
                    CreativeRow.account_id == ctx.account.id,
                )
            )
            total = int(count_q.scalar() or 0)
            page_q = await session.execute(
                select(CreativeRow)
                .where(
                    CreativeRow.tenant_id == ctx.account.metadata["tenant_id"],
                    CreativeRow.account_id == ctx.account.id,
                )
                .order_by(CreativeRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = list(page_q.scalars())
        creatives = [
            {
                "creative_id": row.creative_id,
                "name": row.name,
                "format_id": row.format_id,
                "status": row.status,
                "created_date": row.created_at,
                "updated_date": row.updated_at,
            }
            for row in rows
        ]
        has_more = offset + len(creatives) < total
        self._record(
            "creatives.list",
            {"account_id": ctx.account.id, "limit": limit, "offset": offset},
        )
        return ListCreativesResponse.model_validate(
            {
                "query_summary": {"total_matching": total, "returned": len(creatives)},
                "pagination": {"has_more": has_more, "total_count": total},
                "creatives": creatives,
            }
        )

    # ----- sync_accounts ---------------------------------------------------

    async def sync_accounts(
        self, req: SyncAccountsRequest, ctx: RequestContext
    ) -> SyncAccountsSuccessResponse:
        """Upsert incoming accounts under the authenticated buyer agent.

        Persists the full :class:`BusinessEntity` payload (bank
        details included) on ``billing_entity`` — the column is the
        durable invoicing record. The response goes through
        :func:`project_business_entity_for_response` so bank details
        never echo on the wire.

        Natural key: ``(tenant_id, brand.domain + operator)``. The
        wire ``account_id`` is seller-assigned on first sight and
        stable thereafter.
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
                message="sync_accounts requires a tenant context.",
                recovery="terminal",
            )
        results: list[dict[str, Any]] = []
        async with self._sessionmaker() as session, session.begin():
            # Look up the buyer-agent SQL row id by agent_url.
            ba_q = await session.execute(
                select(BuyerAgentRow).where(
                    BuyerAgentRow.tenant_id == tenant.id,
                    BuyerAgentRow.agent_url == ctx.buyer_agent.agent_url,
                )
            )
            buyer_agent_row = ba_q.scalar_one_or_none()
            if buyer_agent_row is None:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        "Authenticated buyer_agent has no matching row — " "registry / table drift."
                    ),
                    recovery="terminal",
                )
            for incoming in req.accounts:
                # Natural key per the spec — brand.domain + operator
                # under the buyer's agent. Both fields are required by
                # the schema (BrandReference.domain, Account.brand) so
                # no None guard is needed.
                brand_domain = incoming.brand.domain
                natural_account_id = f"{brand_domain}::{incoming.operator}"
                billing_entity_payload: dict[str, Any] | None = None
                if incoming.billing_entity is not None:
                    billing_entity_payload = incoming.billing_entity.model_dump(
                        mode="json", exclude_none=True
                    )
                existing_q = await session.execute(
                    select(AccountRow).where(
                        AccountRow.tenant_id == tenant.id,
                        AccountRow.account_id == natural_account_id,
                    )
                )
                existing = existing_q.scalar_one_or_none()
                billing_value = (
                    incoming.billing.value
                    if hasattr(incoming.billing, "value")
                    else str(incoming.billing)
                )
                if existing is None:
                    new_row = AccountRow(
                        tenant_id=tenant.id,
                        buyer_agent_id=buyer_agent_row.id,
                        account_id=natural_account_id,
                        name=f"{brand_domain} c/o {incoming.operator}",
                        status="active",
                        billing=billing_value,
                        billing_entity=billing_entity_payload,
                        sandbox=bool(incoming.sandbox),
                    )
                    session.add(new_row)
                    action = "created"
                else:
                    existing.billing = billing_value
                    existing.billing_entity = billing_entity_payload
                    existing.sandbox = bool(incoming.sandbox)
                    existing.updated_at = datetime.now(timezone.utc)
                    action = "updated"
                # Project bank out of the echoed billing_entity per
                # spec write-only rule.
                response_billing: dict[str, Any] | None = None
                if incoming.billing_entity is not None:
                    response_billing = project_business_entity_for_response(
                        incoming.billing_entity
                    ).model_dump(mode="json", exclude_none=True)
                results.append(
                    {
                        "account_id": natural_account_id,
                        "brand": incoming.brand.model_dump(mode="json", exclude_none=True),
                        "operator": incoming.operator,
                        "name": f"{brand_domain} c/o {incoming.operator}",
                        "action": action,
                        "status": "active",
                        "billing": billing_value,
                        "billing_entity": response_billing,
                        "sandbox": bool(incoming.sandbox),
                    }
                )
        self._record("accounts.sync", {"count": len(req.accounts)})
        return SyncAccountsSuccessResponse.model_validate(
            {"accounts": results, "dry_run": bool(req.dry_run)}
        )

    # ----- list_accounts ---------------------------------------------------

    async def list_accounts(
        self, req: ListAccountsRequest, ctx: RequestContext
    ) -> ListAccountsResponse:
        """List accounts for the authenticated buyer agent.

        **Headline 3.1-readiness claim**: every account in the
        response is run through
        :func:`project_account_for_response` so the spec's
        write-only ``billing_entity.bank`` field cannot leak on the
        wire — even when adopters persist full bank coordinates for
        invoicing.
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
                message="list_accounts requires a tenant context.",
                recovery="terminal",
            )
        limit = 50
        offset = 0
        if req.pagination is not None:
            limit = getattr(req.pagination, "limit", None) or 50
            offset = getattr(req.pagination, "offset", None) or 0
        async with self._sessionmaker() as session:
            ba_q = await session.execute(
                select(BuyerAgentRow).where(
                    BuyerAgentRow.tenant_id == tenant.id,
                    BuyerAgentRow.agent_url == ctx.buyer_agent.agent_url,
                )
            )
            buyer_agent_row = ba_q.scalar_one_or_none()
            if buyer_agent_row is None:
                # Authenticated agent unknown to the accounts table —
                # return empty page rather than 500.
                self._record(
                    "accounts.list",
                    {"buyer_agent_id": ctx.buyer_agent.agent_url},
                )
                return ListAccountsResponse.model_validate(
                    {
                        "accounts": [],
                        "pagination": {"has_more": False, "total_count": 0},
                    }
                )
            stmt = select(AccountRow).where(
                AccountRow.tenant_id == tenant.id,
                AccountRow.buyer_agent_id == buyer_agent_row.id,
            )
            if req.status is not None:
                status_value = req.status.value if hasattr(req.status, "value") else str(req.status)
                stmt = stmt.where(AccountRow.status == status_value)
            page_q = await session.execute(
                stmt.order_by(AccountRow.created_at.desc()).limit(limit).offset(offset)
            )
            rows = list(page_q.scalars())

        projected_accounts: list[dict[str, Any]] = []
        for row in rows:
            wire_account = AccountWire.model_validate(
                {
                    "account_id": row.account_id,
                    "name": row.name,
                    "status": row.status,
                    "billing": row.billing,
                    "billing_entity": row.billing_entity,
                    "sandbox": row.sandbox,
                }
            )
            # The 3.1-readiness guard: strip bank details before the
            # response leaves the platform.
            safe = project_account_for_response(wire_account)
            projected_accounts.append(safe.model_dump(mode="json", exclude_none=True))
        self._record("accounts.list", {"buyer_agent_id": ctx.buyer_agent.agent_url})
        return ListAccountsResponse.model_validate(
            {
                "accounts": projected_accounts,
                "pagination": {"has_more": len(rows) == limit},
            }
        )


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
