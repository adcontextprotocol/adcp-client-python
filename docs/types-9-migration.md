# Generated types in 9.0: migration guide

Release 9.0 lands a batch of code-generator changes that make `adcp.types`
agree with the AdCP JSON Schemas it is generated from. Each one is a breaking
change on its own; this page collects them so a migration can be done in one
pass. The pull requests are linked for the full rationale and measurements.

| Change | PR | What breaks |
| --- | --- | --- |
| Scalar schema roots are plain `str`/`int`/`float` subclasses | #1286 | `issubclass(X, RootModel)`, `X.model_validate`/`model_dump`/`model_json_schema` on 224 scalar types |
| A composing schema root is emitted as what it composes | #1353 | `.root` on 114 union/single-model roots; `TypeName.model_validate` on a union root |
| Structural pointer refs resolve to the type they select | #1371 | 47 per-position `RootModel` wrapper names under `adcp.types._generated` |
| Generated models validate `boolean`/`integer`/`number` strictly | #1375 | Payloads that relied on `"yes"`, `"1"`, `1` coercion |
| Root-level `anyOf`/`oneOf` required groups are enforced | #1368 | Documents that omit every required group of 42 request/response models, on generated and canonical names alike |
| Consumer `format_kind` fields preserve open strings | [adcp#7929](https://github.com/adcontextprotocol/adcp/issues/7929) | `x.format_kind is CanonicalFormatKind.y` identity comparisons; `ProductFormatDeclaration` still refuses a kind outside its schema's closed set |

Nothing is removed from `adcp` or `adcp.types`: every name importable before
is importable after. The additions are `Issue`, `AdcpVersionEnvelope`, seven
`*Details` error models and `FormatReferenceStructuredObject` under
`adcp.types.legacy` with #1367, plus `is_canonical_format_kind` with the
`format_kind` override below. Identity changes on the public surface
are of exactly the kinds the table names and are pinned by
`tests/fixtures/public_api_snapshot.json`.

## 1. Scalar roots are the scalar (#1286)

A schema whose root is a scalar — `core/property-tag.json` is
`{"type": "string", "pattern": "^[a-z0-9_]+$"}` — generates a `str`, `int` or
`float` subclass instead of `RootModel[str]`.

```python
# before
tag = PropertyTag(root="sports")
code = country.root
PropertyTag.model_validate("sports")

# after
tag = PropertyTag("sports")          # tag == "sports", hash(tag) == hash("sports")
code = country                       # it already is a str
TypeAdapter(PropertyTag).validate_python("sports")
```

`.root` and `X(root=...)` keep working behind a `DeprecationWarning`.
`issubclass(X, RootModel)` is `False` and the `model_*` classmethods are gone,
because the type is no longer a Pydantic model: use `TypeAdapter(X)`.

Integer and number roots validate the way their fields do under #1375:
strictly, with a float carrying no fractional part narrowed to `int`.

## 2. Composing roots are what they compose (#1353)

A root that is a union of models becomes an `Annotated[A | B, Field(...)]`
alias carrying the schema's discriminator; a root that is a single model
becomes a subclass of that model.

```python
# before
event = WholesaleFeedEvent.model_validate(payload)
inner = request.start_time.root

# after
event = TypeAdapter(WholesaleFeedEvent).validate_python(payload)   # honours the discriminator
inner = request.start_time
```

For repeated union validation, use the SDK's cached helper. It returns the
selected model directly and preserves discriminator validation:

```python
from adcp.types import VendorPricingOption, VendorPricingOptionUnion, validate_union

option = validate_union(VendorPricingOption, {
    "pricing_option_id": "price-1", "model": "cpm", "cpm": 2.0, "currency": "USD",
})
assert VendorPricingOption is VendorPricingOptionUnion
```

`validate_union` caches adapters for reusable hashable aliases, with a bounded
cache. Aliases containing unhashable annotation metadata remain supported via
an uncached adapter. Applications can also construct a `TypeAdapter` once and
reuse it. The additional `VendorPricingOption` spelling preserves the existing
`VendorPricingOptionUnion` name.

### List roots still use `.root`

Pure list roots retain their `RootModel[list[...]]` contract. Construct or
validate the wrapper and iterate its `.root`; it is not a list itself:

```python
from adcp.types.domains.core.acceptance_policy_profile_ids import AcceptancePolicyProfileIds

profiles = AcceptancePolicyProfileIds.model_validate(["policy-a"])
for profile in profiles.root:
    process_profile(profile)
```

The bundled generated surface currently declares 33 pure list roots. Domain
module paths distinguish repeated names; numbered generated names can change
when schemas are regenerated.

| Class | Public module |
| --- | --- |
| `LocalizedScalar1` | `adcp.types.domains.brand_discovery` |
| `LocalizedStringList1` | `adcp.types.domains.brand_discovery` |
| `ColorValue2` | `adcp.types.domains.brand_discovery` |
| `Tagline` | `adcp.types.domains.brand_discovery` |
| `Agents` | `adcp.types.domains.brand_discovery` |
| `AcceptancePolicyProfileIds` | `adcp.types.domains.core.acceptance_policy_profile_ids` |
| `NonblockingImpacts` | `adcp.types.domains.core.account_identity_change_preview` |
| `BlockedImpacts` | `adcp.types.domains.core.account_identity_change_preview` |
| `Value` | `adcp.types.domains.core.audience_characteristic` |
| `Assets` | `adcp.types.domains.core.creative_asset` |
| `CreativeAssets1` | `adcp.types.domains.core.creative_assets` |
| `Assets` | `adcp.types.domains.core.creative_localization` |
| `ResolvedAssets1` | `adcp.types.domains.core.creative_localization_readback` |
| `Assets` | `adcp.types.domains.core.creative_manifest` |
| `DaastVersions` | `adcp.types.domains.core.daast_tracker_constraints` |
| `ForecastPointDimensions` | `adcp.types.domains.core.forecast_point_dimensions` |
| `StringArray` | `adcp.types.domains.core.registry_event` |
| `ChangedFields` | `adcp.types.domains.core.registry_event` |
| `Countries` | `adcp.types.domains.core.registry_event` |
| `ReportingVerificationProfileSet` | `adcp.types.domains.core.reporting_verification_profile_set` |
| `IanaTimezones` | `adcp.types.domains.core.targeting_overlay_support` |
| `VastVersions` | `adcp.types.domains.core.vast_tracker_constraints` |
| `Assets` | `adcp.types.domains.creative.list_creatives_response` |
| `EnvelopeField1` | `adcp.types.domains.error_details.requote_required` |
| `BoundedValueLevel31` | `adcp.types.domains.governance.reported_outcome_error` |
| `BoundedValueLevel21` | `adcp.types.domains.governance.reported_outcome_error` |
| `BoundedValue1` | `adcp.types.domains.governance.reported_outcome_error` |
| `StatusFilter` | `adcp.types.domains.media_buy.get_media_buy_delivery_request` |
| `StatusFilter` | `adcp.types.domains.media_buy.get_media_buys_request` |
| `ProductResponseFields` | `adcp.types.domains.media_buy.product_fields` |
| `ProductRefinementRequests` | `adcp.types.domains.media_buy.product_refinement` |
| `TargetingKvs` | `adcp.types.domains.trusted_match.context_match_response` |
| `TargetingKvs` | `adcp.types.domains.trusted_match.provider_context_match_response` |

A single-model root such as `CheckGovernanceRequest` keeps
`model_validate(...)` and can now be subclassed with `extra="forbid"`.

### Product declarations are graded against their root schema

`ProductFormatDeclaration` is a `Format` subclass that enforces the root rules
of `core/product-format-declaration.json`: the six cross-field `allOf` clauses
and the sixteen-branch `format_kind`/`params` `oneOf`, including each branch's
own schema. It is no longer an alias for the open `Format` model, which
enforces none of them.

```python
from adcp.types import ProductFormatDeclaration

declaration = ProductFormatDeclaration(
    format_kind="image", params={"width": 300, "height": 250}
)
```

Because it is a class, `ProductFormatDeclaration(...)`,
`ProductFormatDeclaration.model_validate(...)` and
`isinstance(x, ProductFormatDeclaration)` all work, and `validate_union` and
`TypeAdapter` return it. Because it is a `Format`, every projection helper in
`adcp.canonical_formats` accepts one, and `legacy_format_refs` and `params_as`
are reachable on it.

`params` is the open bag `Format` declares; read it as a typed model with
`declaration.params_as(CanonicalFormatImage)`. Use `Format(...)` for open
consumer parsing of kinds this SDK's pin does not know —
`ProductFormatDeclaration` refuses a `format_kind` outside the schema's closed
set, which is the producer-side rule. `LegacyProductFormatDeclaration` names the
raw generated branch union for code that wants per-branch typed parameters.

#### Validating a declaration and publishing it as a `Format`

`Product.format_options` is `list[Format]`, so a seller converting stored
declarations into products validates each option strictly and then publishes the
validated object itself. There is no conversion step: a
`ProductFormatDeclaration` *is* a `Format`.

```python
from adcp.types import Product, ProductFormatDeclaration

options = [ProductFormatDeclaration.model_validate(raw) for raw in stored_options]
product = Product(product_id="p1", format_options=options, ...)
```

The published element keeps its `ProductFormatDeclaration` type, and the wire
document carries only the fields the stored option set — no branch defaults are
added, under `model_dump()`, `exclude_unset=True` or `exclude_none=True` alike:

```python
product.model_dump(mode="json")["format_options"]
# [{'format_kind': 'image', 'params': {'width': 300, 'height': 250}}]
```

Round-tripping holds: `ProductFormatDeclaration.model_validate(...)` on a
published element re-grades it against the root schema.

One field does not survive, by design. `v1_format_ref` is legacy identity, which
every canonical boundary model strips from its output — `Format` and
`ProductFormatDeclaration` both capture it on input and expose it as
`declaration.legacy_format_refs`, and neither serializes it. Project it
explicitly with `adcp.canonical_formats.project_declaration_to_v1(declaration)`
when a legacy peer needs `format_ids`.

## 3. Pointer refs resolve to the selected type (#1371)

A `$ref` into another schema's `properties`/`items` no longer mints a
single-arm `RootModel` wrapper per referencing site. Request-side overlays
read like response-side ones:

```python
# before: TargetingOverlayInput.geo_countries was GeoCountries (a wrapper); len() raised
# after
for country in overlay.geo_countries:
    code: str = country
```

The removed names were reachable only under `adcp.types._generated`, which is
documented as internal; the per-name replacement table is in #1371. A pointer
to an object or enum property still resolves to the one shared class
(`targeting.AgeRestriction`, `ProductResponseField`).

## 4. Strict scalars (#1375)

`type: boolean`, `integer` and `number` fields refuse what the bundled JSON
Schema refuses:

```python
ListAccountsRequest.model_validate({"sandbox": "yes"})   # before: sandbox=True; after: ValidationError
Budget.model_validate({"amount": "100"})                 # ValidationError; send 100 or 100.0
Count.model_validate({"value": 1.0})                     # still accepted, narrowed to 1
```

The bundled schema validator additionally checks `format: uri` and
`format: hostname`, so a malformed URL in a response fails response validation.

## 5. Root required groups (#1368)

42 models enforce the root-level `anyOf`/`oneOf` their schema declares.
`CreateMediaBuyRequest` needs packages, a budget, or a proposal; a document
with none raises `ValidationError` naming the groups. The rule reaches the
canonical names in `adcp.types` too: a canonical model is a subclass of the
generated class, so it inherits every validator that class declares and
`adcp.CreateMediaBuyRequest` and the generated class agree.

```python
CreateMediaBuyRequest.model_validate({**unconditional_fields})
# ValidationError: CreateMediaBuyRequest requires at least one of these field groups:
#   packages | total_budget+proposal_id | ...
```

Presence is what counts: an explicit `null` satisfies a group, a default the
caller never sent does not.

## 6. `format_kind` is a `str`, not an enum — a deliberate override (#7929)

`core/canonical-format-kind.json` declares a **closed** 16-member `enum` and,
in the same file, states as normative:

> Consumer SDKs MUST treat this enum as **open** at parse time: an unknown
> `format_kind` value MUST be retained as-is on the in-memory object (not
> silently dropped or rewritten to `"custom"`) and MUST NOT cause the
> surrounding payload to fail validation. ... The producer-side enum stays
> closed ...; the consumer-side enum stays open for forward compatibility.

The schema knows the rule is directional and then encodes it as one closed
enum, which cannot carry that. A generated model that reproduces an incoherent
schema faithfully does not inherit correctness from it — it propagates the
incoherence into every consumer. So 9.0 does not reproduce it.

**What changed.** Every reference to that schema generates a bare `str`:

```python
# before
CreativeManifest.model_fields["format_kind"].annotation  # CanonicalFormatKind | None
# after
CreativeManifest.model_fields["format_kind"].annotation  # str | None
```

`adcp.types.CanonicalFormatKind` is unchanged and still has its sixteen
members — it is the vocabulary, and nothing was removed from the public
surface. What changed is that it no longer types a field, so a value read off
a model is the string the seller sent rather than an enum member:

```python
manifest.format_kind == CanonicalFormatKind.image   # True, as before
manifest.format_kind is CanonicalFormatKind.image   # now False
```

If you compared with `is`, compare with `==`. `CanonicalFormatKind` is a
`StrEnum`, so `==` holds against the member and against the plain string.

**Consumer format-kind fields preserve unknown strings.** There is one type,
field and behaviour for these open fields: `format_kind: str`, retained as sent, on
`CreativeManifest` and `CreativeAsset` as much as on `Creative` and
`DeliveryCreative`. If you were relying on a creative request model raising
`ValidationError` for a kind outside the sixteen, it no longer does.

That is deliberate. A seller supports some set of format kinds, and that set
is the seller's — not this library's and not the pinned enum's. It can be
larger than the sixteen (a kind promoted in a spec newer than your pin) or
smaller (four of them). A pinned SDK cannot tell "a kind the seller invented"
from "a kind defined after my pin", so refusing the second to prevent the
first would make the SDK's version a ceiling on what the protocol permits.
"I accept your request and then tell you I cannot process this creative" is a
seller's answer, not a type error. The producer-side `MUST NOT mint ad-hoc
values` is a seller's obligation, and this library gives it the vocabulary and
the helper to meet it.

**Checking the vocabulary yourself** is therefore the sanctioned way to be
strict, and the SDK never does it for you:

```python
from adcp.types import CanonicalFormatKind, is_canonical_format_kind

if not is_canonical_format_kind(creative.format_kind):
    route_as_declared_but_unsupported(creative)

# the vocabulary is a parameter, because only you know which version your
# counterpart speaks, and your support list may be larger or smaller
if not is_canonical_format_kind(manifest.format_kind, MY_SUPPORTED_KINDS):
    reject_with_unsupported_format(manifest)
```

For application models that should reject unknown kinds during construction,
use the opt-in `CanonicalFormatKindStr` annotation or an
`AfterValidator(require_canonical_format_kind(vocabulary))`. Nullable and list
annotations compose normally. See [format-kind validation](canonical-format-kinds-migration.md).
`ProductFormatDeclaration`, described above, is the producer-side exception:
it refuses a `format_kind` outside the closed set its root schema declares,
and the refusal carries the `oneOf` keyword. Use open `Format` when consuming
a declaration for a kind this pin does not know.

**What this replaced.** Five pieces of scaffolding existed only to reconcile
the closed enum with the open requirement, and all five are gone: the
`_OpenCanonicalFormatKind` alias in `adcp.types.canonical_creative`, a second
one in `adcp.types._forward_compat`, the `preserve_open_delivery_format_kind`
post-generation fix that hand-patched one generated field,
`_revalidate_subclass_instances_of_strict_base`, which flipped the wire bases
to `revalidate_instances="subclass-instances"` so a subclass instance could
not pass as validated output, and `_StrictFormatKind`, the per-direction base
that refused a non-canonical kind on a request. With one open type everywhere,
none of them has anything to do.

**The cost, stated.** The generated models no longer agree with the bundled
schema for this one field, and #1375 established that they should. That is
declared as a single named entry with its reason in
`tests/conformance/_schema_parity.py` —
`format_kind_is_an_open_vocabulary_by_decision` — and the parity rule itself is
not relaxed: the entry is deleted automatically by
`test_every_declared_divergence_still_occurs` once it stops matching.

The upstream ask is
[adcontextprotocol/adcp#7929](https://github.com/adcontextprotocol/adcp/issues/7929):
govern `format_kind` with a versioned registry, the way its siblings
`format_shape` and `asset_group_id` already are. If it lands, the override is
deleted and the generated models go back to agreeing with their schema.

## Also in this batch (not breaking)

* `adcp.types.domains.<domain>[.<schema>]` and `adcp.types.error_details`
  (#1367) give every generated class a public path, and the public API
  snapshot records what each name resolves to.

  The generator writes there directly now, so the private
  `adcp.types.generated_poc` tree is gone. Almost every module stem moved
  unchanged, which makes the migration a prefix rename:

  ```python
  -from adcp.types.generated_poc.media_buy.package_request import PackageRequest
  +from adcp.types.domains.media_buy.package_request import PackageRequest
  ```

  `adcp migrate v3-to-v4 --apply` rewrites those lines, and `--auto-apply`
  additionally imports a name from `adcp.types` when every name on the line is
  bound there. The old path also still
  resolves through the whole 9.x line, emitting a `DeprecationWarning` that
  names the new one — #1360 measured 110 such imports in a single production
  seller, and 9.0 does not break all of them at once. **It is removed in
  v10.** What the old path returns is the *same module object*, so
  `adcp.types.generated_poc.core.format_id.FormatReferenceStructuredObject is
  adcp.types.domains.core.format_id.FormatReferenceStructuredObject` — an
  `isinstance` check cannot start failing because a class was reached by its
  old name. Prefer the flat `adcp.types` surface where the name you need is
  bound there, as `docs/type-surface.md` describes.

  **One stem split in two, and it is the one exception to the prefix rename.**
  `brand.json` shares its basename with the `brand/*.json` task schemas, so the
  old `generated_poc.brand` was that discovery schema — `Brand`,
  `BrandDiscovery*`, `LocalizedName` and 137 more — while the new
  `domains.brand` is the generated domain aggregator over
  `domains/brand/<schema>.py`. The discovery schema's classes are at
  `adcp.types.domains.brand_discovery`:

  ```python
  -from adcp.types.generated_poc.brand import Brand, LocalizedName
  +from adcp.types.domains.brand_discovery import Brand, LocalizedName
  ```

  The codemod resolves this split per imported name — a line that mixes the
  two halves becomes two import statements — and flags a bare
  `adcp.types.generated_poc.brand` module reference it cannot place.
  The deprecated name keeps serving both halves for the 9.x line, so an
  unmigrated `from adcp.types.generated_poc.brand import Brand` still works and
  still returns the canonical class. It is the one deprecated name that is a
  compatibility module rather than the canonical module itself, so `is` against
  `adcp.types.domains.brand` is False for it where every other deprecated name
  compares True. Class identity is unaffected, which is what `isinstance`
  reads.
* `scripts/generate_types.py --check` runs in CI, grades every post-generation
  fix against `scripts/post_generation_manifest.json`, and the generator's
  input order is total, so a regeneration is byte-identical on every
  filesystem (#1374).
* `canonical_creative.pyi` is gone (#1366). Every canonical model is now a real
  subclass of the generated wire model it refines, so there is nothing left to
  declare by hand: mypy reads `canonical_creative.py` and sees the actual
  inherited fields. The stub is not regenerated — it was deleted, because the
  thing it was transcribing is ordinary source. A program that type-checked
  against the old stub while constructing `CreateMediaBuyRequest` without
  `brand`, `start_time`, `end_time` or `idempotency_key` now fails mypy, as it
  always failed at runtime.

  One static-typing consequence comes with that. The runtime still coerces the
  wire string for an enum-typed parameter — `PackageRequest(product_id="p1",
  pricing_option_id="po1", pacing="even")` returns `Pacing.even` — but mypy and
  pyright now read the generated `Pacing | None` and reject the `str`. An
  adopter constructing a canonical model directly passes the enum member
  (`pacing=Pacing.even`) or goes through `model_validate`, which takes the wire
  document unchanged. This is not a regression against the hand-written stub:
  that stub did not declare `pacing` at all, so the same call was refused there
  too, as an unexpected keyword rather than a wrong type.

  The legacy-identity fields the canonical models remove — `format_id`,
  `format_ids`, `format_ids_pending`, `format_ids_to_provide` — are declared in
  a `TYPE_CHECKING` block with `init=False`, so a type checker refuses the
  keyword the runtime refuses instead of offering a constructor argument that
  raises `ValidationError`. Read them and you get `None`.
* The canonical models' overriding list fields — `GetProductsResponse.products`,
  `CreateMediaBuyResponse1.packages`, `ListCreativesResponse.creatives`,
  `Product.format_options` and the rest — are declared with their precise
  element types (#1416). They read as `list[Product] | None` and so on under
  mypy with or without `adcp.types.mypy_plugin`, and under pyright and
  Pylance, where the earlier `SchemaVariant[...]` spelling was an error.
* A format reference's `agent_url` is carried as the wire string, validated
  as a URL (#1384). `ref.agent_url` is a `str`, not an `AnyUrl`, so
  `migrated_…` option IDs derived from a model match those derived from the
  wire mapping.
* `adcp.__all__` and `adcp.types.__all__` name each export once (#1380).
