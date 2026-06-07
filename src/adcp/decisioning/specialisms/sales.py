"""SalesPlatform Protocol — covers every ``sales-*`` specialism.

A platform claiming any of the spec ``sales-*`` slugs
(``sales-non-guaranteed``, ``sales-guaranteed``, ``sales-broadcast-tv``,
``sales-social``, ``sales-proposal-mode``, ``sales-catalog-driven``)
implements the methods on this Protocol. The slugs mirror
``schemas/cache/enums/specialism.json``. The unified hybrid shape
collapses 14 method names from v1's dual-method design
(``createMediaBuy`` + ``createMediaBuyTask``) into 7: each mutating
tool returns ``SalesResult[TSuccess]`` so adopters branch per call
between the sync fast path and the HITL slow path.

Required methods (every sales-* specialism):

* :meth:`get_products` — sync catalog read
* :meth:`create_media_buy` — hybrid (sync success or task handoff)
* :meth:`update_media_buy` — sync (v6.1 + adcp#3392 expand to hybrid)
* :meth:`sync_creatives` — hybrid for creative review
* :meth:`get_media_buy_delivery` — sync delivery read

Optional methods present-or-absent (gated by specialism — see per-method
docstrings):

* :meth:`get_media_buys`
* :meth:`provide_performance_feedback`
* :meth:`list_creative_formats`
* :meth:`list_creatives`

Required only when claiming ``sales-catalog-driven``:

* :meth:`sync_catalogs` — catalog sync and discovery. ``validate_platform``
  hard-fails at server boot if a ``sales-catalog-driven`` platform doesn't
  implement this.

The framework's :func:`validate_platform` walks ``capabilities.specialisms``
and confirms each specialism's required methods exist on the platform
subclass — fail-fast at server boot rather than 404 at first dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import DiscoveryResult, MaybeAsync, SalesResult

# Wire types — auto-generated from schemas/cache/3.0.0/*.json. Adopters
# import from ``adcp.types``; the Protocol uses string-name references
# under TYPE_CHECKING to avoid forcing the import at module load time
# (the wire-types module is heavy — it pulls in 80+ generated classes —
# and a Protocol-only import shouldn't require it).
if TYPE_CHECKING:
    from adcp.types import (
        CreateMediaBuyRequest,
        CreateMediaBuySuccessResponse,
        GetMediaBuyDeliveryRequest,
        GetMediaBuyDeliveryResponse,
        GetMediaBuysRequest,
        GetMediaBuysResponse,
        GetProductsRequest,
        GetProductsResponse,
        ListCreativeFormatsRequest,
        ListCreativeFormatsResponse,
        ListCreativesRequest,
        ListCreativesResponse,
        ProvidePerformanceFeedbackRequest,
        ProvidePerformanceFeedbackResponse,
        SyncCatalogsRequest,
        SyncCatalogsSuccessResponse,
        SyncCreativesRequest,
        SyncCreativesSuccessResponse,
        UpdateMediaBuyRequest,
        UpdateMediaBuySuccessResponse,
    )

#: Per-platform metadata generic; matches ``RequestContext[TMeta]`` and
#: ``Account[TMeta]`` upstream so a platform parameterizing
#: ``SalesPlatform[TenantMeta]`` gets ``ctx.account.metadata``-style typed
#: access inside method bodies.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class SalesPlatform(Protocol, Generic[TMeta]):
    """Unified hybrid interface for every ``sales-*`` specialism.

    Methods may be sync (return ``T`` directly) or async (return
    ``Awaitable[T]``); the dispatch adapter detects via
    :func:`inspect.iscoroutinefunction` and runs sync methods on a
    thread pool via :func:`asyncio.to_thread` so a blocking sync
    handler doesn't serialize the event loop.

    Hybrid sellers (programmatic remnant + guaranteed inventory in
    one tenant) branch per call: return the Success directly for the
    sync fast path, return ``ctx.handoff_to_task(fn)`` for the HITL
    slow path. The framework dispatcher detects the
    :class:`TaskHandoff` via type-identity and projects to the wire
    ``Submitted`` envelope.

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    rejection (``BUDGET_TOO_LOW``, ``POLICY_VIOLATION``, etc.); the
    framework projects to the wire structured-error envelope with
    code, recovery, field, suggestion, retry_after, details.
    """

    # ---- Required for every sales-* specialism ----

    def get_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext[TMeta],
    ) -> DiscoveryResult[GetProductsResponse]:
        """Catalog discovery — synchronous by default, MAY hand off.

        Return :class:`GetProductsResponse` directly for the sync fast
        path. Brief / refine discovery MAY hand off via
        ``ctx.handoff_to_task(fn)`` when composing the catalog needs
        background work (custom curation queue, slow proposal
        generation); the framework projects the handoff to the wire
        ``submitted`` envelope. The buyer reaches the terminal
        :class:`GetProductsResponse` via ``tasks/get`` polling, and — when
        the request carried ``push_notification_config`` — the framework
        also delivers the terminal completion / failure webhook to the
        buyer's URL from the background completion path (exactly once).
        ``get_products`` exposes the ``submitted`` / ``working`` /
        ``input_required`` async arms.

        Wholesale (``buying_mode='wholesale'``) MUST return synchronously
        — it is a raw rate-card read with no seller-side composition to
        background. A wholesale call that cannot finish within the
        buyer's ``time_budget`` declares the gap via ``incomplete[]`` on
        a sync response rather than handing off; returning a handoff from
        a wholesale call is rejected at the framework layer with
        ``AdcpError(INVALID_REQUEST, field='buying_mode')``.

        Brief-based proposal generation rides on a separate verb
        (``request_proposal``, adcp#3407); proposal-mode adopters
        surface the eventual products via
        ``ctx.publish_status_change(resource_type='proposal', ...)``
        rather than blocking ``get_products`` waiting for trafficker
        approval.

        **Buying mode dispatch:** when ``req.buying_mode == 'refine'`` and
        the platform implements :meth:`refine_get_products`, the framework
        dispatches there instead of this method.  Platforms that do not
        implement ``refine_get_products`` reject ``buying_mode='refine'``
        with ``AdcpError(INVALID_REQUEST, field='buying_mode')`` at the
        framework layer — the platform method is not called.
        """
        ...

    # Truly optional — adopters who don't implement refine_get_products
    # remain structurally conformant.  The framework uses
    # :func:`adcp.decisioning.has_refine_support` (a ``hasattr`` check) at
    # dispatch time and rejects ``buying_mode='refine'`` with
    # ``AdcpError(INVALID_REQUEST, field='buying_mode')`` when absent.
    #
    # Implementations match this signature::
    #
    #     def refine_get_products(
    #         self,
    #         req: GetProductsRequest,
    #         ctx: RequestContext[TMeta],
    #     ) -> MaybeAsync[RefineResult]: ...
    #
    # Return a :class:`adcp.decisioning.RefineResult` with ``products``,
    # ``proposals``, and exactly ``len(req.refine)`` outcomes in
    # ``per_refine_outcome``.  The framework constructs the wire
    # ``refinement_applied[]`` by zipping outcomes with ``req.refine`` —
    # adopters do NOT echo ``scope`` / ``product_id`` / ``proposal_id``
    # manually.

    def create_media_buy(
        self,
        req: CreateMediaBuyRequest,
        ctx: RequestContext[TMeta],
    ) -> SalesResult[CreateMediaBuySuccessResponse]:
        """Unified hybrid. Return :class:`CreateMediaBuySuccessResponse` directly
        for sync fast path; return :meth:`RequestContext.handoff_to_task`
        for HITL slow path.

        Pre-flight runs sync regardless of path so bad budgets reject
        before allocating a task id — call ``preflight()`` at the top,
        ``raise AdcpError(...)`` on rejection.

        Buyer pattern-matches on the response shape:

        * ``media_buy_id`` field present → sync success
        * ``task_id`` + ``status='submitted'`` → poll ``tasks_get`` or
          receive webhook

        **Framework injection:** when a :class:`~adcp.decisioning.ProductConfigStore`
        is wired via ``config_store=`` in
        :func:`adcp.decisioning.serve.create_adcp_server_from_platform`,
        the framework calls the store before invoking this method and
        injects the result as ``configs: dict[str, dict[str, Any]]``.
        Declare ``configs`` in your method signature to receive it::

            def create_media_buy(self, req, ctx, configs=None):
                configs = configs or {}
                line_item_id = configs.get(pkg.product_id, {}).get("line_item_id")

        When the store is not wired, or when ``req.packages`` is
        ``None`` (proposal-id flow), ``configs`` arrives as an empty
        dict ``{}``.
        """
        ...

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: UpdateMediaBuyRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[UpdateMediaBuySuccessResponse]:
        """Mutate an in-flight media buy.

        v6.0 returns sync only — the per-tool response schema doesn't
        carry the ``Submitted`` arm yet (adcp#3392). Re-approval flows
        return the success with the ``status`` field omitted (in-spec
        per the schema description) and drive lifecycle via
        ``ctx.publish_status_change``. v6.1 + adcp#3392 expand this
        signature to :data:`SalesResult` so re-approval flows can
        hand off cleanly.
        """
        ...

    def sync_creatives(
        self,
        req: SyncCreativesRequest,
        ctx: RequestContext[TMeta],
    ) -> SalesResult[SyncCreativesSuccessResponse]:
        """Unified hybrid for creative review.

        Mixed approved/pending rows in a single sync response, OR
        hand off the whole batch to background standards-and-practices
        review. Adopters with pre-approved buyer pools fast-path; new
        buyers' creatives go to review.
        """
        ...

    def get_media_buy_delivery(
        self,
        req: GetMediaBuyDeliveryRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetMediaBuyDeliveryResponse]:
        """Sync delivery read — pacing, spend, impressions per package."""
        ...

    # ---- Optional (gated by specialism — present-or-absent) ----

    def get_media_buys(
        self,
        req: GetMediaBuysRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetMediaBuysResponse]:
        """List media buys for the resolved account.

        Required when claiming any ``sales-*`` specialism in v6.0 rc.1+.
        ``validate_platform`` fails server boot if a sales-claiming
        platform doesn't implement this.
        """
        ...

    def provide_performance_feedback(
        self,
        req: ProvidePerformanceFeedbackRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ProvidePerformanceFeedbackResponse]:
        """Buyer-supplied performance signal back to the seller.

        Required when claiming any ``sales-*`` specialism in v6.0 rc.1+.
        """
        ...

    def list_creative_formats(
        self,
        req: ListCreativeFormatsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListCreativeFormatsResponse]:
        """Catalog of accepted creative formats.

        Required when claiming any ``sales-*`` specialism in v6.0 rc.1+.
        """
        ...

    def list_creatives(
        self,
        req: ListCreativesRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListCreativesResponse]:
        """List the seller's view of buyer-uploaded creatives.

        Required when claiming any ``sales-*`` specialism in v6.0 rc.1+.
        """
        ...

    # ---- Required when claiming ``sales-catalog-driven`` ----

    def sync_catalogs(
        self,
        req: SyncCatalogsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[SyncCatalogsSuccessResponse]:
        """Sync product catalogs with the platform.

        **Required** when claiming ``sales-catalog-driven``.
        ``validate_platform`` hard-fails at server boot when this method is
        absent on a ``sales-catalog-driven`` platform.

        Discovery mode: when ``req.catalogs is None``, return the account's
        existing synced catalogs without modification (read-only path per
        AdCP spec). Check ``req.catalogs`` before applying any mutations::

            def sync_catalogs(self, req, ctx):
                if req.catalogs is None:
                    return self._get_existing_catalogs(ctx.account_id)
                return self._upsert_catalogs(req.catalogs, ctx)

        **Important:** ``req.delete_missing=True`` with ``req.catalogs=None``
        is spec-undefined — reject it with
        ``AdcpError("INVALID_REQUEST", field="catalogs")`` rather than
        silently deleting buyer-managed catalogs.

        Return a list of :class:`~adcp.types.SyncCatalogResult` rows
        (ergonomic form) or a fully-shaped
        :class:`~adcp.types.SyncCatalogsSuccessResponse`.
        """
        raise NotImplementedError
