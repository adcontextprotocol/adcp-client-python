# Extending ADCP Types with Internal Fields

ADCP types represent the standardized protocol schema. However, your implementation may need additional internal tracking fields (workflow IDs, timestamps, metadata, etc.) that shouldn't be sent over the wire.

This guide shows how to extend ADCP types safely while maintaining protocol compliance.

> **Pydantic v2 serialization note:** Pydantic v2 uses a Rust-backed serializer that
> serializes nested child instances using the declared schema of the parent's field, not the
> child's `model_dump()` override. `AdCPBaseModel.model_dump()` and `model_dump_json()` set
> `serialize_as_any=True` by default so that subclass `@model_serializer` overrides do fire
> through base-typed parent fields, and `Field(exclude=True)` keeps internal fields off the
> wire at every nesting depth. Adopters do **not** need to write parent-side `model_dump`
> overrides to walk children — Pydantic does the walking; this guide covers the two seams
> (`Field(exclude=True)` and `@model_serializer`) that hook into it.

## Picking the Right Base Class — Context-Specific Schema Variants

Several entity names (`Creative`, `Package`, `MediaBuy`, etc.) appear in multiple spec slices with **genuinely different shapes**. The bare name resolves to one specific variant — typically not the one you want when extending response types. The creative inside `ListCreativesResponse.creatives` is a different class from the creative inside `GetCreativeDeliveryResponse.creatives`, even though both are spelled `Creative` in the spec. Subclassing the wrong variant produces silent type drift: construction works, but `mypy` flags `[assignment]` when you wire your subclass into the response that expects a different variant, and runtime serialization may drop fields the consuming code expects.

**The fix is to import the variant-specific alias.** For every name that collides across slices, `adcp.types` exports a disambiguated alias whose prefix names the slice it belongs to — `ListCreativesCreative`, `SyncCreativesCreative`, `DeliveryCreative`, `CapabilitiesCreative`, and so on. Import these from the public `adcp.types` namespace. Do **not** reach into `adcp.types.generated_poc.*` or `adcp.types._generated` — those are internal modules whose class names renumber on every schema regen, so an import that resolves today can silently move tomorrow.

These prefixed aliases live in the flat `adcp.types` namespace, not in the curated partial modules (`adcp.types.creative`, `adcp.types.media_buy`, …). The partials export only the canonical, single-variant names (`Creative`, `Package`, `MediaBuy`). When you need a specific variant, import it from `adcp.types`.

### Common cases

| Adopter use case | Import this | NOT this |
|---|---|---|
| Extend the creative type used in `ListCreativesResponse.creatives` | `from adcp.types import ListCreativesCreative` | `from adcp import Creative` (resolves to a different variant) |
| Extend the creative used in `GetCreativeDeliveryResponse.creatives` | `from adcp.types import DeliveryCreative` | `from adcp import Creative` (same name — different shape) |
| Extend the creative used in `SyncCreativesResponse` | `from adcp.types import SyncCreativesCreative` | `from adcp import Creative` |
| Extend the creative reported in `GetAdcpCapabilitiesResponse` | `from adcp.types import CapabilitiesCreative` | `from adcp import Creative` |
| Extend the package element of `CreateMediaBuyRequest.packages` | `from adcp.types import PackageRequest` (also in `adcp.types.media_buy`) | `from adcp import Package` |
| Extend the media-buy element of `GetMediaBuysResponse.media_buys` | `from adcp.types import GetMediaBuysMediaBuy` | `from adcp import MediaBuy` (resolves to the core variant; the list slice has a narrower shape) |
| Extend the media-buy reported in `GetAdcpCapabilitiesResponse` | `from adcp.types import CapabilitiesMediaBuy` | `from adcp import MediaBuy` |
| Extend a `Deployment` (e.g. for `Signal.deployments`) | `from adcp.types import Deployment` (a structured union over the deployment shapes) | reaching into `generated_poc` for an internal numbered class |

The canonical names (`Creative`, `Package`, `MediaBuy`, `Deployment`) remain available from both `adcp` and the partial modules — use those when the bare name already resolves to the variant you want. The prefixed aliases exist for the cases where it doesn't.

### How to detect a wrong import

mypy under `--strict` will flag the override with `[assignment]` when the element type you subclassed isn't the one the parent response field declares:

```python
# parent field declares the listing-slice creative; you subclassed a different variant:
from adcp import Creative  # canonical variant — wrong slice here

class InternalListCreative(Creative):
    internal_id: str | None = Field(default=None, exclude=True)

class MyListResponse(ListCreativesResponse):
    creatives: list[InternalListCreative] | None = None  # ← mypy: [assignment]
```

The fix is to subclass the listing-slice alias. When the variant matches, mypy is happy:

```python
from adcp.types import ListCreativesCreative, ListCreativesResponse

class InternalListCreative(ListCreativesCreative):
    internal_id: str | None = Field(default=None, exclude=True)

class MyListResponse(ListCreativesResponse):
    # Pydantic v2 covariant Sequence[X] in the library types means list[Subclass]
    # is a valid override here when Subclass IS-A the parent's creative variant.
    creatives: list[InternalListCreative] | None = None  # ✓ no ignore needed
```

### When a variant has no public alias

A few spec shapes have no disambiguated public name. The clearest example is the geo-exclusion element types behind `TargetingOverlay.geo_countries_exclude`, `geo_regions_exclude`, and `geo_metros_exclude`. Each exclusion list uses a distinct element class that is shape-identical to its inclusion counterpart (`GeoCountry`, `GeoRegion`, `GeoMetro`) but is not the same class and has no public alias in `adcp.types`.

If you need to substitute a shape-compatible class into one of these fields, the override is genuinely cross-class, and there is no public element type to subclass. Use the typed escape hatch `adcp.types.SchemaVariant` against the public inclusion variant — it marks the substitution as intentional and retires the `# type: ignore[assignment]`:

```python
from adcp.types import SchemaVariant, GeoCountry

class MyAudienceFilters(SomeLibraryFilters):
    # Cross-variant override: GeoCountry is the public inclusion type, shape-compatible
    # with the exclusion element. SchemaVariant marks the substitution as intentional;
    # no ``# type: ignore[assignment]`` needed.
    excluded_countries: SchemaVariant[list[GeoCountry]]
```

If you find a variant you need to extend that has neither a canonical nor a prefixed public alias, **open an issue** at [adcontextprotocol/adcp-client-python](https://github.com/adcontextprotocol/adcp-client-python/issues) asking for a public alias. Do not import the class from `adcp.types.generated_poc.*` as a workaround — those names renumber on schema regen, so the import is not stable.

`SchemaVariant[T]` collapses to `T` at runtime — Pydantic validates against the wrapped type unchanged. At type-check time the bundled mypy plugin (`adcp.types.mypy_plugin`) rewrites the annotation to `Any` so the LSP override check passes. **Adopters must enable the plugin in their mypy config** — add this line to `pyproject.toml`:

```toml
[tool.mypy]
plugins = ["adcp.types.mypy_plugin"]
```

Tradeoff: inside the override site, mypy sees the field as `Any`. If precise inference matters, `typing.cast(list[GeoCountry], self.excluded_countries)` recovers it. The runtime contract (Pydantic validation against the wrapped type) is unchanged. See [#710](https://github.com/adcontextprotocol/adcp-client-python/issues/710) for the design rationale.

### Tracking the spec-level fix

The cross-slice name collisions (the geo exclusion mirrors of the inclusion items, the capability-vs-response variants) are tracked upstream as a spec rename request: [adcontextprotocol/adcp#4347](https://github.com/adcontextprotocol/adcp/issues/4347). When the rename ships, some of these variants may merge — but the core principle (import the public alias that matches your intended response context, never the internal `generated_poc` class) is durable.

## Field-Level Exclusion with `Field(exclude=True)` — Recommended

The simplest and most reliable way to keep internal fields off the wire. Fields annotated with
`Field(exclude=True)` are excluded by Pydantic's own serializer at **every nesting depth** — no
call-site `exclude={}` plumbing, no parent-model override required.

```python
from pydantic import Field
# Listing-slice creative variant — see "Picking the Right Base Class" above.
from adcp.types import ListCreativesCreative
from adcp.types.base import AdCPBaseModel


class InternalCreative(ListCreativesCreative):
    """Creative extended with seller-internal fields."""
    internal_approval_id: str | None = Field(default=None, exclude=True)
    seller_notes: str | None = Field(default=None, exclude=True)


class CreativePayload(AdCPBaseModel):
    """User-defined payload — creatives declared as the base variant type."""
    creatives: list[ListCreativesCreative]


resp = CreativePayload(
    creatives=[
        InternalCreative(
            creative_id="c-1",
            name="Spring promo",
            format_id={"agent_url": "https://creative.example.com", "id": "display_300x250"},
            status="approved",
            created_date="2024-01-15T10:30:00Z",
            updated_date="2024-01-15T10:30:00Z",
            internal_approval_id="approv-42",
            seller_notes="approved by legal",
        )
    ]
)

wire = resp.model_dump()
# internal_approval_id and seller_notes are absent from the output —
# no parent override needed.
```

`Field(exclude=True)` works with `model_dump()`, `model_dump_json()`, and all standard Pydantic
serialization options including `exclude_none=True`.

## Custom Serialization Logic with `@model_serializer`

For cases where you need Python-level transformation logic beyond field exclusion — reshaping
output, conditional inclusion, derived computed fields — use Pydantic's
`@model_serializer(mode='wrap')`.

When the parent extends `AdCPBaseModel` (which all SDK-generated response types do), the
parent's `model_dump()` defaults `serialize_as_any=True`, so subclass `@model_serializer`
overrides fire automatically through base-typed parent fields. No call-site kwarg is
required.

```python
from typing import Any
from pydantic import SerializationInfo, model_serializer
# Listing-slice creative variant — see "Picking the Right Base Class" above.
from adcp.types import ListCreativesCreative
from adcp.types.base import AdCPBaseModel


class InternalCreative(ListCreativesCreative):
    """Creative with a normalized source_label field."""
    source_label: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any, info: SerializationInfo) -> dict[str, Any]:
        result = handler(self, info)
        # Normalize source_label to lowercase before it hits the wire.
        if result.get("source_label"):
            result["source_label"] = result["source_label"].lower()
        return result


# Direct serialization: the subclass serializer fires.
c = InternalCreative(
    creative_id="c-1",
    name="Spring promo",
    format_id={"agent_url": "https://creative.example.com", "id": "display_300x250"},
    status="approved",
    created_date="2024-01-15T10:30:00Z",
    updated_date="2024-01-15T10:30:00Z",
    source_label="HD_VIDEO",
)
c.model_dump()  # source_label normalized to "hd_video"
# (If you hit the MockValSer error described below, serialize this subclass with
#  serialize_as_any=False.)

# Nested under an AdCPBaseModel parent with a base-variant annotation:
class CreativePayload(AdCPBaseModel):
    creatives: list[ListCreativesCreative]  # declared as the base variant

payload = CreativePayload(creatives=[c])
payload.model_dump()
# AdCPBaseModel.model_dump() defaults serialize_as_any=True so the subclass serializer
# is meant to fire through the base-typed field, producing the nested "hd_video".
# Pass serialize_as_any=False explicitly to suppress runtime-type dispatch.
```

If your parent extends plain `pydantic.BaseModel` (not `AdCPBaseModel`), you must pass
`serialize_as_any=True` yourself — the default kwarg only ships on AdCP types.

> **Deferred-build caveat (known issue):** AdCP variant types are configured with
> `defer_build=True` to keep `import adcp` cheap. A subclass that defines its own
> `@model_serializer(mode="wrap")` invokes `handler(self, info)`, which dispatches to the
> base variant's pydantic-core serializer. On the first serialization that serializer can
> still be a deferred placeholder, and the failure surfaces as
> `PydanticSerializationError: ... 'MockValSer' object is not an instance of
> 'SchemaSerializer'` under the default `serialize_as_any=True`. Passing
> `serialize_as_any=False` serializes the subclass directly and works, but it forgoes the
> runtime-type dispatch this section relies on for nested base-typed fields. The
> `Field(exclude=True)` path above is **not** affected — only subclass *wrap serializers*
> hit this. **Prefer `Field(exclude=True)` for plain wire-isolation;** reach for
> `@model_serializer` only when you need transformation logic, and file an issue at
> [adcontextprotocol/adcp-client-python](https://github.com/adcontextprotocol/adcp-client-python/issues)
> if the `MockValSer` error blocks you — this is an SDK-side build-ordering bug, not
> something to work around by importing from `generated_poc`.

## Migrating from Manual `model_dump()` Dispatch Overrides

A common pattern in early SDK integrations is writing a parent override that manually re-calls
`model_dump()` on each child list:

```python
# ❌ Fragile: every new response type needs this boilerplate, and missing one is silent.
from adcp.types import ListCreativesCreative

class MyCreativePayload(AdCPBaseModel):
    creatives: list[ListCreativesCreative]

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        result = super().model_dump(**kwargs)
        if "creatives" in result and self.creatives:
            result["creatives"] = [c.model_dump(**kwargs) for c in self.creatives]
        return result
```

This requires ~10 lines per response type, must be written for every parent, and silently
produces wrong output if a new child list field is added without updating the override.

**Migration — field exclusion only:**

```python
# ✅ Delete the parent override entirely. Move exclusion to the child via Field(exclude=True).
class InternalCreative(ListCreativesCreative):
    internal_approval_id: str | None = Field(default=None, exclude=True)

# MyCreativePayload needs no model_dump() override — Pydantic handles it at all depths.
```

**Migration — custom Python logic:**

```python
# ✅ Move the logic to the child via @model_serializer.
#    AdCPBaseModel parents default serialize_as_any=True so the subclass serializer
#    fires automatically — no call-site kwarg needed.
class InternalCreative(ListCreativesCreative):
    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any, info: SerializationInfo) -> dict[str, Any]:
        result = handler(self, info)
        # ... custom logic here ...
        return result

# Parent: no model_dump() override needed.
payload = MyCreativePayload(creatives=[InternalCreative(creative_id="c-1", variants=[])])
wire = payload.model_dump()
```

**Adopter migration note (`serialize_as_any` default flip):** If you have subclasses that
add fields *without* `Field(exclude=True)`, those fields previously dropped at the
wire because the parent's base-type annotation acted as an accidental firewall. They will
now appear in `model_dump()` output. Audit each subclass and mark internal fields with
`Field(exclude=True)`; the field is the canonical wire-isolation contract. If you need the
prior behavior at a specific call site, pass `serialize_as_any=False` explicitly.

## Basic Pattern: Subclassing Response Types

```python
from adcp import CreateMediaBuySuccessResponse
from pydantic import ConfigDict, Field

class CreateMediaBuySuccessExtended(CreateMediaBuySuccessResponse):
    """Extended with internal tracking fields."""
    workflow_step_id: str | None = Field(None, description="Internal workflow step ID")
    created_at: str | None = Field(None, description="Internal timestamp")
    internal_notes: str | None = Field(None, description="Internal notes")

    model_config = ConfigDict(extra='allow')  # Allow extra fields

# Create extended response internally
internal_response = CreateMediaBuySuccessExtended(
    # ADCP required fields
    media_buy_id="mb_123",
    buyer_ref="ref_456",
    packages=[],
    # Internal fields
    workflow_step_id="ws_789",
    created_at="2024-01-15T10:30:00Z",
    internal_notes="First attempt"
)

# Serialize to ADCP spec before sending over wire
adcp_response = CreateMediaBuySuccessResponse.model_validate(
    internal_response.model_dump(exclude={'workflow_step_id', 'created_at', 'internal_notes'})
)
```

## Pattern: Generic Extension Base Class

For consistent internal fields across all response types:

```python
from typing import TypeVar, Generic
from pydantic import BaseModel, Field, ConfigDict

T = TypeVar('T', bound=BaseModel)

class InternalResponseWrapper(BaseModel, Generic[T]):
    """Wrapper for ADCP responses with internal tracking fields."""
    response: T
    workflow_step_id: str | None = None
    internal_request_id: str | None = None
    processing_time_ms: int | None = None
    created_at: str | None = None

    model_config = ConfigDict(extra='allow')

# Usage
from adcp import CreateMediaBuySuccessResponse

wrapper = InternalResponseWrapper[CreateMediaBuySuccessResponse](
    response=CreateMediaBuySuccessResponse(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[]
    ),
    workflow_step_id="ws_789",
    processing_time_ms=1234
)

# Access ADCP response
adcp_response = wrapper.response  # Type: CreateMediaBuySuccessResponse

# Access internal fields
workflow_id = wrapper.workflow_step_id
```

## Pattern: Database Storage with Mixed Fields

When storing responses in a database with internal metadata:

```python
from datetime import datetime
from adcp import CreateMediaBuySuccessResponse

class MediaBuyRecord(BaseModel):
    """Database record combining ADCP response with internal metadata."""
    # Internal database fields
    id: int
    created_at: datetime
    updated_at: datetime
    user_id: str
    workflow_step_id: str

    # ADCP response (stored as JSON)
    response_data: CreateMediaBuySuccessResponse

    @classmethod
    def from_response(
        cls,
        response: CreateMediaBuySuccessResponse,
        user_id: str,
        workflow_step_id: str
    ) -> "MediaBuyRecord":
        """Create database record from ADCP response."""
        return cls(
            id=0,  # Database will assign
            created_at=datetime.now(),
            updated_at=datetime.now(),
            user_id=user_id,
            workflow_step_id=workflow_step_id,
            response_data=response
        )

    def to_adcp_response(self) -> CreateMediaBuySuccessResponse:
        """Extract ADCP response for wire protocol."""
        return self.response_data

# Usage
response = await client.create_media_buy(request)
if isinstance(response, CreateMediaBuySuccessResponse):
    record = MediaBuyRecord.from_response(
        response,
        user_id="user_123",
        workflow_step_id="ws_789"
    )
    # Save to database...

# Later, send to another agent
adcp_response = record.to_adcp_response()
```

## Pattern: Webhook Payload Extension

When processing webhook payloads with internal routing metadata:

```python
from adcp import McpWebhookPayload
from pydantic import ConfigDict

class InternalWebhookPayload(McpWebhookPayload):
    """Extended webhook payload with internal routing."""
    internal_destination: str | None = None
    retry_count: int = 0
    routing_key: str | None = None

    model_config = ConfigDict(extra='allow')

async def process_webhook(payload: dict) -> None:
    """Process webhook with internal tracking."""
    # Parse with extensions
    internal_payload = InternalWebhookPayload.model_validate(payload)

    # Add internal routing
    internal_payload.internal_destination = determine_destination(internal_payload)
    internal_payload.routing_key = f"mediabuy.{internal_payload.task_type}"

    # Route internally
    await route_to_handler(internal_payload)

    # When forwarding to another service, use base type
    external_payload = McpWebhookPayload.model_validate(
        internal_payload.model_dump(exclude={'internal_destination', 'retry_count', 'routing_key'})
    )
```

## Pattern: Request Enrichment

When adding internal context to outgoing requests:

```python
from adcp import CreateMediaBuyRequest
from pydantic import ConfigDict

class CreateMediaBuyRequestInternal(CreateMediaBuyRequest):
    """Extended request with internal context."""
    requesting_user_id: str | None = None
    request_source: str | None = None
    idempotency_key: str | None = None

    model_config = ConfigDict(extra='allow')

    def to_adcp_request(self) -> CreateMediaBuyRequest:
        """Convert to wire-protocol request."""
        return CreateMediaBuyRequest.model_validate(
            self.model_dump(exclude={
                'requesting_user_id',
                'request_source',
                'idempotency_key'
            })
        )

# Usage
internal_request = CreateMediaBuyRequestInternal(
    # ADCP fields
    buyer_ref="ref_456",
    targeting=targeting,
    packages=[package],
    # Internal fields
    requesting_user_id="user_123",
    request_source="api",
    idempotency_key="req_xyz"
)

# Send to ADCP agent (internal fields stripped)
response = await client.create_media_buy(internal_request.to_adcp_request())
```

## Best Practices

### 1. Always Use Field Exclusion for Wire Protocol

**Prefer `Field(exclude=True)` over call-site `model_dump(exclude={...})`.** `Field(exclude=True)` is declared once on the field, works at every nesting depth automatically, and cannot be forgotten at a call site.

```python
from adcp import CreateMediaBuySuccessResponse
from pydantic import Field

# ❌ BAD: Relying on field name conventions
class Extended(CreateMediaBuySuccessResponse):
    _internal_id: str  # Private field — may or may not serialize correctly

# ⚠ OK but fragile: call-site exclusion must be repeated every time model_dump() is called
class Extended(CreateMediaBuySuccessResponse):
    internal_id: str

adcp_response = CreateMediaBuySuccessResponse.model_validate(
    extended.model_dump(exclude={"internal_id"})  # Easy to forget, silent if omitted
)

# ✅ BEST: Field-level exclusion fires automatically at all nesting depths
class Extended(CreateMediaBuySuccessResponse):
    internal_id: str = Field(exclude=True)

adcp_response = extended.model_dump()  # internal_id is absent — no extra plumbing
```

### 2. Document Internal Fields

Make it clear which fields are internal:

```python
class Extended(CreateMediaBuySuccessResponse):
    """Extended CreateMediaBuySuccessResponse with internal tracking.

    Internal fields (not part of ADCP spec):
        workflow_step_id: Internal workflow tracking
        created_at: Internal timestamp
    """
    workflow_step_id: str | None = Field(None, description="Internal: workflow step ID")
    created_at: str | None = Field(None, description="Internal: creation timestamp")
```

### 3. Test Serialization Roundtrips

Ensure internal fields don't leak to wire protocol:

```python
def test_internal_fields_excluded():
    extended = CreateMediaBuySuccessExtended(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[],
        workflow_step_id="ws_789"  # Internal field
    )

    # Convert to wire protocol
    adcp_response = CreateMediaBuySuccessResponse.model_validate(
        extended.model_dump(exclude={'workflow_step_id'})
    )

    # Verify internal field not present
    serialized = adcp_response.model_dump()
    assert 'workflow_step_id' not in serialized
    assert serialized['media_buy_id'] == "mb_123"
```

### 4. Use Type Guards for Extended Types

```python
from typing import TypeGuard

def is_extended_response(
    response: CreateMediaBuySuccessResponse
) -> TypeGuard[CreateMediaBuySuccessExtended]:
    """Check if response has extended internal fields."""
    return isinstance(response, CreateMediaBuySuccessExtended)

# Usage
if is_extended_response(response):
    # Type checker knows response has workflow_step_id
    log_workflow_step(response.workflow_step_id)
```

### 5. Consider Configuration for Field Sets

Define reusable field sets for exclusion:

```python
from typing import ClassVar

class CreateMediaBuySuccessExtended(CreateMediaBuySuccessResponse):
    workflow_step_id: str | None = None
    created_at: str | None = None

    # Define internal fields as class variable
    INTERNAL_FIELDS: ClassVar[set[str]] = {'workflow_step_id', 'created_at'}

    def to_adcp_response(self) -> CreateMediaBuySuccessResponse:
        """Convert to wire protocol, excluding internal fields."""
        return CreateMediaBuySuccessResponse.model_validate(
            self.model_dump(exclude=self.INTERNAL_FIELDS)
        )
```

## Common Pitfalls

### Pitfall 1: Forgetting to Exclude Before Sending

```python
# ❌ BAD: Sending extended type directly
extended_response = CreateMediaBuySuccessExtended(...)
await send_to_agent(extended_response)  # Internal fields leak!

# ✅ GOOD: Convert to base type first
extended_response = CreateMediaBuySuccessExtended(...)
adcp_response = extended_response.to_adcp_response()
await send_to_agent(adcp_response)
```

### Pitfall 2: Using Optional[T] Instead of T | None

```python
# ❌ BAD: Python 3.9 syntax in 3.10+ codebase
from typing import Optional
workflow_step_id: Optional[str] = None

# ✅ GOOD: Use 3.10+ union syntax
workflow_step_id: str | None = None
```

### Pitfall 3: Not Testing Both Directions

Always test both extension and conversion back to base type:

```python
def test_roundtrip():
    # Extend ADCP type
    extended = CreateMediaBuySuccessExtended(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[],
        workflow_step_id="ws_789"
    )

    # Convert to base type
    base = CreateMediaBuySuccessResponse.model_validate(
        extended.model_dump(exclude={'workflow_step_id'})
    )

    # Verify base type is valid ADCP
    assert base.media_buy_id == "mb_123"
    assert not hasattr(base, 'workflow_step_id')

    # Verify can parse from wire format
    wire_format = base.model_dump_json()
    parsed = CreateMediaBuySuccessResponse.model_validate_json(wire_format)
    assert parsed.media_buy_id == "mb_123"
```

## Summary

When extending ADCP types:

1. **Subclass** the ADCP type with your internal fields
2. **Use `model_config = ConfigDict(extra='allow')`** if accepting dynamic fields
3. **Always exclude** internal fields when converting to wire protocol
4. **Document** which fields are internal vs ADCP spec
5. **Test** serialization roundtrips to ensure no leakage
