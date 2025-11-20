# AdCP Schema Naming Collision Fix Proposal

**Date**: 2025-11-19 (Updated: 2025-11-20)
**Submitted by**: Python SDK Maintainers
**Status**: ✅✅✅ **ALL RESOLVED!** (PR #222 + PR #223)

## Update: ALL Collisions Resolved! 🎉

**PR #222** ("Consolidate enum types to eliminate naming collisions") - **MERGED**
- ✅ **AssetType** → Now `AssetContentType` (13 values, consolidated from 4 variants)
- ✅ **Type enums** → Now `AssetContentType` + `FormatCategory` (separated semantic types)

**PR #223** ("Make create_media_buy and update_media_buy responses consistent") - **OPEN**
- ✅ **Package** → Unified to always return full Package objects (no more minimal reference variant)

## Executive Summary

~~The current AdCP schemas contain type name collisions that prevent SDK code generators from creating clean, type-safe APIs. We propose renaming 3 types in the schemas to eliminate ambiguity and enable better downstream tooling.~~

**UPDATE**: PR #222 resolved enum collisions. One remaining collision: the `Package` type used for both full state objects and lightweight response references.

## Problem

Multiple schema files define types with identical names but different semantics. Code generators cannot distinguish between them, forcing SDKs to expose internal workarounds or incomplete APIs.

## ✅ RESOLVED: Package Collision (PR #223)

### Better Solution Than Proposed!

**Original Problem:**
- `package.json` defined full `Package` (12 fields: full state)
- `create-media-buy-response.json` defined minimal `Package` (2 fields: just IDs)
- Code generators couldn't distinguish between them

**Our Proposal Was:**
Split into `PackageStatus` + `PackageReference`

**What PR #223 Does (Better!):**
Unifies responses to **always return full Package objects**

**Changes:**
```json
// create-media-buy-response.json - NOW returns full Package
{
  "packages": [{
    "package_id": "pkg_001",
    "buyer_ref": "package_ref",
    "product_id": "ctv_premium",    // NEW
    "budget": 50000,                // NEW
    "status": "active",             // NEW
    "pacing": "even",               // NEW
    "pricing_option_id": "cpm-fixed", // NEW
    "creative_assignments": [],     // NEW
    "format_ids_to_provide": []     // NEW
  }]
}

// update-media-buy-response.json - Already returned full Package (no change)
```

**Benefits:**
- ✅ **No collision**: Only one Package type exists now
- ✅ **Consistency**: Create and update return identical structures
- ✅ **Better UX**: Buyers see complete state without follow-up calls
- ✅ **Backward compatible**: Additive change only (minor bump)
- ✅ **Matches industry patterns**: Google Ad Manager, The Trade Desk do the same

**Result**: Type generators produce clean, single `Package` type with no collisions or workarounds needed.

### ✅ RESOLVED: Type Enums (PR #222)

**What changed:**
- Created `/schemas/v1/enums/asset-content-type.json` (13 values)
- Created `/schemas/v1/enums/format-category.json` (7 values)
- All schemas now reference these via `$ref`
- Removed orphaned `asset-type.json`

**Result**: Type generators now produce `AssetContentType` and `FormatCategory` with no collisions.

### ✅ RESOLVED: AssetType Consolidation (PR #222)

**What changed:**
- Single canonical `AssetContentType` enum with 13 values
- All schemas (`format.json`, `brand-manifest.json`, `list-creative-formats-request.json`) reference it
- Filter APIs accept full enum (no artificial subsets)
- Documentation notes typical usage where needed

**Result**: No more AssetType variants or collisions.

## Next Steps for Python SDK

### When PR #223 Merges

Once PR #223 is merged (currently open), the Python SDK can:

#### 1. Sync Schemas
```bash
python3 scripts/sync_schemas.py
```

This pulls both PR #222 (merged) and PR #223 (pending) changes.

#### 2. Regenerate Types
```bash
uv run python scripts/generate_types.py
```

This will generate:
- `AssetContentType` enum (no more AssetType collisions)
- `FormatCategory` enum (no more Type collisions)
- Single `Package` class (no more Package collisions!)
- No more `_PackageFromPackage` or `_PackageFromCreateMediaBuyResponse` workarounds

#### 3. Update Stable API Exports
Edit `src/adcp/types/stable.py`:
```python
# Import new enum names from PR #222
from adcp.types._generated import (
    AssetContentType,  # New canonical enum
    FormatCategory,    # New semantic type
    Package,           # Now clean! No collision workaround needed
    # ... rest
)

# Remove old collision workaround:
# from adcp.types._generated import _PackageFromPackage as Package  # DELETE THIS
```

#### 4. Add Deprecation Aliases (backward compatibility)
```python
# Deprecated aliases - remove in 3.0.0
AssetType = AssetContentType
# Note: Don't alias Type - it was ambiguous (asset types vs format categories)

__all__ = [
    "AssetContentType",
    "FormatCategory",
    "Package",
    "AssetType",  # Deprecated, will be removed in 3.0.0
]
```

#### 5. Remove Internal Import Workarounds
Search and remove any uses of:
- `from adcp.types.generated_poc.* import AssetType`
- `from adcp.types.generated_poc.* import Type`
- `from adcp.types._generated import _PackageFromCreateMediaBuyResponse`

All should now use stable imports.

## Benefits

1. **Type Safety**: SDKs can generate clean, unambiguous types
2. **Developer Experience**: Autocomplete works correctly, no internal imports needed
3. **Maintainability**: Fewer workarounds in SDK codebases
4. **Future-Proof**: Prevents accumulation of more collisions

## Migration Impact (Python SDK)

### Changes from PR #222
- ✅ `AssetType` → `AssetContentType` (consolidated enum)
- ✅ `Type` → `AssetContentType` + `FormatCategory` (separated enums)

SDK will provide backward-compatible aliases during 2.x releases:
```python
AssetType = AssetContentType  # Deprecated alias
```

### All Breaking Changes Resolved!
- ✅ `AssetType` → `AssetContentType` (PR #222, merged)
- ✅ `Type` → `AssetContentType` + `FormatCategory` (PR #222, merged)
- ✅ `Package` → Unified full Package type (PR #223, open)

## No Remaining Questions!

All collisions have been addressed by the AdCP team. The solutions chosen (especially PR #223's response unification) are superior to our original proposals.

## Acknowledgments

Huge thanks to the AdCP team for:
- **PR #222**: Enum consolidation that follows best practices for type safety
- **PR #223**: Response unification that's even better than our proposed type split - improves consistency AND eliminates collisions!

These changes will significantly improve SDK ergonomics and make AdCP one of the most SDK-friendly ad tech protocols.

## SDK Workarounds (Current State)

```python
# Python SDK currently must do this:
from adcp.types._generated import (
    _PackageFromPackage as Package,  # Qualified import
    _PackageFromCreateMediaBuyResponse,  # Not exported
)

from adcp.types.generated_poc.format import Type as FormatType
from adcp.types.generated_poc.asset_type import Type as AssetContentType

# This is fragile and breaks on schema regeneration
```

## References

- Full analysis: `UPSTREAM_SCHEMA_RECOMMENDATIONS.md`
- Python SDK: https://github.com/conductor-sdk/adcp-client-python
- Affected schemas:
  - `package.json`
  - `create-media-buy-response.json`
  - `asset-type.json`
  - `format.json`
  - `list-creative-formats-request.json`
  - `brand-manifest.json`

---

**Next Steps**: Awaiting AdCP team feedback on proposed changes and timeline.
