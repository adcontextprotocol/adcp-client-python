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
COMPATIBLE_ADCP_VERSIONS: tuple[str, ...] = ("3.0", "3.1", "3.2")

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


def _semver_parts(version: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    """Parse the release and prerelease components used for ordering."""
    match = _VERSION_RE.match(version)
    if match is None:
        raise ValueError(f"adcp_version {version!r} is not a valid semver-shaped string.")

    core = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
    rest_start = match.end(3) if match.group(3) is not None else match.end(2)
    rest = version[rest_start:].split("+", 1)[0]
    prerelease = tuple(rest[1:].split(".")) if rest.startswith("-") else None
    return core, prerelease


def _compare_prerelease(left: tuple[str, ...] | None, right: tuple[str, ...] | None) -> int:
    if left is None:
        return 0 if right is None else 1
    if right is None:
        return -1

    for left_part, right_part in zip(left, right, strict=False):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_part) > int(right_part) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_part > right_part else -1

    if len(left) == len(right):
        return 0
    return 1 if len(left) > len(right) else -1


def is_adcp_version_at_least(version: str, minimum: str) -> bool:
    """Return whether ``version`` is at least ``minimum`` under SemVer ordering.

    Release-precision wire values (for example ``3.2-beta.6``) and full
    bundle versions (``3.2.0-beta.6``) compare equivalently. Stable releases
    sort after their prereleases, so ``3.2`` is newer than ``3.2-beta.6``.
    """
    version_core, version_prerelease = _semver_parts(version)
    minimum_core, minimum_prerelease = _semver_parts(minimum)
    if version_core != minimum_core:
        return version_core > minimum_core
    return _compare_prerelease(version_prerelease, minimum_prerelease) >= 0


def _read_packaged_version() -> str:
    """Return the ``ADCP_VERSION`` value packaged with the wheel."""
    from importlib.resources import files

    return (files("adcp") / "ADCP_VERSION").read_text().strip()


def get_supported_adcp_versions() -> tuple[str, ...]:
    """Release-precision versions to advertise in capabilities.

    ``COMPATIBLE_ADCP_VERSIONS`` keeps the stable release lines the SDK
    can speak. When the packaged spec is a prerelease for one of those
    lines, advertise the exact prerelease instead of the future stable alias
    so buyers can select the schema contract that actually ships with this
    wheel.
    """
    packaged = normalize_to_release_precision(_read_packaged_version())
    packaged_base = packaged.split("-", 1)[0]
    versions = [v for v in COMPATIBLE_ADCP_VERSIONS if v != packaged_base or "-" not in packaged]
    if packaged not in versions:
        versions.append(packaged)
    return tuple(versions)


def resolve_adcp_version(pin: str | None) -> str:
    """Validate and resolve a constructor-supplied ``adcp_version`` pin.

    - ``None`` → reads the packaged ``ADCP_VERSION`` file (SDK default).
    - Explicit same-major pin → accepted only when the normalized release
      is advertised by this SDK.
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

    normalized = normalize_to_release_precision(raw)
    supported = get_supported_adcp_versions()
    if pin is not None and normalized not in supported:
        raise ConfigurationError(
            f"adcp_version={raw!r} is not advertised by this SDK "
            f"(supported_versions={list(supported)}). Use one of those exact "
            "values, or omit adcp_version to use the packaged default."
        )

    return normalized
