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
from adcp.compat.legacy import LEGACY_ADAPTER_VERSIONS

#: Every version the server speaks — natively-validated majors plus
#: legacy versions handled via the adapter path. Used as the default
#: ``supported`` set for :func:`detect_wire_version` so the dispatcher
#: accepts both shapes.
SUPPORTED_WIRE_VERSIONS: tuple[str, ...] = (
    *COMPATIBLE_ADCP_VERSIONS,
    *LEGACY_ADAPTER_VERSIONS,
)


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
    supported: tuple[str, ...] = SUPPORTED_WIRE_VERSIONS,
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
    # Empty-string ``adcp_version`` falls through to ``adcp_major_version``
    # intentionally — pre-3.1 buyers may set both fields, and an empty
    # string from a half-migrated client shouldn't override the int field.

    major_value = payload.get("adcp_major_version")
    # Wire field is strictly an int per spec (``minimum:1, maximum:99``).
    # Two type-coercion cases that would otherwise bypass the supported-set
    # check silently — reject loudly instead:
    # * ``bool`` is an ``int`` subclass; ``True``/``False`` would map to
    #   major=1/0.
    # * String ints (``"3"``) from a buyer that JSON-stringified the field —
    #   ``isinstance(x, int)`` returns False, so without an explicit check
    #   the buyer would silently get SDK-pin validation instead of an error.
    if isinstance(major_value, str):
        raise UnsupportedVersionError(major_value, supported)
    if isinstance(major_value, int) and not isinstance(major_value, bool):
        if major_value < 1:
            raise UnsupportedVersionError(major_value, supported)
        candidates = [v for v in supported if v.startswith(f"{major_value}.")]
        if not candidates:
            raise UnsupportedVersionError(major_value, supported)
        # Highest supported minor for this major.
        return max(candidates, key=lambda v: int(v.split(".")[1].split("-")[0]))

    return None
