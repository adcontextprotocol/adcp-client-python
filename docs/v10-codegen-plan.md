# SDK 10 plan: codegen fidelity and wire round-trip

Status: design memo for SDK 10 workstream 2, written 2026-10-26 against
`main` at 9.2.0 (AdCP 3.2.4 pin) and `next` before the 3.3 regeneration.
Every number below is re-measured on `next` once workstream 1's 3.3 bundle
lands; the commands are in [Re-measurement](#re-measurement-on-next-after-the-33-regeneration).

This memo decides which of the deferred breaking fixes land in 10.0, states
the invariant they serve, compares #1522's generator rewrite with incremental
fixes, and drafts the adopter-facing changes and migration notes.

## 1. The invariant

> A schema-valid wire document survives `Model.model_validate_json(doc)` →
> `model.model_dump(mode="json")` byte-for-byte under RFC 8785 (JCS).

Under the SDK's **default** dump settings, with no opt-in recipe:

- a property the document omitted stays omitted, even when the schema
  declares a default for it;
- a property the document carried stays present, including properties the
  pinned model does not declare where the schema admits them;
- every key is written under its wire name (`$schema`, `pass`, `from`,
  `media-buy`, `_truncation`, `2xl`);
- an explicit `null` the schema admits is re-emitted as `null`;
- no scalar is rewritten (URLs, datetimes, UUIDs and emails already keep
  their bytes since #1617 on `main`).

`tests/conformance/test_wire_round_trip.py` states this today for the
digest- and echo-covered schemas under `model_dump_as_received`, and grades
every other schema against an allowlist of four difference classes. In 10.0
the allowlist (`ACCEPTED`) and the bound-schema exception (`BOUND_DEFAULTS`)
go to empty: every schema, every document, default dump, no classes accepted.

What the invariant does **not** promise: a document the model refuses
(`REFUSING` in the suite) is a schema/model parity defect, graded by
`test_schema_model_parity.py`, and is outside this invariant; and a model
built in Python dumps exactly what the caller set plus what the schema
requires, which is a consequence of the same rule, not a second rule.

## 2. Baseline: what breaks the invariant today

Measured on `main` 9.2.0 with `python -m tests.conformance._round_trip --variants`
(6,184 graded documents over 1,008 base documents; full output in
`.context/sweep-main-9.2.0.txt` of the workstream 2 workspace).

| Difference class | Distinct fields | Schemas | Under `model_dump_as_received` | Issue |
|---|---|---|---|---|
| `default_emitted` | 145 | 62 | 0 | #1613 |
| `field_dropped` | 120 | 5 | 120 | #1614 |
| `alias_emitted` | 14 | 14 | 0 | #1615 |
| `null_dropped` | 5 | 5 | 0 | #1615 |
| refusals (documents) | 121 | 7 | n/a | #1616 and parity |

So the explicit "as received" recipe already closes three of the four classes;
`field_dropped` is the only class the dump settings cannot fix, because the
model never held the value.

Two schema-level facts shape the design:

- **Defaults are almost all optional.** The 3.2 bundle declares a `default`
  on 331 optional properties and on only 5 required ones. The generated tree
  carries 695 non-literal defaults and 340 single-literal defaults (`asset_type
  = 'image'`, `rights_status = 'acquired'`); the literal ones are
  generator-injected discriminators for schema-required `const` properties,
  not schema defaults. A dump rule therefore has to tell "schema-optional
  default" from "wire-required discriminator", and that is knowable only at
  generation time.
- **Most objects are open.** Of 3,205 object nodes with `properties`, 777 are
  `additionalProperties: false`; 2,428 are open (884 `true`, 1,535 absent, 9
  typed). The SDK's `extra='ignore'` default drops unknown keys on all of them,
  which is where `field_dropped` comes from. 103 properties admit `null`.

The two `TypeError` refusals (`core/mcp-webhook-payload.json`,
`core/async-response-data.json`, #1616) are a crash, not a fidelity defect:
`media-buy/request-proposals-response.json` constrains
`purchase_continuation.losses` with `allOf[contains]`, which the generator
renders as an empty `Losses` model unioned with `list[Loss]`, and pydantic
then applies the field's `min_length=2` to the model arm. Every webhook payload
reaches that arm through the response union. #1616 is being fixed on `main`
for 9.2.1 and reaches `next` through the next main → next sync; it is not
part of this workstream.

## 3. What lands in 10.0

| Issue | Decision | Mechanism |
|---|---|---|
| #1613 defaults emitted | **10.0** | generator marks wire-required fields; default dump omits unset schema-optional defaults |
| #1615 aliases | **10.0** | `serialize_by_alias=True` on `AdCPBaseModel` |
| #1615 explicit nulls | **10.0** | models remember a received `null` and re-emit it |
| #1614 unknown fields | **10.0** | generated `extra` follows the schema: open → `allow`, closed → `ignore` |
| #1614 union-arm fields | **10.0** | falls out of the `extra` policy plus #1522's construction dispatch |
| #1414 root `anyOf` conditions | **10.0** | #1522's constraint compiler |
| #1409 arm names | **10.0** | wire the schema-node naming rule into the response emitter; deprecated numbered aliases for 10.x |
| #1397 tolerant subtype | **decide after #1522 lands** | #1522's shared `<Root>Shape` base is the "common ancestor" option; the guard test ships either way |
| #1616 `TypeError` | **9.2.1 on `main`** | out of scope here |

The ordering matters more than the grouping. Everything in the first five
rows is a change to `AdCPBaseModel` plus a generator marker, and is
independent of the generator's class-naming and constraint-compiling
machinery. #1414 and #1409 change what the generator emits, and #1522 is the
vehicle for the first and most of the second.

### 3.1 #1613: omit a schema-optional default the caller did not set

Rule: the default dump emits a field when it is in `model_fields_set`, or
when it is wire-required. A wire-required field is one the schema lists in
`required`, or a generator-injected discriminator for a `const` / one-value
`enum` property. Everything else with a default is emitted only when set.

- The generator knows `required` and `const` at emission time and marks the
  schema-optional defaulted fields with a field marker, in the same way
  `NoExplicitNull` is a `FieldInfo` carried in `Annotated` metadata today. The
  marker is the provenance; nothing is inferred from the Python default.
- `AdCPBaseModel`'s existing wrap serializer drops the marked fields that are
  not in `model_fields_set`. The per-class set is computed once and cached on
  the class, so the cost is one set lookup per marked field per dump.
- An explicit `exclude_unset=` from the caller is honoured as pydantic
  defines it: `exclude_unset=False` restores 9.x output, `exclude_unset=True`
  also drops unset wire-required discriminators (pydantic's semantics, unchanged).
- Attribute reads are unchanged: `account_ref.sandbox is False` whether or
  not the wire carried it.
- Assignment after construction marks the field set (pydantic's `__setattr__`
  does this already), so a field a handler fills in is emitted.

Why not make `exclude_unset=True` the global default: it drops the 340
generator-injected discriminators on models built in Python
(`ImageAsset(url=...)` would dump without `asset_type`), which is the case
#1613 flags. The per-field marker is what keeps those.

Interaction with #1522: its compiled conditions define presence as "what the
class emits" (`_conditions._emitted`), because the two dumps were
byte-identical. After #1613 the default dump is the document, so presence
reduces to `model_fields_set` plus wire-required fields, and the serializer
probe becomes a lookup. That simplifies #1522's vocabulary rather than
conflicting with it, and is the one place the two changes touch.

### 3.2 #1615: wire names and explicit nulls

Aliases: `ConfigDict(serialize_by_alias=True, validate_by_name=True,
validate_by_alias=True)` on `AdCPBaseModel` (pydantic ≥ 2.11; the pin is
2.13). `model_dump()` then writes `$schema`, `pass`, `from`, `media-buy`,
`_truncation`, `2xl`. Construction by either spelling keeps working; code that
reads `dump["pass_"]` reads `dump["pass"]`.

Explicit nulls: the Python convention that `None` means "not supplied" stays.
Of the two designs in #1615, the sentinel one is chosen, because the simple
one ("a field set to `None` on a null-admitting property dumps `null`")
changes the bytes of every Python-built model whose caller passed `None`
through a keyword argument, and the bundle has 103 null-admitting properties,
not the 5 the sweep happens to exercise.

- A before-validator on `AdCPBaseModel` records, for the class's
  null-admitting fields only, which keys arrived as JSON `null`, into a private
  attribute. Classes with no null-admitting field (the `NoExplicitNull` marker
  already identifies the others) pay a single `ClassVar` check.
- The serializer re-emits `null` for a remembered key whose value is still
  `None`. Assigning a value afterwards wins; assigning `None` afterwards reads
  as "not supplied", as today.
- `model_construct` runs no validator and remembers nothing, which is right:
  nothing was received.

With this, `model_dump()` and `model_dump_as_received()` produce the same
bytes for a parsed model. `model_dump_as_received` stays through 10.x as a
documented alias and is removed in 11.

### 3.3 #1614: keep what the schema admits

The generator sets each class's `extra` from its schema node:

- `additionalProperties: false` → `extra='ignore'` (and `forbid` under
  `ADCP_STRICT_VALIDATION`, as today). A closed object cannot carry an
  unknown key and stay schema-valid, so dropping one costs the invariant
  nothing; the strict switch keeps its purpose of catching renamed fields.
- anything else → `extra='allow'`. The value lives in `__pydantic_extra__`
  and is dumped back, which is what `ContextObject` and `ExtensionObject` do
  already.

After this, a `field_dropped` difference on a document the model accepted
means the model is wrong, not the policy: either the arm class is missing a
property the schema's arm declares, or construction chose an arm the document
does not satisfy. The first is a generator defect; the second is what #1522's
"construction dispatched as well as validation" closes for split roots and
response unions. `core/signal-pricing-option.json` and
`core/vendor-pricing-option.json` (`/amount: 0.0 -> None`, 64 fields each) are
the second kind. The suite then goes strict with no `field_dropped` allowance.

Adopter-visible change: a stray keyword argument on an open model is no
longer silently discarded; it is sent. That is the documented 10.0 behaviour,
and `ADCP_STRICT_VALIDATION` is the tool for finding stray kwargs in tests.

### 3.4 #1414 and #1409: what the generator emits

#1414 is closed by #1522's constraint reader and compiler: a split root's
branches become classes that carry their own `required`, `not`, `const` and
nested `allOf[required]` conditions, with the refusal naming the JSON Schema
keyword. The reproduction in #1414 (`packages` without `budget`, no
`total_budget`, no `proposal_id`) is refused on #1522's branch with error
type `anyOf` at the root, and accepted on `main` 9.2.0 (both run on
2026-10-26). The
12-model blast radius in #1414 is the acceptance list for the PR that lands it.

#1409 is half in #1522 and half not. #1522 names split-root branch classes
from their schema node (`CreateMediaBuyRequestExplicitPackagesWithFixedAllocation`,
`...CommittedProposalExecution`) through `scripts/union_branch_names.py`,
which is Konstantin's `union_arm_names.py` rule from #1409 evolved. It does
**not** wire that rule into the response emitter: `restore_response_variant_aliases`
still mints `f"{self.base}{index}"` and still carries the hardcoded
`BuildCreativeResponse` reorder. 10.0 does the wiring as a follow-up PR on top
of #1522:

- the 40 numbered public names on `next` today (`CreateMediaBuyResponse1`,
  `SyncCreativesResponse3`, `RefinementApplied2`, `LegacyBuildCreativeResponse1..6`,
  `Field1`, `CatalogFieldBinding1`, ...) get their title/const/required-set
  names;
- `aliases.py` keeps every numbered name as a deprecated alias for 10.x,
  raising `DeprecationWarning` through the module `__getattr__`, with a
  rename table in `MIGRATION_v9_to_v10.md`; the aliases are removed in 11;
- the rule fails closed: an arm it cannot name fails the build. The one known
  case at 3.2.4 (`content-standards/get-content-standards-response.json`
  `oneOf[0]`, no title; adcontextprotocol/adcp#7913) is re-measured on the 3.3
  bundle, and if still unnameable the class stays reachable at its domain
  path and off the flat `adcp.types` surface, as #1409 proposes.

### 3.5 #1397: decide on #1522's lattice, not before

`main` has already narrowed the case: `_DeliveryCreativeManifest` no longer
redeclares the format-kind vocabulary, and its docstring records the one
measured reason it survives (accepting a parent instance at the delivery
boundary). #1522 introduces a shared public base per split root
(`<Root>Shape`) and routes construction to the branch a document satisfies,
which is the "common ancestor" direction #1397 describes. Whether the
canonical creative models can sit on that base without re-opening the
validator-inheritance fix is a measurement to take on the rebased tree, not a
decision to make in prose. What ships regardless: a guard test that fails if
a strict wire base loses its `revalidate_instances` setting.

## 4. #1522 against incremental fixes

### What #1522 is

`feat(codegen)!: compile what a schema root states, and route every arm it
declares` by KonstantinMirin, open against `main`, 8 commits, base at
`main` of 2026-10-23 (45 commits behind `main` today; `next` is itself 54
commits behind `main`). Hand-written delta against its merge base:

| Area | Files | Lines |
|---|---|---|
| `scripts/` (reader, compiler, branch classes, arm names, post-gen fixes) | 12 | +5,862 / −1,294 |
| `src/adcp/` hand-written, outside `domains/` (`_forward_compat`, `canonical_creative`, `error_narrowing`, `governance`, `_generated`, `aliases`, ...) | 67 | +3,660 / −1,807 |
| `tests/` | 107 | +11,536 / −821 |
| generated `domains/` | 250 | regenerated |

It deletes `types/_ergonomic.py`, `scripts/generate_ergonomic_coercion.py` and
`types/response_dispatch.py` (the `BREAKING CHANGE` footer names the last). CI
on the PR: 39 checks pass, the automated code review fails, and the
description records two deliberate failures (a Python 3.12 `assert-type` and
one Postgres conformance fixture) with reasons.

### Measured conflict with today's `main`

`git merge-tree origin/main pr-1522` conflicts in 717 files: 671 are under
`src/adcp/types/domains/` and are resolved by regenerating, not merging; the
7 hand-written ones are `scripts/post_generate_fixes.py`,
`types/_forward_compat.py`, `types/canonical_creative.py`,
`types/projections.py` and three tests. The generated conflicts exist because
#1522's tree predates #1555 (`NoExplicitNull`), #1617 (`WireDatetime` and the
other wire-preserving scalars) and #1625, all of which also edit
`post_generate_fixes.py`.

### Comparison

| | #1522 as the base | Incremental fixes only |
|---|---|---|
| #1414 | closed, with keyword-named refusals and branch classes | re-implement a subset of the same compiler; nothing smaller closes it honestly |
| #1409 | branch naming done; response arms still positional (one follow-up) | the #1409 branch alone, wired into the emitter |
| #1614 union arms | construction dispatch picks the right arm | `extra='allow'` keeps the bytes but the arm can still be wrong |
| #1613 / #1615 | orthogonal; base-model changes land before or after | same |
| `_ergonomic` patchers | deleted; their checks become compiled conditions | kept; the `publisher-property-selector` refusal in `REFUSING` stays |
| Review cost | one large rebase, mostly generator | several medium PRs |
| Risk | one branch carries the whole 10.0 generator; two known red checks | spreads over releases, and #1414 stays open |

Recommendation: **#1522 is the right base for the generator half, and it
belongs on `next`, not `main`.** It is `feat!`, it removes a public module,
and the deferral note on every issue here says 10.0. The base-model half
(#1613, #1615, #1614's `extra` policy) is independent and should land as its
own PRs first, because each is small, each flips one accepted class in the
suite to strict, and none depends on the 3.3 regeneration beyond re-running
the sweep.

### Proposed path for #1522 (not yet proposed to Konstantin)

1. Workstream 1 lands the 3.3 regeneration on `next`.
2. In a scratch branch (`claude/sdk10-1522-on-next`, never his branch), merge
   #1522's head onto `next`, resolve the 7 hand-written conflicts, delete the
   generated tree and regenerate from the merged generator; run
   `make validate-generated`, the sweep, and the parity suites.
3. Report the result in this chat first. If it is clean enough, comment on
   #1522 proposing the retarget to `next` with the scratch branch as evidence,
   and let Konstantin choose to retarget or to adopt the scratch branch as a
   PR under his authorship (`Co-authored-by` either way).
4. The two known red checks are resolved on the rebased branch, not papered
   over: the 3.12 `assert-type` by the two-types split his description asks
   for, the Postgres fixture by the redesign it asks for.

## 5. Adopter-facing API changes in 10.0

- `model_dump()` / `model_dump_json()` emit only set fields plus wire-required
  ones. A model built in Python no longer carries `sandbox: false`,
  `timezone: "inventory_local"`, `assignment_limit: 50` and the other 143
  schema defaults unless the caller set them. Pass `exclude_unset=False` for
  the 9.x output.
- `model_dump()` writes wire names (`by_alias` semantics by default).
- A JSON `null` received on a null-admitting property is re-emitted;
  `None` passed in Python still means "not supplied".
- Open models accept and re-emit unknown keys; a stray keyword argument
  goes to the wire. Closed models keep dropping them (`forbid` under
  `ADCP_STRICT_VALIDATION`).
- `model_dump_as_received()` is a deprecated alias of `model_dump()`.
- The numbered union-arm names are deprecated aliases of schema-derived
  names (rename table in the migration file).
- `adcp.types.response_dispatch` and `ResponseArmDispatchMixin` are removed
  (from #1522); `types/_ergonomic.py` is gone, and the checks it added are
  compiled from the schema instead.
- Split-root requests such as `CreateMediaBuyRequest` refuse documents that
  satisfy none of their branches, with `error.type` set to the JSON Schema
  keyword (`anyOf`, `required`, `not`, `const`).
- New schema-derived branch classes are public under their domain path and,
  where the flat namespace can bind them, under `adcp.types`.

## 6. Migration notes (draft for `MIGRATION_v9_to_v10.md`)

Each PR adds its own section; the drafts below are the text to adapt.

**Dumps omit unset defaults.** In 9.x `model.model_dump()` emitted every
field, so a schema default appeared on the wire whether or not you set it. In
10.0 only fields you set, or the schema requires, are emitted. Golden-file
tests that compare dumps need regenerating. To keep 9.x output, pass
`exclude_unset=False`. If you relied on a default reaching the peer (for
example `budget_allocation.mode`), set it explicitly.

**Dumps use wire names.** `model_dump()` now writes `$schema`, `pass`, `from`,
`media-buy`, `_truncation` and `2xl` instead of `field_schema`, `pass_`,
`from_`, `media_buy`, `field_truncation`, `field_2xl`. Code that reads a dump
by the Python name must read the wire name. Construction accepts both.

**Received nulls round-trip.** A property that arrived as `null` and whose
schema admits `null` is re-emitted as `null`. Passing `None` in Python is
unchanged and still omits the property.

**Unknown keys on open objects are kept.** A key the model does not declare
is retained in `model_extra` and written back on dump when the schema object
is open. If your code passed stray keyword arguments and relied on them being
dropped, remove them; set `ADCP_STRICT_VALIDATION=1` in tests to find them.

**`model_dump_as_received()` is deprecated.** It returns the same bytes as
`model_dump(mode="json")` now; replace the call. It is removed in 11.

**Numbered union-arm classes are renamed.** `CreateMediaBuyResponse1` is
`CreateMediaBuySuccess`, and so on per the rename table. The old names import
with a `DeprecationWarning` until 11.

**Root `anyOf` conditions are enforced.** A `CreateMediaBuyRequest` whose
packages carry no `budget` and which sets neither `total_budget` nor
`proposal_id` is refused with `required` / `anyOf` issues; 9.x accepted it.
Check the 12 models listed in #1414 if your tests build minimal documents.

**Removed:** `adcp.types.response_dispatch`, `ResponseArmDispatchMixin`,
`adcp.types._ergonomic` (private), `adcp.types.generated_poc` (already gone on
`next`, #1536).

## 7. PR sequence into `next`

All after workstream 1's 3.3 regeneration, each squash-merged, each making
the round-trip suite strict for the class it closes:

1. `feat(types)!: dump wire names by default` (#1615 aliases) — deletes
   `alias_emitted` from `ACCEPTED`.
2. `feat(types)!: re-emit a received null` (#1615 nulls) — deletes
   `null_dropped`.
3. `feat(codegen)!: mark wire-required fields; omit unset defaults on dump`
   (#1613) — deletes `default_emitted` and empties `BOUND_DEFAULTS`; also
   deprecates `model_dump_as_received`.
4. `feat(codegen)!: let the schema decide extra` (#1614 policy) — shrinks
   `field_dropped` to the arm-selection cases.
5. #1522 rebased on `next` (with Konstantin) — closes #1414, deletes
   `field_dropped`, deletes `_ergonomic` entries from `REFUSING`.
6. `feat(codegen)!: name response arms from their schema node` (#1409) —
   deprecated numbered aliases, rename table.
7. #1397 measurement and decision; guard test.

PRs 1–4 touch `src/adcp/types/base.py`, the generator's marker emission and
the conformance suite; they do not touch `domains/` beyond the regeneration
their marker causes. PR 5 is the one that reshapes `domains/`.

## 8. Re-measurement on `next` after the 3.3 regeneration

Run and record before each PR in §7:

```sh
uv sync
python -m tests.conformance._round_trip --variants > .context/sweep-next.txt
python -m pytest tests/conformance/test_wire_round_trip.py -q
python -m pytest tests/conformance/test_schema_model_parity.py -q
```

Then re-count: the four classes and their field/schema counts; `REFUSING`;
`BOUND_DEFAULTS`; the numbered public names (`dir(adcp.types._eager)`
filtered on a trailing digit); the unnameable arms under #1409's rule on the
3.3 bundle (adcp#7913); and the open/closed object split, because 3.3 closes
several previously open objects (`media_buy.features`, `adcp.implementation`,
`dooh_placement_attributes.location`, `commercial_terms`), which moves them
from `allow` to `ignore` under §3.3.

3.3 adds no digest-bound object and changes no canonicalization rule (checked
against the what's-new and migration pages), so `DIGEST_COVERED` and
`ECHO_COVERED` grow only if the regenerated bundle's descriptions say so.

## 9. Open questions for Brian

1. §3.3 sends stray kwargs on open models. The alternative is `allow` only on
   digest- and echo-covered models plus union arms, keeping `ignore`
   elsewhere; that keeps the invariant for the bound objects only and leaves
   `field_dropped` accepted for the rest. The memo recommends the schema-driven
   policy.
2. §3.2 keeps `None`-means-unset and remembers received nulls. If you would
   rather have the simpler "set to `None` dumps `null`", say so; it is less
   code and the cost is the 103 null-admitting properties.
3. §4: the proposal to retarget #1522 to `next` goes to Konstantin only after
   you have seen the rebased scratch branch's results here.
