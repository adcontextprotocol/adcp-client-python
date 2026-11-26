"""Platform identifiers on event source and audience results (AdCP 3.3).

AdCP 3.3 deprecates ``seller_id`` on ``sync_event_sources`` and
``sync_audiences`` results in favor of ``platform_id``, the object's ID in the
seller's ad platform (adcp#8290). A seller reporting one in a 3.3 or later
response MUST emit both names with identical values; buyers MUST prefer
``platform_id`` and fall back to ``seller_id`` only when ``platform_id`` is
absent. At this location ``seller_id`` keeps its object-ID meaning: it never
identifies the seller. Requests keep using ``event_source_id`` and
``audience_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

__all__ = ["platform_id_of"]

#: The result array that carries platform IDs, by task.
_RESULT_ARRAYS = {"sync_event_sources": "event_sources", "sync_audiences": "audiences"}

#: The first release that defines ``platform_id`` on these results.
_PLATFORM_ID_MIN_ADCP_VERSION = "3.3-beta.0"


def platform_id_of(result: Any) -> str | None:
    """The platform ID of one event source or audience result, or ``None``.

    Reads ``platform_id`` and falls back to the deprecated ``seller_id`` alias
    only when ``platform_id`` is absent, as AdCP 3.3 requires of buyers, so it
    reads a 3.2 seller's results too. ``result`` is one item of a
    ``sync_event_sources`` ``event_sources`` or ``sync_audiences``
    ``audiences`` array, as a generated model or a mapping.
    """
    if isinstance(result, Mapping):
        platform_id, alias = result.get("platform_id"), result.get("seller_id")
    else:
        platform_id = getattr(result, "platform_id", None)
        alias = getattr(result, "seller_id", None)
    value = platform_id if platform_id is not None else alias
    return None if value is None else str(value)


def _dual_emit_platform_ids(task: str, payload: Any, *, adcp_version: str | None) -> None:
    """Project a seller's result rows onto both platform ID names, in place.

    Served at 3.3 or later (or with no version), a row carrying either name
    gets both with the same value. Served earlier, it gets only ``seller_id``,
    the one name that release defines. Raises :class:`ValueError` naming the
    row when the handler set the two names to different values: the spec
    requires them identical, and the SDK cannot tell which one is right.
    """
    from adcp._version import (
        is_adcp_version_at_least,
        normalize_to_release_precision,
        resolve_adcp_version_alias,
    )

    key = _RESULT_ARRAYS.get(task)
    rows = payload.get(key) if key is not None and isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return
    dual = adcp_version is None or is_adcp_version_at_least(
        resolve_adcp_version_alias(normalize_to_release_precision(adcp_version)),
        _PLATFORM_ID_MIN_ADCP_VERSION,
    )
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        platform_id, alias = row.get("platform_id"), row.get("seller_id")
        if platform_id is not None and alias is not None and platform_id != alias:
            raise ValueError(
                f"/{key}/{index} carries platform_id and seller_id with different values; "
                "AdCP 3.3 requires them identical (seller_id is the deprecated alias of "
                "platform_id, the object's ID in the ad platform). Return platform_id only."
            )
        value = platform_id if platform_id is not None else alias
        if value is None:
            continue
        row["seller_id"] = value
        if dual:
            row["platform_id"] = value
        else:
            row.pop("platform_id", None)


def _dual_emitted(task: str, result: Any) -> Any:
    """A stored task artifact with both platform ID names, as a copy.

    Task registries persist a terminal artifact once and serve it to every
    later reader (``tasks/get``, the completion webhook) whatever release
    each negotiated, so it carries both names. Returns ``result`` itself for
    any other task. Raises :class:`ValueError` on a mismatch, like
    :func:`_dual_emit_platform_ids`.
    """
    if task not in _RESULT_ARRAYS or not isinstance(result, dict):
        return result
    copied = deepcopy(result)
    _dual_emit_platform_ids(task, copied, adcp_version=None)
    return copied
