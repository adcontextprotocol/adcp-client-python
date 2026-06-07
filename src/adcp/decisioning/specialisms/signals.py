"""SignalsPlatform Protocols for marketplace and owned signals.

``signal-marketplace`` covers marketplace/provisioned signals that need
buyer-triggered destination activation. ``signal-owned`` covers
publisher-owned first-party signals that are already usable on that
seller inventory and only need catalog discovery. The slugs mirror
``schemas/cache/enums/specialism.json``.

Common method:

* :meth:`get_signals` — catalog discovery; synchronous by default,
  MAY hand off brief-driven discovery to a background task (submitted /
  working arms only — no input_required)

Marketplace-only method:

* :meth:`activate_signal` — sync provisioning onto destination platforms

Async story: ``activate_signal`` is sync at the wire level — its
response union has no ``Submitted`` arm. Long-running activation
pipelines (identity-graph match: 5–30 min, destination provisioning:
hours) return :class:`ActivateSignalSuccessResponse` immediately with
``deployments`` rows in ``pending`` state, then emit
``ctx.publish_status_change(resource_type='signal', ...)`` events as
each deployment reaches ``activating`` / ``deployed`` / ``failed``.

Mirrors the JS-side signals interfaces at
``src/lib/server/decisioning/specialisms/signals.ts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import DiscoveryResult, MaybeAsync
    from adcp.types import (
        ActivateSignalRequest,
        ActivateSignalSuccessResponse,
        GetSignalsRequest,
        GetSignalsResponse,
    )

#: Per-platform metadata generic; matches ``RequestContext[TMeta]`` and
#: ``Account[TMeta]`` upstream so a platform parameterizing
#: ``SignalsPlatform[TenantMeta]`` or
#: ``OwnedSignalsPlatform[TenantMeta]`` gets
#: ``ctx.account.metadata``-style typed access inside method bodies.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class OwnedSignalsPlatform(Protocol, Generic[TMeta]):
    """Catalog discovery for seller-owned first-party signals.

    Methods may be sync (return ``T`` directly) or async (return
    ``Awaitable[T]``); the dispatch adapter detects via
    :func:`asyncio.iscoroutinefunction` and runs sync methods on a
    thread pool so a blocking sync handler doesn't serialize the event
    loop.

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    rejection (``SIGNAL_NOT_FOUND``, ``POLICY_VIOLATION``,
    ``INVALID_REQUEST``, etc.); the framework projects to the wire
    structured-error envelope.
    """

    def get_signals(
        self,
        req: GetSignalsRequest,
        ctx: RequestContext[TMeta],
    ) -> DiscoveryResult[GetSignalsResponse]:
        """Catalog discovery — query your signal index, return signals
        matching the buyer's filters (industry, intent type, audience
        size, etc.).

        Return :class:`GetSignalsResponse` directly for the sync fast
        path. Brief-driven discovery MAY hand off via
        ``ctx.handoff_to_task(fn)`` when provider discovery needs
        background work (cross-provider fan-out, identity-graph lookups);
        the framework projects the handoff to the wire ``submitted``
        envelope. The buyer reaches the terminal
        :class:`GetSignalsResponse` via ``tasks/get`` polling, and — when
        the request carried ``push_notification_config`` — the framework
        also delivers the terminal completion / failure webhook to the
        buyer's URL from the background completion path (exactly once).

        Wholesale (``discovery_mode='wholesale'``) MUST return
        synchronously — a raw catalog read with no seller-side
        composition to background. Returning a handoff from a wholesale
        call is rejected at the framework layer with
        ``AdcpError(INVALID_REQUEST, field='discovery_mode')``.

        .. note::
            ``get_signals`` ships ONLY the ``submitted`` and ``working``
            async arms — it has **no** ``input_required`` arm (unlike
            ``get_products`` and ``create_media_buy``). Signal discovery
            cannot pause mid-task to solicit buyer clarification; the
            seller either completes the discovery or fails the task.

        :raises adcp.decisioning.AdcpError: ``code='POLICY_VIOLATION'``
            when the buyer doesn't have rights to the requested data
            category.
        """
        ...


@runtime_checkable
class SignalsPlatform(OwnedSignalsPlatform[TMeta], Protocol, Generic[TMeta]):
    """Catalog discovery + activation for marketplace/provisioned signals.

    Use this Protocol for ``signal-marketplace``. Use
    :class:`OwnedSignalsPlatform` for ``signal-owned`` platforms where
    returned signals are already usable in later media-buy targeting and
    there is no buyer-triggered destination provisioning step.
    """

    def activate_signal(
        self,
        req: ActivateSignalRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ActivateSignalSuccessResponse]:
        """Provision a signal onto one or more destination platforms
        (Snap, Meta, TikTok, etc.).

        Returns the success-arm shape immediately with ``deployments``
        rows in their current state — ``'pending'`` is a valid sync
        return for slow activation pipelines.

        Subsequent state changes (per-deployment ``activating`` /
        ``deployed`` / ``failed``) flow via
        ``ctx.publish_status_change(resource_type='signal',
        resource_id=signal_agent_segment_id, payload=...)`` as each
        destination's identity-graph match completes.

        Use ``req.action='deactivate'`` for GDPR/CCPA-compliant
        teardown when campaigns end.

        :raises adcp.decisioning.AdcpError: ``code='SIGNAL_NOT_FOUND'``
            (unknown ``signal_agent_segment_id``),
            ``code='POLICY_VIOLATION'`` (buyer lacks rights to activate
            this data), or ``code='INVALID_REQUEST'`` (missing or
            unrecognized destination).
        """
        ...


__all__ = ["OwnedSignalsPlatform", "SignalsPlatform"]
