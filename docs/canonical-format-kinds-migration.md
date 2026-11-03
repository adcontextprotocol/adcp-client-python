# Canonical creative format-kind validation

SDK 9 preserves `format_kind` as a string on creative and manifest models,
including values introduced after the SDK's bundled vocabulary. Known strings
also remain strings. Replace enum identity comparisons with string equality:

```python
if creative.format_kind == "image":
    handle_image(creative)
```

`CanonicalFormatKind` remains available as a vocabulary enum, and
`is_canonical_format_kind(value)` checks membership in the bundled vocabulary.
It returns a boolean; do not install that predicate directly as a Pydantic
`AfterValidator`, which must return the validated value.

## Opt into a closed application vocabulary

Use `CanonicalFormatKindStr` on application fields that must accept only the
bundled canonical kinds. It returns the original string and raises a validation
error for an unknown kind. It composes with nullable and list annotations:

```python
from pydantic import BaseModel, Field
from adcp.types import CanonicalFormatKindStr

class CreativeSelection(BaseModel):
    format_kind: CanonicalFormatKindStr
    fallback_kind: CanonicalFormatKindStr | None = None
    accepted_kinds: list[CanonicalFormatKindStr] = Field(default_factory=list)
```

For a deployment-specific vocabulary, `require_canonical_format_kind` builds a
validator and snapshots the supplied iterable so it can be reused safely:

```python
from typing import Annotated
from pydantic import AfterValidator
from adcp.types import require_canonical_format_kind

SellerKind = Annotated[
    str, AfterValidator(require_canonical_format_kind(("image", "video")))
]
```

These helpers are optional application validation. The SDK's creative models
continue to preserve unknown strings, and a versioned wire schema remains the
authority for protocol validation. A seller must also check a chosen kind
against the selected product's declared `format_options`; use
`validate_format_kind_in_options` at that application decision point.

Adopter-defined formats can use `custom` with a corresponding declaration's
`format_shape` and `format_schema`. Choose it when the creative satisfies that
contract, rather than changing an unknown value to `custom` without validation.

## Lenient legacy creative uploads

For AdCP 3.0/3.1 legacy creative uploads, set `validation_mode: "lenient"`
on `sync_creatives` when supported creatives should proceed even if another
creative's `format_id` cannot be projected. The dispatcher passes the supported
creatives to the seller and returns the unprojectable creatives as
`action: "failed"` with `UNSUPPORTED_FEATURE` errors naming their original
input fields. Results retain the submitted creative order. The default mode
is `strict`; strict and omitted modes continue to reject the entire request
before the handler runs.

Malformed envelopes and duplicate creative IDs remain request errors. The
dispatcher preserves `dry_run` and the original idempotency identity, including
rejected inputs. Deprecated `assignments` targeting rejected submissions are
refused and reported in those creatives' `assignment_errors`, so a stale stored
version cannot be assigned instead. `assignment_operations` requires strict
mode and cannot be partially processed.

A partially projected batch with `delete_missing: true` is refused before the
seller runs. The dispatcher cannot safely perform library reconciliation on
the seller's behalf, and removing failed rows would otherwise archive existing
creatives whose IDs the buyer did submit. Upload without `delete_missing`,
resolve the rejected formats, then reconcile with the complete submitted ID set.
Partially projected batches also refuse `creative_ids` filtering; submit the
selected creatives directly instead. If every upload is unprojectable, unrelated
valid assignments are refused before dispatch and must be retried separately.

`PlatformHandler` retains projection failures in inline responses and managed
`TaskHandoff` completion results before task persistence and webhook publication.
`WorkflowHandoff` is refused before workflow enqueue for these partial batches.
Custom `ADCPHandler` implementations with push configuration or overridden task
polling tools are refused before dispatch because the SDK cannot safely overlay
their external completion results. These checks conservatively identify custom
async surfaces; they do not make hand-rolled `submitted` responses supported.
Custom handlers using partial admission must finish inline, or use the managed
`PlatformHandler` task path.
