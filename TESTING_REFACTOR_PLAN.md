# Testing Refactor Plan

## Overview

This plan outlines specific, actionable steps to fix testing issues identified in TESTING_REVIEW.md. The goal is to make tests respect the public API boundary, validate wire format compatibility, and demonstrate proper SDK usage.

## Priority Levels

- **P0 (Critical)**: Violates API contract, users might copy wrong patterns
- **P1 (High)**: Missing important coverage, impacts reliability
- **P2 (Medium)**: Quality improvements, better documentation value
- **P3 (Low)**: Nice to have, incremental improvements

## Phase 1: Fix API Boundary Violations (P0)

### Task 1.1: Fix test_discriminated_unions.py imports

**Problem:** Lines 27-41 import from `adcp.types.generated_poc`, violating public API.

**Files:** `tests/test_discriminated_unions.py`

**Changes:**
```python
# REMOVE these imports (lines 27-41):
from adcp.types.generated import (
    AuthorizedAgents,  # property_ids variant
    AuthorizedAgents1,  # property_tags variant
    ...
)
from adcp.types.generated_poc.product import (
    PublisherProperties,
    PublisherProperties4,
    PublisherProperties5,
)

# REPLACE WITH: Use public API and test via JSON
from adcp import Product
from adcp.types.generated import GetProductsResponse
```

**Refactor Approach:**
1. Keep test class structure (good organization)
2. Change from direct construction to JSON deserialization
3. Focus on behavior: "Can Product accept this JSON?"

**Example Before/After:**

```python
# BEFORE (wrong - tests internal type)
def test_publisher_property_with_property_ids(self):
    prop = PublisherProperties4(  # Internal type!
        publisher_domain="cnn.com",
        property_ids=["site1", "site2"],
        selection_type="by_id",
    )
    assert prop.selection_type == "by_id"

# AFTER (correct - tests wire format)
def test_product_with_publisher_property_by_id_from_json(self):
    """Product deserializes with selection_type='by_id' publisher targeting."""
    product_json = {
        "product_id": "test",
        "name": "Test Product",
        "description": "Test",
        "publisher_properties": [{
            "publisher_domain": "cnn.com",
            "selection_type": "by_id",
            "property_ids": ["site1", "site2"]
        }],
        "pricing_options": [{
            "model": "cpm_fixed_rate",
            "is_fixed": True,
            "cpm": 5.0
        }]
    }

    from adcp import Product
    product = Product.model_validate(product_json)

    # Verify behavior user cares about
    assert product.publisher_properties[0].selection_type == "by_id"
    assert "site1" in product.publisher_properties[0].property_ids

    # Verify round-trip
    roundtrip = Product.model_validate_json(product.model_dump_json())
    assert roundtrip.product_id == product.product_id
```

**Affected Tests (15 tests):**
- `TestPublisherPropertyValidation` (4 tests) - refactor to use Product + JSON
- `TestProductValidation` (3 tests) - refactor to use GetProductsResponse
- `TestAuthorizationDiscriminatedUnions` (4 tests) - use semantic aliases where available
- Keep destination/deployment tests (using generated types is OK for now)

**Estimate:** 3-4 hours

---

### Task 1.2: Fix test_code_generation.py

**Problem:** Lines 36-65 import from `generated_poc` and test internal structure.

**Files:** `tests/test_code_generation.py`

**Changes:**

```python
# REMOVE these tests entirely:
def test_product_type_structure(self):  # Line 36-50
def test_format_type_structure(self):   # Line 52-65

# ADD these tests instead:
def test_generated_types_export_stable_api(self):
    """Test that generated module exports stable public types."""
    from adcp.types import generated

    # Public API types should be available
    assert hasattr(generated, "Product")
    assert hasattr(generated, "Format")
    assert hasattr(generated, "GetProductsResponse")

    # Types should be usable
    Product = generated.Product
    assert hasattr(Product, "model_validate")
    assert hasattr(Product, "model_validate_json")

def test_generated_types_deserialize_from_json(self):
    """Test that generated types work with protocol JSON."""
    from adcp.types.generated import Product

    minimal_product = {
        "product_id": "test",
        "name": "Test",
        "description": "Test product",
        "publisher_properties": [],
        "pricing_options": []
    }

    # Should deserialize without error
    product = Product.model_validate(minimal_product)
    assert product.product_id == "test"
```

**Rationale:**
- Original tests couple to internal structure users shouldn't depend on
- New tests verify public API works correctly
- Focus on "can users use it?" not "does it have field X?"

**Estimate:** 1 hour

---

### Task 1.3: Fix test_cli.py import

**Problem:** Imports `Contact` from `generated_poc.brand_manifest` to test CLI.

**Files:** `tests/test_cli.py`

**Changes:**
```python
# BEFORE (line in test_init_command_optional_dependency):
"import adcp.__main__; from adcp.types.generated_poc.brand_manifest import Contact",

# AFTER:
"import adcp.__main__; from adcp import BrandManifest",
```

**Rationale:**
- Test CLI works, not that internal imports succeed
- Use public API types
- Better represents how users import

**Estimate:** 15 minutes

---

## Phase 2: Add Wire Format Testing (P1)

### Task 2.1: Create wire format fixture structure

**New Files:**
```
tests/
  wire_formats/
    __init__.py
    fixtures/
      __init__.py
      get_products_response.json
      list_creative_formats_response.json
      create_media_buy_success.json
      create_media_buy_error.json
      (... 20+ more)
    test_request_serialization.py
    test_response_deserialization.py
    test_roundtrip_compatibility.py
```

**Content Example:**

`tests/wire_formats/fixtures/get_products_response.json`:
```json
{
  "products": [
    {
      "product_id": "premium_display",
      "name": "Premium Display Ads",
      "description": "High-visibility homepage placements",
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
```

**Estimate:** 2 hours

---

### Task 2.2: Implement response deserialization tests

**New File:** `tests/wire_formats/test_response_deserialization.py`

**Content:**
```python
"""Test that all response types deserialize from protocol JSON."""

import json
from pathlib import Path

import pytest

# Import all response types from public API
from adcp import (
    GetProductsResponse,
    ListCreativeFormatsResponse,
    CreateMediaBuySuccessResponse,
    CreateMediaBuyErrorResponse,
    # ... etc
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestResponseDeserialization:
    """Verify response types handle protocol JSON correctly."""

    def test_get_products_response_from_fixture(self):
        """GetProductsResponse deserializes from fixture JSON."""
        fixture = FIXTURES_DIR / "get_products_response.json"
        json_bytes = fixture.read_bytes()

        response = GetProductsResponse.model_validate_json(json_bytes)

        assert len(response.products) > 0
        product = response.products[0]
        assert product.product_id
        assert product.name
        assert len(product.pricing_options) > 0

    def test_create_media_buy_success_from_fixture(self):
        """CreateMediaBuySuccessResponse deserializes from fixture JSON."""
        fixture = FIXTURES_DIR / "create_media_buy_success.json"
        json_bytes = fixture.read_bytes()

        response = CreateMediaBuySuccessResponse.model_validate_json(json_bytes)

        assert response.media_buy_id
        assert response.buyer_ref
        assert not hasattr(response, "errors")

    def test_create_media_buy_error_from_fixture(self):
        """CreateMediaBuyErrorResponse deserializes from fixture JSON."""
        fixture = FIXTURES_DIR / "create_media_buy_error.json"
        json_bytes = fixture.read_bytes()

        response = CreateMediaBuyErrorResponse.model_validate_json(json_bytes)

        assert len(response.errors) > 0
        assert not hasattr(response, "media_buy_id")

    # ... 30+ more tests for all response types
```

**Coverage:**
- All request types (10 schemas)
- All response types (10 schemas)
- Success/error variants (15+ discriminated unions)
- Edge cases (empty arrays, null optional fields)

**Estimate:** 4-6 hours

---

### Task 2.3: Implement roundtrip compatibility tests

**New File:** `tests/wire_formats/test_roundtrip_compatibility.py`

**Content:**
```python
"""Test that types roundtrip through JSON without data loss."""

import pytest
from adcp import GetProductsResponse, Product


class TestRoundtripCompatibility:
    """Verify serialize -> deserialize preserves data."""

    def test_product_roundtrip_preserves_all_fields(self):
        """Product survives JSON roundtrip with all data intact."""
        original_data = {
            "product_id": "test_product",
            "name": "Test Product",
            "description": "Test description",
            "publisher_properties": [{
                "publisher_domain": "example.com",
                "selection_type": "all"
            }],
            "pricing_options": [{
                "model": "cpm_fixed_rate",
                "is_fixed": True,
                "cpm": 5.50
            }]
        }

        # Create product
        product = Product.model_validate(original_data)

        # Roundtrip through JSON
        json_str = product.model_dump_json()
        roundtrip = Product.model_validate_json(json_str)

        # Should be identical
        assert roundtrip.model_dump() == product.model_dump()

    # ... 20+ more roundtrip tests
```

**Estimate:** 2-3 hours

---

## Phase 3: Add User Workflow Tests (P2)

### Task 3.1: Create user workflow test structure

**New Files:**
```
tests/
  user_workflows/
    __init__.py
    test_product_discovery.py
    test_creative_operations.py
    test_media_buy_lifecycle.py
    test_audience_activation.py
```

**Example:** `tests/user_workflows/test_product_discovery.py`

```python
"""User workflow: Discovering and evaluating ad products."""

import pytest
from unittest.mock import AsyncMock
from adcp import ADCPClient, AgentConfig, Protocol, GetProductsRequest
from adcp.types.core import TaskResult, TaskStatus


class TestProductDiscoveryWorkflow:
    """Buyer discovers products for advertising campaign."""

    @pytest.mark.asyncio
    async def test_buyer_discovers_products_for_coffee_campaign(self, mocker):
        """Buyer finds suitable products for coffee brand campaign."""
        # Setup client
        config = AgentConfig(
            id="publisher_agent",
            agent_uri="https://publisher.example.com",
            protocol=Protocol.A2A
        )
        client = ADCPClient(config)

        # Mock realistic response
        mock_response = {
            "products": [{
                "product_id": "morning_readers",
                "name": "Morning News Audience",
                "description": "Coffee drinkers reading morning news",
                "publisher_properties": [{
                    "publisher_domain": "news.example.com",
                    "selection_type": "by_tag",
                    "property_tags": ["morning", "lifestyle"]
                }],
                "pricing_options": [{
                    "model": "cpm_fixed_rate",
                    "is_fixed": True,
                    "cpm": 4.50
                }]
            }]
        }

        mock_result = TaskResult(
            status=TaskStatus.COMPLETED,
            data=mock_response,
            success=True
        )

        mocker.patch.object(
            client.adapter,
            "get_products",
            return_value=mock_result
        )

        # User action: Discover products
        request = GetProductsRequest(
            brief="Coffee brand targeting morning audience"
        )
        result = await client.get_products(request)

        # Assertions from buyer perspective
        assert result.success, f"Discovery failed: {result.error}"
        assert len(result.data.products) > 0, "No products found"

        product = result.data.products[0]
        assert product.product_id
        assert product.name
        assert len(product.pricing_options) > 0

        # Can extract pricing for budget planning
        pricing = product.pricing_options[0]
        expected_cost = pricing.cpm * 1000  # Cost per 1M impressions
        assert expected_cost > 0

    @pytest.mark.asyncio
    async def test_buyer_handles_no_matching_products(self, mocker):
        """Buyer handles gracefully when no products match."""
        # Setup...
        # Mock empty response...
        # Assert empty list is success, not error
        pass

    @pytest.mark.asyncio
    async def test_buyer_filters_by_publisher_domain(self, mocker):
        """Buyer discovers products from specific publishers."""
        # Test filtering...
        pass

    # ... 10+ more workflow tests
```

**Coverage:**
- Product discovery (5 tests)
- Creative operations (5 tests)
- Media buy lifecycle (8 tests)
- Audience activation (5 tests)

**Estimate:** 8-10 hours

---

## Phase 4: Refactor Existing Tests (P2)

### Task 4.1: Refactor test_discriminated_unions.py

**Strategy:**
- Keep good tests (response deserialization, roundtrips)
- Refactor to use public API and JSON fixtures
- Remove tests of Pydantic mechanics
- Add user perspective to test names

**Example Refactors:**

```python
# BEFORE: Tests internal type mechanics
class TestPublisherPropertyValidation:
    def test_publisher_property_with_property_ids(self):
        prop = PublisherProperties4(...)

# AFTER: Tests user-facing behavior
class TestProductPublisherTargeting:
    """Test Product publisher_properties targeting options."""

    def test_product_with_specific_property_targeting_from_json(self):
        """Product deserializes with specific property ID targeting."""
        # Use JSON, test behavior, user-focused name
```

**Keep (good tests):**
- Roundtrip tests (8 tests)
- Response variant tests (4 tests)

**Refactor (API boundary violations):**
- Authorization tests (5 tests) - use public types
- Publisher property tests (8 tests) - use JSON + Product
- Product validation tests (3 tests) - use GetProductsResponse

**Remove (test Pydantic mechanics):**
- Tests that discriminator enforcement works (Pydantic's job)
- Tests that Literal types work (Pydantic's job)
- Tests of field presence based on discriminator (schema guarantees)

**Estimate:** 4-5 hours

---

### Task 4.2: Simplify test_type_aliases.py

**Current:** Tests type identity (`assert X is Y`)

**Refactor to:** Test usability

```python
# BEFORE
def test_aliases_point_to_correct_types(self):
    assert CreateMediaBuySuccessResponse is CreateMediaBuyResponse1

# AFTER
def test_semantic_aliases_work_in_practice(self):
    """Semantic aliases enable clear, readable code."""
    # User creates response with semantic name
    success = CreateMediaBuySuccessResponse(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[]
    )

    # Can serialize to valid JSON
    json_str = success.model_dump_json()
    assert "media_buy_id" in json_str

    # Can deserialize back
    roundtrip = CreateMediaBuySuccessResponse.model_validate_json(json_str)
    assert roundtrip.media_buy_id == success.media_buy_id
```

**Estimate:** 2 hours

---

## Phase 5: Documentation and Guidelines (P3)

### Task 5.1: Update CLAUDE.md with testing examples

**Add Section:** "Testing Best Practices for AdCP SDK"

**Content:**
```markdown
## Testing AdCP SDK Code

### Test Public API, Not Internals

✅ CORRECT:
```python
from adcp import Product, GetProductsRequest

def test_product_deserialization():
    json_data = {...}
    product = Product.model_validate(json_data)
    assert product.product_id
```

❌ WRONG:
```python
from adcp.types.generated_poc.product import PublisherProperties4

def test_publisher_properties4_construction():
    prop = PublisherProperties4(...)  # Internal type!
```

### Use Wire Format (JSON) in Tests

✅ CORRECT:
```python
product_json = '{"product_id": "test", ...}'
product = Product.model_validate_json(product_json)
```

❌ WRONG:
```python
product = Product(product_id="test", ...)  # Misses serialization bugs
```

### Test User Workflows, Not Type Mechanics

✅ CORRECT:
```python
async def test_buyer_discovers_products():
    """Buyer finds products for campaign."""
    result = await client.get_products(request)
    assert result.success
```

❌ WRONG:
```python
def test_discriminator_field_is_literal():
    """Test Pydantic Literal type works."""  # Pydantic's job
```

### Resources

- See `tests/examples/RECOMMENDED_TESTING_PATTERNS.py` for examples
- See `tests/wire_formats/` for protocol JSON fixtures
- See `tests/user_workflows/` for workflow test patterns
```

**Estimate:** 1 hour

---

### Task 5.2: Create testing contribution guide

**New File:** `CONTRIBUTING_TESTING.md`

**Content:**
- When to add tests
- How to structure test classes
- Where to put fixtures
- How to name tests from user perspective
- Examples of good vs bad tests
- PR review checklist for tests

**Estimate:** 2 hours

---

## Implementation Timeline

### Week 1: Fix Critical Issues (P0)
- Day 1-2: Task 1.1 (Fix test_discriminated_unions.py imports)
- Day 3: Task 1.2 (Fix test_code_generation.py)
- Day 4: Task 1.3 (Fix test_cli.py import)
- Day 5: Code review and fixes

**Deliverable:** All tests respect public API boundary

---

### Week 2: Add Wire Format Tests (P1)
- Day 1: Task 2.1 (Create fixture structure)
- Day 2-3: Task 2.2 (Response deserialization tests)
- Day 4: Task 2.3 (Roundtrip tests)
- Day 5: Review and expand coverage

**Deliverable:** 50+ wire format tests using JSON fixtures

---

### Week 3: Add User Workflows (P2)
- Day 1-2: Task 3.1 (Product discovery workflows)
- Day 3: Creative operations workflows
- Day 4: Media buy lifecycle workflows
- Day 5: Audience activation workflows

**Deliverable:** 20+ workflow tests demonstrating usage

---

### Week 4: Refactor and Document (P2-P3)
- Day 1-2: Task 4.1 (Refactor discriminated unions tests)
- Day 3: Task 4.2 (Simplify type aliases tests)
- Day 4: Task 5.1 (Update CLAUDE.md)
- Day 5: Task 5.2 (Create contribution guide)

**Deliverable:** Clean, well-documented test suite

---

## Success Metrics

### Quantitative Goals
- ✅ Zero imports from `adcp.types.generated_poc` in tests
- ✅ 50+ wire format tests using `model_validate_json()`
- ✅ 20+ user workflow tests
- ✅ Test coverage maintained at 85%+
- ✅ All 258+ existing tests still pass

### Qualitative Goals
- ✅ Tests demonstrate correct SDK usage
- ✅ Tests serve as living documentation
- ✅ Tests catch protocol compatibility bugs
- ✅ Tests respect public API boundaries
- ✅ New contributors can learn from tests

---

## Risk Mitigation

### Risk: Breaking existing tests during refactor

**Mitigation:**
1. Run full test suite before each change
2. Commit working state frequently
3. Keep test names/structure where possible
4. Update tests incrementally, not all at once

### Risk: Wire format fixtures become stale

**Mitigation:**
1. Document fixture source (which agent/version)
2. Add CI check that validates fixtures against schemas
3. Update fixtures when schemas change
4. Version fixtures alongside schema versions

### Risk: Too many tests make CI slow

**Mitigation:**
1. Use pytest markers to separate fast/slow tests
2. Run wire format tests in parallel
3. Cache test fixtures
4. Profile and optimize slow tests

---

## Notes

### What NOT to Change
- Keep `test_client.py` - tests public API correctly
- Keep `test_simple_api.py` - demonstrates API usage well
- Keep `test_adagents.py` - good domain validation tests
- Keep integration tests - valuable real-agent validation

### Future Considerations
- Property-based testing with Hypothesis
- Performance benchmarks
- Load testing for async operations
- Fuzz testing for JSON parsing
- Contract testing against reference agents

---

## Questions to Resolve

1. **Fixture Management:** Should we generate fixtures from schemas or use real agent responses?
   - **Recommendation:** Start with hand-crafted realistic fixtures, later add schema-based generation

2. **Test Organization:** Should workflow tests be in separate directory or integrated?
   - **Recommendation:** Separate directory for clarity, can import shared fixtures

3. **Coverage Goals:** What's acceptable coverage level?
   - **Recommendation:** 85%+ overall, 95%+ for public API, 70%+ for generated types

4. **Performance:** How to balance comprehensive tests with CI speed?
   - **Recommendation:** Use pytest markers, run critical tests on every commit, full suite nightly
