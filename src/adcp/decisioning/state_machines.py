"""Canonical state-machine helpers for AdCP lifecycle objects.

The AdCP spec defines a finite-state graph for media buys and creatives;
adopters who hand-roll lifecycle methods (``update_media_buy``,
``sync_creatives``, etc.) repeatedly re-implement the same legality
checks. These helpers project the spec state graph as a single source
of truth and raise a spec-conformant ``INVALID_STATE`` error when an
adopter tries an illegal transition.

Use in ``update_media_buy`` / lifecycle-changing handlers to refuse
non-monotonic state changes (e.g., ``active`` → ``pending_creatives``).
The helpers are deliberately small — adopters who need richer guards
(e.g., role-based authorization on transitions) compose their own
checks alongside these.

Media buys have two graphs. :data:`MEDIA_BUY_TRANSITIONS` is the
*requested* graph: what a handler may do when a buyer asks for a state
change, enforced by :func:`assert_media_buy_transition`.
:data:`OBSERVED_MEDIA_BUY_TRANSITIONS` is the *observed* graph: every
move a seller may record when it mirrors status from its own platform.
It is a strict superset, and it equals the full AdCP 3.2 published
lifecycle graph.

Seller commitment is a separate axis (AdCP 3.3): a provisional buy, one
whose ``confirmed_at`` is null, is never ``active``. It stays in
``pending_creatives`` or ``pending_start`` until the seller confirms or
rejects it, even after its flight start. Pass ``provisional=True`` to the
transition checks for such a buy, and use
:func:`media_buy_status_at_flight_start` in a flight-start scheduler.

State enum values track the AdCP spec at
``schemas/cache/enums/media-buy-status.json`` and
``schemas/cache/enums/creative-status.json``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from adcp.decisioning.types import AdcpError

# ---------------------------------------------------------------------------
# Media buy state machine
# ---------------------------------------------------------------------------

#: Media-buy state transitions a handler may make on a buyer's request.
#: Maps ``from_state`` → set of legal ``to_state`` values. Terminal
#: states map to an empty frozenset.
#:
#: This is the *requested* graph that :func:`assert_media_buy_transition`
#: enforces. It omits three published edges that follow from the
#: seller's lifecycle rather than from a status a buyer asks for; see
#: :data:`OBSERVED_MEDIA_BUY_TRANSITIONS` for recording what an ad
#: server reports.
MEDIA_BUY_TRANSITIONS: Mapping[str, frozenset[str]] = {
    # Approved but waiting on creatives — buyer attaches via sync_creatives.
    "pending_creatives": frozenset({"pending_start", "canceled", "rejected"}),
    # Creatives attached, waiting on flight start.
    "pending_start": frozenset({"active", "canceled", "rejected"}),
    # Currently serving.
    "active": frozenset({"paused", "completed", "canceled"}),
    # Temporarily halted; can resume to active or terminate.
    "paused": frozenset({"active", "completed", "canceled"}),
    # Terminal states — no outgoing edges.
    "completed": frozenset(),
    "canceled": frozenset(),
    "rejected": frozenset(),
}


#: Media-buy statuses with no outgoing transition: ``completed``,
#: ``canceled``, and ``rejected``.
#:
#: Derived from :data:`MEDIA_BUY_TRANSITIONS`, so it cannot drift from
#: the graph. AdCP 3.2 ``docs/media-buy/specification.mdx``, "Media Buy
#: State Transitions": "Terminal states (``completed``, ``rejected``,
#: ``canceled``) allow no further transitions."
MEDIA_BUY_TERMINAL_STATUSES: frozenset[str] = frozenset(
    status for status, legal in MEDIA_BUY_TRANSITIONS.items() if not legal
)

# Published AdCP 3.2 edges that MEDIA_BUY_TRANSITIONS leaves out. Each
# follows from the seller's lifecycle (creative coverage completing after
# the flight started, a latent create-time hold surfacing), never from a
# status a buyer asks for.
_PLATFORM_MEDIA_BUY_TRANSITIONS: Mapping[str, frozenset[str]] = {
    # Creatives were assigned after the flight start: the buy becomes
    # deliverable at once (or held, when it was created paused).
    "pending_creatives": frozenset({"active", "paused"}),
    # The flight start arrived on a buy created with ``paused: true``.
    "pending_start": frozenset({"paused"}),
}

#: Every media-buy move a seller may record when it mirrors status from
#: its own platform (ad server, OMS). Maps ``from_state`` → set of
#: ``to_state`` values. A superset of :data:`MEDIA_BUY_TRANSITIONS`.
#:
#: The extra edges are published AdCP 3.2 edges that follow from the
#: seller's lifecycle rather than from a status a buyer asks for:
#:
#: * ``pending_creatives → active``: ``docs/media-buy/media-buys/lifecycle.mdx``,
#:   "Creative sync timing": after assignments cover the package, the buy
#:   "moves to ``pending_start`` when the flight is in the future or
#:   ``active`` when delivery can begin."
#: * ``pending_creatives → paused`` and ``pending_start → paused``: the
#:   same page's transition diagram ("sync_creatives assignment (buy
#:   held)", "flight date reached (create held)"), and
#:   ``docs/media-buy/specification.mdx``: a create-time hold is latent
#:   while setup blockers are visible, and "once creatives are present
#:   and the flight can start, the buy enters ``paused`` instead of
#:   ``active``."
#:
#: The graph equals the one the ``@adcp/client`` storyboard runner's
#: ``status.monotonic`` invariant enforces for AdCP 3.2, so a status
#: history accepted here also passes that invariant.
#:
#: Terminal states stay terminal. A platform move out of ``completed``,
#: ``canceled``, or ``rejected`` (an ad server reopening a finished
#: order), or a move back to an earlier phase (``active →
#: pending_start`` when the flight start is pushed later), has no edge
#: in AdCP 3.2 and is not in this graph; ``status.monotonic`` rejects
#: a buyer-visible history that contains one. A seller that sees one
#: should keep the status it reports to buyers on the published graph
#: and track the platform state separately.
OBSERVED_MEDIA_BUY_TRANSITIONS: Mapping[str, frozenset[str]] = {
    status: legal | _PLATFORM_MEDIA_BUY_TRANSITIONS.get(status, frozenset())
    for status, legal in MEDIA_BUY_TRANSITIONS.items()
}


#: Statuses a provisional media buy (``confirmed_at: null``) may not enter.
#:
#: AdCP 3.3 ``enums/media-buy-status.json``: a buy whose ``confirmed_at``
#: is null "MUST NOT be ``active``", and "a provisional buy never becomes
#: active until the seller commits". It stays ``pending_start`` through its
#: flight start, so the flight-start moves into ``paused`` (a buy created
#: held) are closed to it as well. Cancellation and rejection stay open.
PROVISIONAL_MEDIA_BUY_BLOCKED_STATUSES: frozenset[str] = frozenset({"active", "paused"})


def is_terminal_media_buy_status(status: str) -> bool:
    """Return whether ``status`` is a terminal media-buy status.

    An unknown status returns ``False`` rather than raising, so an
    adopter's internal pre-lifecycle statuses (``draft``,
    ``pending_approval``) are simply non-terminal.

    :param status: A media-buy status string or ``MediaBuyStatus`` member.
    """
    return status in MEDIA_BUY_TERMINAL_STATUSES


def is_observable_media_buy_transition(
    from_state: str, to_state: str, *, provisional: bool = False
) -> bool:
    """Return whether a seller may record ``from_state → to_state``.

    Use when mirroring status from the seller's own platform, where the
    move already happened and refusing it with ``INVALID_STATE`` would
    only hide it. The check is against
    :data:`OBSERVED_MEDIA_BUY_TRANSITIONS`, which adds the AdCP 3.2
    published edges that no buyer request produces (``pending_creatives →
    active``, ``pending_creatives → paused``, ``pending_start →
    paused``) to :data:`MEDIA_BUY_TRANSITIONS`.

    Returns ``False`` for an unknown status, for an unchanged status
    (not a transition), and for any move out of a terminal status. For
    a buyer's request, use :func:`assert_media_buy_transition` instead.

    :param from_state: Status the seller last recorded.
    :param to_state: Status the seller's platform now reports.
    :param provisional: ``True`` when the buy's ``confirmed_at`` is null.
        A provisional buy may not enter ``active`` or ``paused``
        (:data:`PROVISIONAL_MEDIA_BUY_BLOCKED_STATUSES`): an ad server that
        starts the order before the seller commits does not make the buy
        active to the buyer.
    """
    if provisional and to_state in PROVISIONAL_MEDIA_BUY_BLOCKED_STATUSES:
        return False
    return to_state in OBSERVED_MEDIA_BUY_TRANSITIONS.get(from_state, frozenset())


def assert_media_buy_transition(
    from_state: str,
    to_state: str,
    *,
    media_buy_id: str | None = None,
    provisional: bool = False,
) -> None:
    """Raise ``INVALID_STATE`` on illegal media-buy transitions.

    Use inside ``update_media_buy`` / ``cancel_media_buy`` /
    lifecycle-changing handlers to refuse non-monotonic state changes
    (e.g., ``active`` → ``pending_creatives``).

    :param from_state: Current media-buy status.
    :param to_state: Requested next status.
    :param media_buy_id: Optional id, surfaced in ``error.details`` for
        buyer-side debugging.
    :param provisional: ``True`` when the buy's ``confirmed_at`` is null.
        Such a buy may not enter ``active`` or ``paused`` until the seller
        commits (AdCP 3.3); the seller confirms it first, then activates.
    :raises AdcpError: with ``code='INVALID_STATE'`` and
        ``recovery='correctable'`` per the spec when the transition is
        not in :data:`MEDIA_BUY_TRANSITIONS`, or when a provisional buy
        would enter ``active`` or ``paused``.
    """
    if (
        provisional
        and from_state in MEDIA_BUY_TRANSITIONS
        and to_state in PROVISIONAL_MEDIA_BUY_BLOCKED_STATUSES
        and to_state in MEDIA_BUY_TRANSITIONS[from_state]
    ):
        raise AdcpError(
            "INVALID_STATE",
            message=(
                f"Media buy is provisional (confirmed_at is null); it cannot move "
                f"{from_state!r} → {to_state!r} until the seller confirms it."
            ),
            recovery="correctable",
            field="status",
            details=_details("media_buy_id", media_buy_id, from_state, to_state),
        )
    _assert_transition(
        from_state,
        to_state,
        graph=MEDIA_BUY_TRANSITIONS,
        resource_kind="media buy",
        resource_id=media_buy_id,
        id_field="media_buy_id",
    )


def media_buy_status_at_flight_start(
    status: str,
    *,
    confirmed_at: datetime | str | None,
    paused: bool = False,
) -> str:
    """Return the status a media buy takes when its flight start arrives.

    For a flight-start scheduler. Only a committed ``pending_start`` buy
    moves: to ``active``, or to ``paused`` when it was created held
    (``paused: true``). A provisional buy (``confirmed_at`` null) stays
    ``pending_start`` until the seller confirms or rejects it (AdCP 3.3,
    adcontextprotocol/adcp#7963). Every other status is returned
    unchanged: a ``pending_creatives`` buy is still waiting on creatives,
    and a buy already past ``pending_start`` is not restarted.

    :param status: The buy's current status.
    :param confirmed_at: The buy's ``confirmed_at``; ``None`` when it is
        provisional.
    :param paused: Whether the buy carries a create-time hold.
    """
    if status != "pending_start" or confirmed_at is None:
        return status
    return "paused" if paused else "active"


# ---------------------------------------------------------------------------
# Creative state machine
# ---------------------------------------------------------------------------

#: Legal creative-asset state transitions per the AdCP spec.
#: Maps ``from_state`` → set of legal ``to_state`` values. Terminal
#: states map to an empty frozenset.
CREATIVE_ASSET_TRANSITIONS: Mapping[str, frozenset[str]] = {
    # Initial ingestion / decode / scan.
    "processing": frozenset({"pending_review", "approved", "rejected"}),
    # Awaiting human or automated policy review.
    "pending_review": frozenset({"approved", "rejected"}),
    # Approved creatives can be archived OR sent back to review by the
    # seller (re-review per ``creative-status.json``).
    "approved": frozenset({"archived", "pending_review"}),
    # Rejected is NOT terminal per spec — buyer fixes the issue and
    # resubmits via sync_creatives, which returns the creative to
    # ``processing``. Also archivable.
    "rejected": frozenset({"archived", "processing"}),
    # Archived can be unarchived back to approved per spec.
    "archived": frozenset({"approved"}),
}


def assert_creative_transition(
    from_state: str,
    to_state: str,
    *,
    creative_id: str | None = None,
) -> None:
    """Raise ``INVALID_STATE`` on illegal creative-asset transitions.

    Use inside ``sync_creatives`` / creative-approval handlers to refuse
    non-monotonic state changes (e.g., ``archived`` → ``approved``).

    :param from_state: Current creative status.
    :param to_state: Requested next status.
    :param creative_id: Optional id, surfaced in ``error.details`` for
        buyer-side debugging.
    :raises AdcpError: with ``code='INVALID_STATE'`` and
        ``recovery='correctable'`` per the spec when the transition is
        not in :data:`CREATIVE_ASSET_TRANSITIONS`.
    """
    _assert_transition(
        from_state,
        to_state,
        graph=CREATIVE_ASSET_TRANSITIONS,
        resource_kind="creative",
        resource_id=creative_id,
        id_field="creative_id",
    )


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _assert_transition(
    from_state: str,
    to_state: str,
    *,
    graph: Mapping[str, frozenset[str]],
    resource_kind: str,
    resource_id: str | None,
    id_field: str,
) -> None:
    if from_state not in graph:
        raise AdcpError(
            "INVALID_STATE",
            message=(
                f"Unknown {resource_kind} state {from_state!r}; "
                f"expected one of {sorted(graph.keys())}."
            ),
            recovery="correctable",
            field="status",
            details=_details(id_field, resource_id, from_state, to_state),
        )

    legal = graph[from_state]
    if not legal:
        raise AdcpError(
            "INVALID_STATE",
            message=(
                f"{resource_kind.capitalize()} is in terminal state "
                f"{from_state!r}; transition to {to_state!r} is not permitted."
            ),
            recovery="correctable",
            field="status",
            details=_details(id_field, resource_id, from_state, to_state),
        )

    if to_state not in legal:
        raise AdcpError(
            "INVALID_STATE",
            message=(
                f"Illegal {resource_kind} transition {from_state!r} → "
                f"{to_state!r}; legal next states: {sorted(legal)}."
            ),
            recovery="correctable",
            field="status",
            details=_details(id_field, resource_id, from_state, to_state),
        )


def _details(
    id_field: str,
    resource_id: str | None,
    from_state: str,
    to_state: str,
) -> dict[str, str]:
    out: dict[str, str] = {
        "from_state": from_state,
        "to_state": to_state,
    }
    if resource_id is not None:
        out[id_field] = resource_id
    return out


__all__ = [
    "CREATIVE_ASSET_TRANSITIONS",
    "MEDIA_BUY_TERMINAL_STATUSES",
    "MEDIA_BUY_TRANSITIONS",
    "OBSERVED_MEDIA_BUY_TRANSITIONS",
    "PROVISIONAL_MEDIA_BUY_BLOCKED_STATUSES",
    "assert_creative_transition",
    "assert_media_buy_transition",
    "is_observable_media_buy_transition",
    "is_terminal_media_buy_status",
    "media_buy_status_at_flight_start",
]
