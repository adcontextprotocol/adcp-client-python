## Type System Updates (AdCP PR #222 + #223)

This release integrates upstream schema improvements that resolved all type naming collisions in the AdCP protocol, enabling cleaner SDK APIs and better type safety.

### New Types

- `AssetContentType`: Consolidated enum for asset content types (13 values: image, video, audio, text, html, css, javascript, vast, daast, promoted_offerings, url, markdown, webhook)
- `FormatCategory`: New enum for format categories (7 values: audio, video, display, native, dooh, rich_media, universal)

### Breaking Changes

- **Removed `CreatedPackageReference`**: PR #223 unified responses - `create_media_buy` now returns full `Package` objects instead of minimal references
- **Removed `AffectedPackage`**: Type removed from upstream schemas

### Improvements

- **Package collision resolved**: `create_media_buy` and `update_media_buy` now return identical Package structures with all fields (budget, status, creative_assignments, etc.)
- **Enum collisions resolved**: Clear semantic separation between `AssetContentType` (what an asset contains) and `FormatCategory` (how a format renders)
- **No more qualified imports**: All types now cleanly exported from stable API - no need for `_PackageFromPackage` or similar workarounds
- **Backward compatibility**: SDK maintains `AssetType` as deprecated alias during 2.x releases (will be removed in 3.0.0)

### Migration Guide

```python
# Before (using internal generated types)
from adcp.types.generated_poc.brand_manifest import AssetType
from adcp.types.generated_poc.format import Type

# After (using stable public API)
from adcp import AssetContentType, FormatCategory

# Package responses now consistent across create and update
response = await client.create_media_buy(...)
for pkg in response.packages:
    # Full Package object with all fields available immediately
    print(f"Package {pkg.package_id}:")
    print(f"  Budget: {pkg.budget}")
    print(f"  Status: {pkg.status}")
    print(f"  Product: {pkg.product_id}")
    print(f"  Pricing: {pkg.pricing_option_id}")
```

### Upstream Schema Quality

The AdCP team removed orphaned schemas as part of PR #222 - `asset-type.json` was properly cleaned up from the repository. This SDK release reflects that cleanup.

Additional opportunities for future schema improvements:

1. **Enum organization**: Consider consolidating all reusable enums into `/schemas/v1/enums/` directory
2. **Discriminator standardization**: Multiple patterns used (`type`, `output_format`, `delivery_type`, etc.) - could standardize on `type`
3. **Schema-level validation**: Add JSON Schema 2019-09 `discriminator` keyword for better tooling support
4. **Enum versioning policy**: Document whether new enum values are minor vs. major version changes

Overall, the AdCP schemas are in excellent shape - these are minor polish suggestions.
