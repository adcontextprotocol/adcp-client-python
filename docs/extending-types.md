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

Several entity names (`Creative`, `Package`, `MediaBuy`, `Deployment`, `GeoCountriesExcludeItem`, etc.) appear in multiple spec slices with **genuinely different shapes**. Codegen emits each as a separate class. Top-level imports like `from adcp import Creative` resolve to one specific variant — typically not the one you want when extending response types. Subclassing the wrong variant produces silent type drift: construction works, but `mypy` flags `[assignment]` when you wire your subclass into the response that expects a different variant, and runtime serialization may drop fields the consuming code expects.

**The fix is import discipline.** When extending a response model's element type, import the element from the same submodule the parent response is generated from — not from the top-level `adcp.types` namespace.

### Common cases

| Adopter use case | Import this | NOT this |
|---|---|---|
| Extend the creative type used in `ListCreativesResponse.creatives` | `from adcp.types.generated_poc.creative.list_creatives_response import Creative` | `from adcp import Creative` (resolves to delivery variant) |
| Extend the creative used in `GetCreativeDeliveryResponse.creatives` | `from adcp.types.generated_poc.creative.get_creative_delivery_response import Creative` | `from adcp import Creative` (same name — different submodule, different shape) |
| Extend the package element of `CreateMediaBuyRequest.packages` | `from adcp.types.generated_poc.media_buy.package_request import PackageRequest` | `from adcp import Package` |
| Extend the affected-package element of `UpdateMediaBuyResponse.affected_packages` | `from adcp import Package` (canonical) — verify against the parent response | — |
| Extend the media-buy element of `GetMediaBuysResponse.media_buys` | `from adcp.types.generated_poc.media_buy.get_media_buys_response import MediaBuy` | `from adcp import MediaBuy` (top-level resolves to the canonical variant; the `get_media_buys_response` slice has a narrower shape) |
| Extend `Deployment` for `Signal.deployments` | `from adcp.types.generated_poc.core.deployment import Deployment1` (the structured class — `Deployment` is a `RootModel` wrapper) | `from adcp.types.generated_poc.core.deployment import Deployment` (you'll get the wrapper, not the fields) |
| Add fields to a geo-exclusion list (`TargetingOverlay.geo_countries_exclude` etc.) | The `Geo*ExcludeItem` classes are shape-identical to the inclusion variants but distinct classes — there is no clean inheritance path; declare your local class against the exclusion variant | — |

### How to detect a wrong import

mypy under `--strict` will flag the override with `[assignment]`:

```python
# parent: list[adcp.types.generated_poc.creative.list_creatives_response.Creative] | None
# you imported the delivery Creative by accident:
from adcp import Creative  # delivery variant

class InternalListCreative(Creative):
    internal_id: str | None = Field(default=None, exclude=True)

class MyListResponse(LibraryListCreativesResponse):
    creatives: list[InternalListCreative] | None = None  # ← mypy: [assignment]
```

The fix is to switch the import to the listing-slice submodule. When the import is right, mypy is happy:

```python
from adcp.types.generated_poc.creative.list_creatives_response import Creative

class InternalListCreative(Creative):
    internal_id: str | None = Field(default=None, exclude=True)

class MyListResponse(LibraryListCreativesResponse):
    # Pydantic v2 covariant Sequence[X] in library types means list[Subclass]
    # is a valid override here when Subclass IS-A parent's Creative.
    creatives: list[InternalListCreative] | None = None  # ✓ no ignore needed
```

If the parent response uses a `Geo*ExcludeItem` (shape-identical-but-distinct class) and you want to substitute it with a more permissive type, the override is genuinely cross-class and `# type: ignore[assignment]` is warranted; document the divergence in a comment so future readers understand the override isn't a bug.

### Tracking the spec-level fix

Several of the cases above (the `Geo*ExcludeItem` mirrors of inclusion items, the `Deployment` RootModel wrapper, the `MediaBuy` capability-vs-response collision) are tracked upstream as a spec rename request: [adcontextprotocol/adcp#4347](https://github.com/adcontextprotocol/adcp/issues/4347). When the rename ships, the workarounds in this section may collapse — but the core principle (import from the submodule that matches your intended response context) is durable.

## Field-Level Exclusion with `Field(exclude=True)` — Recommended

The simplest and most reliable way to keep internal fields off the wire. Fields annotated with
`Field(exclude=True)` are excluded by Pydantic's own serializer at **every nesting depth** — no
call-site `exclude={}` plumbing, no parent-model override required.

```python
from typing import Any
from pydantic import Field
# Listing-slice Creative — see "Picking the Right Base Class" above.
from adcp.types.generated_poc.creative.list_creatives_response import Creative
from adcp.types.base import AdCPBaseModel


class InternalCreative(Creative):
    """Creative extended with seller-internal fields."""
    internal_approval_id: str | None = Field(default=None, exclude=True)
    seller_notes: str | None = Field(default=None, exclude=True)


class CreativePayload(AdCPBaseModel):
    """User-defined payload — creatives declared as base type."""
    creatives: list[Creative]


resp = CreativePayload(
    creatives=[
        InternalCreative(
            creative_id="c-1",
            variants=[],
            internal_approval_id="approv-42",
            seller_notes="approved by legal",
        )
    ]
)

wire = resp.model_dump()
# {"creatives": [{"creative_id": "c-1", "variants": []}]}
# internal_approval_id and seller_notes are absent — no parent override needed.
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
# Listing-slice Creative — see "Picking the Right Base Class" above.
from adcp.types.generated_poc.creative.list_creatives_response import Creative
from adcp.types.base import AdCPBaseModel


class InternalCreative(Creative):
    """Creative with a normalized source_label field."""
    source_label: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any, info: SerializationInfo) -> dict[str, Any]:
        result = handler(self, info)
        # Normalize source_label to lowercase before it hits the wire.
        if result.get("source_label"):
            result["source_label"] = result["source_label"].lower()
        return result


# Direct serialization: serializer fires.
c = InternalCreative(creative_id="c-1", variants=[], source_label="HD_VIDEO")
c.model_dump()  # {"creative_id": "c-1", "variants": [], "source_label": "hd_video"}

# Nested under an AdCPBaseModel parent with a base-type annotation:
class CreativePayload(AdCPBaseModel):
    creatives: list[Creative]  # declared as base type

payload = CreativePayload(creatives=[c])
payload.model_dump()
# {"creatives": [{"creative_id": "c-1", "variants": [], "source_label": "hd_video"}]}
# Subclass serializer fired automatically — AdCPBaseModel.model_dump() defaults
# serialize_as_any=True. Pass serialize_as_any=False explicitly to suppress it.
```

If your parent extends plain `pydantic.BaseModel` (not `AdCPBaseModel`), you must pass
`serialize_as_any=True` yourself — the default kwarg only ships on AdCP types.

## Migrating from Manual `model_dump()` Dispatch Overrides

A common pattern in early SDK integrations is writing a parent override that manually re-calls
`model_dump()` on each child list:

```python
# ❌ Fragile: every new response type needs this boilerplate, and missing one is silent.
class MyCreativePayload(AdCPBaseModel):
    creatives: list[Creative]

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
class InternalCreative(Creative):
    internal_approval_id: str | None = Field(default=None, exclude=True)

# MyCreativePayload needs no model_dump() override — Pydantic handles it at all depths.
```

**Migration — custom Python logic:**

```python
# ✅ Move the logic to the child via @model_serializer.
#    AdCPBaseModel parents default serialize_as_any=True so the subclass serializer
#    fires automatically — no call-site kwarg needed.
class InternalCreative(Creative):
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
from adcp import McpMcpWebhookPayload
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
    internal_payload = InternalMcpWebhookPayload.model_validate(payload)

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
