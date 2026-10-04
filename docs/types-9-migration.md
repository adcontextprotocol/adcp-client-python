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

Nothing is removed from `adcp` or `adcp.types`: every name importable before
is importable after, and the ten additions (`Issue`, `AdcpVersionEnvelope`,
seven `*Details` error models and `FormatReferenceStructuredObject` under
`adcp.types.legacy`) come with #1367. Identity changes on the public surface
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

A single-model root such as `CheckGovernanceRequest` keeps
`model_validate(...)` and can now be subclassed with `extra="forbid"`.

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
canonical names in `adcp.types` too: `_canonical_clone` carries every validator
a generated class declares on itself, so `adcp.CreateMediaBuyRequest` and the
generated class agree.

```python
CreateMediaBuyRequest.model_validate({**unconditional_fields})
# ValidationError: CreateMediaBuyRequest requires at least one of these field groups:
#   packages | total_budget+proposal_id | ...
```

Presence is what counts: an explicit `null` satisfies a group, a default the
caller never sent does not.

## Also in this batch (not breaking)

* `adcp.types.domains.<domain>[.<schema>]` and `adcp.types.error_details`
  (#1367) give every generated class a public path, and the public API
  snapshot records what each name resolves to.
* `scripts/generate_types.py --check` runs in CI, grades every post-generation
  fix against `scripts/post_generation_manifest.json`, and the generator's
  input order is total, so a regeneration is byte-identical on every
  filesystem (#1374).
* `canonical_creative.pyi` is derived from the runtime models (#1366): every
  field the canonical models carry is declared, and the synthesized
  constructors demand exactly the fields the runtime demands. A program that
  type-checked against the old stub while constructing `CreateMediaBuyRequest`
  without `brand`, `start_time`, `end_time` or `idempotency_key` now fails
  mypy, as it always failed at runtime. Enum-typed parameters keep accepting
  the wire string.
* A format reference's `agent_url` is carried as the wire string, validated
  as a URL (#1384). `ref.agent_url` is a `str`, not an `AnyUrl`, so
  `migrated_…` option IDs derived from a model match those derived from the
  wire mapping.
* `adcp.__all__` and `adcp.types.__all__` name each export once (#1380).
