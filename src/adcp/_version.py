"""Internal helpers for AdCP protocol version pinning.

Version pinning is per-instance (Stripe model): each ``ADCPClient`` /
``ADCPMultiAgentClient`` / ``ADCPServerBuilder`` accepts an
``adcp_version`` constructor option that selects which AdCP release the
SDK speaks for that instance. Default is the SDK's compile-time pin
(``ADCP_VERSION`` packaged with the wheel).

Stage 2 (this module): validates the pin at construction and exposes
the resolved value via ``get_adcp_version()``. Cross-major pins raise
:class:`adcp.exceptions.ConfigurationError`. No wire behavior change
yet — Stage 3 lifts the cross-major fence and threads per-instance
schema/validator selection through the validation hooks.

Release-precision strings are the canonical input form (``"3.0"``,
``"3.1"``, ``"3.1-beta"``). Patch-precision strings (``"3.0.1"``) are
accepted for backwards compatibility with the legacy ADCP_VERSION file
shape, but the SDK normalizes to release precision internally and on
the wire — patches are not part of the negotiation contract per the
spec's three-tier model. See specs/version-negotiation.md upstream.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Release-precision versions this SDK can speak. Patch-level pinning is
# intentionally absent — patches don't change the wire contract by
# definition, so making them part of the pin is a category error.
COMPATIBLE_ADCP_VERSIONS: tuple[str, ...] = ("3.0", "3.1")

# Major version this SDK is built for. Cross-major pins are rejected at
# construction. To speak a different major, install the SDK major that
# targets it.
ADCP_MAJOR_VERSION: int = 3

# Matches release-precision (3.0, 3.1) and patch-precision (3.0.0,
# 3.0.1) semver, with optional pre-release tag (3.1-beta, 3.1.0-rc.1).
# Captures the major as group 1.
_VERSION_RE: re.Pattern[str] = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-[a-zA-Z0-9.-]+)?$")


def parse_adcp_major_version(version: str) -> int:
    """Extract the major component from a release- or patch-precision version string.

    Accepts ``"3.0"``, ``"3.1"``, ``"3.0.1"``, ``"3.1-beta"``,
    ``"3.1.0-rc.1"``, etc. Raises :class:`ValueError` (caught by
    :func:`resolve_adcp_version` and reraised as
    :class:`ConfigurationError`) on anything else.

    The integer return value is the only thing the cross-major fence
    cares about — release-vs-patch precision is preserved for downstream
    use elsewhere.
    """
    match = _VERSION_RE.match(version)
    if match is None:
        raise ValueError(
            f"adcp_version {version!r} is not a valid semver-shaped string. "
            f"Expected release-precision (e.g. '3.0', '3.1') or "
            f"patch-precision (e.g. '3.0.1'); pre-release tags allowed "
            f"(e.g. '3.1-beta')."
        )
    return int(match.group(1))


def _read_packaged_version() -> str:
    """Return the ``ADCP_VERSION`` value packaged with the wheel."""
    from importlib.resources import files

    return (files("adcp") / "ADCP_VERSION").read_text().strip()


def resolve_adcp_version(pin: str | None) -> str:
    """Validate and resolve a constructor-supplied ``adcp_version`` pin.

    - ``None`` → returns the packaged ``ADCP_VERSION`` (SDK default).
    - Same-major pin → returned as-is. Release- and patch-precision
      both accepted; the SDK does not normalize the string at this
      layer (callers see what they passed).
    - Cross-major pin → raises :class:`ConfigurationError`.
    - Unparseable string → raises :class:`ConfigurationError`.

    The cross-major fence is the only construction-time fail. Within
    the same major, release-level pins are accepted optimistically —
    Stage 3 (per-instance schema/validator selection) is what
    actually validates that the pinned release exists in the SDK's
    schema cache. Until Stage 3 lands, the pin is plumbing only.
    """
    # Imported here to avoid a circular import at module load time.
    from adcp.exceptions import ConfigurationError

    if pin is None:
        return _read_packaged_version()

    try:
        major = parse_adcp_major_version(pin)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc

    if major != ADCP_MAJOR_VERSION:
        raise ConfigurationError(
            f"adcp_version={pin!r} targets major {major}, but this SDK speaks "
            f"AdCP {ADCP_MAJOR_VERSION}.x. Install the SDK major that targets "
            f"AdCP {major}.x — cross-major pinning is not supported."
        )

    return pin
