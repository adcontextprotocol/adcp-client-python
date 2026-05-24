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

### Canonical-formats public surface (#741)

The v2 catalog-side canonical-formats vocabulary is now reachable from
`adcp.types`, and a new `adcp.canonical_formats` module ships the v2 →
v1 projection layer + closed-set `format_options[]` validator.

**New public-API names on `adcp.types`:**

- Discriminator + projection: `CanonicalFormatKind`,
  `ProductFormatDeclaration`, `ProductFormatSellerPreference`,
  `CanonicalProjectionReference`, `CanonicalAssetSource`,
  `CanonicalSlotOverride`.
- 13 canonical format classes (one per `CanonicalFormatKind` value):
  `CanonicalFormatImage`, `CanonicalFormatHtml5Banner`,
  `CanonicalFormatDisplayTag`, `CanonicalFormatImageCarousel`,
  `CanonicalFormatHostedVideo`, `CanonicalFormatVastVideo`,
  `CanonicalFormatHostedAudio`, `CanonicalFormatDaastAudio`,
  `CanonicalFormatNativeInFeed`, `CanonicalFormatResponsiveCreative`,
  `CanonicalFormatAgentPlacement`, `CanonicalFormatSponsoredPlacement`
  (the last two have shorter names than the codegen output —
  `CanonicalFormatAgentPlacementAiSurfaceSponsoredPlacement` and
  `CanonicalFormatSponsoredPlacementRetailMediaCatalogDriven` are
  collapsed).
- Pixel tracker asset: `PixelTrackerAsset`, `PixelTrackerEvent`,
  `PixelTrackerMethod`.
- v1↔v2 registry types: `V1V2CanonicalFormatMappingRegistry`,
  `V1CanonicalMapping`, `V1CanonicalGlobPattern`,
  `V1CanonicalStructuralPattern`, `V1CanonicalStructural`,
  `V1CanonicalV2Projection`, `V1CanonicalDimensions`.

**`ProductFormatDeclaration` is hand-rolled, not codegen.**
`datamodel-code-generator` flattens the upstream schema's discriminated
`oneOf` and drops the `format_kind` / `params` fields entirely. The
public class lives at `adcp.types.canonical_decl.ProductFormatDeclaration`
and carries both discriminator + open `params` dict with `extra='allow'`.
The codegen output is preserved under `adcp.types.canonical_decl._GeneratedProductFormatDeclaration`
for code that needs the raw shared-properties view.

**New module `adcp.canonical_formats`:**

```python
from adcp.canonical_formats import (
    project_declaration_to_v1,
    project_product_to_v1,
    validate_format_kind_in_options,
    find_declaration_by_kind,
    load_default_registry,
    FormatKindNotInClosedSetError,
)
```

- `project_declaration_to_v1(declaration, *, field_path, product_id)`
  walks one `ProductFormatDeclaration` through the resolution order
  documented in `registries/v1-canonical-mapping.json` and returns a
  `V2ToV1Projection` carrying the projected `format_ids[]` plus any
  SDK-source advisories the resolution emitted
  (`FORMAT_DECLARATION_V1_LOSSY_MULTI_SIZE`,
  `FORMAT_DECLARATION_V1_AMBIGUOUS`).
- `project_product_to_v1(product, *, product_index)` fans out across
  the product's `format_options[]`, accumulating refs + advisories with
  product-indexed field paths for multi-product responses.
- `validate_format_kind_in_options(format_kind, format_options)` is
  the seller-side pre-call guard: raises
  `FormatKindNotInClosedSetError` when the kind is outside the
  product's published closed set. Sellers MUST pair this with
  `UNSUPPORTED_FEATURE` on the wire response.
- `find_declaration_by_kind(format_kind, format_options, *, capability_id)`
  looks up the matching declaration, disambiguating by `capability_id`
  when the closed set carries multiple declarations of the same kind.

**Recipe — emit `format_ids[]` from `format_options[]`:**

```python
from adcp.canonical_formats import project_product_to_v1

projection = project_product_to_v1(product, product_index=i)
response.products[i].format_ids = projection.format_ids
response.errors = (response.errors or []) + projection.advisories
```

**Recipe — reject out-of-set `format_kind` at `create_media_buy`:**

```python
from adcp.canonical_formats import (
    FormatKindNotInClosedSetError,
    validate_format_kind_in_options,
)

try:
    validate_format_kind_in_options(
        manifest.format_kind,
        product.format_options,
    )
except FormatKindNotInClosedSetError as e:
    return CreateMediaBuyErrorResponse(errors=[Error(
        code="UNSUPPORTED_FEATURE",
        message=str(e),
        details={
            "rejected_value": e.format_kind,
            "accepted_values": sorted(set(e.accepted_kinds)),
        },
    )])
```

**Not yet shipped (later beta increments):** v1 → v2 reverse projection,
`pixel_tracker` bidirectional contract, the 14 reference fixtures and
round-trip tests.
