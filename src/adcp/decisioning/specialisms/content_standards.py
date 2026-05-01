"""ContentStandardsPlatform Protocol — covers ``content-standards``.

Content standards enforcement: brand safety policies, content adjacency
rules, and per-creative compliance verification.

The slug mirrors ``schemas/cache/enums/specialism.json``.

Two adopter shapes:

* **Standalone content-standards agent** (Innovid-style): runs apart
  from a sales agent; buyers call ``list_content_standards`` /
  ``get_content_standards`` / ``validate_content_delivery`` directly.
* **Composed within a seller** (governance overlay): seller imports
  content-standards into its agent surface, calls ``calibrate_content``
  internally during creative review, and surfaces violations through
  ``sync_creatives`` review state.

Both shapes use the same interface; difference is whether the
platform field is populated alongside ``sales`` or as the only
specialism.

Required methods (6, all sync):

* :meth:`list_content_standards` — discover published standards
* :meth:`get_content_standards` — read a single standard by id
* :meth:`create_content_standards` — create a new standard
  (idempotent on buyer's ``idempotency_key``)
* :meth:`update_content_standards` — patch an existing standard
* :meth:`calibrate_content` — calibrate content against published
  standards
* :meth:`validate_content_delivery` — post-flight conformance check

Optional (analyzer reads — adopters without analyzer pipelines omit):

* :meth:`get_media_buy_artifacts` — content artifacts produced during
  flight (creative proofs, ad-server tags, completed log captures)
* :meth:`get_creative_features` — per-creative analyzed features
  (object detection, scene classification, transcript)

Mirrors the JS-side ``ContentStandardsPlatform`` interface at
``src/lib/server/decisioning/specialisms/content-standards.ts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import MaybeAsync
    from adcp.types import (
        CalibrateContentRequest,
        CalibrateContentResponse,
        CreateContentStandardsRequest,
        CreateContentStandardsResponse,
        GetContentStandardsRequest,
        GetContentStandardsResponse,
        GetCreativeFeaturesRequest,
        GetCreativeFeaturesResponse,
        GetMediaBuyArtifactsRequest,
        GetMediaBuyArtifactsResponse,
        ListContentStandardsRequest,
        ListContentStandardsResponse,
        UpdateContentStandardsRequest,
        UpdateContentStandardsResponse,
        ValidateContentDeliveryRequest,
        ValidateContentDeliveryResponse,
    )


#: Per-platform metadata generic.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class ContentStandardsPlatform(Protocol, Generic[TMeta]):
    """Content standards CRUD + calibration + delivery validation.

    Methods may be sync (return ``T`` directly) or async (return
    ``Awaitable[T]``); the dispatch adapter detects via
    :func:`asyncio.iscoroutinefunction` and runs sync methods on a
    thread pool.

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    rejection (``REFERENCE_NOT_FOUND``, ``INVALID_REQUEST``,
    ``POLICY_VIOLATION`` for buyer rights issues, etc.).
    """

    def list_content_standards(
        self,
        req: ListContentStandardsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListContentStandardsResponse]:
        """Discover content standards published by this agent."""
        ...

    def get_content_standards(
        self,
        req: GetContentStandardsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetContentStandardsResponse]:
        """Read a single content standard by id."""
        ...

    def create_content_standards(
        self,
        req: CreateContentStandardsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[CreateContentStandardsResponse]:
        """Create a new content standard.

        Adopter validates the policy schema and returns the persisted
        record. Idempotent on the buyer's ``idempotency_key``.
        """
        ...

    def update_content_standards(
        self,
        req: UpdateContentStandardsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[UpdateContentStandardsResponse]:
        """Update an existing content standard."""
        ...

    def calibrate_content(
        self,
        req: CalibrateContentRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[CalibrateContentResponse]:
        """Calibrate content against the published standards.

        Returns the standard's current calibration profile + any
        flags raised against the submitted content.
        """
        ...

    def validate_content_delivery(
        self,
        req: ValidateContentDeliveryRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ValidateContentDeliveryResponse]:
        """Validate that a delivered media-buy / creative meets the
        buyer's declared content-standards.

        Sellers call this post-flight to confirm adjacency and policy
        conformance before issuing a
        ``validate_content_delivery_artifact`` to a governance agent.
        """
        ...

    # ---- Optional analyzer reads ----

    def get_media_buy_artifacts(
        self,
        req: GetMediaBuyArtifactsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetMediaBuyArtifactsResponse]:
        """Read content artifacts produced during a media buy's flight.

        Optional — adopters who don't expose artifact archival omit.
        Required by governance receivers running adjacency validation.
        """
        ...

    def get_creative_features(
        self,
        req: GetCreativeFeaturesRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetCreativeFeaturesResponse]:
        """Read per-creative analyzed features (object detection,
        scene classification, transcript) extracted during calibration.

        Optional — adopters without analyzer pipelines omit.
        """
        ...


__all__ = ["ContentStandardsPlatform"]
