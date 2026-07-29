"""Compatibility helpers for legacy and parameterized creative format IDs.

These helpers cover the common migration case where older buyers or catalogs
refer to a named format such as ``display_300x250`` while newer schema surfaces
carry the canonical template ``display_image`` plus explicit dimensions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from adcp.canonical_formats.identity import canonicalize_agent_url
from adcp.types.legacy import LegacyFormatId

FormatId = LegacyFormatId

CANONICAL_CREATIVE_AGENT_URL = "https://creative.adcontextprotocol.org"
"""Default ``agent_url`` for AdCP standard creative formats."""

_DISPLAY_SIZE_RE = re.compile(
    r"^display_(?P<width>[1-9][0-9]*)x(?P<height>[1-9][0-9]*)(?:_image)?$"
)


def _coerce_format_id(
    value: str | FormatId | Mapping[str, Any],
    *,
    default_agent_url: str,
) -> FormatId:
    """Coerce supported helper inputs to the public ``FormatId`` model."""
    if isinstance(value, FormatId):
        return value
    if isinstance(value, str):
        return FormatId.model_validate({"agent_url": default_agent_url, "id": value})
    if isinstance(value, Mapping):
        body = dict(value)
        body.setdefault("agent_url", default_agent_url)
        try:
            return FormatId.model_validate(body)
        except ValidationError:
            # Re-raise with the public model's normal validation details.
            raise
    raise TypeError("format id must be a string, FormatId, or mapping with at least an 'id' field")


def upgrade_legacy_format_id(
    value: str | FormatId | Mapping[str, Any],
    *,
    default_agent_url: str = CANONICAL_CREATIVE_AGENT_URL,
) -> FormatId:
    """Return ``value`` as a canonical, parameterized ``FormatId`` when known.

    The current canonical upgrade maps legacy display size IDs such as
    ``display_300x250`` and ``display_300x250_image`` to
    ``display_image`` with ``width=300`` and ``height=250``. Unknown IDs are
    still returned as structured ``FormatId`` values so callers can compare
    them consistently.
    """
    is_bare_legacy_id = isinstance(value, str)
    fid = _coerce_format_id(value, default_agent_url=default_agent_url)
    match = _DISPLAY_SIZE_RE.fullmatch(fid.id)
    if match is None:
        return fid
    default_fid = _coerce_format_id("__default__", default_agent_url=default_agent_url)
    if not is_bare_legacy_id and canonicalize_agent_url(fid.agent_url) != canonicalize_agent_url(
        default_fid.agent_url
    ):
        return fid

    return FormatId.model_validate(
        {
            "agent_url": str(fid.agent_url),
            "id": "display_image",
            "width": int(match.group("width")),
            "height": int(match.group("height")),
            "duration_ms": fid.duration_ms,
        }
    )


def formats_are_equivalent(
    a: str | FormatId | Mapping[str, Any],
    b: str | FormatId | Mapping[str, Any],
    *,
    default_agent_url: str = CANONICAL_CREATIVE_AGENT_URL,
) -> bool:
    """Return true when two format IDs identify the same canonical family.

    Both inputs are first passed through :func:`upgrade_legacy_format_id`.
    Declared parameters must not conflict, but an omitted parameter on either
    side is treated as unspecified rather than a mismatch. Use
    :func:`format_is_supported` for product/capability gating where a
    supported fixed size or duration requires the request to state that value.
    """
    left = upgrade_legacy_format_id(a, default_agent_url=default_agent_url)
    right = upgrade_legacy_format_id(b, default_agent_url=default_agent_url)
    if canonicalize_agent_url(left.agent_url) != canonicalize_agent_url(right.agent_url):
        return False
    if left.id != right.id:
        return False

    for field in ("width", "height", "duration_ms"):
        left_value = getattr(left, field)
        right_value = getattr(right, field)
        if left_value is not None and right_value is not None and left_value != right_value:
            return False
    return True


def format_is_supported(
    requested: str | FormatId | Mapping[str, Any],
    supported: str | FormatId | Mapping[str, Any],
    *,
    default_agent_url: str = CANONICAL_CREATIVE_AGENT_URL,
) -> bool:
    """Return true when ``requested`` is acceptable for ``supported``.

    This is intentionally stricter than :func:`formats_are_equivalent`.
    A broad supported format such as ``display_image`` accepts a specific
    request such as ``display_image`` 300x250, but a fixed supported product
    format requires the request to provide and match every fixed parameter
    (``width``, ``height``, and ``duration_ms``).
    """
    req = upgrade_legacy_format_id(requested, default_agent_url=default_agent_url)
    sup = upgrade_legacy_format_id(supported, default_agent_url=default_agent_url)
    if not formats_are_equivalent(req, sup, default_agent_url=default_agent_url):
        return False

    for field in ("width", "height", "duration_ms"):
        supported_value = getattr(sup, field)
        if supported_value is None:
            continue
        if getattr(req, field) != supported_value:
            return False
    return True


__all__ = [
    "CANONICAL_CREATIVE_AGENT_URL",
    "format_is_supported",
    "formats_are_equivalent",
    "upgrade_legacy_format_id",
]
