"""Strict adopter-facing type checks for media-buy terminal and observed graphs."""

from __future__ import annotations

from collections.abc import Mapping

from typing_extensions import assert_type

from adcp.decisioning import (
    MEDIA_BUY_TERMINAL_STATUSES,
    MEDIA_BUY_TRANSITIONS,
    OBSERVED_MEDIA_BUY_TRANSITIONS,
    assert_media_buy_transition,
    is_observable_media_buy_transition,
    is_terminal_media_buy_status,
)
from adcp.types import MediaBuyStatus

assert_type(MEDIA_BUY_TERMINAL_STATUSES, frozenset[str])
assert_type(MEDIA_BUY_TRANSITIONS, Mapping[str, frozenset[str]])
assert_type(OBSERVED_MEDIA_BUY_TRANSITIONS, Mapping[str, frozenset[str]])
assert_type(is_terminal_media_buy_status("completed"), bool)
assert_type(is_terminal_media_buy_status(MediaBuyStatus.completed), bool)
assert_type(is_observable_media_buy_transition("pending_creatives", "active"), bool)


def record_platform_status(current: str, reported: MediaBuyStatus) -> str:
    """Mirror an ad-server status; refuse only buyer requests."""
    if reported == current or is_terminal_media_buy_status(current):
        return current
    if is_observable_media_buy_transition(current, reported):
        return reported.value
    return current


def apply_buyer_request(current: str, requested: str, media_buy_id: str) -> str:
    assert_media_buy_transition(current, requested, media_buy_id=media_buy_id)
    return requested
