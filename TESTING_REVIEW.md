# AdCP Python SDK Testing Philosophy Review

## Executive Summary

The current test suite (258 tests, 4599 LOC) has several architectural issues that undermine the project's API stability goals. Tests are coupled to internal implementation details (`generated_poc`), don't adequately verify wire format compatibility, and fail to demonstrate proper user-facing API usage.

**Priority Issues:**
1. Tests import from `adcp.types.generated_poc` - violates public API boundary
2. Minimal wire format validation (only 8 JSON roundtrip tests)
3. Tests demonstrate wrong patterns that users might copy
4. Over-testing of implementation details vs. external behavior

## Detailed Findings

### 1. Public API Boundary Violations

**Problem:** Tests import directly from internal `generated_poc` directory, violating the stable API layer.

**Evidence:**
- `test_discriminated_unions.py` lines 37-41: Imports `PublisherProperties`, `PublisherProperties4`, `PublisherProperties5` from `adcp.types.generated_poc.product`
- `test_code_generation.py` lines 38, 54: Imports `Product` and `Format` from `adcp.types.generated_poc`
- `test_cli.py`: Tests CLI by importing `Contact` from `adcp.types.generated_poc.brand_manifest`

**Why This Matters:**
The project has invested significant effort creating a stable API layer:
- `src/adcp/types/stable.py` - "shields users from internal implementation details"
- `src/adcp/__init__.py` - Re-exports stable types
- Documentation explicitly warns: "NEVER import directly from adcp.types.generated_poc"

When tests violate this boundary, they:
1. Demonstrate wrong usage patterns users might copy
2. Create false confidence that internal APIs are stable
3. Miss the purpose of the stability layer entirely
4. Will break when schema evolution adds `PublisherProperties6`

**Recommendation:**

```python
# ❌ CURRENT (Wrong - violates API boundary)
from adcp.types.generated_poc.product import (
    PublisherProperties,  # selection_type='all'
    PublisherProperties4,  # selection_type='by_id'
    PublisherProperties5,  # selection_type='by_tag'
)

# ✅ IMPROVED (Test public API behavior)
from adcp import Product
from adcp.types.generated import GetProductsResponse

def test_product_accepts_publisher_properties_by_id():
    """Product accepts publisher_properties discriminated by selection_type."""
    # Test via JSON (the actual wire format)
    product_json = {
        "product_id": "prod_123",
        "name": "Premium Placements",
        "description": "High-value ad slots",
        "publisher_properties": [
            {
                "publisher_domain": "cnn.com",
                "selection_type": "by_id",
                "property_ids": ["site1", "site2"]
            }
        ],
        "pricing_options": [
            {"model": "cpm_fixed_rate", "is_fixed": True, "cpm": 5.00}
        ]
    }

    # Validate it deserializes correctly (tests wire format)
    product = Product.model_validate(product_json)
    assert product.product_id == "prod_123"
    assert len(product.publisher_properties) == 1

    # Verify round-trip (tests serialization)
    roundtrip = Product.model_validate_json(product.model_dump_json())
    assert roundtrip.product_id == product.product_id
```

**Impact:**
- `test_discriminated_unions.py`: 15+ tests need refactoring
- `test_code_generation.py`: 2 tests need removal or refactoring
- `test_cli.py`: 1 test needs updated import

### 2. Insufficient Wire Format Testing

**Problem:** Tests construct Python objects directly, missing JSON deserialization bugs.

**Current State:**
- 8 tests use `model_validate_json()` (3% of test suite)
- 250 tests construct objects with `Type(field=value)`
- Only tests roundtrips, not actual API response payloads

**Why This Matters:**

Your CLAUDE.md explicitly states:
```
Never commit auth tokens, API keys, or secrets to version control!

❌ Compare output to output: assert result == expected_from_code
❌ Mock everything (hides serialization bugs)
✅ Call public API (tools/endpoints)
✅ Parse JSON explicitly
✅ Validate with .model_validate()
```

Yet tests do exactly what's forbidden:
```python
# Current approach - constructs Python object
agent = AuthorizedAgents(
    url="https://agent.example.com",
    authorized_for="All properties",
    authorization_type="property_ids",
    property_ids=["site1", "site2"],
)
```

This approach misses:
- Field name mismatches (`property_ids` vs `propertyIds` vs `property-ids`)
- Type coercion failures (string vs number)
- Missing required fields that have defaults
- Extra fields that should be rejected
- Serialization format of complex types (dates, URLs, nested objects)

**Recommendation:**

Create wire format test fixtures from actual protocol examples:

```python
# tests/fixtures/wire_formats/get_products_response.json
{
  "products": [
    {
      "product_id": "prod_123",
      "name": "Premium Display",
      "description": "High-visibility placements",
      "publisher_properties": [
        {
          "publisher_domain": "example.com",
          "selection_type": "by_id",
          "property_ids": ["site_1", "site_2"]
        }
      ],
      "pricing_options": [
        {
          "model": "cpm_fixed_rate",
          "is_fixed": true,
          "cpm": 5.00
        }
      ]
    }
  ]
}

# Test that validates wire format
def test_get_products_response_wire_format():
    """GetProductsResponse deserializes from actual protocol JSON."""
    fixture_path = Path(__file__).parent / "fixtures/wire_formats/get_products_response.json"
    json_bytes = fixture_path.read_bytes()

    # This is what matters - can we parse actual protocol JSON?
    response = GetProductsResponse.model_validate_json(json_bytes)

    assert len(response.products) == 1
    product = response.products[0]
    assert product.product_id == "prod_123"
    assert product.publisher_properties[0].selection_type == "by_id"

    # Verify round-trip preserves semantics
    roundtrip = GetProductsResponse.model_validate_json(response.model_dump_json())
    assert roundtrip.model_dump() == response.model_dump()
```

**Impact:** Need ~30-50 wire format tests covering:
- All request types (10 request schemas)
- All response types (10 response schemas)
- All discriminated union variants (15+ variants)
- Error cases (malformed JSON, missing fields, wrong types)

### 3. Testing Wrong Abstraction Level

**Problem:** Tests verify internal type mechanics instead of external API behavior.

**Example - Current Approach:**
```python
class TestPublisherPropertyValidation:
    """Test publisher_properties discriminated union validation."""

    def test_publisher_property_with_property_ids(self):
        """PublisherProperties4 with selection_type='by_id' requires property_ids."""
        prop = PublisherProperties4(  # Internal type!
            publisher_domain="cnn.com",
            property_ids=["site1", "site2"],
            selection_type="by_id",
        )
        assert prop.publisher_domain == "cnn.com"
```

**Questions This Raises:**
1. Why are we testing `PublisherProperties4` instead of `Product`?
2. Why test the type number (4) instead of the semantic meaning (by_id)?
3. Does a user ever construct `PublisherProperties4` directly?
4. What user problem does this test prevent?

**Recommended Approach:**
```python
class TestProductPublisherTargeting:
    """Test Product publisher_properties targeting options.

    Products can target publishers by:
    - All properties from publisher (selection_type='all')
    - Specific property IDs (selection_type='by_id')
    - Property tags (selection_type='by_tag')
    """

    async def test_get_products_returns_by_id_targeting(self, mock_agent):
        """get_products returns products with by_id publisher targeting."""
        # Mock realistic response
        response_json = {
            "products": [{
                "product_id": "premium_display",
                "name": "Premium Display",
                "publisher_properties": [{
                    "publisher_domain": "cnn.com",
                    "selection_type": "by_id",
                    "property_ids": ["mobile_app", "homepage"]
                }],
                "pricing_options": [...]
            }]
        }
        mock_agent.return_value = TaskResult(
            status=TaskStatus.COMPLETED,
            data=response_json,
            success=True
        )

        # Test the actual API
        result = await client.get_products(GetProductsRequest(brief="news sites"))

        # Verify behavior from user perspective
        assert result.success
        product = result.data.products[0]
        assert product.publisher_properties[0].selection_type == "by_id"
        assert "mobile_app" in product.publisher_properties[0].property_ids
```

**Impact:** Most tests in `test_discriminated_unions.py` need reconceptualizing.

### 4. Test Documentation Value

**Problem:** Tests don't demonstrate how users should use the library.

**Current Test Names:**
- `test_property_ids_authorization_wrong_type_fails` - tests Pydantic validation
- `test_publisher_property_by_id_without_property_ids_fails` - tests schema enforcement
- `test_invalid_destination_type_rejected` - tests discriminator logic

**What Users Actually Need to Know:**
- How do I get products from an agent?
- How do I handle sync vs async results?
- How do I target specific publisher properties?
- How do I construct requests from user input?
- How do I handle validation errors?

**Recommendation:**

Reorganize tests by user journey:

```python
# tests/user_workflows/test_product_discovery.py
"""User workflow: Discovering and filtering ad products."""

class TestProductDiscovery:
    """User discovers available ad products from publishers."""

    async def test_buyer_discovers_products_for_campaign(self):
        """Buyer gets products matching their campaign requirements."""
        # User story: Buyer wants to run a coffee brand campaign
        brief = "Coffee brand campaign targeting morning readers"

        result = await client.get_products(GetProductsRequest(brief=brief))

        assert result.success, f"Product discovery failed: {result.error}"
        assert len(result.data.products) > 0, "No products found"

        # Verify products have required fields for campaign planning
        product = result.data.products[0]
        assert product.product_id
        assert product.name
        assert product.pricing_options

    async def test_buyer_filters_products_by_publisher_domain(self):
        """Buyer filters products to specific publisher domains."""
        # Get products for specific publishers
        result = await client.get_products(
            GetProductsRequest(
                brief="Display ads",
                target_publishers=["nytimes.com", "wsj.com"]
            )
        )

        assert result.success
        for product in result.data.products:
            domains = [pp.publisher_domain for pp in product.publisher_properties]
            assert any(d in ["nytimes.com", "wsj.com"] for d in domains)

    async def test_buyer_handles_no_products_found(self):
        """Buyer gracefully handles when no products match criteria."""
        result = await client.get_products(
            GetProductsRequest(brief="extremely specific niche requirement")
        )

        # Should succeed even with zero products
        assert result.success
        assert result.data.products == []
```

**Impact:** Need to create new test organization:
- `tests/user_workflows/` - End-to-end user journeys
- `tests/wire_formats/` - Protocol compliance tests
- `tests/integration/` - Real agent integration (already exists)
- Keep `tests/test_*.py` for unit tests, but focus on public API

### 5. Semantic Alias Testing Issues

**Current State:**
`test_type_aliases.py` tests that aliases exist and point to correct types:
```python
def test_aliases_point_to_correct_types():
    """Test that aliases point to the correct generated types."""
    assert ActivateSignalSuccessResponse is ActivateSignalResponse1
```

**Problem:** This tests implementation (type identity) not behavior (can users use it?).

**Recommendation:**
```python
def test_semantic_aliases_enable_clear_code():
    """Semantic aliases make discriminated union code readable."""
    # User writes clear, self-documenting code
    success = CreateMediaBuySuccessResponse(
        media_buy_id="mb_123",
        buyer_ref="campaign_456",
        packages=[]
    )

    error = CreateMediaBuyErrorResponse(
        errors=[{"code": "budget_exceeded", "message": "Budget too low"}]
    )

    # Both serialize to valid protocol JSON
    assert "media_buy_id" in success.model_dump_json()
    assert "errors" in error.model_dump_json()

    # Type system catches mistakes
    with pytest.raises(ValidationError):
        # Can't put success fields in error response
        CreateMediaBuyErrorResponse(media_buy_id="mb_123")
```

## Testing Strategy Recommendations

### Principle: Test the External Contract, Not Internal Mechanics

**What to Test:**
1. **Wire format compatibility** - Can we parse actual protocol JSON?
2. **Public API behavior** - Does `client.get_products()` work as documented?
3. **Error handling** - Do users get helpful error messages?
4. **User workflows** - Can users accomplish their goals?

**What NOT to Test:**
1. Pydantic's discriminated union implementation (already tested by Pydantic)
2. Internal type numbers (PublisherProperties4 vs PublisherProperties5)
3. Generated code structure (unless it affects user-visible behavior)
4. Implementation details users shouldn't depend on

### Recommended Test Structure

```
tests/
├── wire_formats/           # Protocol compliance
│   ├── fixtures/          # JSON from real agents
│   ├── test_requests.py   # Request serialization
│   ├── test_responses.py  # Response deserialization
│   └── test_roundtrips.py # Serialization stability
│
├── user_workflows/        # End-to-end user journeys
│   ├── test_product_discovery.py
│   ├── test_creative_sync.py
│   ├── test_media_buy_lifecycle.py
│   └── test_audience_activation.py
│
├── integration/           # Real agent tests
│   └── test_creative_agent.py  # (already exists)
│
├── test_client.py         # ADCPClient public API
├── test_simple_api.py     # Simple API convenience layer
├── test_adagents.py       # Adagents discovery
└── test_helpers.py        # Test utilities

# Remove or radically refactor:
├── test_discriminated_unions.py  # Tests internal mechanics
├── test_type_aliases.py          # Tests type identity
└── test_code_generation.py       # Tests generated_poc internals
```

### Testing Philosophy Alignment

**Current CLAUDE.md Principles:**
- "Test behavior, not implementation"
- "Parse JSON explicitly"
- "Don't over-mock - it hides serialization bugs"
- "Test actual API calls when possible"

**How Tests Currently Violate These:**
1. **Testing implementation:** Testing `PublisherProperties4` instead of `Product` behavior
2. **Not parsing JSON:** Constructing Python objects directly
3. **Over-mocking:** Not using real JSON fixtures
4. **Not testing actual API:** Testing type construction instead of client methods

**Proposed Changes Align With:**
```python
# ✅ Test behavior (can user discover products?)
async def test_buyer_discovers_products_for_campaign()

# ✅ Parse JSON (use real wire format)
response = GetProductsResponse.model_validate_json(fixture_json)

# ✅ Don't over-mock (use actual protocol JSON)
mock_agent.return_value = TaskResult(data=json.loads(fixture))

# ✅ Test actual API (test client.get_products, not Product())
result = await client.get_products(request)
```

## Specific Refactoring Tasks

### High Priority (Breaks API Contract)

1. **Fix `test_discriminated_unions.py` imports**
   - Lines 37-41: Remove imports from `adcp.types.generated_poc.product`
   - Lines 27-36: Replace numbered types with semantic aliases where possible
   - Convert tests to use wire format (JSON) instead of direct construction

2. **Fix `test_code_generation.py`**
   - Remove lines 36-50 (`test_product_type_structure`)
   - Remove lines 52-65 (`test_format_type_structure`)
   - These test internal structure users shouldn't depend on
   - Add wire format validation tests instead

3. **Fix `test_cli.py` import**
   - Line showing `from adcp.types.generated_poc.brand_manifest import Contact`
   - Change to `from adcp import BrandManifest`
   - Test the CLI behavior, not internal type imports

### Medium Priority (Improves Test Quality)

4. **Add wire format test suite**
   - Create `tests/wire_formats/fixtures/` directory
   - Add JSON fixtures for all request/response types
   - Add `test_wire_format_compatibility.py` with 30-50 deserialization tests

5. **Add user workflow tests**
   - Create `tests/user_workflows/` directory
   - Add workflow-based tests demonstrating real usage
   - Each test should tell a story users can relate to

6. **Refactor existing tests to test behavior**
   - Focus on "can user accomplish X?" not "does type Y validate Z?"
   - Use client methods instead of direct type construction
   - Test error messages are helpful to users

### Low Priority (Nice to Have)

7. **Add property-based tests**
   - Use Hypothesis to generate valid protocol JSON
   - Verify all valid JSON deserializes correctly
   - Verify all Pydantic objects serialize to valid JSON

8. **Add performance benchmarks**
   - Test deserialization performance of large responses
   - Verify no memory leaks in async operation
   - Benchmark connection pooling effectiveness

9. **Improve test documentation**
   - Add module docstrings explaining what each test file validates
   - Add class docstrings explaining user scenarios
   - Make test names more behavior-focused

## Gap Analysis

### What We Test Well
- Client initialization and configuration ✅
- Multi-agent parallel execution ✅
- Error handling in client methods ✅
- Context manager cleanup ✅
- Simple API vs Standard API differences ✅

### What We Test Poorly
- Wire format compatibility ⚠️ (only 8 tests)
- JSON deserialization from real agents ⚠️
- User workflows end-to-end ⚠️
- Public API boundary respect ❌
- Semantic meaning of discriminated unions ⚠️

### What We Over-Test
- Pydantic validation mechanics ⬇️ (Pydantic's job, not ours)
- Internal type structure ⬇️ (implementation detail)
- Type identity checks ⬇️ (assert X is Y)
- Discriminator field presence ⬇️ (schema guarantees this)

### What We Don't Test At All
- Real agent integration for most operations ❌
- Webhook payload validation ❌
- Long-running async operations ❌
- Rate limiting and backoff ❌
- Authentication flows ❌

## Conclusion

The test suite needs fundamental refactoring to:

1. **Respect the public API boundary** - Stop importing from `generated_poc`
2. **Test wire format compatibility** - Use JSON fixtures extensively
3. **Focus on user behavior** - Test workflows, not type mechanics
4. **Demonstrate correct usage** - Tests should be examples users can learn from

This aligns with the project's explicit goals of API stability and shields users from schema evolution. The current tests undermine these goals by coupling to internal implementation details and failing to validate the actual wire protocol.

**Recommended Approach:**
- Start with wire format tests (high impact, clear scope)
- Refactor discriminated union tests to use public API (fixes contract violation)
- Add user workflow tests incrementally (improves documentation value)
- Remove or radically refactor tests that test Pydantic/internal mechanics

**Success Criteria:**
- Zero imports from `adcp.types.generated_poc` in tests
- 50+ wire format tests using JSON fixtures
- 20+ user workflow tests demonstrating real usage
- Test suite serves as reliable documentation of proper SDK usage
