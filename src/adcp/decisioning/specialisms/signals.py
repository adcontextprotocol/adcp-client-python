"""SignalsPlatform Protocol — covers ``signal-marketplace`` + ``signal-owned``.

A platform claiming either ``signal-marketplace`` (third-party data
brokers — LiveRamp, Oracle Data Cloud, third-party DMPs) or
``signal-owned`` (first-party data providers — publisher first-party
data, retailer customer-graph) implements the methods on this Protocol.
The slugs mirror ``schemas/cache/enums/specialism.json``.

Two methods:

* :meth:`get_signals` — sync catalog discovery
* :meth:`activate_signal` — sync provisioning onto destination platforms

Async story: ``activate_signal`` is sync at the wire level — its
response union has no ``Submitted`` arm. Long-running activation
pipelines (identity-graph match: 5–30 min, destination provisioning:
hours) return :class:`ActivateSignalSuccessResponse` immediately with
``deployments`` rows in ``pending`` state, then emit
``ctx.publish_status_change(resource_type='signal', ...)`` events as
each deployment reaches ``activating`` / ``deployed`` / ``failed``.

Mirrors the JS-side ``SignalsPlatform`` interface at
``src/lib/server/decisioning/specialisms/signals.ts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import MaybeAsync
    from adcp.types import (
        ActivateSignalRequest,
        ActivateSignalSuccessResponse,
        GetSignalsRequest,
        GetSignalsResponse,
    )

#: Per-platform metadata generic; matches ``RequestContext[TMeta]`` and
#: ``Account[TMeta]`` upstream so a platform parameterizing
#: ``SignalsPlatform[TenantMeta]`` gets ``ctx.account.metadata``-style
#: typed access inside method bodies.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class SignalsPlatform(Protocol, Generic[TMeta]):
    """Catalog discovery + activation for marketplace / owned signals.

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
    ) -> MaybeAsync[GetSignalsResponse]:
        """Catalog discovery — query your signal index, return signals
        matching the buyer's filters (industry, intent type, audience
        size, etc.).

        Sync at the wire level — :class:`GetSignalsResponse` has no
        async envelope. Platforms with slow catalog stores need
        internal caches.

        :raises adcp.decisioning.AdcpError: ``code='POLICY_VIOLATION'``
            when the buyer doesn't have rights to the requested data
            category.
        """
        ...

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


__all__ = ["SignalsPlatform"]
