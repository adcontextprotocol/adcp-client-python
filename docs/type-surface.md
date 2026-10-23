# The type surface: what is where

Every class in `adcp.types` is derived from one node of one pinned JSON Schema. This
document says where a class lives, which spelling to import it by, and what you can rely
on from a type before you know which tool produced it.

For how to *extend* a generated type — which base to subclass, `Field(exclude=True)`,
`@model_serializer` — see [extending-types.md](extending-types.md). This document covers
layout and guarantees; that one covers subclassing. For how the tree is produced, see
[design/type-generation.md](design/type-generation.md).

## Where to import from

### The flat namespace

```python
from adcp.types import CreateMediaBuyRequest, GetProductsResponse, ProtocolEnvelope
```

`adcp.types.__all__` carries 925 entries binding 923 distinct names. This is the spelling
to use, and it resolves for every name exactly one schema declares.

### The domain tree, for names several schemas declare

AdCP names an inline object after the property that holds it, so several schemas
legitimately declare a class called `Creative` or `QuerySummary`. The flat namespace binds
one of them per name; `adcp.types.domains` follows the bundle's own layout — 26 domain
roots, one module per schema file — so the import path says which one you mean:

```python
from adcp.types.domains.creative.list_creatives_response import Creative
from adcp.types.domains.core.product import Product
```

[shared-type-names.md](shared-type-names.md) is the generated table of every such name and
the modules that declare it: 488 names across 1532 variants. Reach for the domain path when
that table lists your name, or when you want a spelling that cannot be repointed by a name
becoming ambiguous later.

`adcp.types` also exports curated prefixed aliases for the variants adopters have needed —
`ListCreativesCreative`, `DeliveryCreative`, and the rest — and
[extending-types.md](extending-types.md) covers choosing between those and the domain path
when you are subclassing. This document does not repeat that guidance.

### The curated topic modules

```python
from adcp.types.protocol import ProtocolEnvelope, AdcpVersionEnvelope
from adcp.types.legacy import LegacyCreative
```

`adcp.types.core`, `creative`, `media_buy`, `signals`, `protocol` and `legacy` are
hand-picked partial views. They are stable, but they are narrower than the domain tree
and are not derived from the bundle, so a new schema does not appear in one until someone
adds it.

### One tree: the definition site IS the public address

`adcp.types.domains` is not a mirror of a private tree. It is where
`datamodel-code-generator` writes, so the module that DEFINES a class is the module an
adopter imports it from. There is no second tree, no copying step, and nothing that can
drift between them — `scripts/generate_types.py` points `--output` straight at
`src/adcp/types/domains/`, and the consolidation step writes only the namespaces derived
from it (`_generated.py`, each `<domain>/__init__.py`, `error_details.py`).

Every other spelling resolves to the same class object. A public name is a re-export of
the class its generated module defines, never a second class built from the first:

```python
from adcp.types import Product
from adcp.types.domains.core.product import Product as Defined

assert Product is Defined
assert Product.__module__ == "adcp.types.domains.core.product"
```

`tests/test_export_surface_is_derived.py` holds this across the surface: every public name
must be identical to the attribute of the module that defines it, and every generated
public class must be reachable under some public name. A runtime copy cannot pass the
identity check, because `create_model` stamps the calling module onto what it builds.

## What is deliberately not public

`adcp.types._generated` is internal. It binds a bare type name that several generated
modules define to a single winner, chosen by module sort order — so a schema addition can
repoint a name an adopter already imports, and the only trace is a line in a regenerated
file. A domain path cannot move that way: it names the declaring schema.

What a domain path does not protect you from is a NUMBERED class name. Codegen numbers
anonymous variant classes by traversal order, so `Assets162` can name a different shape
after the next regeneration whatever path you reach it by. `aliases.py` exists to give
those a stable spelling, and `adcp.types` is where you import it from.

That is not hypothetical. `_generated` rebinds the whole `AuthorizedAgents*` window at
runtime — `AuthorizedAgents = AuthorizedAgents1`, `AuthorizedAgents1 = AuthorizedAgents2`,
and so on — to preserve historical variant numbering, while mypy keeps the pre-rebind
declarations. Aliasing the shifted names therefore bound each of the six authorization
aliases to the runtime class it names but to the *static type of its neighbour*, which made
every documented constructor call fail `mypy --strict`. `aliases.py` now imports the raw
variants to keep the runtime object and the static type identical, and records the whole
episode at the import.

The ban is mechanical. `tests/test_import_layering.py` walks the AST of every non-facade
module and fails on an import whose module starts with the one forbidden prefix:

```python
_FORBIDDEN_PREFIXES = ("adcp.types._generated",)
```

`ast.walk` sees `if TYPE_CHECKING:` blocks and function-local imports too, so no spelling
slips past it. `test_generated_tree_is_not_forbidden` in the same file asserts the tuple
stays that short: re-adding `adcp.types.domains` would forbid the definition site.

## `AdcpRequest` and `AdcpResponse`: what a message is, before you know the tool

```python
from adcp.types.base import AdcpRequest, AdcpResponse
```

Every request and response message of every task in the pinned bundle's task registry
descends from one of these two — 154 of 154 messages, over 77 tasks. They are the answer to
"is this an AdCP request?", which no type in this SDK used to be able to give: the name that
should mean it, `adcp.types.Request`, resolves to a body fragment of one tool.

They are plain classes. They are not `BaseModel` subclasses, they declare no fields, and
that is deliberate: a field on the marker would put `adcp_version` on
`ValidateInputRequest`, whose schema declares no version fields, and inventing a spec field
to satisfy a type is worse than the gap it fills. So `AdcpRequest` cannot be instantiated or
validated against directly — it is an interface. Every class carrying it is a pydantic model
by construction of the pass that inserts it.

### What you can do with one

| Method | On | Returns |
|---|---|---|
| `get_account()` | `AdcpRequest` | the account reference this request names, or `None` |
| `get_idempotency_key()` | `AdcpRequest` | the at-most-once key, or `None` |
| `get_context()` | both | the buyer's context object, or `None` |
| `get_push_notification_config()` | `AdcpRequest` | the webhook config, or `None` |
| `get_adcp_version()` / `get_adcp_major_version()` | `AdcpRequest` | the version pins, or `None` |
| `get_status()` | `AdcpResponse` | the task status |
| `get_task_id()` | `AdcpResponse` | the async task id, or `None` |
| `get_adcp_error()` | `AdcpResponse` | a typed `Error`, or `None` |
| `get_message()` / `get_replayed()` | `AdcpResponse` | the envelope's message and replay marker |

`None` means *this tool's schema declares no such field*, not *the field was empty*. Of the
87 request schemas, 80 declare `context`, 49 declare `account`, 43 declare
`idempotency_key` and 19 declare `push_notification_config` — the axes are genuinely
ragged, so an accessor that answers `None` is answering correctly.

That is enough to write a transport boundary once, for all 77 tools: validate, resolve the
account, decide at-most-once, negotiate the version, echo the context.

### Using descent as a refusal

If you maintain a tool registry and want to refuse a hand-written model that merely looks
like a spec type, use **both** predicates:

```python
from adcp.types import AdcpVersionEnvelope
from adcp.types.base import AdcpRequest

if not (issubclass(model, AdcpRequest) and issubclass(model, AdcpVersionEnvelope)):
    raise TypeError("a tool request model must be an SDK task-request type")
```

Neither alone is sufficient, and this was settled by building the forgeries rather than
reasoning about them. `AdcpRequest` is field-less, so inheriting it grants nothing — a class
can declare it as a base and invent whatever fields it likes. `AdcpVersionEnvelope` refuses
that, because inheriting it is what supplies the two spec fields; but it accepts a model
that composes the envelope without being any task's message. The conjunction refuses all
three forgeries; either one alone refuses two.

One limit worth knowing: descent from `AdcpRequest` means the generation pass put the class
in a task's request position. It does that for all 77, and a forger can also do it for one.
The conjunction narrows that; it does not close it.

## Envelope fields and payload fields

The spec settles that these are not containers. `core/version-envelope.json` says the
envelope "is part of the payload itself", and `core/protocol-envelope.json` says `payload`
is "a documentary construct — NOT a required wire field": on MCP and REST, envelope fields
and body fields are siblings at the root. So fields stay flat, exactly as on the wire, and
which stratum a field belongs to is answered by **which ancestor declared it**:

```python
wire = response.model_dump()
envelope = {k: wire[k] for k in response.protocol_fields() if k in wire}
body     = {k: wire[k] for k in response.payload_fields() if k in wire}
```

Three classifiers, derived once from the MRO — there is no per-class table to drift:

- `version_fields()` — the intersection with `AdcpVersionEnvelope.model_fields`, empty when
  the class has no envelope ancestry.
- `protocol_fields()` — the intersection with `ProtocolEnvelope.model_fields`.
- `payload_fields()` — `model_fields` minus the other two.

They partition `model_fields`, which is pinned by a test. The tie-breaks follow the
envelopes' own text: `context` is protocol, because the spec says the envelope declaration
is authoritative where a body-level mirror also exists; a `status` narrowed to a `Literal`
on a payload branch is still protocol; `errors` is payload and `adcp_error` protocol, because
the spec says the two must be treated as distinct fields by name. `adcp_version` reports as
version rather than payload even though the wire calls it payload, so that a transport
composing a `DataPart` or an audit record gets tool arguments only — union the two sets if
you want payload-including-version.

### Where the ancestry is incomplete

| | coverage over 77 tasks |
|---|---|
| requests typed `AdcpRequest` | 77 / 77 |
| responses typed `AdcpResponse` | 77 / 77 |
| requests with `AdcpVersionEnvelope` | 76 / 77 |
| responses with `AdcpVersionEnvelope` | 70 / 77 |
| responses with `ProtocolEnvelope` | 67 / 77 |

Counted per branch: a union-rooted message counts only when *every* class it can validate into
has the ancestry, because an branch is what a buyer actually receives.

The residues are the spec's, not the SDK's, and they are visible rather than papered over.
`creative/validate-input-request.json` declares no version fields at all, so the SDK does
not invent them; its `version_fields()` is empty while `get_adcp_version()` still answers,
because the accessor reads the instance, not the MRO. The five compact media-buy responses
compose an envelope-free shared response, and the two proposals responses carry a submitted
branch that declares no version fields. Ten task responses do not compose the protocol
envelope despite its own "REQUIRED on every task response envelope" claim.

Each residue is pinned by equality in the SDK's guard, so a schema that gains the ancestry
upstream fails the guard until its pin is removed — the state cannot silently go stale in
either direction. `scripts/task_message_lattice.py` prints the whole table and names every
residue schema; run it against a checkout or an installed wheel.

## Response variations

A task response whose schema root is a union generates one class per branch and a plain
`TypeAlias` over them. There is no `RootModel` and no `.root`:

```python
CreateMediaBuyResponse: TypeAlias = (
    CreateMediaBuyResponse1 | CreateMediaBuyResponse2 | CreateMediaBuyResponse3
)
```

Dispatch by `isinstance` or structural pattern matching. mypy narrows a closed `TypeAlias`
union through `match`, so the handling is exhaustive:

```python
match response:
    case CreateMediaBuyResponse1() as ok:
        persist(ok.media_buy_id, ok.packages)
    case CreateMediaBuyResponse3() as submitted:
        schedule_poll(submitted.task_id)
    case CreateMediaBuyResponse2() as err:
        raise BuyRejected(err.errors)
```

To validate a union root from raw bytes, use `TypeAdapter` on the alias — the alias is not
a class, so it has no `model_validate`:

```python
from pydantic import TypeAdapter
response = TypeAdapter(CreateMediaBuyResponse).validate_python(raw)
```

**Five response names are classes rather than aliases**, because callers construct them:
`ComplyTestControllerResponse`, `CreateContentStandardsResponse`,
`ListContentStandardsResponse`, `UpdateContentStandardsResponse` and
`SyncGovernanceResponse`. Each is a base over its numbered branches, so for those five the
public name has `model_validate`, it returns the branch the document satisfies on every
validation path, and `isinstance(branch, Base)` holds. Thirteen schema ROOTS are classes for
the same reason — see
[extending-types.md](extending-types.md#a-split-root-is-a-class-and-its-branches-subclass-it)
for which, and what that means for subclassing.

Validation resolves branches left to right. Where the schema gives an branch a discriminating
`const`, that becomes a defaulted `Literal` field and pydantic takes it as a fast path. But
most branches have no such tag: of the 58 numbered branch classes across the 22 union-rooted
response modules, 47 declare no `status` field of their own and are distinguished by their
required sets instead — `create-media-buy-response.json`'s error branch is identified by
requiring `errors` and nothing else. A synthetic discriminator field would make every union
pydantic-discriminated, and is refused: it would be either a non-spec field on the wire or a
serialization-excluded phantom that still shows up in `model_fields`, the extra policy and
the idempotency hash.

### The numbered names, and what replaces them

`CreateMediaBuyResponse1` is a positional name, and a positional name's meaning can change
without the thing it names changing: insert an branch into a `oneOf` upstream and
`CreateMediaBuyResponse2` becomes a different branch.

The schema already carries better names — `create-media-buy-response.json` titles its branches
`CreateMediaBuySuccess`, `CreateMediaBuyError` and `CreateMediaBuySubmitted` — and
`aliases.py` hand-transcribes most of them back today:

```python
from adcp.types import CreateMediaBuySuccessResponse   # == CreateMediaBuyResponse1
```

A rule that derives each branch's name from its own schema node — the branch's `title`,
then a discriminator `const`, then its full `required` set, failing closed rather than
numbering — is wired for the thirteen schema roots whose branches partition by
requiredness. `CreateMediaBuyRequestCommittedProposalExecution` is a name from that rule.

For the numbered RESPONSE classes it is not applied, and 39 numeric-suffixed names are
still on the flat surface. Prefer the semantic alias over the numbered class where one
exists, and expect the numbered names to be removed in a future major with a deprecation
shim and a migration table. Writing new code against `CreateMediaBuyResponse2` is writing
against the drift channel; the two surface-policy decisions wiring the rule for these names
needs are tracked in #1409.

See [design/type-generation.md](design/type-generation.md#union-branch-names-schema-derived)
for the rule itself.

## Scalars, unions and composition, in one paragraph each

These are the shapes a consumer meets most often. [extending-types.md](extending-types.md)
covers each in full, including the subclassing rules.

- **A scalar schema root is a plain scalar.** `PropertyTag` is a `str` subclass carrying the
  schema's constraints, not a `RootModel` wrapper. `tag == "sports"` is `True`.
- **A nested object union is a type alias.** Construct the branch you mean; use
  `TypeAdapter` when the branch is not known in advance. A root that composes rather than
  naming a single scalar keeps its `RootModel`; `isinstance(x, RootModel)` distinguishes
  them.
- **A schema root whose branches PARTITION is a class.** The public name is a base over one
  class per branch; `model_validate` through it returns the branch the document satisfies,
  and a document satisfying no branch is refused naming the root's JSON Schema keyword.
  Thirteen roots, and five response names besides.
- **A root `allOf` plus `$ref` is inheritance.** `GetProductsRequest` *is* an
  `AdcpVersionEnvelope`; the envelope's fields are not copied onto it. So an `isinstance`
  check or a generic boundary can bind to the composed base.
- **A canonical creative model is a subclass of the generated class it refines**, not a copy
  of its fields. Every validator the pipeline injects into the generated class therefore
  applies to the public one, and mypy sees every inherited field. One consequence to know:
  a public canonical model refuses what its generated source refuses, so
  `CreativeManifest` requires one of `format_id` or `format_kind`.

## What this surface refuses to do

Each of these has been proposed, and the reason it was refused is the reason not to propose
it again.

- **No post-hoc `__all__` deduplication.** Duplicate entries are a symptom of an emitter
  that appends instead of unioning. De-duplicating downstream preserves the defect and
  hides its signal; the emitter should write sets instead. Two duplicates remain today,
  `WholesaleFeedEvent` and `WholesaleFeedWebhook`, and they are the emitter's to fix.
- **No second mechanism to reach the public classes.** When an injected validator failed to
  reach the public canonical models, the fix was to delete the runtime copy so inheritance
  carries it — not to carry validators onto the copies as well. Doubling the mechanism that
  caused a gap means every future validator needs the second carry, and the day one is
  forgotten the gap reopens silently.
- **No derived type stub.** A `.pyi` generated from `model_fields` repairs a ledger instead
  of deleting it: two artifacts, one derived from the other, drifting between
  regenerations. Real source makes the stub unnecessary, and the stub that existed hid 715
  inherited fields across 31 classes, required four of them, and invented one.
- **No permanent numbered aliases.** One major of deprecation shim for the positional
  names, then they are gone. A positional name kept alive indefinitely is the drift channel
  reopened.
- **No generator version bump.** The generator is pinned, and the pin is a constraint the
  whole design is built around rather than a task nobody got to. Anything the surface needs
  is reachable through a schema transform, a post-generation pass, or hand-written source
  that inherits from generated output. [design/type-generation.md](design/type-generation.md)
  has the measurements.
