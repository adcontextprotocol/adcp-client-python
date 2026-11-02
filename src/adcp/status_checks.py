"""Schedule status checks from a seller's ``expected_confirmation_at`` (AdCP 3.3).

A seller whose confirmation waits on a scheduled event (a booking round, a
review window) SHOULD set ``expected_confirmation_at`` on the ``submitted``
envelope of ``create_media_buy``, ``buy_products``, ``accept_proposal`` and
finalize-only ``refine_proposals``, and on a ``get_media_buys`` buy whose
``confirmed_at`` is still null. Buyers SHOULD check status at that time rather
than polling tightly before it.

It is an estimate, not a deadline: the seller may confirm earlier or later,
the task status stays authoritative, and a passed timestamp is never a
failure. :func:`next_status_check_delay` therefore bounds the wait between a
floor (the normal polling interval, also used once the estimate has passed)
and a ceiling, so an early confirmation is still noticed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

__all__ = ["expected_confirmation_at", "next_status_check_delay"]


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def expected_confirmation_at(source: Any) -> datetime | None:
    """Return the seller's confirmation estimate carried by ``source``, if any.

    ``source`` is a :class:`~adcp.types.core.TaskResult`, a response model, a
    ``get_media_buys`` buy, or a wire mapping. Returns a timezone-aware
    datetime, or ``None`` when the field is absent or not an RFC 3339 instant.
    """
    from adcp.types.core import TaskResult

    if isinstance(source, TaskResult):
        found = expected_confirmation_at(source.data)
        if found is None and source.metadata:
            found = _parse(source.metadata.get("expected_confirmation_at"))
        return found
    if isinstance(source, Mapping):
        return _parse(source.get("expected_confirmation_at"))
    root = getattr(source, "root", None)
    if root is not None and root is not source:
        return expected_confirmation_at(root)
    return _parse(getattr(source, "expected_confirmation_at", None))


def next_status_check_delay(
    estimate: datetime | str | None,
    *,
    poll_interval: float,
    max_interval: float,
    now: datetime | None = None,
) -> float:
    """Seconds to wait before the next status check.

    Without an estimate, or once it has passed, this is ``poll_interval``. With
    an estimate in the future it is the time until the estimate, never less
    than ``poll_interval`` (the floor) and never more than ``max_interval``
    (the ceiling), so a seller that confirms early is still seen within
    ``max_interval``.
    """
    if poll_interval <= 0 or max_interval <= 0:
        raise ValueError("poll_interval and max_interval must be positive")
    if max_interval < poll_interval:
        raise ValueError("max_interval must be at least poll_interval")
    when = _parse(estimate)
    if when is None:
        return poll_interval
    current = now if now is not None else datetime.now(timezone.utc)
    until = (when - current).total_seconds()
    return min(max(until, poll_interval), max_interval)
