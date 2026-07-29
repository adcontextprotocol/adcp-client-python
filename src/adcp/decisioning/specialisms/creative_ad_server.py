"""CreativeAdServerPlatform Protocol — covers ``creative-ad-server``.

A platform claiming ``creative-ad-server`` (Innovid, Flashtalking,
GAM-creative, CMP-style platforms) implements the methods on this
Protocol. The slug mirrors ``schemas/cache/enums/specialism.json``.

Distinct from :class:`CreativeBuilderPlatform` (stateless transform /
brief-driven generation):

* **Stateful** — adopter persists creatives in a library;
  ``sync_creatives`` pushes assets in, ``list_creatives`` reads them
  back, ``build_creative`` either looks up an existing creative by id
  OR pushes a new one.
* **Pricing per creative** — vendor pricing options on each creative;
  ``pricing_option_id`` selected at activation, billed via
  ``report_usage``.
* **Tag generation** — ``build_creative`` returns ad-server tags
  (VAST, placement-specific tracking pixels, macro-substituted
  creative HTML) when invoked with ``media_buy_id`` + ``package_id``
  context.
* **Per-creative delivery reports** — ``get_creative_delivery``
  returns pacing data per creative across the library.

Required methods (every ``creative-ad-server`` adopter):

* :meth:`build_creative` — library lookup OR inline build with tag generation
* :meth:`preview_creative` — preview-only variant (NOT optional here,
  unlike :class:`CreativeBuilderPlatform`)
* :meth:`list_creatives` — read creatives from the library
* :meth:`get_creative_delivery` — per-creative delivery actuals

Optional:

* :meth:`sync_creatives` — push creatives; hybrid sync/handoff for
  brand-suitability / S&P review

Mirrors the JS-side ``CreativeAdServerPlatform`` interface at
``src/lib/server/decisioning/specialisms/creative-ad-server.ts``.

Multi-archetype omni agents (a single tenant doing both stateless
transform AND library + tag generation) are rare in the wild; the
recommended pattern is to front each archetype as a separate tenant
via ``TenantRegistry``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from collections.abc import Sequence

    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import MaybeAsync, SalesResult
    from adcp.types import (
        CreativeManifest,
        GetCreativeDeliveryRequest,
        GetCreativeDeliveryResponse,
        ListCreativesRequest,
        ListCreativesResponse,
        SyncCreativesRequest,
        SyncCreativesSuccessResponse,
    )
    from adcp.types.legacy import (
        LegacyBuildCreativeRequest,
        LegacyBuildCreativeSuccessResponse,
        LegacyPreviewCreativeRequest,
        LegacyPreviewCreativeResponse,
    )


#: Per-platform metadata generic.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class CreativeAdServerPlatform(Protocol, Generic[TMeta]):
    """Stateful creative library + per-creative pricing + tag generation.

    Methods may be sync (return ``T`` directly) or async (return
    ``Awaitable[T]``); the dispatch adapter detects via
    :func:`asyncio.iscoroutinefunction` and runs sync methods on a
    thread pool.

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    rejection.
    """

    def build_creative_legacy(
        self,
        req: LegacyBuildCreativeRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[
        LegacyBuildCreativeSuccessResponse | Sequence[CreativeManifest] | CreativeManifest
    ]:
        """Build / retrieve creative tags. Two invocation modes:

        * **Library lookup**: ``req.creative_id`` references an
          existing creative; return the manifest with tag fields
          populated (``vast_tag``, click trackers, etc.). When
          ``req.media_buy_id`` + ``req.package_id`` are also set,
          generate placement-specific tags with macro substitution
          baked in.
        * **Inline build**: ``req.creative_manifest`` is provided
          directly; transform / wrap it (similar to template archetype
          but with ad-server side effects: register the creative in
          the library, generate the tag, etc.).

        Sync at the wire level — the per-tool
        ``build-creative-response.json`` ``oneOf`` doesn't include a
        ``Submitted`` arm (spec inconsistency tracked as
        ``adcontextprotocol/adcp#3392``). Until the spec rolls
        Submitted into the ``oneOf``, slow tag-generation pipelines
        await in-request; status changes flow via
        ``ctx.publish_status_change``.

        Return shape: see :meth:`CreativeBuilderPlatform.build_creative`
        for the discriminated-arm rules — single
        :class:`CreativeManifest`, ``Sequence[CreativeManifest]`` for
        multi-format, or :class:`BuildCreativeSuccessResponse` envelope
        when you need ``sandbox``/``expires_at``/``preview``.
        """
        ...

    def preview_creative_legacy(
        self,
        req: LegacyPreviewCreativeRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[LegacyPreviewCreativeResponse]:
        """Preview-only variant — sandbox URL or inline HTML, expires.

        Always sync. NOT optional for ad-server adopters (distinct from
        :class:`CreativeBuilderPlatform.preview_creative`, which is
        optional) — buyers expect preview surface from any stateful
        creative library.
        """
        ...

    def list_creatives(
        self,
        req: ListCreativesRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListCreativesResponse]:
        """Read creatives from the library.

        Filters + pagination. When ``req.include_assignments``,
        include the buyer's package-assignment graph. When
        ``req.include_pricing``, include vendor pricing options on
        each creative.
        """
        ...

    def get_creative_delivery(
        self,
        req: GetCreativeDeliveryRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetCreativeDeliveryResponse]:
        """Per-creative delivery actuals (impressions, spend, pacing).

        Sync — report-running platforms with manual report cycles
        return the latest cached actuals and emit ``delivery_report``
        status changes via ``ctx.publish_status_change`` when fresh
        reports are available.
        """
        ...

    def sync_creatives(
        self,
        req: SyncCreativesRequest,
        ctx: RequestContext[TMeta],
    ) -> SalesResult[SyncCreativesSuccessResponse]:
        """Push creatives. Optional — present-or-absent.

        Return the typed :class:`SyncCreativesSuccessResponse` for the
        sync fast path OR ``ctx.handoff_to_task(fn)`` for HITL —
        brand-suitability, S&P review. ``action: 'created'`` for new
        entries, ``'updated'`` for replacements, ``'unchanged'`` when
        matching. Optional ``status: 'pending_review'`` for sync-arm
        rows awaiting manual review.

        Same wire request type as the sales-* and creative-builder
        archetypes use (``SyncCreativesRequest`` — shared spec shape).
        """
        ...


__all__ = ["CreativeAdServerPlatform"]
