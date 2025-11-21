# Filter Types Migration Guide

## What Changed in v2.10.1

We've added strongly-typed filter classes to improve type safety and developer experience when working with AdCP filters.

### New Exports

```python
from adcp import ProductFilters, CreativeFilters, SignalFilters, Filters
```

## Why This Change?

Previously, all filter parameters used generic `dict[str, Any]` types:

```python
# Before: No type safety, no autocomplete
request = GetProductsRequest(
    brief="Coffee brands",
    filters={"delivery_type": "guaranteed", "min_exposures": 10000}
)
```

**Problems with the old approach:**
- ❌ No IDE autocomplete for filter fields
- ❌ Typos in field names not caught until runtime
- ❌ No documentation of available filter options
- ❌ Type errors (e.g., passing string instead of boolean) not caught

**With strongly-typed filters:**
- ✅ IDE autocomplete shows all available fields
- ✅ Type checking catches errors at compile time
- ✅ Inline documentation for each field
- ✅ Clear separation between different filter types

## Migration Paths

### Option 1: Keep Using Dicts (No Changes Required)

Your existing code continues to work without any changes:

```python
from adcp import GetProductsRequest

# This still works exactly as before
request = GetProductsRequest(
    brief="Coffee brands",
    filters={"delivery_type": "guaranteed", "min_exposures": 10000}
)
```

**Backward compatibility is 100% maintained.**

### Option 2: Migrate to Typed Filters (Recommended)

For better type safety and IDE support, migrate to strongly-typed filters:

```python
from adcp import GetProductsRequest, ProductFilters

# New: Strongly-typed filters
filters = ProductFilters(
    delivery_type="guaranteed",
    min_exposures=10000,
    standard_formats_only=True
)

request = GetProductsRequest(
    brief="Coffee brands",
    filters=filters.model_dump(exclude_none=True)
)
```

## Filter Type Reference

### ProductFilters

Use for `GetProductsRequest` when discovering available advertising products.

```python
from adcp import ProductFilters

filters = ProductFilters(
    delivery_type="guaranteed",              # "guaranteed" | "non_guaranteed"
    is_fixed_price=True,                    # bool
    format_types=["video", "display"],      # list of "video" | "display" | "audio"
    format_ids=["format-1", "format-2"],    # list[str]
    standard_formats_only=True,             # bool
    min_exposures=10000                     # int (>= 1)
)
```

**Available fields:**
- `delivery_type` - Filter by delivery type (guaranteed/non_guaranteed)
- `is_fixed_price` - Filter for fixed price vs auction products
- `format_types` - Filter by format types (video, display, audio)
- `format_ids` - Filter by specific format IDs
- `standard_formats_only` - Only return products accepting IAB standard formats
- `min_exposures` - Minimum exposures/impressions needed

### CreativeFilters

Use for `ListCreativesRequest` when querying creative assets from the library.

```python
from adcp import CreativeFilters

filters = CreativeFilters(
    status="approved",                       # Creative status
    formats=["video", "display"],            # list[str]
    tags=["holiday", "2024"],               # list[str] - all must match
    tags_any=["summer", "spring"],          # list[str] - any must match
    created_after="2024-01-01T00:00:00Z",   # ISO 8601 datetime
    created_before="2024-12-31T23:59:59Z",  # ISO 8601 datetime
    assigned_to_package="pkg-123",          # str
    unassigned=False,                       # bool
    has_performance_data=True,              # bool
)
```

**Available fields:**
- `format` - Filter by single creative format type
- `formats` - Filter by multiple creative format types
- `status` - Filter by approval status (draft, pending_review, approved, rejected, archived)
- `statuses` - Filter by multiple approval statuses
- `tags` - Filter by tags (all tags must match)
- `tags_any` - Filter by tags (any tag must match)
- `name_contains` - Filter by creative names containing text (case-insensitive)
- `creative_ids` - Filter by specific creative IDs (max 100)
- `created_after` - Filter creatives created after date
- `created_before` - Filter creatives created before date
- `updated_after` - Filter creatives updated after date
- `updated_before` - Filter creatives updated before date
- `assigned_to_package` - Filter creatives assigned to specific package
- `assigned_to_packages` - Filter creatives assigned to any of these packages
- `unassigned` - Filter for unassigned (true) or assigned (false) creatives
- `has_performance_data` - Filter creatives with performance data

### SignalFilters

Use for `GetSignalsRequest` when discovering audience signals.

```python
from adcp import SignalFilters

# Currently a flexible base class for forward compatibility
filters = SignalFilters()
```

**Note:** The signals schema defines filters as a flexible object. This class provides a structured base that will be extended as signal filtering requirements evolve.

### Generic Filters Alias

For backward compatibility, a generic `Filters` alias is available (maps to `ProductFilters`):

```python
from adcp import Filters

# This is actually ProductFilters under the hood
filters = Filters(delivery_type="guaranteed")
```

## Migration Examples

### Example 1: GetProductsRequest

**Before:**
```python
from adcp import GetProductsRequest

request = GetProductsRequest(
    brief="Premium video inventory",
    filters={
        "delivery_type": "guaranteed",
        "format_types": ["video"],
        "min_exposures": 10000
    }
)
```

**After:**
```python
from adcp import GetProductsRequest, ProductFilters

filters = ProductFilters(
    delivery_type="guaranteed",
    format_types=["video"],
    min_exposures=10000
)

request = GetProductsRequest(
    brief="Premium video inventory",
    filters=filters.model_dump(exclude_none=True)
)
```

### Example 2: ListCreativesRequest

**Before:**
```python
from adcp.types.tasks import ListCreativesRequest

request = ListCreativesRequest(
    filters={
        "status": "approved",
        "tags": ["holiday", "2024"],
        "has_performance_data": True
    }
)
```

**After:**
```python
from adcp import ListCreativesRequest, CreativeFilters

filters = CreativeFilters(
    status="approved",
    tags=["holiday", "2024"],
    has_performance_data=True
)

request = ListCreativesRequest(
    filters=filters.model_dump(exclude_none=True)
)
```

## Benefits of Migrating

### 1. IDE Autocomplete

Typed filters provide autocomplete for all available fields:

![IDE showing autocomplete for CreativeFilters fields]

### 2. Type Checking

Catch type errors at compile time instead of runtime:

```python
# ❌ This will be caught by type checker:
filters = ProductFilters(
    delivery_type="invalid_type",  # Type error: not a valid literal
    min_exposures="ten thousand"    # Type error: expected int, got str
)

# ✅ This passes type checking:
filters = ProductFilters(
    delivery_type="guaranteed",
    min_exposures=10000
)
```

### 3. Inline Documentation

Hover over fields in your IDE to see documentation:

```python
filters = CreativeFilters(
    status="approved",  # Hover shows: "Filter by creative approval status"
    has_performance_data=True  # Hover shows: "Filter creatives that have performance data"
)
```

### 4. Prevent Typos

Field name typos are caught immediately:

```python
# ❌ Type error: 'statuss' is not a valid field
filters = CreativeFilters(statuss="approved")

# ✅ Correct field name
filters = CreativeFilters(status="approved")
```

## Gradual Migration Strategy

You don't need to migrate all at once. We recommend:

1. **Start with new code** - Use typed filters for all new features
2. **Migrate incrementally** - Update existing code as you touch it
3. **No rush** - Dict-based filters continue to work indefinitely

## FAQ

### Q: Do I have to migrate?

**A:** No. This is a purely additive change. Existing dict-based filters continue to work.

### Q: Will dict-based filters be deprecated?

**A:** No plans to deprecate them. They'll continue to be supported.

### Q: Can I mix typed and dict-based filters?

**A:** Yes. You can use typed filters in some places and dicts in others.

### Q: What if I need a filter field not in the typed class?

**A:** Use the dict approach for that request. Typed filters only include fields defined in the schemas.

### Q: How do I convert a typed filter to a dict?

**A:** Use `.model_dump(exclude_none=True)`:

```python
filters = ProductFilters(delivery_type="guaranteed")
filters_dict = filters.model_dump(exclude_none=True)
# Result: {"delivery_type": "guaranteed"}
```

### Q: Can I use typed filters with validation?

**A:** Yes! Pydantic validates the types automatically:

```python
try:
    filters = ProductFilters(min_exposures=-1)  # Invalid: must be >= 1
except ValidationError as e:
    print(e)
```

## Need Help?

- See examples: `examples/using_filter_types.py`
- Check the source: `src/adcp/types/aliases.py`
- Report issues: https://github.com/adcontextprotocol/adcp-client-python/issues
