# Migration v5 → v6

> Beta line. Surfaces here MAY change between beta increments. Track this
> file as features land; sections will be added per-release rather than
> upfront.

## Why a major version

v6 carries the type-surface changes that come with AdCP 3.1 — most
notably the canonical-formats projection layer (#741), which alters
the `ProductFormatDeclaration` discriminator and adds the
`adcp.canonical_formats` public module. Adopters pinning `adcp~=5.7`
will keep getting the v5 surface; opt into the beta with
`pip install --pre adcp` or `pip install adcp==6.0.0b1`.

## Installing the beta

```bash
pip install --pre adcp
# or pin explicitly
pip install adcp==6.0.0b1
```

PyPI's default resolver excludes prereleases unless `--pre` is set, so
existing `pip install adcp` calls in production continue resolving to
5.7.x.

## What's NOT changing

- `ADCP_VERSION` is the upstream protocol version (currently
  `3.1.0-beta.3`) and is independent of the SDK package version. v6 of
  the SDK can run against multiple `ADCP_VERSION` values; the schema
  cache and generated types are pinned via that file, not via the
  package version.
- The CI schema-sync drift gate remains gated on stable upstream
  versions (`.github/workflows/ci.yml`).

## Per-release entries

(Add sections here as breaking changes land. Format: heading per
released beta increment, bullet list of breaking surfaces with
migration recipe.)
