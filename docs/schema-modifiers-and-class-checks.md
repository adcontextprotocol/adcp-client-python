# Schema modifiers and the class-level checks they become

A JSON Schema root says more than which properties it carries. It can say *one of these
combinations* (`anyOf`, `oneOf`), *if this field then that one* (`if`/`then`), *never these
two together* (`not`), or *this field only alongside that one* (`dependencies`). A Python
class is a flat field set with per-field requiredness, so the generator turns each of those
into a check.

This document tells you, for a given schema construct, what the emitted check does at
validation time: what a refusal reports, which branch's errors you see, how a conditional
binds, and what nothing checks at all.

[How the type tree is generated](design/type-generation.md) owns the other half — the
pipeline, the pre-generation transforms, the proof that decides whether a root splits into
branch classes or keeps a compiled check, the class populations, and the naming. Read that
one for how a check comes to exist, and this one for what it does once it exists. Where a
number belongs to the generator rather than to runtime behavior, this document points there
instead of repeating it.

Measurements here come from `integrate/8qela` at `1c1e49b61`, under pydantic 2.13.5 — a
version this package's own pin admits (`pydantic>=2.13.0,<2.14`). Pydantic decides the union
label table below, so re-measure that table after a pydantic bump. Counts over generated
modules move with every schema refresh; take each as "last measured at" rather than current,
and re-derive with the command named beside it.

Two sections describe behavior a landed fix changed, and each names the bead so you can tell
which side of it your build sits on. Where a fix is still in review, this document describes
the behavior at `1c1e49b61` and names the branch rather than predicting the outcome.

## How a document reaches a branch

A split root's public name carries its branches' core schema, so validating that name
returns the branch the document satisfies. `BranchDispatchMixin`
(`src/adcp/types/_branch_dispatch.py`) installs it, and `_branch_models` and
`_branch_combinator` on the generated base say which branches and which keyword.

```python
FrequencyCapImpressionConstraints.model_validate({"allowed_values": [3]})
# -> FrequencyCapImpressionConstraintsAllowedValues
```

The hook asks one question: does this class DECLARE `_branch_models`
(`"_branch_models" in cls.__dict__`)? A branch inherits the method, so a branch gets its own
shape back rather than the union, and so does any other subclass.

### A subclass keeps the root's constraint and loses the dispatch

This is the asymmetry to internalize, because the two halves go opposite ways and a reader
usually guesses that they travel together. Declare a subclass of a split root and you get:

| What the subclass inherits | Outcome |
|---|---|
| The compiled root check (`_require_schema_required_group`) | Kept — pydantic inherits a model validator, so the root's conjuncts still refuse |
| The branches' core schema | Lost — the hook returns the subclass's own flat shape, so no branch dispatch |
| The union's wrapping validator, which supplies the combinator keyword | Lost with the schema |

Measured on a bare subclass of `FrequencyCapImpressionConstraints`:

```python
class Refined(FrequencyCapImpressionConstraints): ...

TypeAdapter(FrequencyCapImpressionConstraints).validate_python({"allowed_values": [3]})
# -> FrequencyCapImpressionConstraintsAllowedValues     (dispatched)
TypeAdapter(Refined).validate_python({"allowed_values": [3]})
# -> Refined                                            (not dispatched)

FrequencyCapImpressionConstraints.model_json_schema().keys()   # ['$defs', 'anyOf']
Refined.model_json_schema().keys()   # ['$defs', 'additionalProperties', 'properties', ...]

Refined.model_validate({"minimum": 1})
# ValidationError, type 'oneOf': the inherited compiled check still refuses
```

Two consequences you meet in practice:

- The public canonical creative types are such subclasses. `adcp.types.CreativeAsset` and
  `adcp.types.CreativeManifest` declare no `_branch_models`, so neither dispatches, and both
  render a flat JSON Schema (salesagent-8qela.108).
- A subclass reports a *cleaner* refusal than the root for the same document, because it runs
  the compiled check directly instead of going through the union. The root answers `not` for
  `{"minimum": 1}`; the subclass answers `oneOf`. The next section explains why.

A branch class also inherits the method and skips dispatch, and there that is the design
rather than a gap: a branch has to get its own shape back. Of the 29 split types
`adcp.types` exports, 12 declare the method and dispatch, and 17 inherit it. Fifteen of
those 17 are branch classes, so the two that are neither a branch nor a dispatching root are
`CreativeAsset` and `CreativeManifest`.

**What the lost dispatch costs you is the returned class and one refusal keyword, not
strictness.** Measured on `CreativeManifest` against the generated root, with every required
field supplied so only the combinator can fail:

| Document | Generated root, dispatching | Public refinement, inheriting |
|---|---|---|
| Neither `format_id` nor `format_kind` | Refuses, `oneOf` | Refuses, `oneOf` |
| Both keys | Refuses, `not` | Refuses, `value_error` |
| `format_kind` only | Accepts as `CreativeManifestCanonicalFormatManifest` | Accepts as `CreativeManifest` |

So there is no permissiveness gap to work around: the inheritor refuses what the branches
refuse and accepts what they accept, because the inherited compiled check does that work.
Two things do change. You lose the branch type on the way out, since the value validates as
the subclass rather than narrowing to an arm. And on a document that violates a branch's
forbid, the keyword degrades from `not` to `value_error`, which is not in the JSON Schema
vocabulary that `core/error.json` requires — the same class of defect as
salesagent-8qela.94. Annotate the generated root, or a branch, wherever either matters to
you.

## What a refusal reports

`_conditions.name_the_combinator` wraps the union and decides what you see. It groups the
union's errors by `loc[0]`, then classifies each group.

A union's `loc` is not a document path. Pydantic prefixes every error with the label of the
choice that produced it, such as
`function-after[_require_schema_required_group(), AudienceCharacteristicValue]`. That label
names a class in this package rather than a key you sent, so `_in_the_document` drops it
positionally. Dropping it is what lets `issues[].pointer` resolve against your document
(salesagent-8qela.22, salesagent-8qela.28).

The outcomes, in the order the wrapper decides them:

1. **One group holds no branch-requiredness error.** That group's errors are reported, at
   document paths. The wrapper treats this group as the branch you meant.
2. **Every group holds a branch-requiredness error.** One `PydanticCustomError` is raised
   whose type is the root's combinator keyword, with the message `requires at least one of
   these field groups: <title> | <title>` and the absent field names in the error context.
3. **No group and no absent names.** The original error is re-raised unchanged.

A branch-requiredness error is narrow: `_is_branch_requiredness` is `type == "missing"` and
`len(loc) == 2`, which is a required field at the branch's own root. A `missing` deeper than
that says the branch was reached and the document is wrong inside it.

An empty document takes path 2 on every split root. All 19 name their own combinator for
`{}`.

### A sibling branch's forbid displaces the real error

Path 1's test is weaker than it reads, and the result misleads. A branch that FORBIDS a key
the document carries refuses in a `mode="before"` validator, which raises
`PydanticCustomError("not", ...)` as a whole-document error. That error is not `missing`, so
its group holds no branch-requiredness error, so path 1 reports it — even though that branch
is the one the document plainly did not mean.

`core/audience-characteristic.json` is `anyOf(value | range)` with a root-required
`dimension`. Send one key and omit `dimension`:

```python
AudienceCharacteristic.model_validate({"value": 1})
# ValidationError, type 'not': "value is not accepted by this request shape"
```

The answer is the reverse of the truth. `value` is exactly what the Value branch requires,
and the actual fault — the missing `dimension` — never reaches you. Asked directly, the two
branches say:

- `AudienceCharacteristicValue`: `missing ('dimension',)`, which is the real fault.
- `AudienceCharacteristicRange`: `not ()`, "value is not accepted", which is what you get.

`_refuses_the_shape` already encodes the correct classification, and its own docstring calls
such a branch never reached. The wrapper consults it only on the degenerate-label path
described in the next section, so the ordinary path uses the weaker test. Ten of the 19
split roots have branches that forbid keys, so all ten can answer this way. Filed as
salesagent-klblw, with the milder case on the same cause:
`FrequencyCapImpressionConstraints.model_validate({"minimum": 1})` answers "minimum is not
accepted by this request shape", when `minimum` is accepted in the `minimum+maximum` group
and the informative answer is that `maximum` is missing.

Until that is fixed, read a `not` refusal from a split root as "some branch forbids this
key", never as "this key is unacceptable to the schema".

A fix is in review on `fix/branch-election-filter`, which types the election filter on the
error keyword so a forbidding branch stops winning the refusal. The example above then
answers `anyOf` instead. Check whether your build carries that change before you rely on
either keyword.

### The label-collapse hazard, for anyone extending the generator

Grouping by `loc[0]` assumes pydantic gives each choice a distinct label, which nothing
guarantees. Measured on two branches under pydantic 2.13.5:

| Branch schema | Labels | Verdict |
|---|---|---|
| Plain | `['First', 'Second']` | Distinct |
| After-validator only | `['function-after[f(), First]', 'function-after[f(), Second]']` | Distinct |
| Wrap only | `['function-wrap[f()]']` | Degenerate |
| After over wrap | `['function-after[f(), function-wrap[f()]]']` | Degenerate |

**Any wrap validator in a branch's schema collapses every label to one.** A wrap's spelling
carries no class name, so all branches land in one group and the grouping carries no
information.

When that happens, `_errors_of_the_reached_branch` asks the branches directly to decide which
one was reached. That selection matches on `loc` alone, because a branch asked directly
answers in python mode (`model_attributes_type`) where the union parsing JSON says
`dict_type` at the same path. The errors you receive still come from the union, in your own
input mode. The selector returns `None` — and the caller keeps the ungrouped group — when no
branch was reached, when two were, or when a branch accepts on its own while the union
refused.

If you add a validator to a branch class, check its spelling against this table rather than
against the one case that exposed the hazard. The same table sits beside the fallback in
`_conditions.py`.

## What a compiled root check does

Everything a root states beyond its merged properties compiles into one
`model_validator(mode="after")` named `_require_schema_required_group`, as an expression over
the primitives in `src/adcp/types/_conditions.py`.
`scripts/compile_conditions.py`'s `render_root_validator` emits it.

Two conventions run through every primitive:

- **Presence is what the document carries.** `supplied` answers from `model_fields_set`
  membership, and then from the serializer, because a default the class emits is carried by
  the document exactly as a value you set is. `sent` adds the non-null half, because at a
  position the schema does not mark nullable the client drops a null-valued key. Compiled
  checks mostly call `sent`.
- **Every predicate is vacuous off-type.** JSON Schema applies a keyword only to instances of
  the type it grades, so a primitive handed the wrong shape returns `True`.

A check runs `mode="after"`, so it grades what pydantic parsed rather than what the document
carried.

### A conjunct refusal names the file, the index, and its fields — in the message only

```
Product violates a constraint its schema states outside its properties
(core/product.json, root conjunct 0) over signal_targeting_allowed,
signal_targeting_options, signal_targeting_rules
```

The field names come from `compile_conditions.named_fields`, which walks the condition tree.

**A conjunct refusal also carries those names in its error context**, so read them from
`ctx["fields"]` rather than parsing the sentence:

```python
GetSignalsRequest.model_validate({})
# type 'allOf', ctx {'fields': 'discovery_mode, signal_ids, signal_refs, signal_spec'}
```

Earlier builds passed no context on any compiled refusal, which left the field group
reachable only inside a message that also names the model class. That was a defect, fixed
under salesagent-8qela.49. If you read errors from a build predating that fix, `ctx` is
`None` on every compiled refusal and the sentence is your only source.

Which refusals carry a context splits cleanly, over the 626 `PydanticCustomError` raise
sites under `src/adcp/types/domains/`:

| Refusal | Keyword | Sites | Context |
|---|---|---|---|
| A root conjunct naming at least one field | `allOf` | 534 | `fields` |
| A root conjunct naming no field | `allOf` | 15 | None, and the message has no `over` clause |
| A flat root's combinator group | `anyOf`, `oneOf` | 45, 32 | None |

The 15 are conditions whose `named_fields` walk returns nothing, such as a rule over
`keys()` or an array's items. `BiddingPolicy` is one: its message ends at `root conjunct 1`.

Two runtime refusals outside that generated set carry context of their own. A split root's
union wrapper, `name_the_combinator`, passes `groups` and `fields` — so a combinator refusal
has a context when the root split into branch classes and none when it stayed flat. `_c.absent`
passes `fields` with keyword `not`.

Every keyword above is in the JSON Schema vocabulary, which `core/error.json` requires of
every `issues[]` entry. A compiled condition that reaches the wire with keyword `validation`
is a separate open defect (salesagent-8qela.94).

### `dependencies` compiles into root conjuncts

`dependencies`, `dependentRequired`, and `dependentSchemas` compile into root conjuncts like
any other root content. Exactly **27** per-domain pinned documents carry one, all of them
using the draft-07 `dependencies` spelling — including `core/account.json`,
`core/canvas-constraint.json`, and `account/sync-accounts-request.json`.

`core/product.json` carries none, which matters for the bound in the next section.

The `mcp/2026-07-28/` projection is a different artifact and uses the 2020-12
`dependentRequired` spelling across 284 documents. Count the per-domain set by walking each
document and counting the keyword in a schema position; a text search for `"dependencies"`
also matches a property named `dependencies`.

## `_COMBINATOR_FIELDS`: what a consumer that filters a model must keep

A root combinator makes its fields individually optional and collectively required, which
per-field `is_required()` cannot express. The generator therefore declares them on the class:

```python
_COMBINATOR_FIELDS: ClassVar[frozenset[str]] = frozenset({'format_ids', 'format_options'})
```

Keep these when you drop fields, or you produce a document the model refuses. The
`get_products` fields projection reads it for that reason: it kept the seven
individually-required `Product` fields, dropped `format_options` as optional and
unrequested, and the re-validation then failed `core/product.json`'s own
`sent(format_ids) or sent(format_options)`.

**The name is the bound.** The attribute holds the combinator's fields, which is not the
complete set a root constraint can require. A disjunction with a negated presence arm —
`(not supplied(A)) or supplied(B)` — goes false when you drop `B` and keep `A`, and the
combinator never names those fields. That shape lives in the 27 schemas above rather than in
`core/product.json`. The complete rule is "every field whose absence can flip a predicate",
which needs a polarity walk over the condition tree. If that is ever needed it arrives as a
second attribute, without changing what this one means.

The five public response bases declare no `_COMBINATOR_FIELDS` at all, because the
post-generation pass restores them over numbered classes rather than compiling a root check.
They are `ComplyTestControllerResponse`, `CreateContentStandardsResponse`,
`ListContentStandardsResponse`, `UpdateContentStandardsResponse`, and
`SyncGovernanceResponse`, and they are the five split-root modules that carry no compiled
check.

## `if`/`then` binds only an instance that carries the key

The pin states a conditional consequent as `then: {"properties": {"x": {...}}}`, and
`properties` applies only to an instance that carries the key. An instance omitting `x`
satisfies the conditional vacuously. Fourteen per-domain pinned documents carry a root-level
`if`, so predict your own schemas with this rule rather than memorizing the list.

Two of them condition the response envelope's `status` on another field —
`protocol/sync-principal-response.json` and `protocol/get-principal-response.json`:

```json
{"if":   {"properties": {"result": {"properties": {"kind": {"const": "failed"}}}}},
 "then": {"properties": {"status": {"enum": ["failed", "rejected"]}}}}
```

**A class-emitted default makes the key carried.** `supplied` answers from the serializer as
well as from `model_fields_set`, because the two dumps are byte-identical and a constraint
whose verdict differed between them would grade something the wire cannot express.

`post_generate_fixes.fix_protocol_envelope_status_default` gives the envelope
`status = TaskStatus.completed` for ergonomic construction, although
`core/protocol-envelope.json` makes `status` required with no default. So a response built
without one carries `completed`:

```python
ProtocolEnvelope.model_validate({}).model_dump(mode="json", exclude_none=True)
# {'status': 'completed', 'replayed': False}
```

The conditional then binds that emitted value. Before presence included emitted defaults,
such a document passed because the key read as absent.

So a default is not a free convenience on a field some conditional constrains. Supply the
field, as `src/adcp/server/principal.py` does at both of its failed-result sites
(`"status": "rejected"`, lines 189 and 405).

## `additionalProperties`: three states, and `ignore` is always inherited

`AdCPBaseModel` declares `extra='ignore'`, so every generated protocol type is forward
compatible by default. A generated class overrides that in two cases:

| Schema shape | Declared policy |
|---|---|
| Open, or types `additionalProperties` | `extra='allow'` |
| A closed object schema | `extra='forbid'` |
| Anything else | Nothing declared, so `ignore` is inherited |

**No generated class under `adcp.types.domains` declares `extra='ignore'`.** At `1c1e49b61`
the generated sources hold 1208 `extra='allow'` declarations and 1002 `extra='forbid'`, and
zero `ignore`. The only explicit `ignore` in `src/` sits on two hand-written OAuth models in
`src/adcp/oauth.py`. Treat `ignore` as the inherited default rather than as a fourth
declared tier.

Effective policy over the 3044 model classes defined in modules under `adcp.types.domains`:
`allow` 1348, `forbid` 1118, `ignore` 482, and 96 that set no `extra` key and so take
pydantic's own default, which is also `ignore`. Effective counts exceed declared counts
because a subclass inherits its base's merged config. Read a policy off
`cls.model_config` for the effective value; read the generated source for what a class
declares. `cls.__dict__["model_config"]` answers the effective value too, because pydantic
merges the parent's config into every subclass, so it is never evidence that a class
declared anything.

The policy decides where a branch's forbids can live. A branch class types a field it
forbids as `None`, which makes the combination a type error for a typed caller. A raw
document needs more, and the reason is the `allow` tier rather than the `forbid` one: under
`extra='allow'` an undeclared key is filed under extras and accepted, and a declared one
arrives as a `None` the branch does not forbid at a nullable position. So the refusal is a
`mode="before"` validator, `_refuse_keys_this_shape_forbids`, calling `_c.absent`. That is
also why such a refusal reaches you as a whole-document error rather than one at the
offending field, and why it displaces the combinator keyword.

## Two tiers: canonical and legacy

`adcp.types` exposes the **canonical** models. Legacy creative identity is absent from their
declared fields, JSON Schema, and serialized output at every nesting depth. Use this tier for
application code.

`adcp.types.legacy` exposes the wire-faithful generated roots, which retain that identity
through the AdCP 3.x transition. Use it for migration, conformance tooling, and 3.0/3.1 wire
adapters. It exports 63 names under a `Legacy*` alias convention, so the generated
`CreativeAsset` root is `adcp.types.legacy.LegacyCreativeAsset`. The bare names are not
importable from that module.

What the public canonical names bind differs between the two creative types, measured by MRO
membership rather than by annotation text:

| Public name | Binds |
|---|---|
| `adcp.types.CreativeAsset` | A subclass of the canonical BRANCH `CreativeAssetV2CreativeCanonicalFormatKind` |
| `adcp.types.CreativeManifest` | A subclass of the generated ROOT, with no branch in its MRO |

Neither declares `_branch_models`, so neither dispatches. Moving a name between tiers is an
export change rather than a design change. The tier split is stated in
`canonical_creative.py`'s module docstring, and this is the only other place it is written
down.

## The advertised `inputSchema` is a different artifact from the pinned document

An MCP client reads a tool's `inputSchema`, and that projection has its own contract.
`_model_to_json_schema` in `src/adcp/server/mcp_tools.py` requires an unconditional object
root for an input schema, because a discovery client has to render a form. With
`allow_root_union=False`, which is the input default, it refuses a root carrying any of
`$ref`, `allOf`, `anyOf`, `oneOf`, `not`, `if`, `then`, `else`, or `dependentSchemas`.
`tests/test_mcp_schema_drift.py::test_every_input_discovery_schema_has_unconditional_object_root`
asserts it.

The pinned document is not that shape. `media-buy/create-media-buy-request.json` carries
`properties`, `required`, `anyOf`, `allOf`, and `dependencies` together at its root. So a
pin-faithful projection is exactly what the advertised-schema guard refuses, and "match the
pin" is the wrong instruction for this function.

A split root is recoverable, and `_object_root_from_branches` recovers it from what the union
already carries. Each branch's `$defs` entry is a complete object schema, so properties are
unioned and `required` is intersected, and a field's declaration is the union of its
branches'. That is sound because a branch only ever narrows; taking the first branch's
declaration instead would advertise one branch's narrowing as the contract of all of them.

The result is deliberately permissive about which branch a document must satisfy. A client
can build a payload satisfying none, because the advertised schema's job is to let a client
build a candidate and the server's job is to refuse a wrong one. The branches are still
enforced on arrival. A conditional or a composition at the root is not recoverable, so those
tools fall back to a hand-crafted shape.

## What nothing checks

**No grader compares `model_json_schema()` against the pinned document — anywhere.** A
reviewer who does not know this assumes the emitted schema is checked against the pin. It is
not.

Seven test modules read both a pinned schema and call `model_json_schema()`, and every one of
them either asserts on individual properties or compares a model to another model:

- `test_mcp_schema_drift` compares a tool's advertised schema to `model_json_schema()`.
- `test_spec_coverage` compares an MCP tool `inputSchema` to the request model's schema.
- `test_composability_invariant` compares two composed models' wire schemas to each other.
- `test_code_generation`, `test_rc6_adoption`, `test_version_scoped_models`, and
  `test_targeting_overlay_schema` assert on named properties, `required` membership, or a
  description.

`tests/conformance/test_schema_model_parity.py` grades accept and refuse verdicts on
synthesized documents, which is behavior rather than the schema's own shape.

Because nothing grades the document, these divergences sit unnoticed. Each one is open as
salesagent-8qela.90, and each reproduces on a top-level keyword comparison:

| Divergence | What it means for you |
|---|---|
| `allOf` is dropped | A root's conditional rules are absent from the emitted schema. Runtime still enforces them through the compiled validator, so this is a documentation divergence rather than a validation one. |
| An inline `not` is dropped | A branch's forbids are absent from the emitted schema, though `_refuse_keys_this_shape_forbids` still enforces them. |
| `oneOf` with a `discriminator` becomes `anyOf` with none | `brand/verify-brand-claim-request.json`. `anyOf` accepts a document satisfying two branches where `oneOf` refuses it, so the emitted schema is **weaker** than the protocol states. It is sound only because the branches are pairwise disjoint, and that proof lives in the generator rather than in the emitted document. |
| `dependencies` is dropped | A presence implication the pin states is absent from the emitted schema. |

Two things sharpen that list:

- **The divergence is not confined to split roots.** `core/product.json` generates one flat
  `Product` class that keeps `properties`, `required`, `type`, and `additionalProperties`,
  and still drops both `allOf` and `anyOf`.
- **A split root's emitted schema is only `anyOf`.** It carries no top-level `type`,
  `properties`, `required`, or `additionalProperties`, which is why three tools shipped stub
  input schemas (salesagent-8qela.85).

A guard comparing the two documents per root — top-level keyword sets, properties,
`required`, and the combinator keyword — catches all of these at once.

## Known gaps, by bead

The gaps this document describes are tracked as follows.

| Gap | Bead |
|---|---|
| A sibling branch's `not` refusal displaces the real branch error and the combinator keyword | salesagent-klblw, fix in review on `fix/branch-election-filter` |
| An inheriting subclass of a split root does not dispatch, so a refusal names `allOf` where a root names `not` | salesagent-8qela.108 |
| A split root's generated JSON schema diverges from the pin in four ways, and nothing grades it | salesagent-8qela.90 |
| A split root's emitted schema has no top-level properties, so three tools ship stub input schemas | salesagent-8qela.85 |
| A generated root requires keys its schema root does not, masking the combinator refusal | salesagent-8qela.80 |
| A genuine union's generated member name reaches `error.field` | salesagent-8qela.46 |
| A compiled refusal carries a keyword outside the JSON Schema vocabulary | salesagent-8qela.94 |
