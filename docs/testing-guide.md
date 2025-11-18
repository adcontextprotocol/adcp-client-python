# AdCP SDK Testing Guide

Best practices for writing tests that use the AdCP Python SDK.

## Testing Philosophy

When testing code that uses the AdCP SDK, follow these principles:

1. **Test public API, not internal types** - Import from `adcp`, not `adcp.types._generated`
2. **Test wire format compatibility** - Use JSON fixtures to validate protocol compliance
3. **Test user workflows** - Write tests that tell stories about how users accomplish goals
4. **Test behavior, not implementation** - Focus on what users can do, not how it works internally

## Quick Reference

### ✅ DO

```python
# Import from public API
from adcp import Product, CreateMediaBuyRequest, CreateMediaBuySuccessResponse

# Test wire format with JSON
def test_product_deserializes():
    json_data = '{"product_id": "test", ...}'
    product = Product.model_validate_json(json_data)
    assert product.product_id == "test"

# Test user workflows
async def test_buyer_discovers_products():
    client = ADCPClient(config)
    result = await client.get_products(request)
    assert result.success
```

### ❌ DON'T

```python
# Don't import from internal modules
from adcp.types._generated import Product1  # ❌ WRONG
from adcp.types.generated_poc.product import PublisherProperties4  # ❌ WRONG

# Don't test Pydantic mechanics
def test_discriminator_field_enforced():  # ❌ WRONG - testing Pydantic, not our code
    ...

# Don't test type identity
def test_alias_points_to_generated_type():  # ❌ WRONG - internal detail
    assert CreateMediaBuySuccessResponse is CreateMediaBuyResponse1
```

---

## Pattern 1: Test Wire Format Compatibility

These tests validate that the SDK correctly handles protocol JSON. They catch serialization bugs, field name mismatches, and type coercion issues.

### Example: Deserialize Protocol JSON

```python
def test_get_products_response_deserializes_from_protocol_json():
    """GetProductsResponse deserializes from actual protocol JSON."""
    # This JSON comes from a real agent response
    protocol_json = """
    {
      "products": [
        {
          "product_id": "premium_display",
          "name": "Premium Display Placements",
          "description": "High-visibility ad slots on homepage",
          "publisher_properties": [
            {
              "publisher_domain": "example.com",
              "selection_type": "by_id",
              "property_ids": ["homepage", "mobile_app"]
            }
          ],
          "pricing_options": [
            {
              "model": "cpm_fixed_rate",
              "is_fixed": true,
              "cpm": 5.50
            }
          ]
        }
      ]
    }
    """

    from adcp import GetProductsResponse

    # ✅ TEST: Can we parse actual protocol JSON?
    response = GetProductsResponse.model_validate_json(protocol_json)

    # Verify structure
    assert len(response.products) == 1
    product = response.products[0]
    assert product.product_id == "premium_display"

    # ✅ TEST: Round-trip preserves data?
    roundtrip_json = response.model_dump_json()
    roundtrip = GetProductsResponse.model_validate_json(roundtrip_json)
    assert roundtrip.products[0].product_id == product.product_id
```

### Example: Test Discriminated Union Variants

```python
def test_create_media_buy_success_response_wire_format():
    """CreateMediaBuySuccessResponse deserializes success variant."""
    protocol_json = """
    {
      "media_buy_id": "mb_123456",
      "buyer_ref": "campaign_abc",
      "packages": [
        {
          "package_id": "pkg_001",
          "product_id": "premium_display",
          "status": "pending"
        }
      ]
    }
    """

    # ✅ CORRECT: Use semantic alias from public API
    from adcp import CreateMediaBuySuccessResponse

    response = CreateMediaBuySuccessResponse.model_validate_json(protocol_json)

    assert response.media_buy_id == "mb_123456"
    assert not hasattr(response, "errors")  # Success variant doesn't have errors
    assert len(response.packages) == 1

def test_create_media_buy_error_response_wire_format():
    """CreateMediaBuyErrorResponse deserializes error variant."""
    protocol_json = """
    {
      "errors": [
        {
          "code": "budget_exceeded",
          "message": "Requested budget exceeds account limit"
        }
      ]
    }
    """

    from adcp import CreateMediaBuyErrorResponse

    response = CreateMediaBuyErrorResponse.model_validate_json(protocol_json)

    assert len(response.errors) == 1
    assert response.errors[0].code == "budget_exceeded"
    assert not hasattr(response, "media_buy_id")  # Error variant doesn't have media_buy_id
```

---

## Pattern 2: Test User Workflows

These tests tell stories about how users accomplish goals. They focus on external behavior users care about.

### Example: Product Discovery Workflow

```python
@pytest.mark.asyncio
async def test_buyer_discovers_products_for_coffee_campaign(mocker):
    """Buyer gets products suitable for coffee brand campaign."""
    # Setup: Create client
    from adcp import ADCPClient, AgentConfig, Protocol, GetProductsRequest

    config = AgentConfig(
        id="publisher_agent",
        agent_uri="https://publisher.example.com",
        protocol=Protocol.A2A,
    )
    client = ADCPClient(config)

    # Setup: Mock agent response with realistic data
    from adcp.types.core import TaskResult, TaskStatus

    mock_response_data = {
        "products": [
            {
                "product_id": "breakfast_readers",
                "name": "Morning News Readers",
                "description": "Reach coffee drinkers during morning news",
                "publisher_properties": [
                    {
                        "publisher_domain": "news.example.com",
                        "selection_type": "by_tag",
                        "property_tags": ["morning", "lifestyle"],
                    }
                ],
                "pricing_options": [
                    {"model": "cpm_fixed_rate", "is_fixed": True, "cpm": 4.50}
                ],
            }
        ]
    }

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=mock_response_data,
        success=True
    )

    mocker.patch.object(client.adapter, "get_products", return_value=mock_result)

    # Action: User discovers products
    request = GetProductsRequest(brief="Coffee brand campaign for morning audience")
    result = await client.get_products(request)

    # Assert: User gets successful result
    assert result.success, f"Discovery failed: {result.error}"
    assert len(result.data.products) > 0, "No products found"

    # Assert: Product has campaign-relevant data
    product = result.data.products[0]
    assert product.product_id
    assert product.name
    assert len(product.pricing_options) > 0

    # Assert: Can plan budget from pricing
    pricing = product.pricing_options[0]
    assert pricing.model in ["cpm_fixed_rate", "cpm_auction"]
```

### Example: Handle Empty Results

```python
@pytest.mark.asyncio
async def test_buyer_handles_no_products_available(mocker):
    """Buyer gracefully handles when no products match criteria."""
    from adcp import ADCPClient, AgentConfig, Protocol, GetProductsRequest
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="publisher_agent",
        agent_uri="https://publisher.example.com",
        protocol=Protocol.A2A,
    )
    client = ADCPClient(config)

    # Mock empty response
    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"products": []},
        success=True
    )

    mocker.patch.object(client.adapter, "get_products", return_value=mock_result)

    # User makes request
    request = GetProductsRequest(brief="Extremely niche requirement")
    result = await client.get_products(request)

    # Should succeed with empty list (not error)
    assert result.success
    assert result.data.products == []
```

---

## Pattern 3: Test Public API Behavior

These tests verify that API methods work as documented. They focus on method signatures, return types, and error handling.

### Example: Test Method Accepts Correct Types

```python
@pytest.mark.asyncio
async def test_create_media_buy_accepts_request_object(mocker):
    """create_media_buy accepts CreateMediaBuyRequest and returns response."""
    from adcp import (
        ADCPClient,
        AgentConfig,
        Protocol,
        CreateMediaBuyRequest,
        CreateMediaBuySuccessResponse,
    )
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="agent",
        agent_uri="https://agent.example.com",
        protocol=Protocol.A2A
    )
    client = ADCPClient(config)

    # Mock successful response
    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={
            "media_buy_id": "mb_123",
            "buyer_ref": "campaign_456",
            "packages": [],
        },
        success=True,
    )

    mocker.patch.object(client.adapter, "create_media_buy", return_value=mock_result)

    # ✅ TEST: Can user create request and call method?
    request = CreateMediaBuyRequest(
        buyer_ref="campaign_456",
        packages=[
            {
                "product_id": "premium_display",
                "budget": {"amount": 10000.0, "currency": "USD"},
            }
        ],
    )

    result = await client.create_media_buy(request)

    # ✅ TEST: Does result have expected structure?
    assert result.success
    assert isinstance(result.data, CreateMediaBuySuccessResponse)
    assert result.data.media_buy_id == "mb_123"
```

---

## Pattern 4: Test Error Handling

These tests verify that users get helpful error messages and can diagnose problems.

### Example: Test Validation Errors

```python
def test_invalid_json_gives_helpful_error():
    """Invalid JSON produces actionable error message."""
    from pydantic import ValidationError
    from adcp import GetProductsResponse

    invalid_json = '{"products": "not an array"}'

    with pytest.raises(ValidationError) as exc_info:
        GetProductsResponse.model_validate_json(invalid_json)

    # Error should mention the field and expected type
    error_msg = str(exc_info.value)
    assert "products" in error_msg.lower()

def test_missing_required_field_gives_helpful_error():
    """Missing required fields produce clear error messages."""
    from pydantic import ValidationError
    from adcp import Product

    incomplete_data = {
        "product_id": "test",
        "name": "Test Product",
        # Missing: description, publisher_properties, pricing_options
    }

    with pytest.raises(ValidationError) as exc_info:
        Product.model_validate(incomplete_data)

    error_msg = str(exc_info.value)
    # Should tell user what's missing
    assert "field required" in error_msg.lower() or "missing" in error_msg.lower()
```

---

## Recommended Fixtures

### Mock Client Fixture

```python
@pytest.fixture
def mock_adcp_client(mocker):
    """Create a mock ADCPClient for testing.

    Returns a client with mocked adapter so tests can control responses.
    """
    from adcp import ADCPClient, AgentConfig, Protocol

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A
    )

    client = ADCPClient(config)

    # Mock the adapter to avoid real network calls
    client.adapter = mocker.MagicMock()

    return client
```

### Sample Data Fixture

```python
@pytest.fixture
def sample_product_json():
    """Realistic product JSON from protocol.

    Use this in tests that need valid product data.
    """
    return {
        "product_id": "premium_display",
        "name": "Premium Display Ad",
        "description": "High-visibility homepage placement",
        "publisher_properties": [
            {
                "publisher_domain": "example.com",
                "selection_type": "by_id",
                "property_ids": ["homepage", "mobile_app"],
            }
        ],
        "pricing_options": [
            {"model": "cpm_fixed_rate", "is_fixed": True, "cpm": 5.50}
        ],
    }
```

---

## Anti-Patterns to Avoid

### ❌ Don't Import from Internal Modules

```python
# ❌ WRONG: Couples tests to internal implementation
from adcp.types._generated import Product1
from adcp.types.generated_poc.product import PublisherProperties4

# ✅ CORRECT: Use public API
from adcp import Product

# Test using JSON (wire format)
product_json = {
    "product_id": "test",
    "name": "Test",
    "description": "Test product",
    "publisher_properties": [{
        "publisher_domain": "example.com",
        "selection_type": "by_id",
        "property_ids": ["site1"],
    }],
    "pricing_options": [
        {"model": "cpm_fixed_rate", "is_fixed": True, "cpm": 5.0}
    ],
}

product = Product.model_validate(product_json)
assert product.publisher_properties[0].selection_type == "by_id"
```

### ❌ Don't Test Pydantic Mechanics

```python
# ❌ WRONG: Testing Pydantic's discriminator implementation
def test_discriminator_field_is_enforced():
    # Pydantic already tests this extensively
    ...

# ✅ CORRECT: Test user-facing behavior
def test_user_can_deserialize_success_and_error_responses():
    from adcp import CreateMediaBuySuccessResponse, CreateMediaBuyErrorResponse

    success_json = '{"media_buy_id": "mb_123", "buyer_ref": "ref", "packages": []}'
    error_json = '{"errors": [{"code": "err", "message": "msg"}]}'

    success = CreateMediaBuySuccessResponse.model_validate_json(success_json)
    error = CreateMediaBuyErrorResponse.model_validate_json(error_json)

    # User can distinguish by type
    assert isinstance(success, CreateMediaBuySuccessResponse)
    assert isinstance(error, CreateMediaBuyErrorResponse)
```

### ❌ Don't Test Type Identity

```python
# ❌ WRONG: Testing internal implementation detail
def test_alias_points_to_generated_type():
    assert CreateMediaBuySuccessResponse is CreateMediaBuyResponse1

# ✅ CORRECT: Test that alias works in actual usage
def test_semantic_alias_works_for_users():
    from adcp import CreateMediaBuySuccessResponse

    response = CreateMediaBuySuccessResponse(
        media_buy_id="mb_123",
        buyer_ref="ref",
        packages=[]
    )

    # Can serialize to JSON
    json_str = response.model_dump_json()
    assert "media_buy_id" in json_str

    # Can deserialize from JSON
    roundtrip = CreateMediaBuySuccessResponse.model_validate_json(json_str)
    assert roundtrip.media_buy_id == response.media_buy_id
```

---

## Summary: Key Principles

### ✅ DO

1. Test public API (`from adcp import X`)
2. Test wire format with JSON (`model_validate_json`)
3. Test user workflows (can buyer discover products?)
4. Test behavior (does API work as documented?)
5. Use semantic aliases (`CreateMediaBuySuccessResponse`)
6. Write tests users can learn from

### ❌ DON'T

1. Import from `_generated` or `generated_poc`
2. Test Pydantic internals
3. Test type identity (`assert X is Y`)
4. Test implementation details
5. Use numbered types (`CreateMediaBuyResponse1`)
6. Test mechanics instead of behavior

### Remember

- Tests should demonstrate correct SDK usage
- Tests should catch protocol compatibility bugs
- Tests should tell user stories
- Tests should respect public API boundaries

---

## See Also

- [Full executable examples](./examples/testing_patterns.py)
- [AdCP Protocol Specification](https://adcontextprotocol.org/)
- [SDK API Reference](./api-reference.md)
