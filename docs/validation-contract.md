# Validation contract

The bundled canonical JSON Schemas define AdCP wire validity. Generated
Pydantic models are structural application types: they provide typed fields,
common constraints, and wire-shaped serialization, but they do not promise to
implement every JSON Schema conditional or protocol-specific
`x-adcp-validation` rule.

This distinction matters for cross-field rules. A generated model may accept
an object that has the right individual field types while the canonical schema
rejects the combination through `if` / `then` / `else`, `dependencies`, or
another composition keyword. `x-adcp-validation` carries behavioral rules that
neither Pydantic nor a standard JSON Schema validator interprets generically;
the SDK implements security- and commitment-critical instances explicitly.

Absence and `null` are two different things, and the models keep them apart
the way the schemas do. A property outside `required` may be omitted, and
reads back as `None`; an explicit JSON `null` is a value, valid only where the
schema's type admits it. The generator marks every optional property whose
schema refuses `null` with `adcp.types.base.NoExplicitNull`: a JSON document
carrying `null` there is refused (`model_validate_json`, with the type's own
error, e.g. `list_type`) and `model_json_schema()` publishes no null arm, while
the annotation stays `T | None = None`. A property whose schema lists `null`
(`"type": ["string", "null"]`, an `anyOf` with a null arm) keeps accepting it.

On the Python side `None` remains the spelling of "not supplied":
`Model(field=None)` and `model_validate` on a dict accept it, it reads back as
`None`, and `model_dump()` (which defaults to `exclude_none=True`) leaves it
off the wire — the same document an omitted property produces. Set
`ADCP_STRICT_VALIDATION=1` (the switch that also forbids unknown fields) to
refuse a Python `None` as well, for suites that want to catch it.

The omission is the field's own, not the dump call's. A marked field carries
`exclude_if` (pydantic-core level), so a `None` there is left off whoever
drives the serialization: an AdCP model nested in an ordinary Pydantic
parent — a direct field, a list, a dict, a `RootModel` over an AdCP union —
dumps exactly as it does on its own, and `model_validate_json(model_dump_json())`
round-trips from any parent (#1587). That holds under an explicit
`exclude_none=False` too: `null` is never a valid value for a marked property,
so there is nothing to keep. A property whose schema admits `null` is not
marked and still emits `null` under `exclude_none=False`, from a parent or
directly. One consequence for subclasses: a `field_serializer` that substitutes
a default for `None` no longer runs for a `None` value on a marked field,
because `exclude_if` drops the field before the serializer is reached.

## Wire round trip

AdCP binds bytes to several objects. `core/audience-evidence.json` defines
`content_digest` as SHA-256 over the RFC 8785 (JCS) form of the entry *as
declared*; `core/canonical-proposal.json` binds `terms_digest` to
`commercial_terms` the same way; `core/rights-constraint.json`,
`core/creative-representation-set.json`, `media-buy/acceptance-policy-profile.json`,
the attestation schemas and the governance check carry their own; a `context`
is "echoed unchanged". A model that changes a value between parse and dump
breaks every one of those contracts without failing anything, and until 9.1
the SDK had no invariant saying it must not.

### Root cause

The generator maps each JSON Schema `format` to the pydantic type that
*parses* it best — `AnyUrl`, `AwareDatetime`, `UUID`, `EmailStr` — and those
types re-serialize the value they parsed rather than the text they were given:
a bare origin gains a trailing slash, `+00:00` becomes `Z`, `.000` is dropped,
a UUID is lower-cased, an email's domain is lower-cased. The runtime then adds
rewrites of its own: `model_dump` emits every field, so a schema `default` the
generator wrote into the field appears in the output though the document
omitted it; `exclude_none` (the default) drops an explicit `null` the schema
admits; `by_alias` is off, so `$schema` dumps as `field_schema`; and
`extra='ignore'` drops a property the pinned model does not declare. Each of
these is reasonable for an application type and wrong for a document whose
bytes are an identity.

Four earlier fixes were point patches of this one gap: #1384 typed one field
(`agent_url`) as a wire string because an option ID hashed it; #1449 reported
that dumping a validated `ProductFormatDeclaration` "adds every branch default
to the published format"; #1587 made nested models leave unset fields off from
any parent; #1575 kept a `ContextObject`'s null keys. #1610 found the next
instance in `AudienceEvidence`.

### The invariant

**A value the model did not change is dumped as it was received.** Two
mechanisms carry it, and a conformance suite measures it:

- **Wire-preserving scalar types.** `scripts/post_generate_fixes.py`
  (`preserve_wire_text_on_normalizing_fields`) points every generated
  `format: date-time`, `uri`, `uuid` and `email` field at
  `adcp.types._wire` — `WireDatetime`, `WireAnyUrl`, `WireUuid`, `WireEmail`.
  Each validates exactly as the pydantic type did, keeps the attribute API
  (`.year`, `.tzinfo`, `.host`, `.hex`), compares and hashes as the same
  value, advertises the same JSON Schema, and in JSON mode serializes the text
  it was parsed from — when that text passes the strict wire check the bundled
  validator applies (`adcp._formats`: RFC 3339 for a date-time, RFC 3986
  syntax with an agreeing host for a URI, the hyphenated 8-4-4-4-12 form for a
  UUID, a trimmed address for an email). A string pydantic's lenient parser
  accepts but the schema rejects (`"2026-10-01 00:00:00+00:00"`,
  `"https:x.example"`, `"{...}"`) is normalized, as it always was. A
  `datetime`, `AnyUrl` or `UUID` *object* passed to a constructor has no wire
  text and serializes as before: RFC 3339 with `Z` for UTC and `±HH:MM`
  otherwise, fractional seconds only when non-zero; pydantic's normalized URL
  string; a lower-case hyphenated UUID. Every such field is covered, not only the
  digest-covered ones: a shared class (`AttestationReference`, `Provenance`,
  `Targeting`) sits inside digested and undigested objects alike, so a
  per-subtree rule would have to split classes. The rule covers the generated
  AdCP schema types (`adcp.types.domains`); `adcp.types.registry`, the
  OpenAPI-generated registry client, keeps pydantic's `AnyUrl` and
  `AwareDatetime` — no registry object is digest-covered.
- **An explicit "as received" dump.** `AdCPBaseModel.model_dump_as_received()`
  is `model_dump(mode="json", exclude_unset=True, exclude_none=False,
  by_alias=True)`: it leaves off every field the document did not set (where
  a generated default would otherwise appear), keeps an explicit `null` the
  schema admits, and writes wire names. Recompute a digest over this dump, not
  the default one. For a model built in Python it emits exactly the fields the
  caller set.

`tests/conformance/test_wire_round_trip.py` runs every bundle schema that has
a model — the bundle's `examples`, a synthesized document, the differential
fixtures, a checked-in corpus for the digest-covered schemas, and variants of
each exercising every form a normalizing parser rewrites — through
`model_validate_json(doc).model_dump(mode="json")` and compares the result to
the input under JCS. A rewritten URL, datetime, UUID, email, value or shape
fails anywhere. The digest- and echo-covered schemas must round-trip with no
difference of any class under `model_dump_as_received`. Under the default
dump the suite names the classes that remain, each with the issue that tracks
it, and the list only shrinks:

| class | what happens | where it stands |
| --- | --- | --- |
| `default_emitted` | the dump materializes a schema default the document omitted | #1613; `model_dump_as_received` recovers the bytes. Two digest-covered schemas (`canonical-proposal`, `commercial-terms`) are affected under the default dump and are listed in the suite. |
| `field_dropped` | `extra='ignore'` drops an unknown or sibling-arm property | #1614; not recoverable by any dump. No digest-covered schema is affected at pin 3.2.4. |
| `alias_emitted` | `model_dump()` writes `pass_` for `pass`, `field_schema` for `$schema` | #1615; `model_dump_as_received` writes the wire name. |
| `null_dropped` | `exclude_none` drops an admitted explicit `null` | #1615; `model_dump_as_received` keeps it. |

Why the default dump was not changed in 9.x: leaving unset defaults off every
`model_dump()` is wire-equivalent for a schema-applying reader but visible to
any code that indexes a dumped dict (`payload["sandbox"]`), including a
counterparty that does not apply schema defaults. That is a 10.0 decision,
written up in #1613.

## Wire-boundary behavior

- SDK clients validate requests before sending and responses after receiving,
  according to `ValidationHookConfig`.
- `serve()`, `create_mcp_server()`, and `create_a2a_server()` default to strict
  canonical validation for both requests and responses.
- Server adopters can use `ValidationHookConfig` to select `strict`, `warn`, or
  `off` per side. Passing `validation=None` disables boundary validation and is
  the explicit compatibility escape hatch.
- Validation selects the schema bundle matching the negotiated wire version,
  so strict current-version behavior does not tighten legacy 2.5, 3.0, or 3.1
  traffic against the 3.2 schema.
- `"3.2-rc.7"` is an explicit wire compatibility alias for `"3.2"`, validated
  against the stable bundle. Direct schema-loader calls using full historical
  identifiers such as `"3.2.0-rc.7"` retain exact bundle lookup.
- Missing validators for an explicit native-tool version produce
  `VERSION_UNSUPPORTED`, with `field="adcp_version"` and `supported_versions`
  in the wire error details. Ordinary payload violations remain
  `VALIDATION_ERROR`.
- Pre-validation hooks can raise `AdcpError` or `ADCPTaskError` to deliberately
  reject a request. The dispatcher preserves their structured fields; other
  hook exceptions produce `INVALID_REQUEST` with the failing hook's name.

Directly constructed models can be checked ergonomically without converting
them to dictionaries first:

```python
from adcp import GetSignalsRequest
from adcp.validation import validate_request

request = GetSignalsRequest(signal_spec="sports fans")
outcome = validate_request("get_signals", request)
if not outcome.valid:
    for issue in outcome.issues:
        print(issue.pointer, issue.keyword, issue.message)
```

Model inputs are serialized with the same JSON mode, aliases, and `None`
exclusion used by SDK calls before canonical validation.

## Maintaining the boundary

Differential fixtures exercise the same payload through Pydantic and the
canonical validator. Any payload accepted by Pydantic but rejected by the
canonical schema must be listed in the narrow checked-in divergence allowlist
with its lost JSON Schema keyword. Removing a generator limitation must also
remove the corresponding allowlist entry.

For irreversible commitments, authorization, signing, and billing semantics,
prefer an additional targeted runtime validator so invalid state fails during
ordinary model construction as well as at the wire boundary. These targeted
checks supplement canonical validation; they do not change which artifact is
the protocol source of truth.

## Seller request subclasses behind routers

A platform method can annotate a stricter subclass of an SDK request to reject
extras or run seller-specific validators. `PlatformRouter`, `LazyPlatformRouter`,
and `TenantRegistry.as_platform()` resolve the tenant and revalidate through the
actual child method's annotation before invoking it. Validation failures return
`INVALID_REQUEST` with correctable recovery and the failing field path.

```python
from pydantic import ConfigDict
from adcp.decisioning import RequestContext
from adcp.types import GetMediaBuysRequest

class StrictMediaBuysRequest(GetMediaBuysRequest):
    model_config = ConfigDict(extra="forbid")

# On your DecisioningPlatform subclass:
def get_media_buys(self, req: StrictMediaBuysRequest, ctx: RequestContext):
    return {"media_buys": []}
```

The same boundary applies to keyword requests and projected model arguments,
such as `update_media_buy(media_buy_id, patch, ctx)`. Scalar IDs, projected
lists, context and additional arguments keep their original calling convention.
