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
directly.

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
