# How the type tree is generated

Status: current. Generator pinned at `datamodel-code-generator==0.64.0`.
Schema pin: `src/adcp/ADCP_VERSION` = `3.2.3`, which `resolve_bundle_key` collapses to the
bundle `schemas/cache/3.2`.

This document is for whoever has to change the pipeline. It covers every step in order,
what a schema root states that a Python class cannot express and how that is answered, how
a class gets its name, what the guards are, how to add a schema feature, and what to do
when the generator renders something wrong.

For the resulting surface from a consumer's side, see [../type-surface.md](../type-surface.md).
For subclassing a generated type, see [../extending-types.md](../extending-types.md).

## The shape of the thing

1057 non-bundled schema files become 1088 generated model modules, which become one flat
namespace of 3022 names under `adcp.types`, mirrored module-by-module under
`adcp.types.domains`. Nothing
in it is hand-written: the flat `__all__`, the `_generated.py` arena, the mirror, the
error-details exports and the collision table are all emitted.

The generator is a fixed, flawed input. Three of the pipeline's layers exist because of
that: a pre-generation pass that rewrites schemas into shapes the generator renders
correctly, a post-generation pass that rewrites the rendered Python, and a consolidation
step that derives the public surface from the result. Read the pin section at the end
before proposing to remove any of them.

## The pipeline, end to end

`make regenerate-schemas` is the full refresh:

```make
regenerate-schemas: ## Download latest schemas and skills from bundle, then regenerate models
	$(PYTHON) scripts/sync_schemas.py
	$(PYTHON) scripts/fix_schema_refs.py
	$(PYTHON) scripts/bundle_schemas.py
	$(PYTHON) scripts/generate_versioned_stubs.py
	$(PYTHON) scripts/generate_versioned_bases.py
	$(PYTHON) scripts/generate_types.py
	$(PYTHON) scripts/consolidate_exports.py
```

| Step | Consumes | Produces | Network |
|---|---|---|---|
| `sync_schemas.py` | the published schema bundle | `schemas/cache/<bundle>/**` | **yes — the only network step** |
| `fix_schema_refs.py` | the cache | the cache, with absolute `$ref` URLs rewritten to relative file paths | no |
| `bundle_schemas.py` | the cache | `src/adcp/_schemas/**`, the schemas the wheel ships | no |
| `generate_versioned_stubs.py` | the cache | `src/adcp/types/v30.pyi`, `v31.pyi`, `v32.pyi` | no |
| `generate_versioned_bases.py` | the cache | `src/adcp/types/versioned_bases/**` | no |
| `generate_types.py` | the cache | `domains/**`, `_generated.py` | no |
| `consolidate_exports.py` | the generated tree | `_generated.py`, each `domains/<domain>/__init__.py`, `domains/__init__.py`, `error_details.py`, `docs/shared-type-names.md` | no |

`sync_schemas.py` is the only step that touches the network, and skipping it regenerates
against the committed cache. That is the normal inner loop:

```bash
python scripts/generate_types.py            # regenerate from the committed cache
python scripts/generate_types.py --check     # verify without writing anything
```

Note that `generate_types.py` already runs `consolidate_exports.py` as a subprocess
against its staging tree and installs its output, so the Makefile target runs it a second
time. Harmless, but do not read the Makefile as the statement of what depends on what.

`generate_ergonomic_coercion.py` used to be a third step here, producing
`src/adcp/types/_ergonomic.py`, which patched 74 field annotations at import time. Both are
deleted. 70 of those patches were inert — `model_rebuild` rebuilds a schema from
`model_fields`, not from `__annotations__` — and the coercion they appeared to supply is
native: a generated enum is a `StrEnum`, so a string is accepted without help, and pydantic
builds a model field from a dict without help. The four that were live are declared on the
generated field by `COMPATIBILITY_ANNOTATIONS` in `post_generate_fixes.py`
(salesagent-1jj5e).

### One tree

`OUTPUT_DIR` is `src/adcp/types/domains` — the address adopters import from. The generator
defines every class at its public path, so there is no private tree behind the public one
and no re-export tree in front of it: nothing is copied, and nothing can drift. What
`consolidate_exports.py` writes into that tree are the namespaces DERIVED from it — each
domain root, the `DOMAINS` roster, `_generated.py` and `error_details.py` — never a second
copy of a module.

Four domains have no package root to write: `adagents`, `brand_discovery`, `manifest` and
`manifest_schema` are single schemas at the bundle root, so the generator emits
`domains/<domain>.py` and that module IS the domain root. A post-generation pass
(`declare_root_schema_module_exports`) gives each of the four an `__all__`, because a
domain root is a public namespace and `scripts/export_resolution.py` reads `__all__` to
record what one promises.

### Inside `generate_types.py`

`main()` stages everything in a `TemporaryDirectory(dir=REPO_ROOT, prefix=".typegen-")` and
installs it at the end with `os.replace` plus full rollback. Nothing is written to the
checkout until every step has succeeded, which is why a failure leaves `git status` clean —
and why a crash takes the broken intermediate file with it.

In order:

1. Snapshot the committed tree, for the delta report.
2. `flatten_schemas` — copy the bundle into the staging tree with hyphens converted to
   underscores in every path part, applying the pre-generation transforms below.
3. `_staged_source_tree` — copy `src/adcp` into the staging tree, minus the committed
   generated tree. Generation targets `types/domains` inside that copy, so consolidation
   can import what was just generated and write the derived namespaces beside it.
4. Run `datamodel-codegen` over the staged schema tree.
5. `generate_root_discovery_types` — generate `brand.json` a second time, standalone, as
   `brand_discovery.py`. The generator is therefore invoked twice per run.
6. `fix_forward_references` — repair aliased imports.
7. `apply_post_generation_fixes` — a subprocess running `post_generate_fixes.py` against
   the staged tree.
8. `prune_unused_bundled_modules` — keep only the three paths in `BUNDLED_KEEP`.
9. Run `consolidate_exports.py` against the staging tree, which writes the domain roots
   into the tree generated in step 4.
10. `restore_unchanged_files` — restore byte-for-byte any file whose only change is a
    timestamp header, so a no-op regeneration produces no diff. Once, over the whole tree,
    after the roots are in it.
11. Compare the artifacts against the checkout — `domains/`, `_generated.py`,
    `error_details.py`, `docs/shared-type-names.md`. Under `--check`,
    print the drift and return 1 without writing. Otherwise install and write
    `SCHEMA_DELTAS.md`.

### The pre-generation transforms

Applied per schema file, in this order, inside `flatten_schemas`:

```python
schema = normalize_enum_descriptions(schema)
schema = inline_structural_pointer_refs(schema, rel_path)
schema = collapse_nullable_unions(schema)
schema = normalize_version_envelope_composition(schema, rel_path)
schema = rewrite_refs(schema, rel_path)
schema = stabilize_inlined_core_refs(schema, rel_path)
schema = stabilize_nested_discriminators(schema, rel_path)
schema = flatten_root_object_validation_union(schema, rel_path)
schema = flatten_validation_oneof(schema)
```

Order matters in two places, and both constraints are on the same step.
`normalize_version_envelope_composition` must precede `rewrite_refs`, so the reference it
appends takes the same canonical-URL path through the rewriter as the schemas that already
compose; and it must follow `inline_structural_pointer_refs`, so the envelope reference it
appends is never itself inlined back into copied fields — which would turn inheritance into
flattened fields.

`flatten_root_object_validation_union` and `flatten_validation_oneof` delete a root's
combinator, which is what keeps the root's public name a subclassable class rather than a
`RootModel` union. What then enforces the combination is
*[What a schema root says that a class cannot](#what-a-schema-root-says-that-a-class-cannot)*
below; the two halves only make sense together.

Each of these operates on the staging copy only. The cached bundle, the schemas the wheel
ships and any direct-from-schema validation are untouched, which is what keeps the
transforms from changing what the SDK validates against.

Two of them carry explicit ceilings rather than trusting a bump not to move the problem.
`normalize_version_envelope_composition` raises if it would rewrite more than 12 schemas:
*"A bump changed the shape of the problem: re-measure which schemas declare the version
fields without composing the envelope before raising the ceiling."* It also carries four
refusals, one of which exists because the rewrite deleted a field without it — see
*When the generator renders something wrong*, item 3.

### `$ref` resolution never reaches the network

`--allow-remote-refs` is passed, but so is `--http-local-ref-path` pointing at a temp
mirror of the pinned cache laid out as `adcontextprotocol.org/schemas/<bundle>`. A reference
the cache does not contain fails locally with `$ref local file not found`; there is no
network fallback. `tests/test_generate_types_safety.py` grades this both ways, parametrized
on the file being present and absent.

## The post-generation pass

`scripts/post_generate_fixes.py` is 7954 lines holding 153 module-level functions: 80 fix
entrypoints, built as one ordered list in `main()` and run in list order, plus the helpers
they share.

Three ordering constraints are commented in the list itself and must be preserved:

- `point_integer_fields_at_the_schema_integer_type` retypes every `type: integer`,
  including an integer root's own `root` annotation, so it must precede
  `rewrite_scalar_rootmodels` — run after, and an integer root is still spelled
  `RootModel[StrictInt]` and keeps its wrapper.
- `rewrite_scalar_rootmodels` is last of the substantive fixes, because every earlier fixer
  can emit or edit a `RootModel[<scalar>]`.
- The import-hygiene passes run after everything that can add an import or a base-class
  name, with `strip_extra_blank_lines_at_eof` last.

**A fix that cannot find its target must raise, not return.** The sharpest failure mode in
this file is a pass that matches literal source text which another pass legitimately
changed. Two instances are on record. One produced a syntactically invalid module:
a regex matching only the flat single-line `from pydantic import ...` form captured the bare
`(` from a parenthesized import and wrote `from pydantic import (, model_validator`. The
other was silent — `fix_product_publisher_property_model_coercion` matched an exact import
line, failed to find it, printed a message, returned early, and never added its validator;
no syntax error and no test failure. Locate by AST, or through the shared
`ensure_pydantic_import` / `add_to_import` helpers, which read whichever names are present,
preserve the statement's form, and raise rather than mangle. Ten fixes still match an
import by exact literal text; that is recorded fragility, not a pattern to copy.

`scripts/post_generation_manifest.json` is the per-fix effect ledger: it declares which
fixes change files, it is re-measured rather than hand-edited, and a fix the manifest
records as firing that changes nothing fails the run. Extend it rather than inventing a
second mechanism — a renamed identifier inside a fix's locator string is exactly what it
catches, because the fix then searches for a class the generator never writes.

## What a schema root says that a class cannot

A schema root says more than "here are my properties". It can say *one of these
combinations of fields* (`anyOf`, `oneOf`), or *if this field, then that one* (`if`/`then`,
usually inside a root `allOf`), or *never these two together* (`not`), or
*this field only alongside that one* (`dependencies`).

**A Python class is a flat field set with per-field requiredness, and that cannot express
"these three together, or that one alone."** `create-media-buy-request` declares three ways
to be a media buy — explicit packages with a fixed allocation, explicit packages with a
seller-optimised one, or the execution of a committed proposal — and the first two require
`packages` while the third forbids it. One class with `packages` optional accepts a request
that is none of the three.

Three consequences follow, and the pipeline has an answer for each.

`datamodel-code-generator` renders a root `anyOf`/`oneOf` as a `RootModel` union. That
union *does* enforce the combination, and it is not subclassable or constructible — so the
root's public name stops being a type a consumer can extend, annotate against, or hold.

The pre-generation transforms `flatten_root_object_validation_union` and
`flatten_validation_oneof` therefore delete the root combinator before the generator sees
it, which is what keeps the public name a class. They say so in their own docstrings.

A root `if`/`then` inside an `allOf` was a third case, and the quietest: the admission
predicate that decides which roots carry constraint content read only the root's own keys,
and this bundle puts conditional rules inside `allOf` MEMBERS. It descends into them now —
`core/reporting-obligation` has ten such members, one of which requires
`pending_adjustment_count`.

## Branches that partition become types; rules that do not partition stay checks

That is the whole rule, and which side a root falls on is proved rather than chosen.

| the root's branches | what is generated | population |
|---|---|---|
| partition by requiredness | one class per branch, a shared public base, the base dispatches | **19 declaring classes, 49 branch classes** |
| do not partition | a compiled check on the class | **215 generated modules carry one** |

Nineteen classes declare `_branch_models`: the split roots, the five public response bases
described below, and the canonical create request. 43 of the 215 compiled checks refuse with
a combinator keyword; the rest state a conjunct.

Measured at `3488ff9c3` over 1083 generated modules. Count with
`git grep -l <pattern> <commit> -- src/adcp/types/domains/` rather than `grep -r` over a
working tree: `grep -r` matches `__pycache__/*.pyc` too and returns exactly double. The
earlier figures here (13 roots, 30 branch classes, 203 modules) were correct when written
and are not re-derived on a refresh, so treat any number in this file as "last measured at"
rather than current.

**What the emitted checks DO at validation time** — what a refusal reports, which branch's
errors a caller sees, how a conditional binds, and what is not checked at all — is
[`../schema-modifiers-and-class-checks.md`](../schema-modifiers-and-class-checks.md).

`scripts/schema_constraints.py` is the one reader. It parses a root's constraint content
into `RootConstraints` — a combinator, its `Branch` records, and the conjuncts beside it —
and `partitions_by_requiredness` decides the row. Partitioning means the branches'
required-field sets are pairwise disjoint in a way a document cannot satisfy two of, which
`provably_disjoint` establishes from the branches themselves.

### Parsing is key-consuming

For every node it visits, the reader accounts for every key as exactly one of: **compiled**
into a `Cond`; **non-asserting** by a named frozen set (`title`, `$comment`, `default` and
the rest, each listed with the reason it carries no validation semantics); **declared
behavioral**, which is exactly `x-adcp-validation`; or a **proven restatement** of what the
generated class already declares, where equality is checked rather than assumed.

Anything else raises `UnexpressibleKeywordError` and aborts generation, naming the file, the
JSON pointer and the keyword. There is no list of known-dropped keywords in the module, no
skip lane and no allowlist: a keyword is compiled, classified by a named rule, or fatal.
That is what makes "the model is weaker than its schema, silently" unconstructible for a
root the reader examines rather than merely detected.

### A partitioning root: one class per branch

`scripts/branch_classes.py` renders a base carrying the root's own properties, one subclass
per branch restating only what that branch changes, and a union alias over them:

```python
class WebhookChallengeResponse(BranchDispatchMixin, AdCPBaseModel):
    """What every branch of core/webhook-challenge-response.json shares."""
    challenge: str | None = None
    token: str | None = None

    @classmethod
    def _branch_models(cls) -> tuple[type, ...]:
        return (WebhookChallengeResponseChallenge, WebhookChallengeResponseToken)

    @classmethod
    def _branch_combinator(cls) -> str:
        return 'anyOf'
```

What that makes **unspellable**, which is a different claim from refusing it:

- a field a branch forbids is typed `None` on that branch, so
  `CreateMediaBuyRequestCommittedProposalExecution(packages=[...])` is a type error before
  anything runs. It is declared rather than omitted because the base declares it; omitting
  it would inherit the base's permissive annotation.
- a branch's required field is required on that branch's class, so leaving it out is a
  missing argument rather than a validator's refusal.

Everything in a branch's condition that is **not** requiredness or absence stays a compiled
check on that branch's own class: a `const` one level down, an overlay on an array's items,
a value bound. The refusal then still arrives from the class the caller named.

Branch class names come from the schema, through `scripts/union_branch_names.py`'s ordered
rungs over the same `Branch` records — the branch's `title` first, then a discriminator
`const`, then its full `required` set — failing closed rather than numbering. No identifier
is invented: `anyOf/0` titled "Explicit packages with fixed allocation" becomes
`CreateMediaBuyRequestExplicitPackagesWithFixedAllocation`, and that title also opens the
branch class's docstring, which is where a refusal reads it back.

### The base dispatches, on every validation path

`src/adcp/types/_branch_dispatch.py` holds one `BranchDispatchMixin` for the whole tree. It
gives the base the branches' core schema, so the root's public name resolves to the branch
a document satisfies — called directly, nested as another model's field, validated through
a `TypeAdapter`, or constructed.

All four paths matter, and three of them are invisible to a `model_validate` override:
pydantic validates a nested field and a `TypeAdapter` through the core schema, and most
roots with branches are nested object schemas. `adcp.types.validation.validate_union`, the
public helper the server framework calls on a handler's return value, is a `TypeAdapter`.

**Which class gets the branches' schema is a fact about the class, not a registration:** the
base is the class that DECLARES `_branch_models`. A branch inherits the method and gets its
own shape back; so does a refinement declared elsewhere, such as the canonical creative
layer's narrowing of a single branch. Nothing has to be registered, so nothing can be
forgotten — a new partitioning root works by emitting that method.

Membership is not a sufficient test for it. `cls in branches` admits both "is a branch" and
"is an unrelated subclass of the base", and only the second matters once the schema itself
is being replaced.

The branches subclass the base, so they do not exist while the base's body is compiled. The
hook returns the base's own schema then, and the declaring module's
`model_rebuild(force=True)` after the branches are defined installs the union — a forward
reference resolved in the module that declares it, which is what `model_rebuild` is for.

### A refusal names a JSON Schema keyword

`core/error.json` requires every `issues[]` entry to carry a `keyword` drawn from the JSON
Schema vocabulary, and tells a consumer not to invent one. A document satisfying no branch
fails each branch at FIELD validation, so pydantic reports `missing`, which is outside that
vocabulary — a consumer assembling a conformant envelope would have nothing valid to put in
the field.

So the union carries a wrapping validator that supplies the root's own combinator, and the
sentence it writes is the one a flat root's compiled validator writes:

```
requires at least one of these field groups: Explicit packages with fixed allocation |
Explicit packages with seller-optimized allocation | Committed proposal execution
```

A root moving between the two mechanisms changes how it refuses, not what a reader sees.
The absent field names travel beside the titles in the error context, where a consumer
reads them without parsing the sentence.

A compiled check runs `mode="after"`, so the value it grades is what pydantic PARSED rather
than what the document carried. The `type` predicate therefore accepts the Python types a
JSON string arrives as — `datetime`, `AnyUrl`, `UUID` and the rest — because
`isinstance(value, str)` is false for every `format`-bearing field. They are enumerated
rather than defined by excluding the other five JSON types: a JSON *number* also arrives as
a non-`float`, since pydantic parses one into `Decimal` where the field says so.

### What this does not reach

The admission predicate `root_carries_constraints` admits **533** roots across the bundle's
1625 schema files. **205** of those have a shape the reader can read, and get a compiled
check or a split. Of the rest, **114 are canonical-tree roots with a generated module that
get nothing**: `flat_root_shape` needs a flat class carrying the root's properties, and
these are the roots where the generator emitted numbered classes plus a union alias instead
of merging onto one class.

`account/sync-accounts-response.json` is the shape — a root `oneOf` with two members, root
`properties` empty, `SyncAccountsResponse1` and `SyncAccountsResponse2` generated, and no
validator in the module. Its root `oneOf` and its two `allOf` conjuncts are unenforced.

**92 of the 114 have exactly the shape the five public response bases have** (below), so the
mechanism that covers them is the one already here. This is a gap in the ADMISSION path
rather than a residue set inside the parser — a root that reaches the parser is still
compiled, classified or fatal — but it produces the same outcome the parser's contract
exists to prevent, so it is named here rather than left to turn up one root at a time.

### The five public response bases

Five response names predate the generator emitting a union, and callers construct them. For
those, the post-generation pass restores a constructible base over the numbered classes the
generator wrote and mixes in the same `BranchDispatchMixin`:

```python
class CreateContentStandardsResponse(BranchDispatchMixin, AdcpVersionEnvelope, ProtocolEnvelope):
    """Constructible compatibility base for generated response branches."""
```

`ComplyTestControllerResponse`, `CreateContentStandardsResponse`,
`ListContentStandardsResponse`, `UpdateContentStandardsResponse` and
`SyncGovernanceResponse`. The combinator each names on refusal is read from its root's own
schema; all five declare `oneOf`.

The list of five is hand-written, because "which names stay constructible" is a
compatibility decision rather than a property of the schemas. A row the generated tree no
longer answers raises rather than being stepped over.

## `consolidate_exports.py`

1414 lines, and the step that turns the generated tree into a public surface. It writes
four artifacts: `_generated.py`, the flat arena every public name is re-exported from;
`src/adcp/types/domains/**`, the mirror of the schema layout; `error_details.py`; and
`docs/shared-type-names.md`, the table of every name more than one schema declares.

**The surface is derived, and two guards make it stay that way.** Both are properties of
the tree, so a schema addition satisfies them with no edit, and both fail the build rather
than recording an exception.

Reachability — every generated public class must be bound by some public name:

```python
raise ValueError(
    f"{len(unreachable)} generated public class(es) are reachable under no "
    f"public name:\n{details}\n\n"
    "Every public class in a non-bundled generated module must be importable "
    "from adcp.types.domains.<domain>.<schema>, the mirror of the module that "
    "declares it. A class that is reachable under no name is a model an "
    "adopter cannot construct, which is how the error-details family became "
    "unusable (#1080).\n"
)
```

Unambiguous binding — no two classes may claim one public path inside a domain. Its
message is the instruction for fixing it, and the instruction is not "allow the clash":

```python
raise ValueError(
    f"{len(clashes)} public name(s) are claimed by more than one generated "
    f"class inside one domain:\n{details}\n\n"
    "qualified_public_name() suffixes the module stem, which is unique "
    "inside a domain for every pair that needs it today. A clash means two "
    "modules in one domain now share a stem — widen the suffix to the "
    "domain-relative path, do not drop a binding.\n"
)
```

One hand-maintained table survives: `KNOWN_COLLISIONS`, 13 names handled by qualified
import. `scripts/collision_allowlist.json` is still in the tree and nothing reads it —
`grep -rl collision_allowlist scripts/ src/ tests/` matches only `.pyc` caches — so it is
an orphan of the mechanism the guards replaced, and the thing not to do is revive it. An
allowlist records which names collide without pinning which variant wins; the file's own
generated comment said the names were "knowingly resolved by first-seen / stem-preference
order", and first-seen order is a generator-traversal artifact, so a regeneration that
repointed a name to a different schema node passed the check it was supposed to fail.

The mirror is over a thousand files, so it is written unformatted and black is invoked once
over the whole directory rather than per file.

## How a class gets its name

Four mechanisms. Three are live; one is written, graded, and not wired.

### Module names: path-derived

Each schema's relative path has every part's `-` replaced with `_`, and that becomes the
generated module path: `media-buy/create-media-buy-response.json` →
`media_buy/create_media_buy_response.py`.

Nothing recomputes that mapping to read it back. The generator emits a `#   filename:`
header per module, and `scripts/task_message_lattice.py` reads it:

> The header is the generator's own statement of provenance, which is why it is read
> instead of recomputed: a path convention would have to re-derive the hyphen conversion,
> the directory layout and the root-discovery special cases.

Reuse that function. Any new pass needing schema-to-module provenance should go through it
rather than re-deriving a second answer that could agree while both are wrong.

One trap if you walk the task registry yourself: `index.json` holds **77 task entries under
76 distinct task names**, because `list-creative-formats` is declared in two domains. Key a
registry walk by `(domain, task)` — a dict keyed by task name silently loses one task, and
with it two messages.

### Root class names: `title`-derived

The generator names a root class from the schema's `title`, and the repo ASKS it for that
name rather than restating the rule. `scripts/generated_names.py` owns both of the
generator's naming rules and nothing else:

```python
generated_root_class_name(title)     # a schema ROOT
generated_nested_class_name(title)   # a named NESTED object
```

There are two because the generator applies two: a root goes through
`_resolve_root_model_name`, which title-cases a title that is not already a Python
identifier and keeps one that is, so `Get AdCP Capabilities Request` becomes
`GetAdcpCapabilitiesRequest` while `GetCreativeFeaturesSuccess` is kept verbatim; a nested
object goes through `default_class_name_generator`, which PRESERVES an acronym, so
`VAST Asset` becomes `VASTAsset` and not `VastAsset`. Using either rule in the other's
position is wrong for every acronym.

Use these two functions for any pass that needs a class name from a title, and do not
re-derive a third answer — a derived name that matches nothing is silently green, and a
consumer keying on one (a capture index, a name resolved before the branches exist) has no
module to resolve against.
`tests/test_generated_hierarchy.py::test_the_root_class_name_derived_from_a_title_is_the_name_the_module_declares`
grades the root derivation against the generated tree on every run, over the 946 titled
roots that declare a single class to compare against, with a population floor so a
derivation that stops resolving fails instead of comparing an empty list.

Nothing in the pipeline injects a `title` to steer this.

### `$ref` position: classified, then eliminated

```python
_DEFINITION_POINTER_ROOTS = frozenset({"$defs", "definitions"})


def _is_structural_pointer(fragment: str) -> bool:
    """Report whether a ``$ref`` fragment addresses a structural position."""
    if not fragment.startswith("/"):
        return False
    first_token = fragment[1:].split("/", 1)[0]
    return first_token not in _DEFINITION_POINTER_ROOTS
```

A definition-container pointer names a reusable subschema, and the generator emits one
shared class for it. Every other pointer addresses a structural position —
`/properties/<name>`, `/items`, `/oneOf/<n>` — which has no name, so the generator mints a
one-branch `RootModel` wrapper per referencing site. `inline_structural_pointer_refs` replaces
such a reference with the subschema it selects, so the wrapper is never named at all.

The predicate is on the *selected subschema*, not on the pointer token: inline only what
the generator would wrap — a `type: array` node, a constrained scalar, a bare `$ref` — and
leave a pointer whose target already generates a shared named class, so no import is
replaced by a copy. `core/targeting-input.json` is the worked case: 27 of its 37 fields are
declared as a pointer-or-null union, which otherwise produces
`targeting.AudienceExclude = RootModel[list[str]]` and becomes `list[str] | None` once
resolved.

### Union branch names: schema-derived

A branch class is named from its own schema node, through
`scripts/union_branch_names.py`'s ordered rungs over the reader's `Branch` records: the
branch's `title`, then a discriminator `const`, then its full `required` set. A branch no
rung can name raises rather than taking a position, so a name is never a number.

```python
# core/webhook-challenge-response.json, anyOf/0 titled "Challenge"
WebhookChallengeResponseChallenge
# media-buy/create-media-buy-request.json, anyOf/2 titled "Committed proposal execution"
CreateMediaBuyRequestCommittedProposalExecution
```

Exactly one branch in the pinned bundle is unnameable by every rung, and
`test_union_branch_names.py` names it.

Response branches the generator numbers are a separate case.
`restore_response_variant_aliases` mints `<Base>1 … <Base>N` off a hand-written 22-row
table of `(module path, base class name)` pairs, and carries a hardcoded permutation so an
upstream insertion does not move two existing public names:

```python
if self.base == "BuildCreativeResponse" and len(branches) == 6:
    # Preserve the long-standing public numbering where Response1 is the simple
    # success branch and Response2 is the error branch; append newer schema
    # branches after those.
    branches = [branches[index] for index in (0, 4, 1, 2, 3, 5)]
```

That permutation is the drift channel made visible: a positional name's meaning changed,
and a hand-written reorder was the only way to hold it still. Extending the schema-derived
rule to those names needs two surface-policy decisions, tracked in #1409.

## What keeps a name's meaning stable, and where it does not

The property wanted is: a name refers to the same schema node until the schema changes.

1. **Schema-file order is explicit.** `flatten_schemas` walks
   `sorted(SCHEMAS_DIR.rglob("*.json"))`, with a comment recording that `rglob` otherwise
   follows filesystem insertion order. `test_flatten_schemas_uses_stable_path_order` grades
   it.
2. **Provenance is read, not guessed.** The generator's own `filename:` header, and
   `scripts/generated_names.py` asking the generator for a class name instead of restating
   its rule, mean a pass that renames or marks a class finds it by what produced it.
3. **The staleness check makes instability visible.** `generate_types.py --check` re-runs
   the whole pipeline into a staging tree and compares all three artifacts byte-for-byte.
   Any input-order sensitivity shows up as a dirty regeneration.

**The hole.** The generator walks its input by basename rather than by full path, and
basenames are not unique here: of the 1169 prepared inputs, 1009 basenames are distinct and
**316 inputs share one with another input**. Every such pair ties, and a tie resolves in
readdir order, which differs between filesystems. PR #1374's own notes record what flipping
every tie does — it renumbers anonymous variant classes, `Result9` to `Result13`, in five
measured files, while the semantic aliases in `src/adcp/types/aliases.py` re-point correctly
so the names adopters import stay stable. The fix is to feed the generator an explicit file
list, or to sort its walk by full path.

`--reuse-model` is the other known cross-file-order sensitivity in the flag set, and it is
**unmeasured**. Measure it before adding near-duplicate documents to the tree: generate
once with the schema file list reversed and compare the public surface map. That
measurement is a prerequisite for anything that adds schema variants, which is why the
canonical-model field strip is one declared removal rule rather than a set of variant
documents.

## The guards

The one authority on whether the committed tree matches the pipeline is:

```bash
python scripts/generate_types.py --check     # "✓ Generated types are up to date"
```

It stages a complete regeneration, compares `domains/` and `_generated.py` byte-for-byte
against the checkout, and writes nothing. **Run it yourself
after any pipeline change: `make validate-generated` does not.** That target checks syntax
and the two versioned-stub generators only; PR #1374 is what wires the staleness check into
the Makefile and into CI as a blocking step. Until it lands, nothing automated fails on a
stale generated tree.

`pytest tests/` is the rest of the gate. The modules that grade generation:

| Module | Catches |
|---|---|
| `test_code_generation.py` | a pre-generation transform or a named fix losing its behavior |
| `test_generate_types_safety.py` | the pipeline becoming unsafe: a canonical `$ref` reaching the network, check mode writing to the checkout, a late failure leaving a half-installed tree, a partial `os.replace` not rolling back |
| `test_collision_guard.py` | the derived guards going blind: a declared class the mirror does not carry, a domain namespace splitting a name its own domain declares twice, an aggregate module exporting more than its root |
| `test_export_surface_is_derived.py` | a public name that does not resolve |
| `test_extra_policy.py` | `AdCPBaseModel` drifting off `extra='ignore'`, or an open-`additionalProperties` schema failing to get `extra='allow'` |
| `test_scalar_roots.py` | a `RootModel[str\|int\|float]` surviving, or a scalar root not behaving as its scalar |
| `test_generated_hierarchy.py` | a root `allOf`/`$ref` ceasing to render as inheritance |
| `test_generated_enum_strenum.py` | enums no longer string-comparable or string-hashable |
| `test_literal_discriminator_defaults.py` | single-value `Literal` discriminators losing their default, and the inverse |
| `test_canceled_literal_default.py` | `canceled: Literal[True] = True` returning, which makes an omitted field silently cancel a media buy |
| `test_codegen_invariants.py` | optional boolean constants inventing capabilities or response state |
| `test_codegen_deprecation_contract.py` | JSON Schema `deprecated: true` no longer reaching `Field(deprecated=True)` |
| `test_task_message_lattice.py` | the marker pass failing: a task message unmarked, a marker gaining fields, the envelope residues moving |
| `test_canonical_models_are_subclasses.py` | a canonical model built as a copy rather than a subclass, or a generated validator not reaching the public model |
| `test_canonical_validator_parity.py` | a document the generated class refuses that the public canonical name accepts |
| `test_rootmodel_proxy.py` | a retained `RootModel` union needing `.root` for attribute access |
| `test_import_layering.py` | a non-facade module importing from the generated layer |
| `test_adopter_public_paths.py` | a real downstream seller's generated imports losing their public path |

There is a second, **dormant** staleness checker: `scripts/diff_generated_types.py check`
implements a renumbering-tolerant drift check, and nothing invokes its CLI. Only its
`snapshot` and `format_diff` library API is used, to write `SCHEMA_DELTAS.md`.

## Adding a schema feature

The order below is the order that works, because each step's failure is cheap and names its
cause.

1. **Put the schema in the cache and look at what the generator does with it.** Run
   `python scripts/generate_types.py` and read the new module. Do not reason about the
   rendering; the generator has surprising rules and this is the step that tells you which
   one you have hit.
2. **If the rendering is wrong, prefer a schema transform.** Add it to the chain in
   `flatten_schemas`, operating on the staging copy. A transform is testable against a
   constructed schema dict, is idempotent by obligation, and cannot affect what the SDK
   validates against. Give it an explicit refusal set and a ceiling, like
   `normalize_version_envelope_composition`: raise when a bump makes it do more than
   measured, rather than silently changing shape.
3. **If it cannot be expressed on the schema, add a post-generation fix.** Append it to
   `main()`'s list, respecting the three ordering constraints.
4. **Locate by structure, not by spelling** — see the post-generation section. A fix that
   cannot find its target must raise.
5. **Give it a test in `test_code_generation.py`** that feeds the real function a
   constructed input and asserts the rewrite.
6. **Break the new guard on purpose, once.** Reintroduce the defect it targets on a scratch
   edit and watch it go red, then revert by editing the file back — not with
   `git checkout`, which eats untracked test files. A guard that has never failed is not
   known to grade anything.
7. **Regenerate and check that the tree is idempotent.** `python scripts/generate_types.py
   --check` must say "Generated types are up to date" from the committed state.

If the feature needs a *name* that adopters import, the reachability guard already supplies
one: every public class in a non-bundled generated module is bound at
`adcp.types.domains.<domain>.<schema>`, and the build fails if it is not. Whether the name
also belongs in the flat namespace is the separate question, and it answers itself — if
exactly one schema declares it, it is there; if more than one does, it is in
`docs/shared-type-names.md` and the domain path is the spelling.

## When the generator renders something wrong

1. **Reproduce it outside the pipeline.** Build the smallest schema that shows it and run
   the generator over it with the repo's exact flag set. Several supposed pipeline defects
   have turned out to be two correct passes composing badly, and a minimal repro is what
   separates them.
2. **Find the staging tree.** On failure `generate_types.py` deletes its `.typegen-*`
   directory, so a syntax error in a generated file leaves no evidence. Run the script
   under a wrapper that detaches that directory's finalizer, then `ast.parse` every staged
   module to find the broken one. That is how the `from pydantic import (, model_validator`
   composition defect was located at all.
3. **Ask what the generator does with the whole root, not just the node you changed.** A
   root `properties` is merged into every branch the generator builds, but a root `allOf`
   `$ref` becomes a base only of the branch built *from the root object*: an branch that is a bare
   `$ref` to another schema is generated from that schema, and the root's bases never reach
   it. Moving a field from `properties` into an `allOf` therefore **deletes** it from such an
   branch rather than relocating it — measured on two schemas, where it removed `adcp_version`
   from a submitted branch whose `extra` policy is `forbid`, so a seller echoing that field
   would then have been rejected. `_has_bare_ref_root_arm` is the refusal that resulted.
4. **Measure the whole field set, not the field you expected to change.** That deletion was
   found by comparing full `model_fields` sets before and after, in a step already reported
   as safe on the strength of the one field type that had been examined.
5. **Ask whether the pass is reading a spelling or a structure.** The most common failure
   shape is a pass that matched literal text which another pass legitimately changed. Both
   branches pass their own tests; only the composition breaks.
6. **Check whether the thing you want already exists.** Before adding a helper, grep for
   one — `ensure_pydantic_import`, `_remove_unused_imports`, the deferred-adapter
   primitive, `task_message_lattice`'s provenance map.
7. **Do not reach for a newer generator.** See below.

## The generator pin

```toml
"datamodel-code-generator[http]==0.64.0",     # pyproject.toml:152, dev extra
"datamodel-code-generator==0.64.0",           # pyproject.toml:400, dependency-groups dev
```

Two pin sites, both exact. There is no lock file, and CI installs with
`pip install -e ".[dev]"`, so the dev extra is the pin CI uses. The comment above it
explains why the pin is *exact*: variant numbering shifts between versions, producing diff
churn and breaking generated-code imports that reference specific suffixes.

**Why 0.64.0 specifically — two measured facts, neither sufficient alone.**

**0.72.0 and later cannot generate this tree.** Generation aborts:

```
✗ Generation failed:
$ref file not found: .../.schema_temp/media_buy/enums/proposal_status.json
```

The trigger is a relative `$ref` inside a **multi-branch** `allOf`.
`media-buy/media-buy-commitment-response.json` declares, at
`/oneOf/0/properties/accepted_proposal`, an `allOf` of two branches: a cross-directory `$ref` to
`core/canonical-proposal.json`, and an inline `const` overlay. The referenced document then
refs `../enums/proposal-status.json`, and from 0.72.0 the generator joins that `../`
against the *referring* document's path instead of the *defining* document's directory.
Isolated to the multi-branch form: a single-branch `allOf` and a plain `$ref` both generate
cleanly.

19 releases were probed with a three-file minimal repro. 0.64.0 through 0.71.0 all return 0;
0.72.0, 0.78.0, 0.80.0, 0.82.0 and 0.83.0 all fail. **0.72.0 is the first broken release and
0.71.0 the last working one.** 0.72.0's notes list four breaking changes, none of them this.
Past the 28 distinct mis-resolved refs, a run still fails on an independent
0.72.0-boundary defect in `JsonSchemaObject` validation.

**0.71.0 can generate the tree and is rejected on non-equivalence.** It was actually tried,
and the output is not equivalent — not equivalent in the dangerous direction. It drops const
defaults that were correct at 0.64.0: `status: Literal['completed'] = 'completed'` becomes
`Literal['completed'] | None = None`, and the same for
`canonicalization_contract_version`, `canonicalization_media_type`, and the deprecated
postal flags. Measured on a committed regeneration that touches no schemas at all, so the
whole diff is generator-attributable: 44 removed const defaults, and three
`discriminator='type'` parameters dropped from `core/audience_evidence.py`, which silently
turns three discriminated unions into plain unions. The two `canonicalization_*` drops alone
produce seven reporting-production test failures with
`ValueError: offering must satisfy the complete public contract`.

The direction matters. The bump's own changelog promises to fix optional boolean constants
inventing state — and that correctness win has already been obtained at 0.64.0 by hand, by a
post-generation pass that corrects 44 fields while *preserving* required boolean fields,
string discriminator defaults, and deprecation metadata. 0.71.0 removes precisely what that
pass deliberately kept.

**The consequence for anyone designing in this repository: there is no newer-generator lever
at all**, not even for a future defect whose changelog claims a fix. Anything the type tree
needs must be reachable through a schema transform, a post-generation pass, or hand-written
source that inherits from generated output. That is not a deferred task; it is the
constraint every layer above is built around.

## What this architecture refuses

- **Refused: any generator bump.** 0.64.0 is the generator, indefinitely, for the two
  measured reasons above. An earlier recommendation called 0.71.0 "worth taking separately"
  on changelog reasoning without an equivalence measurement; that recommendation is
  withdrawn, and this entry exists so it is not proposed a third time from the same
  changelog.
- **Refused: a post-hoc deduplication of any emitted list.** A duplicate `__all__` entry is
  a symptom of an emitter that appends instead of unioning. De-duplicating downstream
  preserves the defect and hides its signal; emit sorted sets at the point of emission,
  where the class of defect becomes unexpressible rather than fixed.
- **Refused: a second mechanism for reaching the public classes.** When an injected
  validator failed to reach the public canonical models, the fix was to delete the runtime
  copies so inheritance carries it. Carrying validators onto the copies as well would
  double the mechanism that caused the gap: every future validator would need the second
  carry, and the day one is forgotten the gap reopens silently.
- **Refused: a derived type stub.** Generating a `.pyi` from `model_fields` repairs a ledger
  instead of deleting it — two artifacts, one derived from the other, drifting between
  regenerations. Real source makes the stub unnecessary.
- **Refused: growing a hand-maintained allowlist.** The collision allowlist's reader is
  gone; `KNOWN_COLLISIONS` is the one table left, and the derived guards above are what
  replaced the rest. Nothing should add a third — including for branch naming, which fails
  closed and takes a one-line upstream title change rather than a ledger of tolerated
  numbers.
- **Refused: a positional name on the public surface.** Numbered classes may remain as
  module-internal helpers reached only through a field annotation, where the name is
  cosmetic. A public binding is the contract — 38 of the 923 public names are
  numeric-suffixed today — and renaming all 1037 numbered internal definitions as a gate is
  refused for the opposite reason: it converts a surface guarantee into a beautification
  project.
- **Refused: inventing composition the schema lacks.** No `required: [status]` added to the
  ten responses that do not compose the protocol envelope; no version fields added to
  `validate-input-request`; no marker on a request file the task registry does not name.
  Residues are pinned shrink-only and named upstream — degraded honestly, never papered.
