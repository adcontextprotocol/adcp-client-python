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
* Release-precision prereleases from the wire — ``3.1-beta.1`` resolves
  to ``3.1.0-beta.1``. Per ``core/version-envelope.json`` the wire's
  ``adcp_version`` is release-precision only, so v3.1+ sellers emit the
  patchless form (``3.1-beta.1``, not ``3.1.0-beta.1``); the SDK injects
  ``.0`` to find the cache directory.

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
# ``MAJOR.MINOR-PRERELEASE`` — the wire-format prerelease shape (e.g.
# ``3.1-beta.1``). Per ``core/version-envelope.json`` this is the only
# valid prerelease shape on the wire; the patch component is dropped
# because patches aren't part of the wire contract. SDK normalizes to
# ``MAJOR.MINOR.0-PRERELEASE`` to find the cache directory.
_MAJOR_MINOR_PRERELEASE_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)-(?P<prerelease>[0-9A-Za-z.-]+)$"
)


def resolve_bundle_key(version: str) -> str:
    """Collapse a version string to its on-disk cache key.

    Accepts:

    * ``MAJOR.MINOR.PATCH`` — collapsed to ``MAJOR.MINOR``.
    * ``MAJOR.MINOR.PATCH-PRERELEASE`` — kept exact; prereleases ship with
      breaking changes vs. the matching stable, so each one is its own bucket.
    * ``MAJOR.MINOR`` — passed through as-is (already a bundle key; matches
      the wire-level ``adcp_version`` field's release-precision shape).
    * ``MAJOR.MINOR-PRERELEASE`` — release-precision prerelease wire shape
      (e.g. ``"3.1-beta.1"`` from a v3.1 seller's response envelope). The
      ``.0`` patch component is injected to match the cache directory
      (``schemas/cache/3.1.0-beta.1/``). Per ``core/version-envelope.json``
      this is the canonical wire form — adding the SDK's recognition is
      what makes validator routing actually find the 3.1 bundle when an
      agent emits ``adcp_version: "3.1-beta.1"``.

    Raises ``ValueError`` for anything else — adopters pin on real release
    identifiers, so a malformed version is a real bug.
    """
    stripped = version.strip()
    full = _FULL_SEMVER_RE.match(stripped)
    if full is not None:
        if full.group("prerelease"):
            return stripped
        return f"{full.group('major')}.{full.group('minor')}"
    mm_pre = _MAJOR_MINOR_PRERELEASE_RE.match(stripped)
    if mm_pre is not None:
        # Inject ``.0`` patch so the wire shape ``3.1-beta.1`` lines up
        # with the on-disk directory ``3.1.0-beta.1``.
        return f"{mm_pre.group('major')}.{mm_pre.group('minor')}.0-{mm_pre.group('prerelease')}"
    mm = _MAJOR_MINOR_RE.match(stripped)
    if mm is not None:
        return stripped
    raise ValueError(
        f"resolve_bundle_key: {version!r} is not a valid version "
        "(expected MAJOR.MINOR, MAJOR.MINOR.PATCH, "
        "MAJOR.MINOR-PRERELEASE, or MAJOR.MINOR.PATCH-PRERELEASE)"
    )
