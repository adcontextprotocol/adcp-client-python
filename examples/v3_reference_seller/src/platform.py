"""DecisioningPlatform impl for the v3 reference seller — translator pattern.

Sales-non-guaranteed AND sales-guaranteed specialism. The seller is a
**translator**: AdCP wire on the inside, the JS mock-server (GAM-flavored
upstream) on the outside. Ad-ops state — orders / line items / creatives /
delivery — lives upstream. The local Postgres carries only the
commercial-identity layer (tenants, buyer agents, accounts).

Required (every sales-* specialism):

* :meth:`get_products` — translate ``GET /v1/products`` upstream
* :meth:`create_media_buy` — ``POST /v1/orders``; returns
  :class:`Submitted` task envelope; background handoff polls
  ``/v1/tasks/{id}`` until approved
* :meth:`update_media_buy` — UNSUPPORTED (mock has no order-update
  endpoint; the framework raises ``UNSUPPORTED_FEATURE``)
* :meth:`sync_creatives` — ``POST /v1/creatives`` per creative
* :meth:`get_media_buy_delivery` — ``GET /v1/orders/{id}/delivery``

Optional (v6.0 rc.1+):

* :meth:`get_media_buys` — ``GET /v1/orders``
* :meth:`provide_performance_feedback` — ``POST /v1/orders/{id}/conversions``
  (CAPI is the GAM-flavored equivalent of perf feedback)
* :meth:`list_creative_formats` — STATIC (publisher-defined; no upstream
  endpoint)
* :meth:`list_creatives` — ``GET /v1/creatives``

Account ops (3.1-readiness anchor — local Postgres):

* :meth:`sync_accounts` — upsert with full :class:`BusinessEntity`
  payload; the AdCP account → upstream ``network_code`` translation
  is the durable record this seller owns.
* :meth:`list_accounts` — projected through
  :func:`adcp.decisioning.project_account_for_response` so bank
  details never leak on response.

Upstream HTTP routing — adopter migration template
--------------------------------------------------

Every sales-* method body resolves the upstream client via
:meth:`DecisioningPlatform.upstream_for`. The framework picks the URL
based on the resolved account's ``mode``:

* ``mode='live'`` / ``mode='sandbox'`` → :attr:`upstream_url` (the
  adopter's production URL declared on the platform class).
* ``mode='mock'`` → ``account.metadata['mock_upstream_url']``.

The reference seller's only upstream is the JS mock-server fixture, so
every account it ships is ``mode='mock'`` (see :func:`_make_account_store`
which sets ``mock_upstream_url`` from the ``MOCK_AD_SERVER_URL`` env on
every Account it returns). Adopters with a real production upstream
declare :attr:`upstream_url` to that production URL and mark only their
test/conformance accounts ``mode='mock'``.

Adopters fork this file and replace the upstream payload helpers
(:mod:`upstream`) with their real ad server's API. Method bodies stay
shape-compatible — only the upstream URL / auth / payload mapping
changes.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import select

from adcp.decisioning import (
    Account,
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    MockAdServer,
    StaticBearer,
    UpstreamHttpClient,
    project_account_for_response,
    project_business_entity_for_response,
)
from adcp.decisioning.capabilities import (
    Account as CapsAccount,
)
from adcp.decisioning.capabilities import (
    MediaBuy as CapsMediaBuy,
)
from adcp.decisioning.capabilities import (
    Specialism as CapsSpecialism,
)
from adcp.decisioning.capabilities import (
    WebhookSigning as CapsWebhookSigning,
)
from adcp.decisioning.specialisms import SalesPlatform
from adcp.server import current_tenant
from adcp.types import (
    Account as AccountWire,
)
from adcp.types import (
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

from . import upstream as upstream_helpers

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from adcp.decisioning import RequestContext

from .models import Account as AccountRow
from .models import BuyerAgent as BuyerAgentRow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AccountStore — explicit (wire ref drives lookup)
# ---------------------------------------------------------------------------


def _make_account_store(
    sessionmaker: async_sessionmaker,
    *,
    mock_upstream_url: str | None,
):
    """Adopter ``AccountStore`` — resolves AdCP account references to a
    framework :class:`Account`. Supports two reference shapes per the
    v3 spec:

    * ``{account_id: "..."}`` — explicit lookup against the ``accounts``
      table's ``account_id`` column. The path adopters use once they've
      onboarded a buyer and shared the persistent ``account_id``.
    * ``{brand: {...}, operator: "..."}`` — buyer-brand-shaped reference
      with no ``account_id``. The seller resolves to the FIRST active
      account associated with the authenticated buyer agent. This is
      the path AdCP storyboards exercise for "discover products" flows
      where the buyer hasn't yet been issued a per-relationship
      ``account_id``.

    Reads ``ext`` (the upstream routing payload, ``{"network_code":
    ..., "advertiser_id": ...}``) onto :attr:`Account.metadata` so
    platform methods can pluck them out without a second query.

    Every account the reference seller resolves runs in ``mode='mock'``
    — the seller's only upstream is the per-specialism mock-server
    fixture (see module docstring). ``mock_upstream_url`` is sourced
    from the ``MOCK_AD_SERVER_URL`` env var (set in :func:`app.main`)
    and stamped onto every Account; the framework's
    :meth:`DecisioningPlatform.upstream_for` reads it to point the
    :class:`UpstreamHttpClient` at the fixture.

    When ``mock_upstream_url`` is ``None`` the loader fails-fast with
    ``CONFIGURATION_ERROR`` — there is no URL to stamp onto a
    ``mode='mock'`` Account, so resolving here would only defer the
    failure to a downstream :class:`httpx.ConnectError` that the SDK's
    :class:`UpstreamHttpClient` does not project. ``None`` is only legal
    when callers bypass the AccountStore by constructing ``Account``
    objects directly into ``RequestContext`` (the unit-test pattern).

    Adopters with a real production upstream:

    * Declare :attr:`V3ReferenceSeller.upstream_url` to their production URL.
    * Default new accounts to ``mode='live'`` here.
    * Reserve ``mode='mock'`` (with ``mock_upstream_url``) for
      conformance / storyboard accounts only.
    """

    def _project_row(row: AccountRow) -> Account[dict[str, Any]]:
        """Translate an :class:`AccountRow` to the framework
        :class:`Account` shape. Shared by both resolution paths."""
        ext_payload = row.ext or {}
        network_code = ext_payload.get("network_code")
        advertiser_id = ext_payload.get("advertiser_id")
        if not network_code or not advertiser_id:
            # Server-side onboarding misconfig from the buyer's POV: the
            # account exists but is unusable until ``ext`` is reseeded.
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message=(
                    f"Account {row.account_id!r} is missing upstream routing "
                    "(ext.network_code / ext.advertiser_id). Reseed the "
                    "account with translator-pattern routing."
                ),
                recovery="transient",
            )
        return Account(
            id=row.id,
            name=row.name,
            status=row.status,
            mode="mock",
            metadata={
                "tenant_id": row.tenant_id,
                "buyer_agent_id": row.buyer_agent_id,
                "account_id": row.account_id,
                "billing": row.billing,
                "sandbox": row.sandbox,
                "network_code": network_code,
                "advertiser_id": advertiser_id,
                # Framework-reserved key — read by ``upstream_for`` to
                # route the UpstreamHttpClient at the mock-server.
                "mock_upstream_url": mock_upstream_url,
            },
            _mode_explicit=True,
        )

    class _V3ReferenceAccountStore:
        """Custom AccountStore supporting explicit-id AND brand-shaped
        references. See :func:`_make_account_store` for the spec
        background."""

        resolution: ClassVar[str] = "explicit"

        async def resolve(
            self,
            ref: dict[str, Any] | None,
            auth_info: Any = None,
        ) -> Account[dict[str, Any]]:
            if mock_upstream_url is None:
                raise AdcpError(
                    "CONFIGURATION_ERROR",
                    message=(
                        "V3ReferenceSeller account store was invoked without a "
                        "mock_upstream_url. Pass mock_upstream_url to "
                        "V3ReferenceSeller(...) (sourced from MOCK_AD_SERVER_URL "
                        "env in app.main), or override the AccountStore in tests "
                        "that construct Account objects directly."
                    ),
                    recovery="terminal",
                )
            tenant = current_tenant()
            if tenant is None:
                raise AdcpError(
                    "AUTH_REQUIRED",
                    message=(
                        "AccountStore.resolve called without a tenant context. "
                        "Wire the SubdomainTenantMiddleware before serve()."
                    ),
                    recovery="terminal",
                )

            # Path 1: explicit `account_id` on the wire ref.
            account_id: str | None = None
            if ref is not None:
                account_id = ref.get("account_id")

            async with sessionmaker() as session:
                if account_id:
                    result = await session.execute(
                        select(AccountRow).where(
                            AccountRow.tenant_id == tenant.id,
                            AccountRow.account_id == account_id,
                            AccountRow.status == "active",
                        )
                    )
                    row = result.scalar_one_or_none()
                    if row is None:
                        raise AdcpError(
                            "ACCOUNT_NOT_FOUND",
                            message=(
                                f"No active account {account_id!r} under " f"tenant {tenant.id!r}."
                            ),
                            recovery="terminal",
                            field="account.account_id",
                        )
                    return _project_row(row)

                # Path 2: brand-shaped reference (no account_id). Resolve
                # to the first active account for the authenticated
                # buyer agent. The buyer-agent's agent_url is the
                # `principal` field on auth_info — populated by the
                # framework's auth middleware from the validated bearer.
                principal: str | None = None
                if auth_info is not None:
                    principal = getattr(auth_info, "principal", None)
                if not principal:
                    raise AdcpError(
                        "ACCOUNT_NOT_FOUND",
                        message=(
                            "Request did not include `account.account_id` "
                            "and no authenticated buyer-agent principal was "
                            "available to resolve a brand-shaped reference. "
                            "Send `account.account_id` explicitly, or "
                            "authenticate with a bearer token bound to a "
                            "seeded buyer agent."
                        ),
                        recovery="correctable",
                        field="account.account_id",
                    )
                ba_result = await session.execute(
                    select(BuyerAgentRow).where(
                        BuyerAgentRow.tenant_id == tenant.id,
                        BuyerAgentRow.agent_url == principal,
                    )
                )
                buyer_agent = ba_result.scalar_one_or_none()
                if buyer_agent is None:
                    raise AdcpError(
                        "ACCOUNT_NOT_FOUND",
                        message=(
                            f"No buyer agent matches principal {principal!r} "
                            f"under tenant {tenant.id!r}."
                        ),
                        recovery="terminal",
                    )
                acct_result = await session.execute(
                    select(AccountRow)
                    .where(
                        AccountRow.tenant_id == tenant.id,
                        AccountRow.buyer_agent_id == buyer_agent.id,
                        AccountRow.status == "active",
                    )
                    .limit(1)
                )
                row = acct_result.scalar_one_or_none()
                if row is None:
                    raise AdcpError(
                        "ACCOUNT_NOT_FOUND",
                        message=(
                            f"Buyer agent {principal!r} has no active accounts "
                            f"under tenant {tenant.id!r}."
                        ),
                        recovery="terminal",
                    )
                return _project_row(row)

    return _V3ReferenceAccountStore()


# ---------------------------------------------------------------------------
# Platform — sales-non-guaranteed + sales-guaranteed (translator)
# ---------------------------------------------------------------------------


_DELIVERY_STATUS_MAP: dict[str, str] = {
    # Upstream → AdCP MediaBuyStatus
    "draft": "pending_creatives",
    "pending_approval": "pending_creatives",
    "approved": "pending_start",
    "delivering": "active",
    "completed": "completed",
    "canceled": "canceled",
    "rejected": "rejected",
}


# Per-package fields the seller persists locally because the mock-server
# upstream's line-item model stores only ``(product_id, budget,
# ad_unit_targeting, creative_ids)``. Without this shadow store the spec's
# echo contract (``targeting_overlay``, ``measurement_terms``, etc. must
# survive create → get / update) is unsatisfiable. Real adopters whose
# ad server stores the full package shape upstream drop this layer.
_PERSISTED_PACKAGE_FIELDS: tuple[str, ...] = (
    "product_id",
    "format_ids",
    "budget",
    "pricing_option_id",
    "bid_price",
    "impressions",
    "pacing",
    "start_time",
    "end_time",
    "catalogs",
    "optimization_goals",
    "targeting_overlay",
    "measurement_terms",
    "performance_standards",
    "creative_assignments",
    "agency_estimate_number",
)


def _project_request_package_echo(pkg: Any) -> dict[str, Any]:
    """Project a buyer-requested package's echo fields onto a plain dict.

    Shared by the confirmed-package response shape and the seller-local
    shadow-store entry: same field set, same projection. The package_id
    is added by the caller (issued upstream as ``line_item_id``).
    """
    out: dict[str, Any] = {}
    for field in _PERSISTED_PACKAGE_FIELDS:
        value = getattr(pkg, field, None)
        if value is None:
            continue
        if hasattr(value, "model_dump"):
            out[field] = value.model_dump(mode="json", exclude_none=True)
        elif isinstance(value, list):
            out[field] = [
                (
                    item.model_dump(mode="json", exclude_none=True)
                    if hasattr(item, "model_dump")
                    else item
                )
                for item in value
            ]
        else:
            out[field] = value
    return out


def _projected_package_state(state: dict[str, Any]) -> dict[str, Any]:
    """Project a shadow-store package entry onto the wire-shape fields.

    Drops the internal ``canceled`` / ``paused`` keys and returns the
    echo fields verbatim. Caller injects ``package_id`` separately.
    """
    return {k: v for k, v in state.items() if k not in {"canceled", "paused"}}


class V3ReferenceSeller(DecisioningPlatform, SalesPlatform):
    """Translator-pattern seller against the JS mock-server upstream.

    Every method body reads :attr:`RequestContext.account` for the
    upstream routing (``network_code`` + ``advertiser_id``) and resolves
    an :class:`UpstreamHttpClient` via :meth:`upstream_for`. The local
    Postgres is consulted only for the commercial-identity layer
    (account resolution + ``sync_accounts`` / ``list_accounts``).

    The reference seller's only upstream is a mock-server fixture, so
    every account it serves is ``mode='mock'`` and the upstream URL
    comes from ``account.metadata['mock_upstream_url']`` (sourced from
    the ``MOCK_AD_SERVER_URL`` env). :attr:`upstream_url` carries a
    placeholder URL that adopters forking this template replace with
    their real production URL when they migrate accounts to
    ``mode='live'``.
    """

    #: Production upstream URL placeholder. The reference seller is
    #: mock-mode by design, so this URL is never resolved at runtime —
    #: every account is ``mode='mock'`` and the upstream URL comes from
    #: ``account.metadata['mock_upstream_url']``. Adopters forking this
    #: template replace this value with their real production ad-server
    #: URL when migrating accounts to ``mode='live'``.
    upstream_url = "https://sales-guaranteed.example.invalid/v1"

    capabilities = DecisioningCapabilities(
        # Real GAM-shaped publishers sell BOTH guaranteed (IO-driven)
        # and non-guaranteed (programmatic remnant). The mock supports
        # ``delivery_type: guaranteed/non_guaranteed`` directly so we
        # claim both — adopters whose upstream is non-guaranteed-only
        # narrow this to the single specialism.
        specialisms=[
            CapsSpecialism.sales_non_guaranteed,
            CapsSpecialism.sales_guaranteed,
        ],
        # ``account.supported_billing`` is required by the spec
        # whenever ``media_buy`` is in ``supported_protocols``. The
        # reference seller invoices the operator (agency / brand
        # buying direct) and supports agent-consolidated billing for
        # platforms acting on behalf of multiple advertisers.
        account=CapsAccount(supported_billing=["operator", "agent"]),
        # Pricing declared on the structured ``media_buy`` block —
        # the reference seller supports CPM only.
        media_buy=CapsMediaBuy(supported_pricing_models=["cpm"]),
    )

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker,
        upstream_api_key: str,
        mock_upstream_url: str | None = None,
        mock_ad_server: MockAdServer | None = None,
        approval_poll_interval_s: float = 1.0,
        approval_poll_max_iterations: int = 60,
        webhook_signing_alg: str | None = None,
    ) -> None:
        """Construct the reference seller.

        :param sessionmaker: Async SQLAlchemy sessionmaker for the
            commercial-identity tables (tenants / buyer_agents /
            accounts).
        :param upstream_api_key: API key the upstream expects in the
            ``Authorization: Bearer ...`` header. Wired into a
            :class:`adcp.decisioning.StaticBearer` and threaded through
            every :meth:`upstream_for` call.
        :param mock_upstream_url: Where the JS mock-server is listening.
            When set, every Account the loader resolves is ``mode='mock'``
            and carries this URL in ``account.metadata['mock_upstream_url']``.
            When ``None``, the loader fails-fast with ``CONFIGURATION_ERROR``
            if anything ever resolves through it — only legal when callers
            bypass the AccountStore by constructing ``Account`` objects
            directly into ``RequestContext`` (the unit-test pattern).
            :func:`app.main` always sets this from the ``MOCK_AD_SERVER_URL``
            env.
        :param mock_ad_server: Optional anti-façade traffic recorder.
        :param approval_poll_interval_s: Base sleep between polls of
            ``/v1/tasks/{id}`` during async order approval.
        :param approval_poll_max_iterations: Maximum polls before
            raising ``SERVICE_UNAVAILABLE`` (transient).
        :param webhook_signing_alg: When set (``"ed25519"`` or
            ``"ecdsa-p256-sha256"``), this seller advertises
            ``capabilities.webhook_signing.supported=True`` and the
            named algorithm. ``app.main`` sets this iff a webhook-signing
            key PEM is wired via env vars; the framework's #384 boot
            validator then enforces that the wired
            :class:`~adcp.webhook_sender.WebhookSender` produces RFC 9421
            signatures over outbound deliveries. Default ``None`` —
            no signing advertised, sender wiring optional.
        """
        # Override the class-level capabilities iff signing is wired.
        # ``dataclasses.replace`` preserves every other field from the
        # class-level template — adding a new field to the template
        # (e.g. ``signals=...``) propagates automatically without
        # touching this override.
        if webhook_signing_alg is not None:
            self.capabilities = _dc_replace(
                type(self).capabilities,
                webhook_signing=CapsWebhookSigning(
                    supported=True,
                    profile="adcp/webhook-signing/v1",
                    algorithms=[webhook_signing_alg],  # type: ignore[list-item]
                ),
            )

        self._sessionmaker = sessionmaker
        # Single auth instance shared across every upstream_for() call.
        # The framework's client cache keys on (base_url, id(auth)),
        # so a stable instance means one pooled httpx client per URL.
        self._upstream_auth = StaticBearer(token=upstream_api_key)
        self._mock_ad_server = mock_ad_server
        self._approval_poll_interval_s = approval_poll_interval_s
        self._approval_poll_max_iterations = approval_poll_max_iterations
        # Seller-local shadow store for state the mock-server doesn't
        # model: per-package ``targeting_overlay`` / ``measurement_terms``
        # echo data, plus media-buy and per-package ``canceled`` / ``paused``
        # flags. Keyed by upstream ``order_id``. Real adopters whose ad
        # server tracks this shape upstream drop the shadow store.
        self._buy_state: dict[str, dict[str, Any]] = {}
        # Monotonic per-buy revision counter (update_media_buy's
        # optimistic-concurrency token).
        self._buy_revisions: dict[str, int] = {}
        # Bidirectional buyer-creative-id ↔ upstream-creative-id map.
        # The upstream mints ``cr_<uuid>`` on every upload regardless of
        # ``client_request_id``, so the seller has to track the mapping
        # to (a) echo the buyer's id in ``list_creatives`` and (b)
        # translate before calling ``attach_creative`` upstream.
        self._creative_id_map: dict[str, str] = {}  # buyer_id → upstream_id
        self._creative_id_reverse: dict[str, str] = {}  # upstream_id → buyer_id
        # AccountStore is always wired. ``app.main`` passes the
        # MOCK_AD_SERVER_URL env so resolved accounts route at the JS
        # mock-server fixture. Tests that bypass the AccountStore (by
        # passing ``Account`` objects directly into ``RequestContext``)
        # still need a non-None ``accounts`` attribute for
        # ``validate_platform`` — they pass ``mock_upstream_url=None``
        # and the loader fails-fast with CONFIGURATION_ERROR if anything
        # ever resolves through it.
        self.accounts = _make_account_store(
            sessionmaker,
            mock_upstream_url=mock_upstream_url,
        )

    def _client(self, ctx: RequestContext) -> UpstreamHttpClient:
        """Resolve the pooled :class:`UpstreamHttpClient` for this
        request via the framework's :meth:`upstream_for`.

        The framework picks the URL from ``ctx.account.mode``:

        * ``mode='mock'`` → ``account.metadata['mock_upstream_url']``
          (the JS mock-server fixture for this specialism).
        * ``mode='live'`` / ``mode='sandbox'`` → :attr:`upstream_url`.

        ``treat_404_as_none=False`` — the reference seller wants 404s
        to surface as :class:`AdcpError` (with per-callsite override of
        the AdCP error code) rather than be papered over to ``None``.
        """
        return self.upstream_for(
            ctx,
            auth=self._upstream_auth,
            treat_404_as_none=False,
        )

    def _record(self, method: str, args: dict[str, Any]) -> None:
        """Record an outbound upstream call on the wired
        :class:`MockAdServer`, if any.

        Anti-façade contract — storyboard runners assert traffic
        counts via ``GET /_debug/traffic``.
        """
        if self._mock_ad_server is not None:
            self._mock_ad_server.record_call(method, args)

    # ----- get_products ----------------------------------------------------

    async def get_products(
        self, req: GetProductsRequest, ctx: RequestContext
    ) -> GetProductsResponse:
        """Translate ``GET /v1/products`` upstream → AdCP ``Product[]``.

        Maps upstream ``pricing.cpm`` + ``min_spend`` onto an AdCP
        :class:`CpmPricingOption` (``pricing_model='cpm'``,
        ``fixed_price``, ``min_spend_per_package``). ``delivery_type``
        passes through unchanged (upstream and AdCP use the same
        ``guaranteed``/``non_guaranteed`` enum).
        """
        if ctx.account is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated account.",
                recovery="transient",
            )
        network_code = ctx.account.metadata["network_code"]
        client = self._client(ctx)
        payload = await upstream_helpers.list_products(client, network_code=network_code)
        self._record("products.list", {"network_code": network_code})
        agent_url = "https://reference.adcp.org"
        products: list[Product] = []
        for upstream_row in payload.get("products", []):
            pricing = upstream_row.get("pricing", {})
            pricing_model = pricing.get("model", "cpm")
            # The seller's ``pricing_models`` capability declares ``cpm``
            # only; skip upstream rows that price on any other model
            # (e.g. ``cpv``) rather than projecting them onto a CPM
            # pricing option and silently lying on the wire. Adopters
            # whose upstream supports ``cpv`` add an explicit branch
            # here that emits AdCP ``CpvPricingOption`` instead.
            if pricing_model != "cpm":
                logger.debug(
                    "Skipping product %r — pricing model %r not in seller's capability set",
                    upstream_row.get("product_id"),
                    pricing_model,
                )
                continue
            currency = pricing.get("currency", "USD")
            cpm = pricing.get("cpm")
            min_spend = pricing.get("min_spend")
            pricing_option: dict[str, Any] = {
                "pricing_option_id": f"{upstream_row['product_id']}-{pricing_model}",
                "pricing_model": "cpm",
                "currency": currency,
            }
            if cpm is not None:
                pricing_option["fixed_price"] = float(cpm)
            if min_spend is not None:
                pricing_option["min_spend_per_package"] = float(min_spend)
            # Project upstream format ids onto AdCP structured format
            # references. The reference seller's format namespace lives
            # at ``reference.adcp.org`` — adopters whose upstream uses a
            # different format namespace (their own publisher domain)
            # rewrite ``agent_url`` here.
            upstream_formats = upstream_row.get("format_ids") or []
            format_ids = [{"agent_url": agent_url, "id": fid} for fid in upstream_formats]
            if not format_ids:
                # Spec requires at least one format on the response.
                # Fall back to the channel-default — adopters with
                # richer per-product format tables wire the lookup here.
                channel = upstream_row.get("channel", "display")
                fallback_id = "display_300x250" if channel == "display" else "video_16x9_30s"
                format_ids = [{"agent_url": agent_url, "id": fallback_id}]
            products.append(
                Product.model_validate(
                    {
                        "product_id": upstream_row["product_id"],
                        "name": upstream_row["name"],
                        "description": upstream_row.get("name", ""),
                        "delivery_type": upstream_row.get("delivery_type", "non_guaranteed"),
                        "publisher_properties": [
                            # The reference seller is a single-publisher
                            # demo; ``selection_type='all'`` matches the
                            # spec's "all properties from this publisher"
                            # discriminator. Multi-publisher adopters
                            # narrow with ``selection_type='by_id'`` /
                            # ``'by_tag'``.
                            {
                                "publisher_domain": "reference.adcp.org",
                                "selection_type": "all",
                            }
                        ],
                        "format_ids": format_ids,
                        "reporting_capabilities": {
                            "available_reporting_frequencies": ["daily"],
                            "expected_delay_minutes": 240,
                            "timezone": "UTC",
                            "supports_webhooks": False,
                            "available_metrics": [
                                "impressions",
                                "spend",
                                "clicks",
                            ],
                            "date_range_support": "date_range",
                        },
                        "pricing_options": [pricing_option],
                    }
                )
            )
        return GetProductsResponse(products=products)

    # ----- create_media_buy ------------------------------------------------

    async def create_media_buy(self, req: CreateMediaBuyRequest, ctx: RequestContext):
        """``POST /v1/orders`` → upstream returns ``pending_approval``
        with an ``approval_task_id``. Hand off to a background coroutine
        that polls ``/v1/tasks/{id}`` until approved, then returns the
        :class:`CreateMediaBuySuccessResponse`.

        Buyer experience: ``{status: 'submitted', task_id}`` immediately;
        framework's task registry surfaces the success on
        ``tasks/get`` polling once the upstream approves.

        Measurement terms gating: this seller cannot guarantee zero
        variance on billing measurement (``max_variance_percent == 0``
        is unworkable for any real third-party measurement vendor). We
        reject such requests up front with ``TERMS_REJECTED`` rather
        than accepting them and letting the upstream silently violate
        the buyer's term. Adopters whose ad server has different terms
        capacity edit ``_reject_unworkable_terms`` to match.
        """
        self._reject_unworkable_terms(req)
        if ctx.buyer_agent is None or ctx.account is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated buyer_agent and account.",
                recovery="transient",
            )
        network_code = ctx.account.metadata["network_code"]
        advertiser_id = ctx.account.metadata["advertiser_id"]
        budget_amount = req.total_budget.amount if req.total_budget else 0.0
        budget_currency = req.total_budget.currency if req.total_budget else "USD"
        order_payload: dict[str, Any] = {
            "name": (
                req.brand.domain
                if req.brand and getattr(req.brand, "domain", None)
                else f"adcp-buy-{req.idempotency_key[:12]}"
            ),
            "advertiser_id": advertiser_id,
            "currency": budget_currency,
            "budget": float(budget_amount),
            "client_request_id": req.idempotency_key,
        }
        client = self._client(ctx)
        order = await upstream_helpers.create_order(
            client, network_code=network_code, payload=order_payload
        )
        self._record(
            "media_buy.create",
            {
                "network_code": network_code,
                "advertiser_id": advertiser_id,
                "order_id": order.get("order_id"),
            },
        )

        order_id: str = order["order_id"]
        approval_task_id: str | None = order.get("approval_task_id")
        # Sync fast path — the upstream may auto-approve on creation
        # for non-guaranteed delivery (rare, but possible).
        if order.get("status") in {"approved", "delivering"} and not approval_task_id:
            return await self._project_create_success(
                order, req, budget_amount, budget_currency, client, network_code
            )

        # No approval task but status not already terminal-success —
        # the upstream has either auto-progressed past creation or is
        # still pending. Refetch once and project from current status;
        # don't enter a polling loop we have no signal to drive.
        if approval_task_id is None:
            current = await upstream_helpers.get_order(
                client, network_code=network_code, order_id=order_id
            )
            self._record(
                "media_buy.confirm",
                {"order_id": order_id, "status": current.get("status")},
            )
            return await self._finalize_create_or_raise(
                current, req, budget_amount, budget_currency, client, network_code
            )

        # Slow path — hand off to background polling. The framework
        # allocates a task_id, returns the Submitted envelope, and runs
        # the handoff coroutine in the background. When this coroutine
        # returns, the framework persists the success as the terminal
        # artifact on the registry; buyers see it via ``tasks/get`` or
        # via the push-notification webhook. When this coroutine raises
        # :class:`AdcpError`, the framework persists ``failed`` with the
        # wire-shaped error payload — so terminal-failure projection
        # (rejected, timed-out polling) goes through ``raise``, not
        # through fabricating a success response.
        bound_task_id = approval_task_id

        async def _poll_until_approved(task_handoff_ctx: Any) -> CreateMediaBuySuccessResponse:
            del task_handoff_ctx
            for _ in range(self._approval_poll_max_iterations):
                task = await upstream_helpers.get_task(
                    client, network_code=network_code, task_id=bound_task_id
                )
                self._record(
                    "task.poll",
                    {"task_id": bound_task_id, "status": task.get("status")},
                )
                if task.get("status") == "completed":
                    result = task.get("result") or {}
                    if result.get("outcome") == "rejected":
                        raise AdcpError(
                            "POLICY_VIOLATION",
                            message=(result.get("reviewer_note") or "Upstream rejected the order."),
                            recovery="terminal",
                        )
                    break
                if task.get("status") == "rejected":
                    raise AdcpError(
                        "POLICY_VIOLATION",
                        message="Upstream rejected the order.",
                        recovery="terminal",
                    )
                # Jitter the poll interval so concurrent buys don't
                # synchronize their upstream calls. Honoring an upstream
                # ``Retry-After`` is a follow-up — it requires plumbing
                # the response headers through the SDK client.
                jitter = random.uniform(0.5, 1.5)
                await asyncio.sleep(self._approval_poll_interval_s * jitter)
            else:
                # Loop exhausted without a terminal task status. We
                # cannot project a success from a still-pending order,
                # and we cannot keep polling forever. Surface as a
                # transient failure so the buyer can retry the create
                # call later.
                raise AdcpError(
                    "SERVICE_UNAVAILABLE",
                    message=(
                        "Upstream approval task did not complete within polling window — "
                        "buyer should retry the create call later."
                    ),
                    recovery="transient",
                )
            # Refetch the order; project from the actual current status
            # rather than assume the broken-out loop saw a green light.
            approved_order = await upstream_helpers.get_order(
                client, network_code=network_code, order_id=order_id
            )
            self._record(
                "media_buy.confirm",
                {"order_id": order_id, "status": approved_order.get("status")},
            )
            return await self._finalize_create_or_raise(
                approved_order, req, budget_amount, budget_currency, client, network_code
            )

        # Reference seller is mock-mode against a fast upstream — auto-approval
        # completes within milliseconds. Awaiting the polling synchronously
        # lets create_media_buy return the full CreateMediaBuySuccessResponse
        # with media_buy_id in the response body, which is what AdCP
        # storyboards and most buyers expect. Production adopters with slow
        # real-world approvals swap this for the task-handoff path:
        #
        #     return ctx.handoff_to_task(_poll_until_approved)
        #
        # which returns a Submitted({task_id, status: 'submitted'}) envelope
        # and runs the polling coroutine in the background while buyers
        # poll via tasks/get.
        return await _poll_until_approved(None)

    def _reject_unworkable_terms(self, req: CreateMediaBuyRequest) -> None:
        """Reject ``create_media_buy`` requests whose ``measurement_terms``
        propose terms this seller cannot fulfill.

        Adopters tune this list to match their ad-server's tolerance.
        For the reference seller we reject:

        * ``billing_measurement.max_variance_percent == 0`` — zero
          variance on third-party measurement is unworkable; any real
          measurement vendor has noise floor > 0.
        """
        for pkg in req.packages or []:
            measurement_terms = getattr(pkg, "measurement_terms", None)
            if measurement_terms is None:
                continue
            billing = getattr(measurement_terms, "billing_measurement", None)
            if billing is None:
                continue
            mvp = getattr(billing, "max_variance_percent", None)
            if mvp is not None and mvp <= 0:
                raise AdcpError(
                    "TERMS_REJECTED",
                    message=(
                        "billing_measurement.max_variance_percent must be > 0. "
                        "Zero-variance measurement is unworkable — every real "
                        "third-party measurement vendor has a non-zero noise "
                        "floor. Propose a variance >= 5% to match this seller's "
                        "measurement capacity."
                    ),
                    recovery="correctable",
                    field="packages[].measurement_terms.billing_measurement.max_variance_percent",
                )

    async def _finalize_create_or_raise(
        self,
        order: dict[str, Any],
        req: CreateMediaBuyRequest,
        budget_amount: float,
        budget_currency: str,
        client: UpstreamHttpClient,
        network_code: str,
    ) -> CreateMediaBuySuccessResponse:
        """Project a terminal upstream order onto a buyer-facing success
        response — but refuse to fabricate success when the upstream is
        still ``pending_approval`` / ``draft``, or has gone ``rejected``.
        """
        upstream_status = order.get("status", "")
        if upstream_status == "rejected":
            # Spec doesn't carry a "human approver rejected" code; the
            # closest match is ``PERMISSION_DENIED`` (recovery=terminal),
            # which buyers handle by surfacing the rejection to the
            # operator rather than retrying.
            raise AdcpError(
                "PERMISSION_DENIED",
                message="Upstream rejected the order during human approval review.",
                recovery="terminal",
            )
        if upstream_status in {"pending_approval", "draft"}:
            # Reached only when the polling window ran out OR the
            # no-task refetch path saw the order still pending. Either
            # way, transient — the buyer retries.
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message=(
                    f"Upstream order is still in {upstream_status!r} status — "
                    "approval has not completed. Buyer should retry the create "
                    "call later."
                ),
                recovery="transient",
            )
        return await self._project_create_success(
            order, req, budget_amount, budget_currency, client, network_code
        )

    async def _project_create_success(
        self,
        order: dict[str, Any],
        req: CreateMediaBuyRequest,
        budget_amount: float,
        budget_currency: str,
        client: UpstreamHttpClient,
        network_code: str,
    ) -> CreateMediaBuySuccessResponse:
        """Translate upstream ``Order`` to AdCP
        :class:`CreateMediaBuySuccessResponse`.

        Side-effect: persists each requested package as an upstream
        line item via ``add_line_item``, then mirrors the spec-marked
        echo fields into the seller-local shadow store keyed by the
        line-item id. The upstream-issued ``line_item_id`` is returned
        as the AdCP ``package_id``.
        """
        invoice_recipient = None
        if req.invoice_recipient is not None:
            # Project bank details out before echoing on response.
            invoice_recipient = project_business_entity_for_response(req.invoice_recipient)
        del budget_amount, budget_currency
        wire_status = _DELIVERY_STATUS_MAP.get(order.get("status", ""), "active")
        # Spec: when no creatives were supplied at create time, the buy
        # transitions to ``pending_creatives`` until the buyer calls
        # ``sync_creatives``. Otherwise carry the upstream-derived status.
        req_packages = list(req.packages or [])
        no_creatives_supplied = req_packages and all(
            getattr(pkg, "creatives", None) is None
            and getattr(pkg, "creative_assignments", None) is None
            for pkg in req_packages
        )
        if no_creatives_supplied:
            wire_status = "pending_creatives"
        order_id = order["order_id"]
        buy_state = self._buy_state.setdefault(order_id, {"packages": {}, "canceled": False})
        response_packages: list[dict[str, Any]] = []
        for idx, pkg in enumerate(req_packages):
            line_item = await upstream_helpers.add_line_item(
                client,
                network_code=network_code,
                order_id=order_id,
                payload={
                    "product_id": pkg.product_id,
                    "budget": float(getattr(pkg, "budget", 0.0) or 0.0),
                    "client_request_id": f"{req.idempotency_key}:pkg:{idx}",
                },
            )
            package_id = str(line_item.get("line_item_id") or f"pkg_{order_id}_{idx:03d}")
            echo = _project_request_package_echo(pkg)
            buy_state["packages"][package_id] = {
                **echo,
                "canceled": False,
                "paused": False,
            }
            response_packages.append({"package_id": package_id, **echo})
        return CreateMediaBuySuccessResponse.model_validate(
            {
                "media_buy_id": order_id,
                "status": wire_status,
                "packages": response_packages,
                "invoice_recipient": (
                    invoice_recipient.model_dump(mode="json", exclude_none=True)
                    if invoice_recipient is not None
                    else None
                ),
            }
        )

    # ----- update_media_buy ------------------------------------------------

    async def update_media_buy(
        self, media_buy_id: str, patch: UpdateMediaBuyRequest, ctx: RequestContext
    ) -> UpdateMediaBuySuccessResponse:
        """Apply buyer-supplied changes to a media buy.

        The mock-server upstream has no PATCH endpoint for orders or line
        items, so cancel / pause / package-state changes are stored in the
        seller-local shadow store (``self._buy_state``). Per-package
        creative assignments DO go upstream via ``POST
        /v1/orders/{id}/lineitems/{li}/creative-attach``. Real GAM-fronting
        adopters wire the cancel/pause path to
        ``LineItemService.performLineItemAction`` and drop the shadow
        store.
        """
        if ctx.account is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated account.",
                recovery="transient",
            )
        network_code = ctx.account.metadata["network_code"]
        client = self._client(ctx)

        # Validate the media buy exists upstream. The SDK maps a 404 onto
        # ``MEDIA_BUY_NOT_FOUND`` automatically.
        await upstream_helpers.get_order(client, network_code=network_code, order_id=media_buy_id)

        # Validate referenced packages exist on the order. The mock's
        # ``serializeOrder`` strips ``line_items`` from ``GET /orders/{id}``
        # — they're only readable via the dedicated lineitems endpoint.
        line_items = await upstream_helpers.list_line_items(
            client, network_code=network_code, order_id=media_buy_id
        )
        existing_ids = {li.get("line_item_id") for li in line_items if li.get("line_item_id")}

        if patch.packages:
            for pkg_patch in patch.packages:
                pkg_id = getattr(pkg_patch, "package_id", None)
                if pkg_id is not None and pkg_id not in existing_ids:
                    raise AdcpError(
                        "PACKAGE_NOT_FOUND",
                        message=(
                            f"Package {pkg_id!r} not found on media buy "
                            f"{media_buy_id!r}. The buyer must reference an "
                            f"existing package — see ``create_media_buy``'s "
                            f"response for the issued package_ids."
                        ),
                        recovery="terminal",
                    )

        buy_state = self._buy_state.setdefault(
            media_buy_id, {"packages": {}, "canceled": False, "paused": False}
        )

        # Buy-level cancel — irreversible. A second cancel is NOT_CANCELLABLE.
        if patch.canceled:
            if buy_state.get("canceled"):
                raise AdcpError(
                    "NOT_CANCELLABLE",
                    message=(
                        f"Media buy {media_buy_id!r} is already canceled. "
                        "Cancellation is irreversible."
                    ),
                    recovery="terminal",
                )
            buy_state["canceled"] = True
            buy_state["cancellation_reason"] = patch.cancellation_reason
        # Buy-level pause / resume.
        if patch.paused is not None:
            buy_state["paused"] = bool(patch.paused)

        affected_packages: list[dict[str, Any]] = []
        for pkg_patch in patch.packages or []:
            pkg_id = getattr(pkg_patch, "package_id", None)
            if pkg_id is None:
                continue
            pkg_state = buy_state["packages"].setdefault(
                pkg_id, {"canceled": False, "paused": False}
            )
            if getattr(pkg_patch, "canceled", None):
                pkg_state["canceled"] = True
            if getattr(pkg_patch, "paused", None) is not None:
                pkg_state["paused"] = bool(pkg_patch.paused)
            # Echo-field patches use replacement semantics per the wire
            # spec on UpdateMediaBuyRequest.Package — when the buyer
            # supplies a field, it replaces the persisted value; when the
            # field is absent, the previous value survives.
            patch_echo = _project_request_package_echo(pkg_patch)
            pkg_state.update(patch_echo)
            # Attach buyer-assigned creatives upstream so the mock surfaces
            # the assignment on subsequent ``GET .../lineitems`` reads.
            new_assignments = list(getattr(pkg_patch, "creative_assignments", None) or [])
            if new_assignments:
                existing_assignments = list(pkg_state.get("creative_assignments") or [])
                seen_creative_ids = {
                    a.get("creative_id") if isinstance(a, dict) else getattr(a, "creative_id", None)
                    for a in existing_assignments
                }
                for ca in new_assignments:
                    creative_id = getattr(ca, "creative_id", None)
                    if creative_id is None:
                        continue
                    if creative_id in seen_creative_ids:
                        continue
                    # Translate buyer's creative_id to the upstream id
                    # before issuing attach_creative. Pass through unchanged
                    # when no mapping is known (the upstream will surface
                    # a 404 → CREATIVE_NOT_FOUND).
                    upstream_creative_id = self._creative_id_map.get(creative_id, creative_id)
                    await upstream_helpers.attach_creative(
                        client,
                        network_code=network_code,
                        order_id=media_buy_id,
                        line_item_id=pkg_id,
                        creative_id=upstream_creative_id,
                    )
                    existing_assignments.append(
                        ca.model_dump(mode="json", exclude_none=True)
                        if hasattr(ca, "model_dump")
                        else {"creative_id": creative_id}
                    )
                    seen_creative_ids.add(creative_id)
                pkg_state["creative_assignments"] = existing_assignments
            affected_packages.append({"package_id": pkg_id, **_projected_package_state(pkg_state)})

        # Bump the optimistic-concurrency revision token.
        revision = self._buy_revisions.get(media_buy_id, 0) + 1
        self._buy_revisions[media_buy_id] = revision

        # Compute response status. Cancel beats pause beats whatever the
        # upstream says — buyer's intent is the source of truth for the
        # local-state fields.
        if buy_state.get("canceled"):
            response_status = "canceled"
        elif buy_state.get("paused"):
            response_status = "paused"
        else:
            order_now = await upstream_helpers.get_order(
                client, network_code=network_code, order_id=media_buy_id
            )
            upstream_status = order_now.get("status", "")
            # If creatives have just landed on previously empty line items,
            # the buy advances out of pending_creatives.
            any_creatives = any(
                (state.get("creative_assignments") or [])
                for state in buy_state["packages"].values()
            )
            response_status = _DELIVERY_STATUS_MAP.get(upstream_status, "active")
            if response_status == "pending_creatives" and any_creatives:
                response_status = "pending_start"

        return UpdateMediaBuySuccessResponse.model_validate(
            {
                "media_buy_id": media_buy_id,
                "status": response_status,
                "revision": revision,
                "affected_packages": affected_packages or None,
            }
        )

    # ----- sync_creatives --------------------------------------------------

    async def sync_creatives(
        self, req: SyncCreativesRequest, ctx: RequestContext
    ) -> SyncCreativesSuccessResponse:
        """``POST /v1/creatives`` per creative.

        Idempotency: the upstream accepts ``client_request_id`` per
        upload; we pass the AdCP ``creative_id`` through so a buyer
        re-syncing the same creative_id is upstream-deduplicated.
        """
        if ctx.account is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated account.",
                recovery="transient",
            )
        network_code = ctx.account.metadata["network_code"]
        advertiser_id = ctx.account.metadata["advertiser_id"]
        results: list[SyncCreativeResult] = []
        client = self._client(ctx)
        for creative in req.creatives:
            # Re-sync of a known creative_id is a no-op upload — the
            # upstream's idempotency table is keyed on body-fingerprint
            # and rejects same-key/different-body with 409. The spec
            # treats sync as an upsert that may carry refreshed assets;
            # the seller treats the second call as "creative already
            # known, just acknowledge". The buyer's intent for the new
            # placement flows through the ``assignments`` field below.
            if creative.creative_id in self._creative_id_map:
                results.append(
                    SyncCreativeResult.model_validate(
                        {
                            "creative_id": creative.creative_id,
                            "action": "unchanged",
                            "status": creative.status or "approved",
                        }
                    )
                )
                continue
            # The upstream's ``format_id`` is a string; the AdCP
            # ``format_id`` is a structured ``{agent_url, id}`` object.
            # Pass the ``id`` through — adopters whose upstream uses a
            # different format namespace map across here.
            format_id_raw = creative.format_id
            format_id_str = format_id_raw.id if hasattr(format_id_raw, "id") else str(format_id_raw)
            payload: dict[str, Any] = {
                "name": creative.name,
                "format_id": format_id_str,
                "advertiser_id": advertiser_id,
                "client_request_id": creative.creative_id,
            }
            snippet = getattr(creative, "snippet", None)
            if snippet is not None:
                payload["snippet"] = str(snippet)
            upstream_resp = await upstream_helpers.upload_creative(
                client, network_code=network_code, payload=payload
            )
            upstream_id = str(upstream_resp.get("creative_id") or "")
            if upstream_id:
                self._creative_id_map[creative.creative_id] = upstream_id
                self._creative_id_reverse[upstream_id] = creative.creative_id
            results.append(
                SyncCreativeResult.model_validate(
                    {
                        "creative_id": creative.creative_id,
                        "action": "created",
                        "status": creative.status or "approved",
                    }
                )
            )
        # Apply bulk creative-to-package assignments. Each entry attaches
        # the creative upstream via the line-item's creative-attach
        # endpoint. Missing buy/package context is the buyer's error to
        # diagnose — sellers surface upstream errors verbatim.
        for assignment in getattr(req, "assignments", None) or []:
            buyer_creative_id = getattr(assignment, "creative_id", None)
            package_id = getattr(assignment, "package_id", None)
            if not buyer_creative_id or not package_id:
                continue
            upstream_creative_id = self._creative_id_map.get(buyer_creative_id, buyer_creative_id)
            # Find the owning order via the shadow store: package_ids are
            # globally unique (upstream line_item ids).
            owning_order_id = next(
                (
                    oid
                    for oid, state in self._buy_state.items()
                    if package_id in state.get("packages", {})
                ),
                None,
            )
            if owning_order_id is None:
                continue
            await upstream_helpers.attach_creative(
                client,
                network_code=network_code,
                order_id=owning_order_id,
                line_item_id=package_id,
                creative_id=upstream_creative_id,
            )
            pkg_state = self._buy_state[owning_order_id]["packages"].setdefault(
                package_id, {"canceled": False, "paused": False}
            )
            existing = list(pkg_state.get("creative_assignments") or [])
            if not any(
                (e.get("creative_id") if isinstance(e, dict) else getattr(e, "creative_id", None))
                == buyer_creative_id
                for e in existing
            ):
                existing.append({"creative_id": buyer_creative_id})
                pkg_state["creative_assignments"] = existing
        self._record(
            "creative.upload",
            {"network_code": network_code, "count": len(req.creatives) if req.creatives else 0},
        )
        return SyncCreativesSuccessResponse(creatives=results)

    # ----- get_media_buy_delivery ------------------------------------------

    async def get_media_buy_delivery(
        self, req: GetMediaBuyDeliveryRequest, ctx: RequestContext
    ) -> GetMediaBuyDeliveryResponse:
        """``GET /v1/orders/{id}/delivery`` → AdCP delivery shape.

        The request lists media_buy_ids; we fan out one upstream call
        per id. Adopters whose upstream supports batch delivery
        replace this with a single batched call.
        """
        if ctx.account is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated account.",
                recovery="transient",
            )
        network_code = ctx.account.metadata["network_code"]
        media_buy_ids: list[str] = list(getattr(req, "media_buy_ids", None) or [])
        # Defaults — the spec requires ``reporting_period`` + ``currency``
        # on the response root even when no buys are returned. We carry
        # them from the first upstream report that succeeds.
        report_currency = "USD"
        report_period: dict[str, Any] | None = None
        delivery_rows: list[dict[str, Any]] = []
        client = self._client(ctx)
        for order_id in media_buy_ids:
            try:
                upstream_row = await upstream_helpers.get_delivery(
                    client, network_code=network_code, order_id=order_id
                )
            except AdcpError as exc:
                # 404 on delivery → skip this buy (the spec allows
                # partial responses). Other errors propagate.
                if exc.code == "MEDIA_BUY_NOT_FOUND":
                    continue
                raise
            # The mock's DeliveryReport schema doesn't carry order
            # status (see openapi.yaml § DeliveryReport). Double-fetch
            # the order so we project the correct AdCP MediaBuyStatus
            # — completed / canceled / rejected buys would otherwise
            # all surface as 'active' to the buyer.
            try:
                order_meta = await upstream_helpers.get_order(
                    client, network_code=network_code, order_id=order_id
                )
                upstream_status = order_meta.get("status", "")
            except AdcpError as exc:
                if exc.code == "MEDIA_BUY_NOT_FOUND":
                    # Delivery row exists but order is gone — odd,
                    # surface as 'active' so the row is at least
                    # well-formed; the operator's audit log will catch it.
                    upstream_status = ""
                else:
                    raise
            wire_status = _DELIVERY_STATUS_MAP.get(upstream_status, "active")
            buy_state = self._buy_state.get(order_id, {})
            if buy_state.get("canceled"):
                wire_status = "canceled"
            elif buy_state.get("paused"):
                wire_status = "paused"
            totals = upstream_row.get("totals", {})
            report_currency = upstream_row.get("currency", report_currency)
            if report_period is None and upstream_row.get("reporting_period"):
                report_period = upstream_row["reporting_period"]
            delivery_rows.append(
                {
                    "media_buy_id": order_id,
                    "status": wire_status,
                    "totals": {
                        "impressions": int(totals.get("impressions", 0)),
                        "clicks": int(totals.get("clicks", 0)),
                        "spend": float(totals.get("spend", 0.0)),
                    },
                    "by_package": [],
                }
            )
        self._record(
            "delivery.read",
            {"network_code": network_code, "count": len(media_buy_ids)},
        )
        # The mock-server returns a per-order reporting_period; if no
        # upstream call succeeded (no media_buy_ids, or all 404'd), use
        # a now-anchored window. Adopters with a richer reporting
        # surface plumb a request-level start/end through.
        if report_period is None:
            now = datetime.now(timezone.utc)
            report_period = {
                "start": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                "end": now.isoformat(),
            }
        return GetMediaBuyDeliveryResponse.model_validate(
            {
                "reporting_period": report_period,
                "currency": report_currency,
                "media_buy_deliveries": delivery_rows,
            }
        )

    # ----- get_media_buys --------------------------------------------------

    async def get_media_buys(
        self, req: GetMediaBuysRequest, ctx: RequestContext
    ) -> GetMediaBuysResponse:
        """``GET /v1/orders`` → AdCP ``MediaBuy[]``.

        Pagination is offset/limit applied client-side after the
        upstream returns the full list. Adopters whose upstream
        supports cursor pagination plumb the cursor through here.
        """
        if ctx.account is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated account.",
                recovery="transient",
            )
        network_code = ctx.account.metadata["network_code"]
        advertiser_id = ctx.account.metadata["advertiser_id"]
        limit = 50
        offset = 0
        if req.pagination is not None:
            limit = getattr(req.pagination, "limit", None) or 50
            offset = getattr(req.pagination, "offset", None) or 0
        client = self._client(ctx)
        payload = await upstream_helpers.list_orders(client, network_code=network_code)
        # Filter to this advertiser_id (the upstream is per-network,
        # but a single network can host multiple advertisers under the
        # same network_code — our AdCP account maps to one of them).
        upstream_orders = [
            o for o in payload.get("orders", []) if o.get("advertiser_id") == advertiser_id
        ]
        # Narrow to the requested media_buy_ids when the buyer supplied
        # them. Storyboards chain get_media_buys after create with the
        # captured media_buy_id; without this filter the response leaks
        # every advertiser-scoped buy and the buyer's ``media_buys[0]``
        # lookup hits a different scenario's order.
        if getattr(req, "media_buy_ids", None):
            wanted_ids = {str(x) for x in req.media_buy_ids}
            upstream_orders = [o for o in upstream_orders if o.get("order_id") in wanted_ids]
        page = upstream_orders[offset : offset + limit]
        media_buys: list[dict[str, Any]] = []
        for order in page:
            order_id = order["order_id"]
            buy_state = self._buy_state.get(order_id, {})
            wire_status = _DELIVERY_STATUS_MAP.get(order.get("status", ""), "active")
            if buy_state.get("canceled"):
                wire_status = "canceled"
            elif buy_state.get("paused"):
                wire_status = "paused"
            line_items = await upstream_helpers.list_line_items(
                client, network_code=network_code, order_id=order_id
            )
            packages: list[dict[str, Any]] = []
            for li in line_items:
                pkg_id = li.get("line_item_id")
                if pkg_id is None:
                    continue
                pkg_state = buy_state.get("packages", {}).get(pkg_id, {})
                pkg_entry: dict[str, Any] = {
                    "package_id": pkg_id,
                    **_projected_package_state(pkg_state),
                }
                if pkg_state.get("canceled"):
                    pkg_entry["canceled"] = True
                if pkg_state.get("paused"):
                    pkg_entry["paused"] = True
                packages.append(pkg_entry)
            media_buys.append(
                {
                    "media_buy_id": order_id,
                    "status": wire_status,
                    "currency": order.get("currency", "USD"),
                    "total_budget": float(order.get("budget", 0.0)),
                    "packages": packages,
                    "created_at": order.get("created_at"),
                    "updated_at": order.get("updated_at"),
                }
            )
        self._record(
            "media_buys.list",
            {
                "network_code": network_code,
                "advertiser_id": advertiser_id,
                "limit": limit,
                "offset": offset,
            },
        )
        return GetMediaBuysResponse.model_validate({"media_buys": media_buys})

    # ----- provide_performance_feedback ------------------------------------

    async def provide_performance_feedback(
        self, req: ProvidePerformanceFeedbackRequest, ctx: RequestContext
    ) -> ProvidePerformanceFeedbackSuccessResponse:
        """``POST /v1/orders/{id}/conversions`` (CAPI).

        CAPI is the GAM-flavored equivalent of buyer-supplied
        performance feedback, but the shapes don't line up cleanly:
        AdCP perf feedback is an aggregate ``(media_buy_id,
        metric_type, value)`` over a measurement window; CAPI ingests
        per-event records. The only AdCP metric whose semantics map
        even loosely is ``conversion_rate`` (a measured rate that we
        project as a single dedup'd CAPI event). For every other
        AdCP metric_type we raise ``INVALID_REQUEST`` rather than
        fabricate a synthetic event upstream. See MIGRATION.md →
        "CAPI semantic mismatch".
        """
        if ctx.account is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated account.",
                recovery="transient",
            )
        network_code = ctx.account.metadata["network_code"]
        metric_type = (
            (req.metric_type.value if hasattr(req.metric_type, "value") else str(req.metric_type))
            if req.metric_type is not None
            else None
        )
        if metric_type != "conversion_rate":
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    f"This seller only ingests metric_type='conversion_rate' via CAPI; "
                    f"got {metric_type!r}. AdCP aggregate metrics don't round-trip to "
                    "CAPI's per-event ingest — see MIGRATION.md "
                    "(CAPI semantic mismatch)."
                ),
                recovery="terminal",
                field="metric_type",
            )
        # Use measurement_period.end (or now) as the event_time.
        period = getattr(req, "measurement_period", None)
        period_end = getattr(period, "end", None) if period is not None else None
        event_time = (
            int(period_end.timestamp())
            if isinstance(period_end, datetime)
            else int(datetime.now(timezone.utc).timestamp())
        )
        # ``performance_index`` is the spec field; default to 1.0 if
        # the buyer omitted it (the spec allows it on conversion-rate
        # and similar metrics where the value lives elsewhere).
        performance_index = float(getattr(req, "performance_index", None) or 1.0)
        payload: dict[str, Any] = {
            "order_id": req.media_buy_id,
            "conversions": [
                {
                    "event_name": metric_type,
                    "event_time": event_time,
                    "value": performance_index,
                    "dedup_key": f"{req.media_buy_id}:{metric_type}:{event_time}",
                }
            ],
        }
        client = self._client(ctx)
        await upstream_helpers.post_conversions(
            client,
            network_code=network_code,
            order_id=req.media_buy_id,
            payload=payload,
        )
        self._record(
            "performance.feedback",
            {"media_buy_id": req.media_buy_id, "metric_type": metric_type},
        )
        return ProvidePerformanceFeedbackSuccessResponse.model_validate({"success": True})

    # ----- list_creative_formats -------------------------------------------

    async def list_creative_formats(
        self, req: ListCreativeFormatsRequest, ctx: RequestContext
    ) -> ListCreativeFormatsResponse:
        """Static catalog of accepted formats — the upstream has no
        format-list endpoint (formats are publisher-defined, baked
        into the upstream's product catalog). Real adopters drive this
        from a creative-format registry."""
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
        """``GET /v1/creatives`` → AdCP ``Creative[]``.

        Pagination is offset/limit applied client-side after the
        upstream returns the full list.
        """
        if ctx.account is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated account.",
                recovery="transient",
            )
        network_code = ctx.account.metadata["network_code"]
        advertiser_id = ctx.account.metadata["advertiser_id"]
        agent_url = "https://reference.adcp.org"
        limit = 50
        offset = 0
        if req.pagination is not None:
            limit = getattr(req.pagination, "limit", None) or 50
            offset = getattr(req.pagination, "offset", None) or 0
        client = self._client(ctx)
        payload = await upstream_helpers.list_creatives(client, network_code=network_code)
        upstream_creatives = [
            c for c in payload.get("creatives", []) if c.get("advertiser_id") == advertiser_id
        ]
        # Apply the buyer-supplied filter on creative_ids. The wire schema
        # accepts buyer-facing ids; translate each through the buyer↔upstream
        # map before matching upstream rows.
        filters = getattr(req, "filters", None)
        wanted_ids = list(getattr(filters, "creative_ids", None) or []) if filters else []
        if wanted_ids:
            upstream_wanted = {self._creative_id_map.get(cid, cid) for cid in wanted_ids}
            upstream_creatives = [
                c for c in upstream_creatives if c.get("creative_id") in upstream_wanted
            ]
        total = len(upstream_creatives)
        page = upstream_creatives[offset : offset + limit]
        creatives = [
            {
                # Surface the buyer's original creative_id when the seller
                # owns the mapping; falls back to the upstream id when the
                # creative was synced outside this seller instance.
                "creative_id": self._creative_id_reverse.get(c["creative_id"], c["creative_id"]),
                "name": c["name"],
                "format_id": {"agent_url": agent_url, "id": c.get("format_id", "")},
                "status": _project_creative_status(c.get("status", "active")),
                "created_date": c.get("created_at"),
                "updated_date": c.get("created_at"),
            }
            for c in page
        ]
        has_more = offset + len(creatives) < total
        self._record(
            "creatives.list",
            {"network_code": network_code, "advertiser_id": advertiser_id},
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

        **Local Postgres only — this is the translator's commercial
        identity layer.** The AdCP account → upstream ``network_code``
        mapping is the durable record this seller owns; the upstream
        ad server doesn't model AdCP accounts at all.
        """
        if ctx.buyer_agent is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated buyer_agent.",
                recovery="transient",
            )
        tenant = current_tenant()
        if tenant is None:
            raise AdcpError(
                "AUTH_REQUIRED",
                message="sync_accounts requires a tenant context.",
                recovery="terminal",
            )
        results: list[dict[str, Any]] = []
        async with self._sessionmaker() as session, session.begin():
            ba_q = await session.execute(
                select(BuyerAgentRow).where(
                    BuyerAgentRow.tenant_id == tenant.id,
                    BuyerAgentRow.agent_url == ctx.buyer_agent.agent_url,
                )
            )
            buyer_agent_row = ba_q.scalar_one_or_none()
            if buyer_agent_row is None:
                raise AdcpError(
                    "SERVICE_UNAVAILABLE",
                    message=(
                        "Authenticated buyer_agent has no matching row — registry / table drift."
                    ),
                    recovery="transient",
                )
            for incoming in req.accounts:
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

        Local Postgres only — the upstream doesn't know about AdCP
        accounts. Every row is run through
        :func:`project_account_for_response` so the spec's
        write-only ``billing_entity.bank`` field cannot leak on the
        wire.
        """
        if ctx.buyer_agent is None:
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Dispatch should have populated buyer_agent.",
                recovery="transient",
            )
        tenant = current_tenant()
        if tenant is None:
            raise AdcpError(
                "AUTH_REQUIRED",
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
            # Total-count probe runs against the same WHERE clause as
            # the page query so ``pagination.total_count`` matches
            # ``list_creatives`` semantics. Adopters with very large
            # account tables swap this for a separate count() query
            # rather than materializing all rows.
            all_q = await session.execute(stmt)
            total_count = len(list(all_q.scalars()))
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
            safe = project_account_for_response(wire_account)
            projected_accounts.append(safe.model_dump(mode="json", exclude_none=True))
        self._record("accounts.list", {"buyer_agent_id": ctx.buyer_agent.agent_url})
        has_more = offset + len(rows) < total_count
        return ListAccountsResponse.model_validate(
            {
                "accounts": projected_accounts,
                "pagination": {"has_more": has_more, "total_count": total_count},
            }
        )


def _project_creative_status(upstream_status: str) -> str:
    """Translate the upstream's ``Creative.status`` enum (active/paused/
    archived) onto the AdCP ``CreativeStatus`` enum (approved/
    pending_review/rejected/archived/processing).

    Adopters whose upstream models richer review states upgrade this
    table.
    """
    if upstream_status == "archived":
        return "archived"
    if upstream_status == "paused":
        # ``paused`` upstream means an operator has held the creative
        # back from serving — surface as ``pending_review`` so the
        # buyer knows it's not currently approved-and-eligible.
        return "pending_review"
    return "approved"


__all__ = ["V3ReferenceSeller"]
