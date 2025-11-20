## Type System Updates (AdCP PR #222 + #223)

### New Types

- `AssetContentType`: Consolidated enum for asset content types (replaces multiple `AssetType` variants)
- `FormatCategory`: New enum for format categories (display, video, native, dooh, etc.)

### Breaking Changes

- **Removed `CreatedPackageReference`**: PR #223 unified responses - `create_media_buy` now returns full `Package` objects instead of minimal references
- **Removed `AffectedPackage`**: Type removed from upstream schemas

### Improvements

- **Package collision resolved**: create_media_buy and update_media_buy now return identical Package structures
- **Enum collision resolved**: Clear semantic names for asset content types vs. format categories
- **Backward compatibility**: `AssetType` maintained as deprecated alias to `AssetContentType` (will be removed in 3.0.0)

### Migration Guide

```python
# Before
from adcp.types.generated_poc.brand_manifest import AssetType
from adcp.types.generated_poc.format import Type

# After
from adcp import AssetContentType, FormatCategory

# Package responses now consistent
response = await client.create_media_buy(...)
for pkg in response.packages:
    # Full Package object with all fields (budget, status, etc.)
    print(f"Package {pkg.package_id}: budget={pkg.budget}, status={pkg.status}")
```
