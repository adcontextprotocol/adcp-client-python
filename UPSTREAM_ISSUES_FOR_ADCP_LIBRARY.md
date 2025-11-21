# Upstream Issues Report for adcp Library

**Report Date:** 2025-11-20
**Reported By:** Downstream Python client during 2.9.0 migration
**Current adcp Library Version:** 2.10.0

---

## Executive Summary

A downstream client migrating to adcp library version 2.9.0 identified one remaining type export issue that needs to be addressed in the upstream adcp library. The Package status field issue was successfully resolved in 2.9.0, but the Filters type collision remains.

---

## ✅ RESOLVED in 2.9.0

### Package Status Field

**Issue:**
- Previously, Package responses returned a minimal type without the `status` field
- Clients needed workarounds to access package status

**Resolution:**
- ✅ Fixed in 2.9.0
- Package now returns full type with `status` field included
- All client code now uses the complete Package type
- No more minimal response Package

**Impact:** Issue fully resolved. No action needed.

---

## ❌ REMAINING ISSUE: Filters Type Name Collision

### Problem Description

The adcp library currently exports a generic `Filters` type that causes ambiguity because different request types require different filter fields.

**Current State:**
- `stable.Filters` is exported from the `get_products_request` module
- This Filters type has fields: `delivery_type`, `format_ids`, `format_types`, `is_fixed_price`, `min_exposures`, `standard_formats_only`
- The `list_creatives_request` module has its own Filters type with completely different fields: `status`, `format`, `formats`, `tags`, `creative_ids`, `assigned_to_package`, `created_after`, `created_before`, etc.
- Only the ProductFilters variant is exported to stable API
- CreativeFilters is not exported, forcing clients to import from `generated_poc`

**Client Workaround:**
```python
# Currently clients must do this:
from adcp.types.generated_poc.list_creatives_request import (
    Filters as LibraryCreativeFilters,
)

# Instead of the preferred:
from adcp.types.stable import CreativeFilters
```

**Why This Matters:**
- The two Filters types serve fundamentally different purposes and have different field sets
- ProductFilters is for product discovery filtering
- CreativeFilters is for creative library filtering
- They should be distinct types with clear names
- `generated_poc` module may not be stable/guaranteed API surface

**Risk:**
- Clients importing from `generated_poc` have technical debt
- If library removes or restructures `generated_poc` module, client code breaks
- Other adcp library users likely face the same issue
- Type confusion when both filter types are needed in same file

---

## Recommended Solutions

### Option 1: Immediate Fix (Patch Release)

Export both Filters types with descriptive names to distinguish them.

**In `adcp/types/aliases.py` or `adcp/types/stable.py`:**
```python
from adcp.types.generated_poc.list_creatives_request import Filters as CreativeFilters
from adcp.types.generated_poc.get_products_request import Filters as ProductFilters

__all__ = [
    "CreativeFilters",  # For ListCreativesRequest
    "ProductFilters",   # For GetProductsRequest
    # ... other exports
]
```

**Benefits:**
- Quick fix that can ship in patch release
- Maintains backward compatibility (existing `Filters` import still works)
- Clear, descriptive names prevent confusion
- Clients can migrate away from `generated_poc` imports

**Migration Path for Clients:**
```python
# Before (risky):
from adcp.types.generated_poc.list_creatives_request import Filters

# After (stable):
from adcp.types.stable import CreativeFilters
```

### Option 2: Long-term Fix (Future Major Release)

Fix the schema generator to create unique type names based on context.

**Schema Generator Changes:**
- Instead of generating multiple `Filters` types in different modules
- Generate unique names: `CreativeFilters`, `ProductFilters`, etc.
- Eliminates name collision at the source
- More maintainable long-term

**Benefits:**
- Cleaner type system
- No ambiguous generic names
- Better developer experience
- Self-documenting code

**Drawbacks:**
- Requires schema generator changes
- Breaking change for existing clients
- Needs major version bump

---

## Documentation Needs

Once fixed, the adcp library should document:

1. **Which Filters type to use for which request**
   - `CreativeFilters` → use with `ListCreativesRequest`
   - `ProductFilters` → use with `GetProductsRequest`

2. **Migration guide for 2.9.0+ users**
   - How to update imports from `generated_poc` to `stable`
   - Breaking changes in Package type (if any)
   - New features and improvements

3. **Type export guarantees**
   - What's considered stable API (`stable.*`)
   - What's internal/unstable (`generated_poc.*`)
   - Versioning policy for type changes

---

## Current State Summary

| Issue | Status | Files Affected | Workaround |
|-------|--------|---------------|------------|
| Package Status Field | ✅ RESOLVED in 2.9.0 | All adapters | Using full Package type |
| Response vs Request Package | ✅ RESOLVED in 2.9.0 | All adapters | 2.9.0 returns full Package |
| Creative Filters Type | ❌ NEEDS FIX | Type exports | Import from `generated_poc` |

---

## Client Code Reference

The downstream client has documented their workaround:

**File:** `src/core/schemas.py:36-43`
```python
# TODO(adcp-library): Move creative Filters to stable API
# Currently using generated_poc because stable.Filters is from get_products_request
# which doesn't have the fields we need (status, format, tags, etc.)
# This import is at risk if the library removes generated_poc module
from adcp.types.generated_poc.list_creatives_request import (
    Filters as LibraryCreativeFilters,
)
```

---

## Recommendation

**Priority:** Medium
**Effort:** Low (Option 1) / Medium (Option 2)
**Impact:** Improves developer experience, reduces confusion, eliminates technical debt for clients

Recommend implementing **Option 1** in next patch release (2.10.1) as it:
- Requires minimal code changes
- Maintains backward compatibility
- Immediately unblocks clients
- Can be done while planning Option 2 for next major version

---

## Contact

For questions about this report, contact the downstream Python client team that reported these issues during their 2.9.0 migration.
