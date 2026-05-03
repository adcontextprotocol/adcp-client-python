"""State-machine transition helpers and account-reference utilities.

Provides:

* :data:`MEDIA_BUY_TRANSITIONS` — valid ``from → {to}`` state moves for
  media buys, keyed on :class:`~adcp.types.MediaBuyStatus`.
* :func:`validate_media_buy_transition` — assert a transition is allowed,
  raising :class:`~adcp.decisioning.types.AdcpError` with
  ``code='INVALID_REQUEST'`` if not.
* :data:`CREATIVE_TRANSITIONS` — same shape for
  :class:`~adcp.types.CreativeStatus`.
* :func:`validate_creative_transition` — same pattern for creatives.
* :func:`ref_account_id` — extract ``account_id`` from a wire account
  reference dict without repeating ``ref.get('account_id')`` inline.

Transition maps are derived from the AdCP 3.0 spec state-machine diagrams:
``schemas/cache/3.0.0/enums/media-buy-status.json`` and
``schemas/cache/3.0.0/enums/creative-status.json``.  Terminal states have
empty sets; resubmit / unarchive paths for creatives are included.

These helpers are distinct from :data:`adcp.server.helpers.MEDIA_BUY_STATE_MACHINE`,
which maps a status to the buyer *actions* available at that status.  These
helpers validate *state-to-state transitions* (e.g. that a seller does not
move a media buy from ``completed`` back to ``active``).
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning.types import AdcpError
from adcp.types import CreativeStatus, MediaBuyStatus

# ---------------------------------------------------------------------------
# Media buy state machine
# ---------------------------------------------------------------------------

#: Valid from-state → set-of-allowed-to-states for media buys.
#:
#: Terminal states (``completed``, ``rejected``, ``canceled``) map to the
#: empty set — no further transitions are allowed.
MEDIA_BUY_TRANSITIONS: dict[MediaBuyStatus, set[MediaBuyStatus]] = {
    MediaBuyStatus.pending_creatives: {
        MediaBuyStatus.pending_start,
        MediaBuyStatus.canceled,
    },
    MediaBuyStatus.pending_start: {
        MediaBuyStatus.active,
        MediaBuyStatus.canceled,
    },
    MediaBuyStatus.active: {
        MediaBuyStatus.paused,
        MediaBuyStatus.completed,
        MediaBuyStatus.canceled,
    },
    MediaBuyStatus.paused: {
        MediaBuyStatus.active,
        MediaBuyStatus.canceled,
    },
    MediaBuyStatus.completed: set(),
    MediaBuyStatus.rejected: set(),
    MediaBuyStatus.canceled: set(),
}


def validate_media_buy_transition(
    from_state: str | MediaBuyStatus,
    to_state: str | MediaBuyStatus,
) -> None:
    """Assert that a media buy state transition is allowed by the spec.

    Accepts both ``MediaBuyStatus`` enum members and raw string values (as
    they arrive from the wire).  Unknown string values raise
    ``AdcpError('INVALID_REQUEST')`` rather than a bare ``ValueError`` so
    the framework's error envelope is always produced.

    :raises AdcpError: ``code='INVALID_REQUEST'``, ``recovery='correctable'``
        when the transition is not in :data:`MEDIA_BUY_TRANSITIONS` or when
        either state string is not a recognised :class:`MediaBuyStatus` value.
    """
    if isinstance(from_state, str):
        try:
            from_s = MediaBuyStatus(from_state)
        except ValueError:
            raise AdcpError(
                "INVALID_REQUEST",
                message=f"Unknown media buy status: {from_state!r}",
                recovery="correctable",
            )
    else:
        from_s = from_state

    if isinstance(to_state, str):
        try:
            to_s = MediaBuyStatus(to_state)
        except ValueError:
            raise AdcpError(
                "INVALID_REQUEST",
                message=f"Unknown media buy status: {to_state!r}",
                recovery="correctable",
            )
    else:
        to_s = to_state

    allowed = MEDIA_BUY_TRANSITIONS.get(from_s, set())
    if to_s not in allowed:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                f"Media buy transition {from_s.value!r} → {to_s.value!r} is not allowed"
            ),
            recovery="correctable",
        )


# ---------------------------------------------------------------------------
# Creative asset state machine
# ---------------------------------------------------------------------------

#: Valid from-state → set-of-allowed-to-states for creative assets.
#:
#: ``rejected`` allows resubmission back to ``processing`` (the buyer may fix
#: the creative and call ``sync_creatives`` again).  ``archived`` allows
#: unarchiving back to ``approved``.
CREATIVE_TRANSITIONS: dict[CreativeStatus, set[CreativeStatus]] = {
    CreativeStatus.processing: {
        CreativeStatus.pending_review,
        CreativeStatus.rejected,
    },
    CreativeStatus.pending_review: {
        CreativeStatus.approved,
        CreativeStatus.rejected,
    },
    CreativeStatus.approved: {
        CreativeStatus.archived,
    },
    CreativeStatus.rejected: {
        CreativeStatus.processing,  # resubmit via sync_creatives
    },
    CreativeStatus.archived: {
        CreativeStatus.approved,  # unarchive path
    },
}


def validate_creative_transition(
    from_state: str | CreativeStatus,
    to_state: str | CreativeStatus,
) -> None:
    """Assert that a creative asset state transition is allowed by the spec.

    Accepts both :class:`~adcp.types.CreativeStatus` enum members and raw
    string values.  Unknown string values raise ``AdcpError('INVALID_REQUEST')``
    so the error is always wire-shaped.

    :raises AdcpError: ``code='INVALID_REQUEST'``, ``recovery='correctable'``
        when the transition is not in :data:`CREATIVE_TRANSITIONS` or when
        either state string is not a recognised :class:`CreativeStatus` value.
    """
    if isinstance(from_state, str):
        try:
            from_s = CreativeStatus(from_state)
        except ValueError:
            raise AdcpError(
                "INVALID_REQUEST",
                message=f"Unknown creative status: {from_state!r}",
                recovery="correctable",
            )
    else:
        from_s = from_state

    if isinstance(to_state, str):
        try:
            to_s = CreativeStatus(to_state)
        except ValueError:
            raise AdcpError(
                "INVALID_REQUEST",
                message=f"Unknown creative status: {to_state!r}",
                recovery="correctable",
            )
    else:
        to_s = to_state

    allowed = CREATIVE_TRANSITIONS.get(from_s, set())
    if to_s not in allowed:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                f"Creative transition {from_s.value!r} → {to_s.value!r} is not allowed"
            ),
            recovery="correctable",
        )


# ---------------------------------------------------------------------------
# Account reference utilities
# ---------------------------------------------------------------------------


def ref_account_id(ref: dict[str, Any] | None) -> str | None:
    """Extract ``account_id`` from a wire account reference dict.

    Eliminates the ``ref.get('account_id') if ref else None`` pattern that
    appears in multi-tenant platform methods.

    :param ref: An account reference dict as received from the wire, or
        ``None`` (for requests that carry no account reference).
    :returns: The ``account_id`` string if present and a ``str``, else
        ``None``.
    """
    if ref is None:
        return None
    account_id = ref.get("account_id")
    return account_id if isinstance(account_id, str) else None


__all__ = [
    "CREATIVE_TRANSITIONS",
    "MEDIA_BUY_TRANSITIONS",
    "ref_account_id",
    "validate_creative_transition",
    "validate_media_buy_transition",
]
