"""The one release predicate for the reporting-subscription wire surface.

The SDK serves AdCP 3.3.0-beta.3, and its generated models declare every
reporting-subscription field the engine emits: the fields settled after
3.3.0-beta.1 (adcontextprotocol/adcp#8585, #8588), ``status: "inactive"`` with
``deactivated_at`` (#8613), and the beta.3 additions. The SDK no longer accepts
the earlier 3.3 prereleases, so nothing is projected per field: a response
served at 3.3.0-beta.3 or later carries the whole subscription surface, and a
response served at an earlier release carries none of it.
"""

from __future__ import annotations

from adcp._version import is_adcp_version_at_least, resolve_adcp_version_alias

#: The first release whose schemas carry the reporting-subscription surface the
#: SDK serves.
SUBSCRIPTIONS_MIN_ADCP_VERSION = "3.3.0-beta.3"


def serves_subscriptions(adcp_version: str | None) -> bool:
    """Whether a response served at ``adcp_version`` carries reporting subscriptions.

    True for 3.3.0-beta.3 and later 3.3 prereleases, any 3.3 release candidate
    or final release, and anything later. An absent version is the SDK's pinned
    default (3.3.0-beta.3). An unparseable one is treated as older.
    """
    if adcp_version is None:
        return True
    try:
        return is_adcp_version_at_least(
            resolve_adcp_version_alias(adcp_version.strip()), SUBSCRIPTIONS_MIN_ADCP_VERSION
        )
    except ValueError:
        return False


__all__ = ["SUBSCRIPTIONS_MIN_ADCP_VERSION", "serves_subscriptions"]
