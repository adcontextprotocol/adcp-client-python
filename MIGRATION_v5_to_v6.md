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
    return CreateMediaBuyErrorResponse(errors=[e.to_wire_error()])
```

The `e.to_wire_error()` helper builds the wire-correct
`UNSUPPORTED_FEATURE` `Error` with `details.rejected_value` +
`details.accepted_values` per the canonical rejection shape in
`error.json`. Override `field=` when the rejection isn't at the
default `manifest.format_kind` pointer.

**Recipe — seller-side v1 inbound lookup:**

When a v1-only buyer's `create_media_buy` arrives with a `format_id`
rather than a v2 `format_kind`, sellers walk the product's
`format_options[]` looking for the declaration that asserted that
v1 ref:

```python
from adcp.canonical_formats import find_declaration_by_v1_format_id

decl = find_declaration_by_v1_format_id(
    manifest.format_id,
    product.format_options,
)
if decl is None:
    return CreateMediaBuyErrorResponse(errors=[Error(
        code="UNSUPPORTED_FEATURE",
        message="v1 format_id not in product format_options[]",
        field="manifest.format_id",
    )])
# Use decl.format_kind + decl.params_as(...) from here.
```

**Recipe — recover typed canonical body from `params`:**

```python
from adcp.types import CanonicalFormatImage, CanonicalFormatKind

# decl.params is dict[str, Any] for cross-kind compatibility — narrow
# it via params_as(...) once you've discriminated on format_kind.
if decl.format_kind is CanonicalFormatKind.image:
    img = decl.params_as(CanonicalFormatImage)
    for size in img.sizes:
        ...  # typed: size.width, size.height
```

**Wire-shape enforcement landed in `ProductFormatDeclaration`:**

- `params` is now required (matches `required: ["format_kind", "params"]` on the schema).
- `canonical_formats_only=True` and `v1_format_ref[]` are rejected at
  construction when combined (the schema's `allOf.not` clause).
- Credential-shaped keys in `params` or model extras raise at
  construction. Same suffix list and rationale as the dispatcher's
  `ctx_metadata` gate (`credential`, `token`, `secret`, `api_key`,
  `apikey`, `password`, `bearer`).

**SDK-source advisory provenance:**

All advisories emitted by the projection layer carry `source="sdk"` and
`sdk_id="adcontextprotocol-adcp-python@<version>"`. The distribution-name
prefix is fixed (independent of `pyproject.toml`'s `[project].name`)
so wheel installs and dev installs emit the same attribution string —
the multi-hop `(code, field, sdk_id)` dedup contract in `core/error.json`
keys on this and would corrupt under drift. Adopters relying on a
particular `sdk_id` for multi-hop dedup should pin to a specific SDK
release rather than parsing the string.

### Canonical-formats part 2 (#741 second half)

Ships the v1 → v2 reverse projection, the bidirectional `pixel_tracker`
contract, the divergence narrowing check, and the upstream reference
fixtures + round-trip tests.

**New public-API helpers on `adcp.canonical_formats`:**

```python
from adcp.canonical_formats import (
    # v1 → v2 inbound projection
    project_v1_format_to_declaration,
    project_v1_catalog_to_v2,
    V1ToV2Projection,
    V1CatalogProjection,
    # pixel_tracker bidirectional
    downgrade_pixel_tracker,
    downgrade_pixel_trackers,
    upgrade_v1_tracker,
    upgrade_v1_trackers,
    PixelTrackerDowngrade,
    PixelTrackerUpgrade,
    PixelTrackerBatchResult,
    V1Tracker,
    # narrowing check
    check_narrows,
    narrowing_advisory,
)
```

**Recipe — read a v1 catalog and emit v2 declarations:**

```python
import json
from adcp.canonical_formats import project_v1_catalog_to_v2

v1_formats = json.loads(catalog_path.read_text())
result = project_v1_catalog_to_v2(v1_formats)
for decl in result.declarations:
    ...  # use the typed ProductFormatDeclaration
response.errors = (response.errors or []) + result.advisories
```

Resolution order per `registries/v1-canonical-mapping.json`:
1. v1 `canonical:` annotation set → use seller-declared kind.
2. registry `format_id_glob` match → use registry's canonical + params.
3. registry `structural` match → use registry's canonical, emit
   `FORMAT_DECLARATION_V1_AMBIGUOUS` (family-level guess).
4. no match → emit `FORMAT_PROJECTION_FAILED`, no declaration.

**Recipe — narrowing check before publishing:**

```python
from adcp.canonical_formats import narrowing_advisory

advisory = narrowing_advisory(
    declaration,
    v1_requirements=v1_format.requirements,
    v1_format_id=v1_format.format_id.id,
    field_path="format_options[0]",
)
if advisory is not None:
    response.errors.append(advisory)  # FORMAT_DECLARATION_DIVERGENT
```

`check_narrows(v2_params, v1_requirements)` returns the raw divergence
list when adopters want to drive their own error shape; the
`narrowing_advisory` helper wraps that in the wire-correct
`FORMAT_DECLARATION_DIVERGENT` `Error`.

**Recipe — bidirectional `pixel_tracker`:**

```python
from adcp.canonical_formats import (
    downgrade_pixel_tracker, upgrade_v1_tracker,
)

# v2 → v1 (talking to a 3.0.x seller):
v1 = downgrade_pixel_tracker(pixel_tracker_asset).v1
v1_asset = {"asset_type": "url", "url_type": "tracker_pixel",
            "asset_id": v1.asset_id, "url": v1.url}

# v1 → v2 (reading a v1 manifest as a 3.1 buyer):
result = upgrade_v1_tracker(asset_id="impression_tracker", url="...")
typed_pixel = result.pixel_tracker
# result.advisory is ALWAYS present — PIXEL_TRACKER_UPGRADE_INFERRED
```

Lossy combinations are listed in the
`adcp.canonical_formats.pixel_tracker` module docstring.

**Vendored reference fixtures** under `tests/fixtures/canonical/`:

* 14 v2 `Product` fixtures from `adcontextprotocol/adcp@main/static/
  examples/products/canonical/` — exercise the v2 → v1 path against
  real seller catalogs.
* 1 v1 reference catalog (`v1-reference-formats.json`, 50 entries
  with explicit `canonical:` annotations) from
  `adcontextprotocol/adcp@main/server/src/creative-agent/
  reference-formats.json` — exercises the v1 → v2 path.

Round-trip tests in `tests/test_canonical_formats_roundtrip.py` pin
the projection layer against these fixtures so an upstream-contract
drift (e.g., a dropped `canonical:` annotation, a renamed slot)
surfaces immediately in CI.


### Canonical-formats polish (post-#741)

Bundles the deferred follow-ups noted in #845's expert review.

* **`projection.py` renamed to `v2_to_v1.py`.** The module covering
  the v2 → v1 outbound projection is now symmetric with the half-2
  `v1_to_v2.py` (inbound). All imports continue to work via the
  package re-export — `from adcp.canonical_formats import
  project_product_to_v1`. Adopters who reached into the private path
  `from adcp.canonical_formats.projection import ...` must switch to
  `from adcp.canonical_formats.v2_to_v1 import ...`.
* **`Divergence` typed record.** `check_narrows` now returns
  `list[Divergence]` (a dataclass with `field` / `kind` / `cap` /
  `value`) instead of `list[dict[str, Any]]`. The wire-shape
  projection `Divergence.to_dict()` still emits the original key
  vocabulary (`v1_max` / `v1_min` / `v1_allowed` / `v1_value`) so
  buyer-side parsers reading advisory `details.divergences` aren't
  affected. Adopters who called `check_narrows` and indexed via
  `d["field"]` must switch to `d.field`.
* **Public fixture loader.** `adcp.canonical_formats.fixtures`
  exposes `load_reference_product(name)`,
  `load_v1_reference_catalog()`, and
  `REFERENCE_PRODUCT_NAMES` so adopter test suites can reuse the
  14 v2 + 50 v1 vendored fixtures without re-vendoring upstream.
* **`group_declarations_by_product` helper.** After running
  `project_v1_catalog_to_v2` over a flat v1 catalog, this helper
  buckets the resulting declarations into per-product
  `format_options[]` lists given a
  `{product_id: [v1_format_id, ...]}` mapping the adopter already
  has from their internal routing table.
* **`_versions_overlap` forward-compat.** Unknown DSL operator
  prefixes (`~>`, `^`, etc.) now log a WARNING and treat the
  constraint as non-matching, rather than raising — the registry
  may publish operators ahead of SDK support, and crashing a
  cached session is worse than missing one match.
