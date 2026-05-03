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

Warn-if-absent methods (v6.0 alpha → hard-required in v6.0 rc.1):

:func:`adcp.decisioning.dispatch.validate_platform` emits a one-time
``UserWarning`` at server boot for each of these that is missing.
They become hard-enforced (``AdcpError``) in v6.0 rc.1.

* :meth:`get_media_buys`
* :meth:`provide_performance_feedback`
* :meth:`list_creative_formats`
* :meth:`list_creatives`
* :meth:`sync_catalogs` — required when claiming
  ``sales-catalog-driven`` (already hard-enforced)

The framework's :func:`validate_platform` walks ``capabilities.specialisms``
and confirms each specialism's required methods exist on the platform
subclass — fail-fast at server boot rather than 404 at first dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import MaybeAsync, SalesResult

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
    ) -> MaybeAsync[GetProductsResponse]:
        """Sync catalog read — no HITL even on broadcast/proposal-mode.

        Brief-based proposal generation rides on a separate verb
        (``request_proposal``, adcp#3407); proposal-mode adopters
        surface the eventual products via
        ``ctx.publish_status_change(resource_type='proposal', ...)``
        rather than blocking ``get_products`` waiting for trafficker
        approval.
        """
        ...

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

        ``validate_platform`` emits a ``UserWarning`` at server boot in v6.0
        if this method is absent. Becomes a hard boot-time error in v6.0 rc.1.
        """
        ...

    def provide_performance_feedback(
        self,
        req: ProvidePerformanceFeedbackRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ProvidePerformanceFeedbackResponse]:
        """Buyer-supplied performance signal back to the seller.

        ``validate_platform`` emits a ``UserWarning`` at server boot in v6.0
        if this method is absent. Becomes a hard boot-time error in v6.0 rc.1.
        """
        ...

    def list_creative_formats(
        self,
        req: ListCreativeFormatsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListCreativeFormatsResponse]:
        """Catalog of accepted creative formats.

        ``validate_platform`` emits a ``UserWarning`` at server boot in v6.0
        if this method is absent. Becomes a hard boot-time error in v6.0 rc.1.
        """
        ...

    def list_creatives(
        self,
        req: ListCreativesRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListCreativesResponse]:
        """List the seller's view of buyer-uploaded creatives.

        ``validate_platform`` emits a ``UserWarning`` at server boot in v6.0
        if this method is absent. Becomes a hard boot-time error in v6.0 rc.1.
        """
        ...
