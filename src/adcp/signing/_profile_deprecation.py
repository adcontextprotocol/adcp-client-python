"""When to announce the RFC 9421 request-signing profile's 3.3 deprecation."""

from __future__ import annotations


def rfc9421_deprecation_active() -> bool:
    """Whether this SDK advertises AdCP 3.3 or later.

    AdCP 3.3 deprecates the RFC 9421 profile (it stays supported through 3.x).
    While the SDK's wire pin is below 3.3, warning on every legacy
    construction would only add noise for sellers who cannot yet migrate,
    so the warning is emitted once a 3.3 release is a compatible version.
    """
    from adcp._version import COMPATIBLE_ADCP_VERSIONS

    for version in COMPATIBLE_ADCP_VERSIONS:
        major, _, rest = version.partition(".")
        minor = rest.split(".", 1)[0].split("-", 1)[0]
        if major.isdigit() and minor.isdigit() and (int(major), int(minor)) >= (3, 3):
            return True
    return False
