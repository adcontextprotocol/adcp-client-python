"""Bundle-key resolution for per-version schema validation.

The schema cache is laid out as ``schemas/cache/{bundle_key}/`` so multiple
AdCP spec versions can coexist on disk and at runtime. ``bundle_key`` is
derived from a version string with these rules:

* Stable releases collapse to ``MAJOR.MINOR`` so ``3.0.0`` / ``3.0.7`` /
  ``3.0.42`` all resolve to ``3.0``. Adopter pins to patch granularity
  inside a minor pick up patch fixes without a cache reshuffle.
* Prereleases keep their full identifier — ``3.1.0-beta.1`` resolves to
  ``3.1.0-beta.1`` (not ``3.1``). Prereleases ship with breaking changes
  vs. the matching stable, so each one is its own cache bucket.

Mirrors ``resolveBundleKey()`` in the TypeScript SDK
(``src/lib/validation/schema-loader.ts``).
"""

from __future__ import annotations

import re

# ``MAJOR.MINOR.PATCH`` with an optional ``-PRERELEASE`` tail. Build
# metadata (``+SHA``) is intentionally not in the SDK contract — adopters
# pin to release identifiers.
_FULL_SEMVER_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$"
)
# Bare ``MAJOR.MINOR`` — already a bundle key. The wire's
# ``adcp_version`` field (3.1+) is emitted at this precision per the
# version-envelope spec, so callers passing it through verbatim land here.
_MAJOR_MINOR_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)$")


def resolve_bundle_key(version: str) -> str:
    """Collapse a version string to its on-disk cache key.

    Accepts:

    * ``MAJOR.MINOR.PATCH`` — collapsed to ``MAJOR.MINOR``.
    * ``MAJOR.MINOR.PATCH-PRERELEASE`` — kept exact; prereleases ship with
      breaking changes vs. the matching stable, so each one is its own bucket.
    * ``MAJOR.MINOR`` — passed through as-is (already a bundle key; matches
      the wire-level ``adcp_version`` field's release-precision shape).

    Raises ``ValueError`` for anything else — adopters pin on real release
    identifiers, so a malformed version is a real bug.
    """
    stripped = version.strip()
    full = _FULL_SEMVER_RE.match(stripped)
    if full is not None:
        if full.group("prerelease"):
            return stripped
        return f"{full.group('major')}.{full.group('minor')}"
    mm = _MAJOR_MINOR_RE.match(stripped)
    if mm is not None:
        return stripped
    raise ValueError(
        f"resolve_bundle_key: {version!r} is not a valid version "
        "(expected MAJOR.MINOR, MAJOR.MINOR.PATCH, or "
        "MAJOR.MINOR.PATCH-PRERELEASE)"
    )
