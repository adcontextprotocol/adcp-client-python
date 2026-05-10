# Extending ADCP Types with Internal Fields

ADCP types represent the standardized protocol schema. However, your implementation may need additional internal tracking fields (workflow IDs, timestamps, metadata, etc.) that shouldn't be sent over the wire.

This guide shows how to extend ADCP types safely while maintaining protocol compliance.

> **Pydantic v2 serialization note:** Pydantic v2 uses a Rust-backed serializer. When a parent
> model calls `model_dump()`, Pydantic serializes nested child instances using its own compiled
> pipeline — it does **not** call Python-level `model_dump()` overrides on child objects. This
> means that if you override `model_dump()` on a child class, that override will not fire when
> the child is serialized as part of a parent. Use `Field(exclude=True)` or `@model_serializer`
> instead (both covered below).

## Field-Level Exclusion with `Field(exclude=True)` — Recommended

The simplest and most reliable way to keep internal fields off the wire. Fields annotated with
`Field(exclude=True)` are excluded by Pydantic's own serializer at **every nesting depth** — no
call-site `exclude={}` plumbing, no parent-model override required.

```python
from pydantic import Field
from adcp import Product
from adcp.types import AdCPBaseModel

class InternalProduct(Product):
    """Product extended with seller-internal fields."""
    implementation_config: dict = Field(default_factory=dict, exclude=True)
    seller_notes: str | None = Field(default=None, exclude=True)


class GetProductsResponseInternal(AdCPBaseModel):
    products: list[InternalProduct]


resp = GetProductsResponseInternal(
    products=[
        InternalProduct(
            product_id="p1",
            name="Display Home",
            publisher_properties=[],
            pricing_options=[],
            inventory_type="publisher_owned",
            implementation_config={"ad_server": "gam", "line_item_template_id": "42"},
            seller_notes="budget-locked to Q3",
        )
    ]
)

wire = resp.model_dump()
# {"products": [{"product_id": "p1", "name": "Display Home", ...}]}
# implementation_config and seller_notes are absent — no parent override needed.
```

`Field(exclude=True)` works with `model_dump()`, `model_dump_json()`, and all standard Pydantic
serialization options including `exclude_none=True`.

> **Note on `serialize_as_any`:** Pydantic 2.1+ also accepts `model_dump(serialize_as_any=True)`,
> which tells the Rust serializer to use the actual runtime type's schema for nested instances
> rather than the declared annotation type. This is useful when the declared field type is a
> base class but you're passing subclass instances and want subclass-specific fields to appear.
> It does **not** call Python-level `model_dump()` overrides — use `@model_serializer` for that.

## Custom Serialization Logic with `@model_serializer`

For cases where you need Python-level transformation logic beyond field exclusion — reshaping
output, conditional inclusion, derived computed fields — use Pydantic's
`@model_serializer(mode='wrap')`. This runs as part of Pydantic's own serialization pipeline
and fires correctly when the model is nested inside a parent's `model_dump()`.

```python
from typing import Any
from pydantic import model_serializer
from adcp import Creative


class InternalCreative(Creative):
    """Creative with a transformed render_url for the wire response."""
    _cdn_base: str = "https://cdn.example.com"

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any, info: Any) -> dict[str, Any]:
        result = handler(self, info)
        # Make render_url absolute before it hits the wire.
        if result.get("render_url") and not result["render_url"].startswith("http"):
            result["render_url"] = f"{self._cdn_base}/{result['render_url']}"
        return result
```

Unlike Python `model_dump()` overrides, `@model_serializer` integrates with Pydantic's
compilation pipeline and fires correctly whether the model is serialized directly **or** nested
inside a parent model's `model_dump()` call.

## Migrating from Manual `model_dump()` Dispatch Overrides

A common pattern in early SDK integrations is writing a parent override that manually re-calls
`model_dump()` on each child list:

```python
# ❌ Fragile: every new response type needs this boilerplate, and missing one is silent.
class GetCreativesResponse(AdCPBaseModel):
    creatives: list[Creative]

    def model_dump(self, **kwargs):
        result = super().model_dump(**kwargs)
        if "creatives" in result and self.creatives:
            result["creatives"] = [c.model_dump(**kwargs) for c in self.creatives]
        return result
```

This works but requires ~10 lines per response type, duplicates across every parent, and silently
produces wrong output if a new child list field is added and the override isn't updated.

**Migration — field exclusion only:**

```python
# ✅ Delete the parent override entirely. Move exclusion to the child via Field(exclude=True).
class InternalCreative(Creative):
    internal_approval_id: str | None = Field(default=None, exclude=True)

# GetCreativesResponse needs no model_dump() override — Pydantic handles it.
```

**Migration — custom Python logic:**

```python
# ✅ Move the logic to the child via @model_serializer. Parent override not needed.
class InternalCreative(Creative):
    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any, info: Any) -> dict[str, Any]:
        result = handler(self, info)
        # ... custom logic here ...
        return result
```

## Basic Pattern: Subclassing Response Types

```python
from adcp import CreateMediaBuySuccess
from pydantic import ConfigDict, Field

class CreateMediaBuySuccessExtended(CreateMediaBuySuccess):
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
adcp_response = CreateMediaBuySuccess.model_validate(
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
from adcp import CreateMediaBuySuccess

wrapper = InternalResponseWrapper[CreateMediaBuySuccess](
    response=CreateMediaBuySuccess(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[]
    ),
    workflow_step_id="ws_789",
    processing_time_ms=1234
)

# Access ADCP response
adcp_response = wrapper.response  # Type: CreateMediaBuySuccess

# Access internal fields
workflow_id = wrapper.workflow_step_id
```

## Pattern: Database Storage with Mixed Fields

When storing responses in a database with internal metadata:

```python
from datetime import datetime
from adcp import CreateMediaBuySuccess

class MediaBuyRecord(BaseModel):
    """Database record combining ADCP response with internal metadata."""
    # Internal database fields
    id: int
    created_at: datetime
    updated_at: datetime
    user_id: str
    workflow_step_id: str

    # ADCP response (stored as JSON)
    response_data: CreateMediaBuySuccess

    @classmethod
    def from_response(
        cls,
        response: CreateMediaBuySuccess,
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

    def to_adcp_response(self) -> CreateMediaBuySuccess:
        """Extract ADCP response for wire protocol."""
        return self.response_data

# Usage
response = await client.create_media_buy(request)
if isinstance(response, CreateMediaBuySuccess):
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
from adcp import WebhookPayload
from pydantic import ConfigDict

class InternalWebhookPayload(WebhookPayload):
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
    external_payload = WebhookPayload.model_validate(
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
# ❌ BAD: Relying on field name conventions
class Extended(CreateMediaBuySuccess):
    _internal_id: str  # Private field — may or may not serialize correctly

# ⚠ OK but fragile: call-site exclusion must be repeated every time model_dump() is called
class Extended(CreateMediaBuySuccess):
    internal_id: str

adcp_response = CreateMediaBuySuccess.model_validate(
    extended.model_dump(exclude={"internal_id"})  # Easy to forget, silent if omitted
)

# ✅ BEST: Field-level exclusion fires automatically at all nesting depths
class Extended(CreateMediaBuySuccess):
    internal_id: str = Field(exclude=True)

adcp_response = extended.model_dump()  # internal_id is absent — no extra plumbing
```

### 2. Document Internal Fields

Make it clear which fields are internal:

```python
class Extended(CreateMediaBuySuccess):
    """Extended CreateMediaBuySuccess with internal tracking.

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
    adcp_response = CreateMediaBuySuccess.model_validate(
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
    response: CreateMediaBuySuccess
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

class CreateMediaBuySuccessExtended(CreateMediaBuySuccess):
    workflow_step_id: str | None = None
    created_at: str | None = None

    # Define internal fields as class variable
    INTERNAL_FIELDS: ClassVar[set[str]] = {'workflow_step_id', 'created_at'}

    def to_adcp_response(self) -> CreateMediaBuySuccess:
        """Convert to wire protocol, excluding internal fields."""
        return CreateMediaBuySuccess.model_validate(
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
    base = CreateMediaBuySuccess.model_validate(
        extended.model_dump(exclude={'workflow_step_id'})
    )

    # Verify base type is valid ADCP
    assert base.media_buy_id == "mb_123"
    assert not hasattr(base, 'workflow_step_id')

    # Verify can parse from wire format
    wire_format = base.model_dump_json()
    parsed = CreateMediaBuySuccess.model_validate_json(wire_format)
    assert parsed.media_buy_id == "mb_123"
```

## Summary

When extending ADCP types:

1. **Subclass** the ADCP type with your internal fields
2. **Use `model_config = ConfigDict(extra='allow')`** if accepting dynamic fields
3. **Always exclude** internal fields when converting to wire protocol
4. **Document** which fields are internal vs ADCP spec
5. **Test** serialization roundtrips to ensure no leakage
