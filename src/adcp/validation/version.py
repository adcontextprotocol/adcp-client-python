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
_SEMVER_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$"
)


def resolve_bundle_key(version: str) -> str:
    """Collapse a version string to its on-disk cache key.

    Raises ``ValueError`` for non-semver inputs — the schema fetch
    pipeline pins on real release identifiers, so a malformed version
    here is a real bug in the caller, not user input.
    """
    match = _SEMVER_RE.match(version.strip())
    if match is None:
        raise ValueError(
            f"resolve_bundle_key: {version!r} is not a valid semver "
            "(expected MAJOR.MINOR.PATCH[-PRERELEASE])"
        )
    if match.group("prerelease"):
        return version.strip()
    return f"{match.group('major')}.{match.group('minor')}"
