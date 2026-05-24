"""Shared helpers for media-buy lifecycle status compatibility.

The sync create/update media-buy response arms accept legacy constructor and
handler payloads where ``status`` carried lifecycle state. ``completed`` is a
valid lifecycle enum value, but it is also the 3.1 task-envelope success status,
so the SDK only infers lifecycle status from the unambiguous subset below.
"""

from __future__ import annotations

from typing import Any

MEDIA_BUY_LEGACY_STATUS_VALUES = frozenset(
    {
        "active",
        "canceled",
        "paused",
        "pending_creatives",
        "pending_start",
        "rejected",
    }
)


def unwrap_enum_value(value: Any) -> Any:
    return getattr(value, "value", value)
