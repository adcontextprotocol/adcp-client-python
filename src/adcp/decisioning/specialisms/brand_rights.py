"""BrandRightsPlatform Protocol — covers ``brand-rights``.

Brand-rights agents handle identity discovery + licensing for branded
inventory. Adopters: IP holders (sports leagues, movie studios), CTV
brand-rights desks, and brand-licensing marketplaces.

The slug mirrors ``schemas/cache/enums/specialism.json``.

Required methods (3, all sync):

* :meth:`get_brand_identity` — sync read; brand catalog + identity record
* :meth:`get_rights` — sync read; rights matching a brand + use query
* :meth:`acquire_rights` — buyer commits to an offering; 3-arm
  discriminated success union (acquired / pending / rejected). Async
  outcomes for the ``pending`` arm flow via the buyer-supplied
  ``push_notification_config`` webhook (NOT a polling tool — the spec
  doesn't define one for this surface; do NOT reach for ``tasks_get``).

Mirrors the JS-side ``BrandRightsPlatform`` interface at
``src/lib/server/decisioning/specialisms/brand-rights.ts``.

Two other surfaces in this domain (``update_rights``,
``creative_approval``) are spec-published but not yet in
``AdcpToolMap``; adopters wire them via the merge seam (custom
handler) until they land in v6.1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import MaybeAsync
    from adcp.types import (
        AcquireRightsAcquiredResponse,
        AcquireRightsPendingResponse,
        AcquireRightsRejectedResponse,
        AcquireRightsRequest,
        GetBrandIdentityRequest,
        GetBrandIdentitySuccessResponse,
        GetRightsRequest,
        GetRightsSuccessResponse,
    )


#: Per-platform metadata generic.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class BrandRightsPlatform(Protocol, Generic[TMeta]):
    """Brand identity discovery + rights licensing.

    Methods may be sync (return ``T`` directly) or async (return
    ``Awaitable[T]``); the dispatch adapter detects via
    :func:`asyncio.iscoroutinefunction` and runs sync methods on a
    thread pool.

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    REQUEST rejection (``REFERENCE_NOT_FOUND``, ``INVALID_REQUEST``,
    ``BUDGET_TOO_LOW``). For spec-defined GRANT rejection (rights
    unavailable in jurisdiction, talent dispute pending) return the
    :class:`AcquireRightsRejectedResponse` arm so the buyer sees the
    structured wire response with ``reason`` + ``suggestions``.
    """

    def get_brand_identity(
        self,
        req: GetBrandIdentityRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetBrandIdentitySuccessResponse]:
        """Read brand identity record — ``brand_id``, ``house``,
        localized ``names``, optional logos / industries /
        ``keller_type``. Sync; no async ceremony.

        :raises adcp.decisioning.AdcpError: ``code='REFERENCE_NOT_FOUND'``
            when the brand reference doesn't resolve to an identity
            the platform tracks.
        """
        ...

    def get_rights(
        self,
        req: GetRightsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetRightsSuccessResponse]:
        """List rights matching a brand + use query.

        Sync read; framework wraps the response in the wire envelope.
        Returning an empty ``rights`` array is valid (= "no rights
        available for the requested terms"); throw ``AdcpError`` only
        for buyer-fixable rejection (e.g., unsupported jurisdiction).

        Note: the wire field is ``rights``, NOT ``offerings``.
        Adopters who named their internal model ``offerings``
        translate at this seam.
        """
        ...

    def acquire_rights(
        self,
        req: AcquireRightsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[
        AcquireRightsAcquiredResponse | AcquireRightsPendingResponse | AcquireRightsRejectedResponse
    ]:
        """Acquire rights — buyer commits to an offering.

        Three discriminated wire arms:

        * :class:`AcquireRightsAcquiredResponse` — rights granted
          immediately. Carries ``rights_id``, ``status: 'acquired'``,
          ``brand_id``, ``terms``, ``generation_credentials``
          (scoped per-LLM-provider keys), and ``rights_constraint``
          so the buyer can plumb the grant directly into creative
          generation.
        * :class:`AcquireRightsPendingResponse` — clearance pending
          counter-signature, legal review, or rights-holder approval.
          Carries ``rights_id``, ``status: 'pending_approval'``,
          ``brand_id``, plus optional ``detail`` and
          ``estimated_response_time``. **Async delivery is
          webhook-only** — the buyer's ``push_notification_config.url``
          receives the eventual ``Acquired`` or ``Rejected`` outcome.
          The spec does NOT define a polling tool for
          ``acquire_rights``; do not reach for ``tasks_get`` here.
        * :class:`AcquireRightsRejectedResponse` — terminal rejection.
          Carries ``rights_id``, ``status: 'rejected'``, ``brand_id``,
          ``reason``, and optional ``suggestions[]`` for buyer
          remediation.

        Pre-flight (catalog availability, agency authorization) MUST
        run sync regardless of arm — invalid requests reject before
        allocating any state.

        :raises adcp.decisioning.AdcpError: only for buyer-fixable
            REQUEST rejection (``INVALID_REQUEST``,
            ``BUDGET_TOO_LOW``). For GRANT rejection return the
            :class:`AcquireRightsRejectedResponse` arm — that's the
            structured business outcome path.
        """
        ...


__all__ = ["BrandRightsPlatform"]
