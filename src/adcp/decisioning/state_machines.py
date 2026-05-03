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

State enum values track the AdCP spec at
``schemas/cache/enums/media-buy-status.json`` and
``schemas/cache/enums/creative-status.json``.
"""

from __future__ import annotations

from collections.abc import Mapping

from adcp.decisioning.types import AdcpError

# ---------------------------------------------------------------------------
# Media buy state machine
# ---------------------------------------------------------------------------

#: Legal media-buy state transitions per the AdCP spec.
#: Maps ``from_state`` → set of legal ``to_state`` values. Terminal
#: states map to an empty frozenset.
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


def assert_media_buy_transition(
    from_state: str,
    to_state: str,
    *,
    media_buy_id: str | None = None,
) -> None:
    """Raise ``INVALID_STATE`` on illegal media-buy transitions.

    Use inside ``update_media_buy`` / ``cancel_media_buy`` /
    lifecycle-changing handlers to refuse non-monotonic state changes
    (e.g., ``active`` → ``pending_creatives``).

    :param from_state: Current media-buy status.
    :param to_state: Requested next status.
    :param media_buy_id: Optional id, surfaced in ``error.details`` for
        buyer-side debugging.
    :raises AdcpError: with ``code='INVALID_STATE'`` and
        ``recovery='correctable'`` per the spec when the transition is
        not in :data:`MEDIA_BUY_TRANSITIONS`.
    """
    _assert_transition(
        from_state,
        to_state,
        graph=MEDIA_BUY_TRANSITIONS,
        resource_kind="media buy",
        resource_id=media_buy_id,
        id_field="media_buy_id",
    )


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
    "MEDIA_BUY_TRANSITIONS",
    "assert_creative_transition",
    "assert_media_buy_transition",
]
