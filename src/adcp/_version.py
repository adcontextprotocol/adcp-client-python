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

# Release-precision versions this SDK can speak. Patch-level pinning is
# intentionally absent — patches don't change the wire contract by
# definition, so making them part of the pin is a category error.
COMPATIBLE_ADCP_VERSIONS: tuple[str, ...] = ("3.0", "3.1")

# Major version this SDK is built for. Cross-major pins are rejected at
# construction. To speak a different major, install the SDK major that
# targets it.
ADCP_MAJOR_VERSION: int = 3

# Matches release-precision (3.0, 3.1) and patch-precision (3.0.0,
# 3.0.1) semver, with optional pre-release tag (3.1-beta, 3.1.0-rc.1)
# and optional build metadata (3.0.1+canary, 3.1.0-beta+exp.sha.5114f85).
# Captures the major as group 1.
_VERSION_RE: re.Pattern[str] = re.compile(
    r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-[a-zA-Z0-9.-]+)?(?:\+[a-zA-Z0-9.-]+)?$"
)


def normalize_to_release_precision(version: str) -> str:
    """Strip patch component (and build metadata) for wire emission.

    Per the AdCP version-negotiation spec
    (``core/version-envelope.json``), wire values for ``adcp_version``
    are release-precision only. SDKs that read full-semver values
    from bundle metadata (``ADCP_VERSION`` file, ``published_version``,
    etc.) MUST normalize before emitting on the wire — meta-field
    values are not valid wire values.

    Pre-release tags are preserved (they describe the release line);
    build metadata is dropped (it's purely a build identifier, never
    part of a contract).

    Examples:

    - ``"3.0"``              → ``"3.0"`` (already release-precision)
    - ``"3.0.0"``            → ``"3.0"``
    - ``"3.0.1"``            → ``"3.0"``
    - ``"3.1-beta"``         → ``"3.1-beta"``
    - ``"3.1.0-beta"``       → ``"3.1-beta"``
    - ``"3.1.0-rc.1"``       → ``"3.1-rc.1"``
    - ``"3.0.1+canary"``     → ``"3.0"``
    - ``"3.1.0-beta+sha.5"`` → ``"3.1-beta"``

    Raises :class:`ValueError` on unparseable strings.
    """
    match = _VERSION_RE.match(version)
    if match is None:
        raise ValueError(f"adcp_version {version!r} is not a valid semver-shaped string.")
    major, release = match.group(1), match.group(2)
    # Skip past patch component (group 3) if present, then take whatever's
    # left (pre-release tag and/or build metadata) and drop the +build half.
    rest_start = match.end(3) if match.group(3) is not None else match.end(2)
    rest = version[rest_start:]
    if "+" in rest:
        rest = rest.split("+", 1)[0]
    return f"{major}.{release}{rest}"


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

    - ``None`` → reads the packaged ``ADCP_VERSION`` file (SDK default).
    - Same-major pin → accepted.
    - Cross-major pin → raises :class:`ConfigurationError`.
    - Unparseable string → raises :class:`ConfigurationError`.

    All resolved pins are normalized to release-precision before being
    returned, per the spec's wire-value rule
    (``core/version-envelope.json``). Patch-precision inputs like
    ``"3.0.1"`` are accepted (the ``ADCP_VERSION`` file ships in this
    shape today) but stored and emitted as ``"3.0"``. ``get_adcp_version()``
    therefore returns release-precision regardless of what the caller
    passed; this is intentional — wire values are the canonical form.
    """
    # Imported here to avoid a circular import at module load time.
    from adcp.exceptions import ConfigurationError

    raw = pin if pin is not None else _read_packaged_version()

    try:
        major = parse_adcp_major_version(raw)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc

    if major != ADCP_MAJOR_VERSION:
        raise ConfigurationError(
            f"adcp_version={raw!r} targets major {major}, but this SDK speaks "
            f"AdCP {ADCP_MAJOR_VERSION}.x. Install the SDK major that targets "
            f"AdCP {major}.x — cross-major pinning is not supported."
        )

    return normalize_to_release_precision(raw)
