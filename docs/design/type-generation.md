# How the type tree is generated

Status: current. Generator pinned at `datamodel-code-generator==0.64.0`.
Schema pin: `src/adcp/ADCP_VERSION` = `3.2.1`, which `resolve_bundle_key` collapses to the
bundle `schemas/cache/3.2`.

This document is for whoever has to change the pipeline. It covers every step in order, how
a class gets its name, what the guards are, how to add a schema feature, and what to do
when the generator renders something wrong.

For the resulting surface from a consumer's side, see [../type-surface.md](../type-surface.md).
For subclassing a generated type, see [../extending-types.md](../extending-types.md).

## The shape of the thing

1053 non-bundled schema files become 1056 generated model modules, which become one flat
namespace of 923 names and a 1084-module public mirror under `adcp.types.domains`. Nothing
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
	$(PYTHON) scripts/generate_ergonomic_coercion.py
```

| Step | Consumes | Produces | Network |
|---|---|---|---|
| `sync_schemas.py` | the published schema bundle | `schemas/cache/<bundle>/**` | **yes — the only network step** |
| `fix_schema_refs.py` | the cache | the cache, with absolute `$ref` URLs rewritten to relative file paths | no |
| `bundle_schemas.py` | the cache | `src/adcp/_schemas/**`, the schemas the wheel ships | no |
| `generate_versioned_stubs.py` | the cache | `src/adcp/types/v30.pyi`, `v31.pyi`, `v32.pyi` | no |
| `generate_versioned_bases.py` | the cache | `src/adcp/types/versioned_bases/**` | no |
| `generate_types.py` | the cache | `domains/**`, `_generated.py`, `_ergonomic.py` | no |
| `consolidate_exports.py` | the generated tree | `_generated.py`, each `domains/<domain>/__init__.py`, `domains/__init__.py`, `error_details.py`, `docs/shared-type-names.md` | no |
| `generate_ergonomic_coercion.py` | the generated tree | `_ergonomic.py` | no |

`sync_schemas.py` is the only step that touches the network, and skipping it regenerates
against the committed cache. That is the normal inner loop:

```bash
python scripts/generate_types.py            # regenerate from the committed cache
python scripts/generate_types.py --check     # verify without writing anything
```

Note that `generate_types.py` already runs `consolidate_exports.py` and
`generate_ergonomic_coercion.py` as subprocesses against its staging tree and installs
their output, so the Makefile target runs both of them a second time. Harmless, but do not
read the Makefile as the statement of what depends on what.

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
9. Run `consolidate_exports.py`, then `generate_ergonomic_coercion.py`, against the staging
   tree. The first writes the domain roots into the tree generated in step 4.
10. `restore_unchanged_files` — restore byte-for-byte any file whose only change is a
    timestamp header, so a no-op regeneration produces no diff. Once, over the whole tree,
    after the roots are in it.
11. Compare the artifacts against the checkout — `domains/`, `_generated.py`,
    `_ergonomic.py`, `error_details.py`, `docs/shared-type-names.md`. Under `--check`,
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

Each of these operates on the staging copy only. The cached bundle, the schemas the wheel
ships and any direct-from-schema validation are untouched, which is what keeps the
transforms from changing what the SDK validates against.

One more transform runs once over the whole staged tree after every file is written,
because it has to resolve `$ref`s into the documents the generator will read:
`mark_no_explicit_null_properties`. JSON Schema separates two things the generator
conflates — a property outside `required` may be *absent*, and `null` is a valid *value*
only where the declared type admits it — and the generator renders both as
`T | None = None`, so the model accepted `{"creatives": null}` where the bundled validator
reports a `type` error (#1547). The pass grades every optional property with the same
`jsonschema` validator the bundled arm uses (`is_valid(None)` on the property node, refs
resolved against the staged tree), and marks the ones that refuse `null` with
`x-adcp-no-explicit-null: true`. The generator carries the mark into
`Field(json_schema_extra=...)` via `--field-extra-keys`, and the first post-generation fix
turns it into the `NoExplicitNull` annotation (below). The key exists only in the staged
input; the shipped schemas never carry it. A property whose schema admits `null` — a type
list, an `anyOf` with a null arm, a `$ref` to a nullable schema — is not marked and keeps
accepting it.

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

`scripts/post_generate_fixes.py` is 7367 lines holding 130 module-level functions: 72 fix
entrypoints, built as one ordered list in `main()` and run in list order, plus 58 helpers.

Four ordering constraints are commented in the list itself and must be preserved:

- `point_omittable_fields_at_no_explicit_null` runs first. It removes the
  `json_schema_extra={'x-adcp-no-explicit-null': True}` keyword the generator emitted for
  every marked property and appends `adcp.types.base.NoExplicitNull` to the field's
  `Annotated` metadata, re-rendering the field the way black lays it out, so every later
  fix reads exactly the text the generator produced on the pinned flags plus that one
  element. The annotation stays `T | None = None`: omitted still reads as `None`, a JSON
  `null` is refused (with the type's own error; a Python `None` stays the spelling of absence
  unless `ADCP_STRICT_VALIDATION` is set), and the `null` arm leaves the published JSON
  Schema. A marked field whose annotation already refuses `None` (a
  `Literal` with a default, a field an `allOf` arm made required) only loses the keyword.
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

There is no per-fix effect ledger today, so a fix whose locator goes stale stops working
silently until a test notices. PR #1374 adds one — a declared manifest of which fixes
change files, re-measured rather than hand-edited, checked on every run — and that is the
mechanism to extend rather than inventing a second one.

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

The generator names a root class from the schema's `title`. The repo reproduces that rule
only to read it back:

```python
def mangle_schema_title(title: str) -> str:
    return "".join(word.capitalize() for word in re.split(r"[^0-9a-zA-Z]+", title) if word)
```

Measured against all 154 task-message schemas at the pin, this reproduces the generated
root class or union-alias name for 154 of 154. Note `str.capitalize`, which lower-cases the
tail — that is the generator's behavior, and it is the part a filename-derived guess gets
wrong in both directions: `Get AdCP Capabilities Request` becomes
`GetAdcpCapabilitiesRequest`, and `list-creative-formats-request.json` does *not* become
`ListCreativeFormatsRequest` because its title names the agent variant.

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
one-arm `RootModel` wrapper per referencing site. `inline_structural_pointer_refs` replaces
such a reference with the subschema it selects, so the wrapper is never named at all.

The predicate is on the *selected subschema*, not on the pointer token: inline only what
the generator would wrap — a `type: array` node, a constrained scalar, a bare `$ref` — and
leave a pointer whose target already generates a shared named class, so no import is
replaced by a copy. `core/targeting-input.json` is the worked case: 27 of its 37 fields are
declared as a pointer-or-null union, which otherwise produces
`targeting.AudienceExclude = RootModel[list[str]]` and becomes `list[str] | None` once
resolved.

### Union arm names: positional today

Root-level union arms are named by position. `restore_response_variant_aliases` mints
them off a hand-written 22-row table of `(module path, base class name)` pairs:

```python
class_names = [f"{self.base}{index}" for index in range(1, len(arms) + 1)]
```

and carries a hardcoded permutation so that an upstream insertion did not move two existing
public names:

```python
if self.base == "BuildCreativeResponse" and len(arms) == 6:
    # Preserve the long-standing public numbering where Response1 is the simple
    # success arm and Response2 is the error arm; append newer schema branches after those.
    arms = [arms[index] for index in (0, 4, 1, 2, 3, 5)]
```

That permutation is the drift channel made visible: a positional name's meaning changed,
and a hand-written reorder was the only way to hold it still.

A rule that names an arm from its schema node (`title`, then discriminator `const`,
then full `required` set, failing closed instead of numbering) is designed and measured
against the pinned bundle but not wired in; it is tracked, with the two surface-policy
decisions wiring it needs, in #1409.

## What keeps a name's meaning stable, and where it does not

The property wanted is: a name refers to the same schema node until the schema changes.

1. **Schema-file order is explicit.** `flatten_schemas` walks
   `sorted(SCHEMAS_DIR.rglob("*.json"))`, with a comment recording that `rglob` otherwise
   follows filesystem insertion order. `test_flatten_schemas_uses_stable_path_order` grades
   it.
2. **Provenance is read, not guessed.** The generator's own `filename:` header, and
   `mangle_schema_title` reproducing the generator's title rule for 154 of 154 task
   messages, mean a pass that renames or marks a class finds it by what produced it.
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

It stages a complete regeneration, compares `domains/`, `_generated.py` and
`_ergonomic.py` byte-for-byte against the checkout, and writes nothing. **Run it yourself
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
   root `properties` is merged into every arm the generator builds, but a root `allOf`
   `$ref` becomes a base only of the arm built *from the root object*: an arm that is a bare
   `$ref` to another schema is generated from that schema, and the root's bases never reach
   it. Moving a field from `properties` into an `allOf` therefore **deletes** it from such an
   arm rather than relocating it — measured on two schemas, where it removed `adcp_version`
   from a submitted arm whose `extra` policy is `forbid`, so a seller echoing that field
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

The trigger is a relative `$ref` inside a **multi-arm** `allOf`.
`media-buy/media-buy-commitment-response.json` declares, at
`/oneOf/0/properties/accepted_proposal`, an `allOf` of two arms: a cross-directory `$ref` to
`core/canonical-proposal.json`, and an inline `const` overlay. The referenced document then
refs `../enums/proposal-status.json`, and from 0.72.0 the generator joins that `../`
against the *referring* document's path instead of the *defining* document's directory.
Isolated to the multi-arm form: a single-arm `allOf` and a plain `$ref` both generate
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
  replaced the rest. Nothing should add a third — including for arm naming, which fails
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
