"""Wire-version detection for inbound AdCP requests.

Per the AdCP version-envelope contract (``core/version-envelope.json``),
every request carries either:

* ``adcp_version`` — release-precision string (``"3.0"``, ``"3.1"``,
  ``"3.1-beta"``). Added in 3.1+; takes precedence when present.
* ``adcp_major_version`` — integer (``2``, ``3``). The pre-3.1 wire shape
  and the lowest common denominator for buyers that don't yet emit the
  release-precision field.

:func:`detect_wire_version` collapses both shapes to a release-precision
string the loader can pass to :func:`adcp.validation.schema_loader.get_validator`
as ``version=``. A buyer claiming an unsupported version raises a
:class:`UnsupportedVersionError`, which the dispatcher converts to an
``AdcpError`` with code ``VERSION_UNSUPPORTED`` per the spec.

Mirrors the JS SDK's ``applyVersionEnvelope`` in
``src/lib/protocols/index.ts``.
"""

from __future__ import annotations

from typing import Any

from adcp._version import COMPATIBLE_ADCP_VERSIONS, normalize_to_release_precision


class UnsupportedVersionError(ValueError):
    """The wire version the buyer claims isn't supported by this server.

    Carries the original wire value plus the supported list so the
    dispatcher can echo both into ``VERSION_UNSUPPORTED`` error details.
    """

    def __init__(self, wire_value: str | int, supported: tuple[str, ...]) -> None:
        self.wire_value = wire_value
        self.supported = supported
        super().__init__(
            f"AdCP version {wire_value!r} is not supported by this server "
            f"(supported release-precision versions: {list(supported)})."
        )


def detect_wire_version(
    payload: Any,
    *,
    supported: tuple[str, ...] = COMPATIBLE_ADCP_VERSIONS,
) -> str | None:
    """Return the release-precision version a request claims, or ``None``.

    Resolution order:

    1. ``payload['adcp_version']`` — string, normalized to release
       precision (``"3.0.7"`` → ``"3.0"``). Must be in ``supported`` or
       raises :class:`UnsupportedVersionError`.
    2. ``payload['adcp_major_version']`` — int. Maps to the highest minor
       in ``supported`` for that major. No supported minor for the major
       raises :class:`UnsupportedVersionError`.
    3. Neither field set — returns ``None`` so the caller falls back to
       the SDK's compile-time pin.

    Non-dict payloads return ``None`` (validation skipped — the schema
    layer rejects non-dict requests via its own type check).
    """
    if not isinstance(payload, dict):
        return None

    explicit = payload.get("adcp_version")
    if isinstance(explicit, str) and explicit:
        try:
            normalized = normalize_to_release_precision(explicit)
        except ValueError as exc:
            raise UnsupportedVersionError(explicit, supported) from exc
        if normalized not in supported:
            raise UnsupportedVersionError(explicit, supported)
        return normalized

    major_int = payload.get("adcp_major_version")
    # Reject bool (subclass of int) — the wire field is strictly numeric;
    # ``True``/``False`` slipping through would otherwise map to major=1/0.
    if isinstance(major_int, int) and not isinstance(major_int, bool):
        candidates = [v for v in supported if v.startswith(f"{major_int}.")]
        if not candidates:
            raise UnsupportedVersionError(major_int, supported)
        # Highest supported minor for this major.
        return max(candidates, key=lambda v: int(v.split(".")[1].split("-")[0]))

    return None
