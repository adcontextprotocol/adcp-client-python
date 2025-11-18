# High-Value Testing Improvements
## Focused Plan for Immediate Impact

**Date:** 2025-11-18
**Status:** Post-Initial Cleanup

---

## Executive Summary

We've made significant progress:
- ✅ Fixed `test_discriminated_unions.py` to use public API and semantic aliases
- ✅ Fixed `test_code_generation.py` to test public API behavior, not internals
- ✅ Fixed `test_cli.py` to import from public API
- ✅ Created `RECOMMENDED_TESTING_PATTERNS.py` demonstrating best practices

**Current State:** 258+ passing tests, 85%+ coverage, public API boundary mostly respected

**Remaining Gap:** Tests still don't adequately validate wire format compatibility or demonstrate real user workflows

---

## Priority 1: Add Minimal Wire Format Validation (HIGH IMPACT, LOW EFFORT)

### Why This Matters
Currently only 8 tests use `model_validate_json()`. We're not catching:
- Field name mismatches between protocol and Python (e.g., `property_ids` vs `propertyIds`)
- JSON type coercion bugs (string vs number)
- Missing/extra fields in real agent responses
- Discriminated union deserialization from actual JSON

### What To Do
Create a lightweight wire format test suite WITHOUT maintaining complex fixtures.

**Action:** Add `tests/test_wire_format_validation.py`

```python
"""Wire format validation tests.

Tests that key types can deserialize from protocol JSON and roundtrip correctly.
Uses inline JSON fixtures rather than external files for maintainability.
"""

class TestCoreTypeWireFormat:
    """Test core types deserialize from protocol JSON."""

    def test_product_deserializes_from_minimal_json(self):
        """Product deserializes from minimal valid JSON."""
        json_str = """
        {
          "product_id": "test_product",
          "name": "Test Product",
          "description": "A test product",
          "publisher_properties": [],
          "pricing_options": []
        }
        """
        from adcp import Product
        product = Product.model_validate_json(json_str)
        assert product.product_id == "test_product"

    def test_product_with_discriminated_publisher_properties(self):
        """Product handles publisher_properties discriminated unions."""
        json_str = """
        {
          "product_id": "test",
          "name": "Test",
          "description": "Test",
          "publisher_properties": [
            {
              "publisher_domain": "example.com",
              "selection_type": "by_id",
              "property_ids": ["site1", "site2"]
            }
          ],
          "pricing_options": [
            {"model": "cpm_fixed_rate", "is_fixed": true, "cpm": 5.0}
          ]
        }
        """
        from adcp import Product
        product = Product.model_validate_json(json_str)
        assert product.publisher_properties[0].selection_type == "by_id"

        # Verify roundtrip
        roundtrip = Product.model_validate_json(product.model_dump_json())
        assert roundtrip.product_id == product.product_id

    def test_create_media_buy_success_response_wire_format(self):
        """CreateMediaBuySuccessResponse deserializes from JSON."""
        json_str = """
        {
          "media_buy_id": "mb_123",
          "buyer_ref": "campaign_456",
          "packages": []
        }
        """
        from adcp import CreateMediaBuySuccessResponse
        response = CreateMediaBuySuccessResponse.model_validate_json(json_str)
        assert response.media_buy_id == "mb_123"
        assert not hasattr(response, "errors")

    def test_create_media_buy_error_response_wire_format(self):
        """CreateMediaBuyErrorResponse deserializes from JSON."""
        json_str = """
        {
          "errors": [
            {"code": "budget_exceeded", "message": "Budget too high"}
          ]
        }
        """
        from adcp import CreateMediaBuyErrorResponse
        response = CreateMediaBuyErrorResponse.model_validate_json(json_str)
        assert len(response.errors) == 1
        assert not hasattr(response, "media_buy_id")

    # Add 10-15 more tests covering:
    # - GetProductsResponse
    # - ListCreativeFormatsResponse
    # - ActivateSignal success/error
    # - BuildCreative success/error
    # - UpdateMediaBuy success/error
    # - Key discriminated unions (preview renders, assets)
```

**Effort:** 3-4 hours
**Impact:** High - Catches real serialization bugs without fixture maintenance burden
**Status:** Not started

---

## Priority 2: Simplify `test_type_aliases.py` (MEDIUM IMPACT, LOW EFFORT)

### Why This Matters
Current tests verify type identity (`assert X is Y`) which tests implementation, not behavior.
Users don't care if `CreateMediaBuySuccessResponse is CreateMediaBuyResponse1`, they care if they can use it.

### What To Do
Refactor to test usability instead of identity.

**Action:** Replace identity tests with usage tests

```python
# BEFORE (tests implementation)
def test_aliases_point_to_correct_types():
    assert CreateMediaBuySuccessResponse is CreateMediaBuyResponse1

# AFTER (tests behavior)
def test_semantic_aliases_work_in_practice():
    """Semantic aliases enable clear, readable code."""
    from adcp import CreateMediaBuySuccessResponse

    # User can construct with semantic name
    response = CreateMediaBuySuccessResponse(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[]
    )

    # Can serialize to JSON
    json_str = response.model_dump_json()
    assert "media_buy_id" in json_str

    # Can deserialize from JSON
    roundtrip = CreateMediaBuySuccessResponse.model_validate_json(json_str)
    assert roundtrip.media_buy_id == response.media_buy_id
```

**Keep:**
- Import tests (verify aliases exist)
- Export tests (verify __all__ is correct)

**Remove/Replace:**
- Type identity tests
- "Point to correct types" tests

**Effort:** 2 hours
**Impact:** Medium - Better demonstrates proper usage patterns
**Status:** Not started

---

## Priority 3: Add Simple User Workflow Tests (HIGH IMPACT, MEDIUM EFFORT)

### Why This Matters
Current tests verify individual methods work, but don't demonstrate how users accomplish goals.
New users can't look at tests to understand "How do I discover products for my campaign?"

### What To Do
Add 5-10 workflow tests that tell stories users can relate to.

**Action:** Add `tests/test_user_workflows.py`

```python
"""User workflow tests demonstrating real usage patterns."""

class TestProductDiscoveryWorkflow:
    """Buyer discovers products for advertising campaign."""

    @pytest.mark.asyncio
    async def test_buyer_discovers_products_for_campaign(self, mocker):
        """Buyer finds products matching campaign requirements.

        Story: Marketing manager at coffee brand wants to reach
        morning news readers. They use AdCP to discover suitable
        ad products from publisher.
        """
        # Setup client
        from adcp import ADCPClient, AgentConfig, Protocol, GetProductsRequest

        config = AgentConfig(
            id="publisher_agent",
            agent_uri="https://publisher.example.com",
            protocol=Protocol.A2A
        )
        client = ADCPClient(config)

        # Mock realistic response
        mock_data = {
            "products": [{
                "product_id": "morning_readers",
                "name": "Morning News Audience",
                "description": "Reach readers during breakfast hours",
                "publisher_properties": [{
                    "publisher_domain": "news.example.com",
                    "selection_type": "by_tag",
                    "property_tags": ["morning", "news"]
                }],
                "pricing_options": [{
                    "model": "cpm_fixed_rate",
                    "is_fixed": True,
                    "cpm": 4.50
                }]
            }]
        }

        from adcp.types.core import TaskResult, TaskStatus
        mocker.patch.object(
            client.adapter,
            "get_products",
            return_value=TaskResult(
                status=TaskStatus.COMPLETED,
                data=mock_data,
                success=True
            )
        )

        # User action: Discover products
        request = GetProductsRequest(
            brief="Coffee brand campaign targeting morning audience"
        )
        result = await client.get_products(request)

        # Verify from user perspective
        assert result.success, f"Discovery failed: {result.error}"
        assert len(result.data.products) > 0, "No products found"

        product = result.data.products[0]
        assert product.product_id
        assert product.name
        assert len(product.pricing_options) > 0

        # User can calculate budget
        pricing = product.pricing_options[0]
        cost_per_thousand = pricing.cpm
        assert cost_per_thousand > 0

class TestMediaBuyLifecycle:
    """Buyer creates and manages media buy."""

    @pytest.mark.asyncio
    async def test_buyer_creates_media_buy_and_checks_status(self, mocker):
        """Buyer creates media buy and monitors its status."""
        # Similar pattern - tell a story users understand
        pass

class TestCreativeOperations:
    """Buyer syncs and builds creatives."""

    @pytest.mark.asyncio
    async def test_buyer_syncs_creatives_for_campaign(self, mocker):
        """Buyer syncs creative library with publisher."""
        # Tell story about syncing creatives
        pass
```

**Coverage:**
- Product discovery (2-3 tests)
- Media buy lifecycle (2-3 tests)
- Creative operations (2-3 tests)

**Effort:** 6-8 hours
**Impact:** High - Serves as documentation for new users
**Status:** Not started

---

## Priority 4: Document Testing Guidelines (LOW IMPACT, LOW EFFORT)

### Why This Matters
New contributors don't know which patterns to follow. We have:
- `RECOMMENDED_TESTING_PATTERNS.py` (good examples)
- `test_discriminated_unions.py` (recently fixed, but complex)
- Mix of good and questionable patterns elsewhere

### What To Do
Add a simple testing guide to CLAUDE.md.

**Action:** Add section to `CLAUDE.md`

```markdown
## Testing the AdCP SDK

### Quick Rules

✅ DO:
- Import from public API: `from adcp import Product`
- Test with JSON: `Product.model_validate_json(json_str)`
- Test user workflows: "Can buyer discover products?"
- Follow examples in `tests/examples/RECOMMENDED_TESTING_PATTERNS.py`

❌ DON'T:
- Import from `generated_poc`: `from adcp.types.generated_poc...`
- Test Pydantic internals: "Does discriminator validation work?"
- Test type identity: `assert X is Y`
- Create complex fixture files we can't maintain

### Common Patterns

**Testing Deserialization:**
```python
def test_product_deserializes_from_json():
    json_str = '{"product_id": "test", ...}'
    product = Product.model_validate_json(json_str)
    assert product.product_id == "test"
```

**Testing Workflows:**
```python
@pytest.mark.asyncio
async def test_buyer_discovers_products(mocker):
    # Setup client
    client = ADCPClient(config)

    # Mock response
    mocker.patch.object(client.adapter, "get_products", return_value=...)

    # User action
    result = await client.get_products(request)

    # Verify behavior
    assert result.success
```

See `tests/examples/RECOMMENDED_TESTING_PATTERNS.py` for complete examples.
```

**Effort:** 1 hour
**Impact:** Low immediate, High long-term - Prevents regressions
**Status:** Not started

---

## Priority 5: Refactor Remaining `test_discriminated_unions.py` Tests (LOW IMPACT, HIGH EFFORT)

### Why This Matters (or Doesn't)
The current tests in `test_discriminated_unions.py` work and pass. While they could be improved, they:
- ✅ Now use public API and semantic aliases
- ✅ Test roundtrips and serialization
- ✅ Catch real validation bugs

The main issue is they're **verbose** (770 lines) and focus on mechanics over workflows.

### What To Do (If We Do Anything)
This is LOW priority because:
1. Tests are passing and catching bugs
2. Recent fixes addressed the main API boundary violations
3. Effort to refactor is high (15+ hours)
4. Benefit is mostly aesthetic (cleaner tests)

**Recommendation:** Leave as-is for now. Focus on P1-P4 first.

If we do refactor later:
- Consolidate similar tests (all the "reject wrong discriminator" tests are similar)
- Move to more scenario-based organization
- Remove tests that just verify Pydantic works

**Effort:** 15+ hours
**Impact:** Low - Tests work, just verbose
**Status:** Deferred

---

## What We're NOT Doing (And Why)

### ❌ External JSON Fixture Files
**Why not:** Maintenance burden. Files go stale, get out of sync with schemas, require documentation.
**Alternative:** Inline JSON strings in tests (easier to maintain, co-located with usage)

### ❌ Testing Pydantic's Discriminator Implementation
**Why not:** That's Pydantic's job, not ours. They test it extensively.
**What we test instead:** That our types deserialize from protocol JSON correctly.

### ❌ Testing Internal `generated_poc` Types Directly
**Why not:** Violates public API boundary, will break on schema evolution.
**What we test instead:** Public API behavior with JSON deserialization.

### ❌ Complex Multi-File Test Organization
**Why not:** Overkill for current needs. Simple structure is easier to navigate.
**Current structure works:** Tests are in `tests/test_*.py`, examples in `tests/examples/`

### ❌ Testing Against Real Agents (Except Integration Tests)
**Why not:** Flaky, slow, requires external dependencies, hard to reproduce.
**Alternative:** Mock responses with realistic data structure.

---

## Success Metrics

### Quantitative
- ✅ Zero imports from `adcp.types.generated_poc` in tests (ACHIEVED)
- 🎯 30+ tests using `model_validate_json()` (currently 8)
- 🎯 10+ user workflow tests (currently 0)
- ✅ 258+ tests passing (maintained)
- ✅ 85%+ coverage (maintained)

### Qualitative
- ✅ Tests respect public API boundary (ACHIEVED)
- 🎯 Tests demonstrate proper SDK usage to new users
- 🎯 Tests catch wire format compatibility bugs
- ✅ Tests don't over-test Pydantic internals (MOSTLY ACHIEVED)

---

## Implementation Timeline

### Week 1 (Immediate)
- **Day 1-2:** Priority 1 - Add wire format validation tests (15-20 tests)
- **Day 3:** Priority 2 - Refactor type alias tests
- **Day 4:** Priority 4 - Document testing guidelines
- **Day 5:** Review and merge

**Deliverable:** 30+ wire format tests, cleaner alias tests, documented guidelines

### Week 2 (If Needed)
- **Day 1-3:** Priority 3 - Add user workflow tests (5-10 tests)
- **Day 4-5:** Polish and documentation

**Deliverable:** Workflow tests that serve as user documentation

### Future (Deferred)
- Priority 5 - Refactor discriminated unions tests (only if time permits)
- Performance benchmarks
- Property-based testing with Hypothesis
- Contract testing against reference agents

---

## Key Insights

### What's Working Well
1. **Public API abstraction** - The stable API layer (`adcp.types.stable`) is solid
2. **Semantic aliases** - `CreateMediaBuySuccessResponse` is clearer than `CreateMediaBuyResponse1`
3. **Existing test coverage** - 258+ tests is good foundation
4. **Recent fixes** - API boundary violations are resolved

### What Needs Improvement
1. **Wire format validation** - Only 8 tests use JSON deserialization
2. **User workflow documentation** - Tests don't tell stories users understand
3. **Type alias tests** - Test identity instead of usability

### What We Learned
1. **Less is more** - Simple inline JSON beats complex fixture files
2. **Test behavior not internals** - Focus on "can user do X?" not "does type Y have field Z?"
3. **Public API matters** - Tests should demonstrate what users import
4. **Maintenance burden counts** - Complex test infrastructure has ongoing cost

---

## Recommendation

**Start with Priority 1 (wire format tests).**

This gives the highest ROI:
- Only 3-4 hours effort
- Catches real bugs (serialization, field names, type coercion)
- No external dependencies or maintenance burden
- Directly aligns with testing philosophy ("test wire format")

Then do Priority 2 (type alias refactor) and Priority 4 (documentation) for quick wins.

Only tackle Priority 3 (workflows) and Priority 5 (refactor) if there's time and clear user demand for better examples.

**Remember:** Tests that pass and catch bugs are more valuable than perfect tests that don't exist yet.
