# Migrating from v1.x to v2.0.0

Version 2.0.0 introduces discriminated union types for better type safety and clearer error handling. This guide shows practical examples of updating your code.

## Breaking Changes

### Response Types are now Discriminated Unions

**What changed**: Response types that could return either success or error are now explicit discriminated unions instead of optional fields.

**Before (v1.x)**:
```python
response = await client.create_media_buy(...)
if response.errors:
    # Handle error - but type checker doesn't know this branch
    for error in response.errors:
        print(error.message)
else:
    # Handle success - but media_buy_id might still be None
    print(f"Created: {response.media_buy_id}")
```

**After (v2.0.0)**:
```python
from adcp import CreateMediaBuySuccess, CreateMediaBuyError

response = await client.create_media_buy(...)
match response:  # Python 3.10+ pattern matching
    case CreateMediaBuySuccess():
        print(f"Created: {response.media_buy_id}")  # Type: str (guaranteed)
    case CreateMediaBuyError():
        for error in response.errors:
            print(error.message)  # Type: str (guaranteed)
```

Or with `isinstance()`:
```python
if isinstance(response, CreateMediaBuySuccess):
    print(f"Created: {response.media_buy_id}")  # Type narrowed to str
elif isinstance(response, CreateMediaBuyError):
    for error in response.errors:
        print(error.message)
```

### Asset Types now use Discriminators

**What changed**: `SubAsset` is now a union of `MediaSubAsset` and `TextSubAsset` with an `asset_kind` discriminator.

**Before (v1.x)**:
```python
# All fields were optional
sub_asset = SubAsset(
    asset_type="thumbnail_image",
    asset_id="thumb_1",
    content_uri="https://example.com/image.jpg",  # Optional
    content=None  # Optional
)
```

**After (v2.0.0)**:
```python
from adcp import MediaSubAsset, TextSubAsset

# Media assets require content_uri
media_asset = MediaSubAsset(
    asset_kind="media",  # Required discriminator
    asset_type="thumbnail_image",
    asset_id="thumb_1",
    content_uri="https://example.com/image.jpg"  # Required, not Optional
)

# Text assets require content
text_asset = TextSubAsset(
    asset_kind="text",  # Required discriminator
    asset_type="headline",
    asset_id="headline_1",
    content="Amazing Product!"  # Required, not Optional
)
```

### Deployment Types now use Discriminators

**Before (v1.x)**:
```python
destination = Destination(
    platform="google_ads",  # Optional
    account="123",
    agent_url=None  # Optional
)
```

**After (v2.0.0)**:
```python
from adcp import PlatformDestination, AgentDestination

# Platform destination
platform_dest = PlatformDestination(
    type="platform",  # Required discriminator
    platform="google_ads",  # Required
    account="123"
)

# Agent destination
agent_dest = AgentDestination(
    type="agent",  # Required discriminator
    agent_url="https://agent.example.com",  # Required
    account="123"
)
```

## Updated Patterns

### Error Handling

**Old pattern**:
```python
try:
    response = await client.create_media_buy(...)
    if response.errors:
        raise ValueError(f"Creation failed: {response.errors[0].message}")
    return response.media_buy_id
except Exception as e:
    logger.error(f"Error: {e}")
```

**New pattern**:
```python
try:
    response = await client.create_media_buy(...)
    if isinstance(response, CreateMediaBuyError):
        raise ValueError(f"Creation failed: {response.errors[0].message}")

    # Type checker knows response is CreateMediaBuySuccess here
    return response.media_buy_id  # Type: str (not str | None)
except Exception as e:
    logger.error(f"Error: {e}")
```

### Type Hints

**Old pattern**:
```python
def handle_response(response: CreateMediaBuyResponse):
    # Type checker can't narrow types from optional field checks
    if response.errors:
        return response.errors[0].message  # Type: str | None
    return response.media_buy_id  # Type: str | None
```

**New pattern**:
```python
def handle_response(response: CreateMediaBuyResponse):
    # Type checker uses isinstance() for type narrowing
    if isinstance(response, CreateMediaBuySuccess):
        return response.media_buy_id  # Type: str (not None!)
    else:
        return response.errors[0].message  # Type: str (guaranteed)
```

### Testing

**Old pattern**:
```python
def test_success_response():
    response = CreateMediaBuyResponse(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[],
        errors=None  # Optional field
    )
    assert response.media_buy_id == "mb_123"
```

**New pattern**:
```python
def test_success_response():
    response = CreateMediaBuySuccess(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[]
        # No errors field on success variant
    )
    assert isinstance(response, CreateMediaBuySuccess)
    assert response.media_buy_id == "mb_123"

def test_error_response():
    response = CreateMediaBuyError(
        errors=[Error(code="invalid_budget", message="Budget too low")]
        # No media_buy_id field on error variant
    )
    assert isinstance(response, CreateMediaBuyError)
    assert len(response.errors) == 1
```

### Working with Union Types in Functions

**Pattern: Early return on error**:
```python
async def process_media_buy(request: CreateMediaBuyRequest):
    response = await client.create_media_buy(request)

    # Early return pattern
    if isinstance(response, CreateMediaBuyError):
        logger.error(f"Failed to create media buy: {response.errors}")
        return None

    # Type checker knows response is CreateMediaBuySuccess here
    media_buy_id = response.media_buy_id
    buyer_ref = response.buyer_ref

    # Continue processing...
    return media_buy_id
```

**Pattern: Exhaustive matching**:
```python
def get_status_message(response: CreateMediaBuyResponse) -> str:
    match response:
        case CreateMediaBuySuccess(media_buy_id=mb_id):
            return f"Success: Created {mb_id}"
        case CreateMediaBuyError(errors=errs):
            return f"Failed: {errs[0].message}"
        case _:
            # Type checker ensures all variants are handled
            raise TypeError(f"Unknown response type: {type(response)}")
```

### Working with Asset Unions

**Pattern: Processing mixed asset types**:
```python
from adcp import MediaSubAsset, TextSubAsset, SubAsset

def process_assets(assets: list[SubAsset]) -> dict[str, list[str]]:
    media_urls = []
    text_content = []

    for asset in assets:
        match asset:
            case MediaSubAsset(content_uri=uri):
                media_urls.append(uri)
            case TextSubAsset(content=text):
                if isinstance(text, str):
                    text_content.append(text)
                else:
                    text_content.extend(text)

    return {"media": media_urls, "text": text_content}
```

## Extending Types with Internal Fields

If you need to add internal tracking fields to ADCP response types:

```python
from adcp import CreateMediaBuySuccess as AdCPSuccess
from pydantic import ConfigDict

class CreateMediaBuySuccessExtended(AdCPSuccess):
    """Extended with internal tracking fields."""
    workflow_step_id: str | None = None
    internal_notes: str | None = None

    model_config = ConfigDict(extra='allow')  # Allow extra fields

# Use extended type internally
internal_response = CreateMediaBuySuccessExtended(
    media_buy_id="mb_123",
    buyer_ref="ref_456",
    packages=[],
    workflow_step_id="ws_789"  # Internal field
)

# Serialize to ADCP spec (excludes internal fields)
adcp_response = CreateMediaBuySuccess.model_validate(
    internal_response.model_dump(exclude={'workflow_step_id', 'internal_notes'})
)
```

## All Changed Response Types

The following response types are now discriminated unions:

- `CreateMediaBuyResponse = CreateMediaBuySuccess | CreateMediaBuyError`
- `UpdateMediaBuyResponse = UpdateMediaBuySuccess | UpdateMediaBuyError`
- `ActivateSignalResponse = ActivateSignalSuccess | ActivateSignalError`
- `SyncCreativesResponse = SyncCreativesSuccess | SyncCreativesError`

Asset and destination types:

- `SubAsset = MediaSubAsset | TextSubAsset` (discriminator: `asset_kind`)
- `Destination = PlatformDestination | AgentDestination` (discriminator: `type`)
- `Deployment = PlatformDeployment | AgentDeployment` (discriminator: `type`)

## Tips for Migration

1. **Use pattern matching where possible** - Python 3.10+ pattern matching provides the cleanest syntax

2. **Let type checkers help** - Run `mypy` or `pyright` to find places where optional field access needs updating

3. **Test both success and error paths** - Discriminated unions make it easier to test each variant independently

4. **Use isinstance() for backwards compatibility** - Works in Python 3.9+ unlike pattern matching

5. **Import specific variant types** - Import `CreateMediaBuySuccess` and `CreateMediaBuyError` instead of just `CreateMediaBuyResponse` for clearer code
