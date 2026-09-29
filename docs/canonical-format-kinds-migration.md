# Canonical creative format-kind validation

`CreativeAsset.format_kind`, `Creative.format_kind`, and
`CreativeManifest.format_kind` now reject strings outside `CanonicalFormatKind`.
This is a breaking change from releases through 8.0.0-rc.2, which accepted and
preserved arbitrary strings in these fields. It aligns their enum validation
with the pinned `core/canonical-format-kind.json` schema.

Recognized wire strings still normalize to enum members and serialize to their
original string values. The public type stubs and validation JSON Schema now
describe the same closed set. `CreativeAsset` and `Creative` still require a
non-null kind. `CreativeManifest` retains its optional `None` default for model
composition; a complete wire manifest must also satisfy the versioned schema's
identity and asset requirements.

## Updating callers

Use a recognized canonical kind when validating creative data:

```python
from adcp.types import CanonicalFormatKind, CreativeAsset

creative = CreativeAsset.model_validate(
    {
        "creative_id": "creative-1",
        "name": "Product image",
        "format_kind": "image",
        "assets": {},
    }
)
kind: CanonicalFormatKind = creative.format_kind
assert kind is CanonicalFormatKind.image
```

Validate stored values before upgrading workflows that read existing creatives.
Unknown values such as `"totally_bogus"` now raise Pydantic `ValidationError`
during construction, `model_validate`, and `model_validate_json`, including
nested creative and manifest input fields. Correct each value to the canonical kind
whose contract the creative satisfies. Applications receiving a kind introduced
by a newer protocol version need an SDK version that supports that kind.

Adopter-defined formats use the existing `custom` kind with a corresponding
format declaration's `format_shape` and `format_schema`. Use it when the creative
conforms to that custom contract, rather than as a fallback for unknown values.

The public input annotations are `CanonicalFormatKind` for `CreativeAsset` and
`Creative`, and `CanonicalFormatKind | None` for `CreativeManifest`. Remove
application branches that treat kinds validated by these types as arbitrary strings.

## Buyer manifest readback

The SDK preserves unknown `format_kind` strings when reading manifests returned
by another agent. Known kinds still normalize to enum members. This applies to
all manifest-bearing response paths:

| Response | Manifest path |
| --- | --- |
| Canonical and legacy creative delivery | `creatives[].variants[].manifest` |
| `LegacyPreviewCreativeResponse3` | `manifest` |
| `LegacyBuildCreativeResponse1` | `creative_manifest` |
| `LegacyBuildCreativeResponse3` | `creative_manifests[]` |
| `LegacyBuildCreativeResponse4` | `creatives[].variants[].creative_manifest` |
| Trusted-match router and provider responses | `offers[].creative_manifest` |

Completed async build and preview results follow the same rule, including the
webhook result wrapper. `DeliveryCreative.format_kind` also remains
`CanonicalFormatKind | str | None`. This is tolerant SDK response parsing; it
does not widen the versioned wire schema's enum.

Direct `Creative.format_kind` and `CreativeAsset.format_kind` fields remain
strict, including `ListCreativesResponse.creatives[].format_kind`. The tolerance
applies to response manifests, not to these directly embedded creative types.

Responses use private manifest views, with private enclosing variants and offers
where needed. These types are reachable through responses but are not exported
from `adcp.types`. The public and generated `CreativeManifest` types remain
strict inputs. To reuse a returned manifest as input, dump it and validate it
with `CreativeManifest.model_validate(returned_manifest.model_dump())`. An
unknown kind fails this validation; choose a supported kind or upgrade the SDK
before submitting it. Passing the tolerant instance directly also cannot bypass
the input validator.
