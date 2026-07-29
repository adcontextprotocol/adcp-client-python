"""Negotiated creative dialect selection for AdCP 3.x."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from adcp._version import normalize_to_release_precision


class CreativeDialect(str, Enum):
    """Creative identity shape expected at a protocol boundary."""

    LEGACY = "legacy"
    CANONICAL = "canonical"


class CreativeDialectError(ValueError):
    """Raised when negotiation cannot select a safe creative dialect."""


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="python", exclude_none=True)
        return result if isinstance(result, Mapping) else None
    return None


def canonical_creatives_capability(capabilities: Any) -> bool | None:
    """Read ``media_buy.features.canonical_creatives`` without guessing."""

    root = _as_mapping(capabilities)
    if root is None:
        return None
    media_buy = _as_mapping(root.get("media_buy"))
    features = _as_mapping(media_buy.get("features")) if media_buy else None
    value = features.get("canonical_creatives") if features else None
    return value if isinstance(value, bool) else None


def _schema_evidence(value: Any) -> tuple[bool, bool]:
    """Return ``(canonical, legacy)`` evidence found in a request-local value."""

    canonical_model = bool(getattr(value.__class__, "__adcp_canonical_creative_model__", False))
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python", exclude_none=True)
    canonical = canonical_model
    legacy = False
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"format_kind", "format_options", "format_option_refs"} and item:
                canonical = True
            if key in {"format_id", "format_ids"} and item:
                legacy = True
            child_canonical, child_legacy = _schema_evidence(item)
            canonical = canonical or child_canonical
            legacy = legacy or child_legacy
    elif isinstance(value, (list, tuple)):
        for item in value:
            child_canonical, child_legacy = _schema_evidence(item)
            canonical = canonical or child_canonical
            legacy = legacy or child_legacy
    return canonical, legacy


def resolve_creative_dialect(
    adcp_version: str,
    *,
    capabilities: Any = None,
    request: Any = None,
    legacy_projection_available: bool = False,
) -> CreativeDialect:
    """Apply the normative 3.0/3.1/3.2 canonical-creatives matrix.

    AdCP 3.1 is deliberately evidence-driven. Contradictory or absent evidence
    fails closed, except when the caller has already established an unambiguous
    legacy projection route.
    """

    normalized = normalize_to_release_precision(adcp_version)
    release = normalized.split("-", 1)[0]
    major, minor = (int(part) for part in release.split(".", 1))
    if major != 3:
        raise CreativeDialectError(
            f"canonical creative negotiation only supports AdCP 3.x, got {adcp_version!r}"
        )

    capability = canonical_creatives_capability(capabilities)
    if minor == 0:
        return CreativeDialect.LEGACY
    if minor >= 2:
        if capability is False:
            raise CreativeDialectError(
                "AdCP 3.2+ requires canonical creatives, but the seller advertised "
                "canonical_creatives=false"
            )
        return CreativeDialect.CANONICAL

    if capability is True:
        return CreativeDialect.CANONICAL
    if capability is False:
        return CreativeDialect.LEGACY

    canonical, legacy = _schema_evidence(request)
    if canonical and not legacy:
        return CreativeDialect.CANONICAL
    if legacy and not canonical:
        return CreativeDialect.LEGACY
    if legacy_projection_available:
        return CreativeDialect.LEGACY
    raise CreativeDialectError(
        "AdCP 3.1 does not establish a creative dialect: advertise "
        "media_buy.features.canonical_creatives or provide unambiguous "
        "request-local schema evidence"
    )


__all__ = [
    "CreativeDialect",
    "CreativeDialectError",
    "canonical_creatives_capability",
    "resolve_creative_dialect",
]
