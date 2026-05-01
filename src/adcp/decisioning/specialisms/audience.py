"""AudiencePlatform Protocol — covers the ``audience-sync`` specialism.

Used standalone (LiveRamp, Oracle Data Cloud, Salesforce CDP) or
composed with ``sales-social`` (Snap/Meta/TikTok). The framework owns
cross-platform threading + idempotency + cross-tenant scoping; the
adopter answers "given this audience, what happened on my system?"

The slug mirrors ``schemas/cache/enums/specialism.json``.

Two methods:

* :meth:`sync_audiences` — push audiences to the platform (creates,
  updates, deletes per the wire spec)
* :meth:`poll_audience_statuses` — batch-poll current status for one
  or more audiences

Mirrors the JS-side ``AudiencePlatform`` interface at
``src/lib/server/decisioning/specialisms/audiences.ts``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import MaybeAsync
    from adcp.types import (
        SyncAudiencesAudience,
        SyncAudiencesSuccessResponse,
    )

#: Per-platform metadata generic; matches ``RequestContext[TMeta]`` and
#: ``Account[TMeta]`` upstream.
TMeta = TypeVar("TMeta", default=dict[str, Any])

# Note on adopter-facing row types: the wire schema doesn't export a
# top-level ``Audience`` type — the row shape is defined inline on
# ``SyncAudiencesRequest.audiences[]``. Adopters import
# :class:`adcp.types.SyncAudiencesAudience` directly for typing.
# The wire success response is :class:`adcp.types.SyncAudiencesSuccessResponse`,
# which wraps per-audience result rows in ``{audiences: [...]}`` with
# the spec's status enum (``created`` / ``updated`` / ``unchanged`` /
# ``deleted`` / ``failed``; note ``rejected`` is NOT a valid wire
# status — use ``failed`` for buyer-rejected audiences).


@runtime_checkable
class AudiencePlatform(Protocol, Generic[TMeta]):
    """Sync first-party CRM audiences with delta upsert semantics.

    Methods may be sync (return ``T`` directly) or async (return
    ``Awaitable[T]``); the dispatch adapter detects via
    :func:`asyncio.iscoroutinefunction` and runs sync methods on a
    thread pool.

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    rejection (``AUDIENCE_TOO_SMALL``, ``REFERENCE_NOT_FOUND``, etc.);
    the framework projects to the wire structured-error envelope.
    """

    def sync_audiences(
        self,
        audiences: Sequence[SyncAudiencesAudience],
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[SyncAudiencesSuccessResponse]:
        """Push audiences to the platform.

        Framework handles batching, idempotency, and cross-tenant
        scoping; the adopter handles match-rate computation and
        activation lifecycle.

        Sync acknowledgment with status changes via
        ``ctx.publish_status_change``: return per-audience result rows
        immediately (``'pending'`` / ``'matching'`` are valid sync
        outcomes). The match-rate computation and activation pipeline
        run in the background — call
        ``ctx.publish_status_change(resource_type='audience', ...)``
        from the platform's webhook handler / job queue / cron when
        each audience reaches a terminal state.

        :param audiences: List of audience rows projected from the
            wire ``SyncAudiencesRequest.audiences[]`` field. Adopter
            ergonomic — receives the list directly rather than the
            full request.
        :raises adcp.decisioning.AdcpError: for buyer-fixable
            rejection (e.g., ``AUDIENCE_TOO_SMALL``).
        """
        ...

    def poll_audience_statuses(
        self,
        audience_ids: Sequence[str],
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[Mapping[str, str]]:
        """Batch-poll current status for one or more audiences.

        Sync — this is a state-read, not a mutating operation. Useful
        for buyer-side polling outside the framework's task envelope
        (e.g., querying long-lived audiences) and for adapter code
        that needs to check N audiences at once.

        Returns a ``dict[audience_id, AudienceStatus]``. Audiences not
        found are omitted from the map (callers handle missing keys);
        raise ``AdcpError(code='REFERENCE_NOT_FOUND')`` only when the
        entire batch is unresolvable for the tenant.

        Single-audience polling is
        ``poll_audience_statuses([id], ctx).get(id)``. The batch shape
        composes with upstream identity-graph APIs that natively
        return per-audience-id arrays — adopters do NOT need to wrap
        a single-id lookup over an N-call loop.

        Adopter-internal helper — not surfaced as a wire tool. Used
        by adopter code orchestrating cross-platform audience flows
        and by the framework's optional bulk-status middleware.
        """
        ...


__all__ = ["AudiencePlatform"]
