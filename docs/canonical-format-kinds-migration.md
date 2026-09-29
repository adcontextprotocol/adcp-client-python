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

Readback annotations are now `CanonicalFormatKind` for `CreativeAsset` and
`Creative`, and `CanonicalFormatKind | None` for `CreativeManifest`. Remove
application branches that treat their validated kinds as arbitrary strings.

## Delivery readback

`DeliveryCreative.format_kind` and the rendered manifest kinds in the canonical
`GetCreativeDeliveryResponse` remain `CanonicalFormatKind | str | None`.
Both canonical and legacy delivery
responses preserve unknown future kinds, including rendered manifests, so a
new kind does not discard an entire delivery result. Known kinds still normalize
to enum members. This is tolerant SDK response parsing; it does not widen the
versioned wire schema's enum.

Delivery uses private manifest and variant types that are reachable through the
response and are not exported from `adcp.types`. They are separate from the
strict public input types. To reuse a served manifest as input, dump it and
validate it with `CreativeManifest.model_validate(served_manifest.model_dump())`.
An unknown kind fails this validation; choose a supported kind or upgrade the
SDK before submitting it. Passing the tolerant instance directly also cannot
bypass the input validator.
