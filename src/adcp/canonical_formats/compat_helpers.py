"""Compatibility helpers for legacy and parameterized creative format IDs.

These helpers cover the common migration case where older buyers or catalogs
refer to a named format such as ``display_300x250`` while newer schema surfaces
carry the canonical template ``display_image`` plus explicit dimensions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from adcp.canonical_formats.identity import canonicalize_agent_url, coerce_format_id
from adcp.types.legacy import FormatReferenceStructuredObject, LegacyFormatId

FormatId = LegacyFormatId

FormatIdInput = str | FormatReferenceStructuredObject | Mapping[str, Any]
"""Accepted format-id input: a bare legacy ID, any structured format
reference (the schema class generated model fields carry, or the legacy
subclass), or a mapping of the reference's fields."""

CANONICAL_CREATIVE_AGENT_URL = "https://creative.adcontextprotocol.org"
"""Default ``agent_url`` for AdCP standard creative formats."""

_DISPLAY_SIZE_RE = re.compile(
    r"^display_(?P<width>[1-9][0-9]*)x(?P<height>[1-9][0-9]*)(?:_image)?$"
)

#: The parameters a format reference carries beyond its ``(agent_url, id)``
#: identity, derived from the model instead of listed.
#:
#: A hand-written list here is the defect that dropped ``pixel_ratio``: three
#: of the four parameters were copied by name, so a creative that claimed no
#: pixel density compared equal to a product format that requires 2x, two
#: references declaring *conflicting* densities compared equal, and the
#: legacy-size upgrade discarded the density it was handed. Deriving the set
#: means a parameter added to ``core/format-id.json`` — and so to the
#: generated model — is compared without being named here.
_PARAMETER_FIELDS: tuple[str, ...] = tuple(sorted(set(FormatId.model_fields) - {"agent_url", "id"}))

#: Parameters the schema gives a value to when they are absent.
#:
#: ``core/format-id.json`` describes omitting ``pixel_ratio`` as selecting
#: "the backward-compatible 1x variant", which is a value, not silence. An
#: omitted width or duration means "template, unparameterized"; an omitted
#: density means 1x — on either side. So a product fixed at 2x refuses a
#: request that says nothing, a product that says nothing refuses a 2x
#: request, and one fixed at 1x still serves a request that says nothing.
#: Both helpers read it this way, so equivalence never contradicts gating
#: on density.
_IMPLIED_WHEN_ABSENT: dict[str, float] = {"pixel_ratio": 1.0}


def _parameter_value(fid: FormatId, field: str) -> Any:
    """The value ``fid`` gives ``field``, reading an implied default for absence.

    The implied value applies to whichever side omits the field — a supported
    300x250 with no density is the 1x variant just as a requested one is. It
    applies only to a dimensioned reference, because ``pixel_ratio`` "Requires
    ``width`` and ``height``": a template such as bare ``display_image`` has
    no 1x variant to select and stays broad.
    """
    value = getattr(fid, field)
    if value is None and fid.width is not None and fid.height is not None:
        return _IMPLIED_WHEN_ABSENT.get(field)
    return value


def _coerce_format_id(
    value: FormatIdInput,
    *,
    default_agent_url: str,
    param: str,
) -> FormatId:
    """Coerce supported helper inputs to the public ``FormatId`` model.

    ``param`` names the caller's parameter so a refusal says which argument
    carried the unsupported value.
    """
    if isinstance(value, FormatReferenceStructuredObject):
        return coerce_format_id(value)
    if isinstance(value, str):
        return coerce_format_id({"agent_url": default_agent_url, "id": value})
    if isinstance(value, Mapping):
        body = dict(value)
        body.setdefault("agent_url", default_agent_url)
        return coerce_format_id(body)
    raise TypeError(
        f"{param} must be a string, FormatId, or mapping with at least an 'id' field; "
        f"got {type(value).__name__}"
    )


def _upgrade_legacy_format_id(
    value: FormatIdInput,
    *,
    default_agent_url: str,
    param: str,
) -> FormatId:
    """Upgrade ``value``, reporting a refusal against the caller's parameter."""
    is_bare_legacy_id = isinstance(value, str)
    fid = _coerce_format_id(value, default_agent_url=default_agent_url, param=param)
    match = _DISPLAY_SIZE_RE.fullmatch(fid.id)
    if match is None:
        return fid
    default_fid = _coerce_format_id("__default__", default_agent_url=default_agent_url, param=param)
    if not is_bare_legacy_id and canonicalize_agent_url(fid.agent_url) != canonicalize_agent_url(
        default_fid.agent_url
    ):
        return fid

    return FormatId.model_validate(
        {
            # Every parameter the reference carried, then the two the legacy
            # size ID itself states. Reparameterizing the ID must not drop a
            # parameter the caller supplied alongside it.
            **{field: getattr(fid, field) for field in _PARAMETER_FIELDS},
            "agent_url": str(fid.agent_url),
            "id": "display_image",
            "width": int(match.group("width")),
            "height": int(match.group("height")),
        }
    )


def upgrade_legacy_format_id(
    value: FormatIdInput,
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
    return _upgrade_legacy_format_id(value, default_agent_url=default_agent_url, param="value")


def formats_are_equivalent(
    a: FormatIdInput,
    b: FormatIdInput,
    *,
    default_agent_url: str = CANONICAL_CREATIVE_AGENT_URL,
) -> bool:
    """Return true when two format IDs identify the same canonical family.

    Both inputs are first passed through :func:`upgrade_legacy_format_id`.
    Declared parameters must not conflict, but an omitted width, height or
    duration on either side is treated as unspecified rather than a mismatch:
    omitting them names the template, which covers every variant. An omitted
    ``pixel_ratio`` on a sized reference is not unspecified — it names the 1x
    variant — so 300x250 and 300x250 at 2x are different formats here too.
    Use :func:`format_is_supported` for product/capability gating, where a
    supported fixed size, duration or pixel density requires the request to
    state that value.
    """
    left = _upgrade_legacy_format_id(a, default_agent_url=default_agent_url, param="a")
    right = _upgrade_legacy_format_id(b, default_agent_url=default_agent_url, param="b")
    if canonicalize_agent_url(left.agent_url) != canonicalize_agent_url(right.agent_url):
        return False
    if left.id != right.id:
        return False

    for field in _PARAMETER_FIELDS:
        left_value = _parameter_value(left, field)
        right_value = _parameter_value(right, field)
        if left_value is not None and right_value is not None and left_value != right_value:
            return False
    return True


def format_is_supported(
    requested: FormatIdInput,
    supported: FormatIdInput,
    *,
    default_agent_url: str = CANONICAL_CREATIVE_AGENT_URL,
) -> bool:
    """Return true when ``requested`` is acceptable for ``supported``.

    This is intentionally stricter than :func:`formats_are_equivalent`.
    A broad supported format such as ``display_image`` accepts a specific
    request such as ``display_image`` 300x250, but a fixed supported product
    format requires the request to provide and match every parameter the
    supported side fixes — every parameter ``core/format-id.json`` declares,
    not a chosen few.

    ``pixel_ratio`` is the one parameter whose absence carries a value rather
    than meaning "unspecified": the schema describes omitting it as selecting
    the backward-compatible 1x variant. That reading applies to both sides
    of a dimensioned reference: a product fixed at 2x refuses a request that
    omits the field, a product that omits it (so is 1x) refuses a 2x request,
    and a product fixed at 1x accepts a request that omits it.
    """
    req = _upgrade_legacy_format_id(
        requested, default_agent_url=default_agent_url, param="requested"
    )
    sup = _upgrade_legacy_format_id(
        supported, default_agent_url=default_agent_url, param="supported"
    )
    if not formats_are_equivalent(req, sup, default_agent_url=default_agent_url):
        return False

    for field in _PARAMETER_FIELDS:
        supported_value = _parameter_value(sup, field)
        if supported_value is None:
            continue
        if _parameter_value(req, field) != supported_value:
            return False
    return True


__all__ = [
    "CANONICAL_CREATIVE_AGENT_URL",
    "format_is_supported",
    "formats_are_equivalent",
    "upgrade_legacy_format_id",
]
