# Changelog

## [2.0.0] - 2025-11-15

### ⚠️ BREAKING CHANGES

#### Migration to datamodel-code-generator

This release completes the migration from custom type generation to the industry-standard `datamodel-code-generator` tool. This brings improved type safety, better Pydantic v2 support, and alignment with JSON Schema specifications.

**Breaking Changes:**

1. **Dictionary access no longer supported** - All generated types are now proper Pydantic models, not TypedDicts
   - ❌ **OLD**: `format.assets_required[0]["asset_id"]`
   - ✅ **NEW**: `format.assets_required[0].asset_id`
   - This affects ALL nested objects throughout the SDK (assets, deployments, destinations, etc.)

2. **Backward compatibility aliases removed** - The 23 type aliases introduced in v1.6.x have been removed
   - Use numbered discriminated union variants directly (e.g., `Destination1`, `Destination2` instead of `PlatformDestination`, `AgentDestination`)
   - Or import from discriminated union types (e.g., `Destination`)
   - See migration guide below for full list

3. **Simplified main module exports** - The main `adcp` module no longer re-exports all generated types
   - ✅ **NEW**: Import from `adcp.types.generated` directly: `from adcp.types.generated import Product, Format, etc.`
   - Or access via module: `from adcp.types import generated; generated.Product`

**Removed Type Aliases:**

- `ActivateSignalSuccess` / `ActivateSignalError` → Use `ActivateSignalResponse1` / `ActivateSignalResponse2`
- `CreateMediaBuySuccess` / `CreateMediaBuyError` → Use `CreateMediaBuyResponse1` / `CreateMediaBuyResponse2`
- `UpdateMediaBuySuccess` / `UpdateMediaBuyError` → Use `UpdateMediaBuyResponse1` / `UpdateMediaBuyResponse2`
- `SyncCreativesSuccess` / `SyncCreativesError` → Use `SyncCreativesResponse1` / `SyncCreativesResponse2`
- `PlatformDestination` / `AgentDestination` → Use `Destination1` / `Destination2`
- `PlatformDeployment` / `AgentDeployment` → Use `Deployment1` / `Deployment2`
- `Segment_idActivationKey` / `Key_valueActivationKey` → Use `ActivationKey1` / `ActivationKey2`
- `UrlVastAsset` / `InlineVastAsset` → Use `VastAsset1` / `VastAsset2`
- `UrlDaastAsset` / `InlineDaastAsset` → Use `DaastAsset1` / `DaastAsset2`
- `MediaSubAsset` / `TextSubAsset` → Use `SubAsset1` / `SubAsset2`
- `UrlPreviewRender` / `HtmlPreviewRender` / `BothPreviewRender` → Use `PreviewRender1` / `PreviewRender2` / `PreviewRender3`
- `ListCreativeFormatsRequest` / `ListCreativeFormatsResponse` → Use `ListCreativeFormatsRequestCreativeAgent` / `ListCreativeFormatsResponseCreativeAgent`

### Features

* **Runtime Validation** - Added validation utilities for constraints not enforced by upstream JSON schemas:
  - `validate_adagents()` - Validates mutual exclusivity in adagents.json authorization fields
  - `validate_product()` - Validates publisher_properties mutual exclusivity
  - `validate_agent_authorization()` - Validates agent authorization field constraints
  - `validate_publisher_properties_item()` - Validates property_ids/property_tags mutual exclusivity
  - These validators are automatically applied by `fetch_adagents()` but can also be used standalone

* **Schema Validation Gap Documentation** - Added `SCHEMA_VALIDATION_GAPS.md` documenting upstream schema issues where mutual exclusivity is documented but not enforced

### Migration Guide

**Update dictionary access to attribute access:**

```python
# Before (v1.x)
asset_id = format.assets_required[0]["asset_id"]
deployment = signal["deployments"][0]["platform"]

# After (v2.0)
asset_id = format.assets_required[0].asset_id
deployment = signal.deployments[0].platform
```

**Update type imports:**

```python
# Before (v1.x)
from adcp import (
    ActivateSignalSuccess,
    ActivateSignalError,
    PlatformDestination,
    AgentDestination,
)

# After (v2.0) - Option 1: Use numbered variants
from adcp.types.generated import (
    ActivateSignalResponse1,  # Success
    ActivateSignalResponse2,  # Error
    Destination1,             # Platform
    Destination2,             # Agent
)

# After (v2.0) - Option 2: Use union types
from adcp.types.generated import (
    ActivateSignalResponse,   # Union of Response1 | Response2
    Destination,              # Union of Destination1 | Destination2
)
```

**Check type discriminators in conditional logic:**

```python
# Before (v1.x) - used isinstance with aliases
if isinstance(destination, PlatformDestination):
    print(f"Platform: {destination.platform}")

# After (v2.0) - check discriminator field
if destination.root.type == "platform":
    print(f"Platform: {destination.root.platform}")

# Or unwrap the RootModel
dest = destination.root
if dest.type == "platform":
    print(f"Platform: {dest.platform}")
```

## [1.6.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.6.0...v1.6.1) (2025-11-13)


### Bug Fixes

* context field for custom injected types ([e37c095](https://github.com/adcontextprotocol/adcp-client-python/commit/e37c095ffcca8e8586aeea84a854721c64f990d1))
* context field for custom injected types ([8e96c6e](https://github.com/adcontextprotocol/adcp-client-python/commit/8e96c6e06782eab3c78effbfc2e16f40fd2f7466))
* mypy failures ([c186472](https://github.com/adcontextprotocol/adcp-client-python/commit/c186472304eb494dda95fd713993a289269a43f2))
* ruff check ([b8296e5](https://github.com/adcontextprotocol/adcp-client-python/commit/b8296e52da0c3d1d9f4cd524946a47519cfd802a))

## [1.6.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.5.0...v1.6.0) (2025-11-13)


### Features

* add adagents.json validation and discovery ([#42](https://github.com/adcontextprotocol/adcp-client-python/issues/42)) ([4ea16a1](https://github.com/adcontextprotocol/adcp-client-python/commit/4ea16a141a52aa1996420b9d8042d3f8d9d8ddd6))
* add AdCPBaseModel with exclude_none serialization ([#40](https://github.com/adcontextprotocol/adcp-client-python/issues/40)) ([c3cd590](https://github.com/adcontextprotocol/adcp-client-python/commit/c3cd5902d1ea9ad62e3e61b6e843b64e768feead))

## [1.5.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.4.1...v1.5.0) (2025-11-13)


### Features

* generate type adcp context field ([38600f2](https://github.com/adcontextprotocol/adcp-client-python/commit/38600f2f10a922efb3d9e05b81e37bafe219baa9))
* generate type adcp context field ([92e90c9](https://github.com/adcontextprotocol/adcp-client-python/commit/92e90c96f5d757d119cc6e3ddb7f77078f8e979c))

## [1.4.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.4.0...v1.4.1) (2025-11-11)


### Bug Fixes

* handle MCP error responses without structuredContent ([#34](https://github.com/adcontextprotocol/adcp-client-python/issues/34)) ([52956bc](https://github.com/adcontextprotocol/adcp-client-python/commit/52956bccb86c649024c4b111a890ac43e30d321b))

## [1.4.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.3.1...v1.4.0) (2025-11-10)


### Features

* add ergonomic .simple accessor to all ADCPClient instances ([#32](https://github.com/adcontextprotocol/adcp-client-python/issues/32)) ([5404325](https://github.com/adcontextprotocol/adcp-client-python/commit/54043251e86b44264070c37025d5bd9407bb906b))

## [1.3.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.3.0...v1.3.1) (2025-11-10)


### Bug Fixes

* export no-auth test helpers from main module ([#30](https://github.com/adcontextprotocol/adcp-client-python/issues/30)) ([fb2459d](https://github.com/adcontextprotocol/adcp-client-python/commit/fb2459da396ee1dfd01bfa437130b0042f8360e7))

## [1.3.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.2.1...v1.3.0) (2025-11-10)


### Features

* add no-auth test agent helpers ([#29](https://github.com/adcontextprotocol/adcp-client-python/issues/29)) ([baa5608](https://github.com/adcontextprotocol/adcp-client-python/commit/baa56082d8cac5f3fc48da5905eeaabb7cf0473a))
* add test helpers for quick testing ([#27](https://github.com/adcontextprotocol/adcp-client-python/issues/27)) ([80dee92](https://github.com/adcontextprotocol/adcp-client-python/commit/80dee92c93635d0b6665393eacc5a1d36e4480bd))

## [1.2.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.2.0...v1.2.1) (2025-11-09)


### Documentation

* add Python version requirement note to README ([#25](https://github.com/adcontextprotocol/adcp-client-python/issues/25)) ([b2e5233](https://github.com/adcontextprotocol/adcp-client-python/commit/b2e5233a482d48251df875e77a70523972bfc988))

## [1.2.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.1.0...v1.2.0) (2025-11-08)


### Features

* improve type generation with discriminated union support ([#21](https://github.com/adcontextprotocol/adcp-client-python/issues/21)) ([54da596](https://github.com/adcontextprotocol/adcp-client-python/commit/54da5967f0962b814460ce53fc35494af2f023b6))

## [1.1.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.5...v1.1.0) (2025-11-07)


### Features

* batch preview API with 5-10x performance improvement ([#18](https://github.com/adcontextprotocol/adcp-client-python/issues/18)) ([813df8a](https://github.com/adcontextprotocol/adcp-client-python/commit/813df8a5c27e44cccf832552ad331c364c27925f))


### Bug Fixes

* improve MCP adapter cleanup on connection failures ([#19](https://github.com/adcontextprotocol/adcp-client-python/issues/19)) ([40d83f3](https://github.com/adcontextprotocol/adcp-client-python/commit/40d83f3ac727126a329784638a98094670ab3b45))

## [1.0.5](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.4...v1.0.5) (2025-11-07)


### Bug Fixes

* return both message and structured content in MCP responses ([#16](https://github.com/adcontextprotocol/adcp-client-python/issues/16)) ([696a3aa](https://github.com/adcontextprotocol/adcp-client-python/commit/696a3aa94dd44ee303577efceefb038ac3bac06a))

## [1.0.4](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.3...v1.0.4) (2025-11-07)


### Bug Fixes

* handle Pydantic TextContent objects in MCP response parser ([#14](https://github.com/adcontextprotocol/adcp-client-python/issues/14)) ([6b60365](https://github.com/adcontextprotocol/adcp-client-python/commit/6b60365ffd0c084b3989d38e548e0d2de8c85c57))

## [1.0.3](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.2...v1.0.3) (2025-11-07)


### Bug Fixes

* parse list_creative_formats response into structured type ([#12](https://github.com/adcontextprotocol/adcp-client-python/issues/12)) ([15b5395](https://github.com/adcontextprotocol/adcp-client-python/commit/15b53950ed2ed1f208fb93b73f0743725fb0e718))

## [1.0.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.1...v1.0.2) (2025-11-06)


### Bug Fixes

* correct tool name in list_creative_formats method ([#10](https://github.com/adcontextprotocol/adcp-client-python/issues/10)) ([d9eff68](https://github.com/adcontextprotocol/adcp-client-python/commit/d9eff68df85a018eefd3b1a0d1a4d763d9aa106b))

## [1.0.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.0...v1.0.1) (2025-11-06)


### Bug Fixes

* use correct PYPY_API_TOKEN secret for PyPI publishing ([#8](https://github.com/adcontextprotocol/adcp-client-python/issues/8)) ([b48a33a](https://github.com/adcontextprotocol/adcp-client-python/commit/b48a33aaafa9f407b375036b7e656e63ed37544a))

## [1.0.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v0.1.2...v1.0.0) (2025-11-06)


### ⚠ BREAKING CHANGES

* All client methods now require typed request objects. The legacy kwargs API has been removed for a cleaner, more type-safe interface.
* All client methods now require typed request objects. The legacy kwargs API has been removed for a cleaner, more type-safe interface.
* All client methods now require typed request objects. The legacy kwargs API has been removed for a cleaner, more type-safe interface.

### Features

* complete Python AdCP SDK with typed API and auto-generated types ([#5](https://github.com/adcontextprotocol/adcp-client-python/issues/5)) ([bc8ebc9](https://github.com/adcontextprotocol/adcp-client-python/commit/bc8ebc957349550887b0d329fba02d5222a311ef))


### Bug Fixes

* correct PyPI API token secret name ([#6](https://github.com/adcontextprotocol/adcp-client-python/issues/6)) ([eae30ce](https://github.com/adcontextprotocol/adcp-client-python/commit/eae30ceb9538a4ff2baf0a0a9a944b9e5ae0c5a0))


### Reverts

* restore correct PYPY_API_TOKEN secret name ([#7](https://github.com/adcontextprotocol/adcp-client-python/issues/7)) ([330f484](https://github.com/adcontextprotocol/adcp-client-python/commit/330f48449dce18356e94bf1f95c8e4e4d4c59178))


### Documentation

* update PyPI setup guide with correct secret name and current status ([085b961](https://github.com/adcontextprotocol/adcp-client-python/commit/085b961ef6d49050a9dc4bcdd956ff29d2955aed))

## [0.1.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v0.1.1...v0.1.2) (2025-11-05)


### Bug Fixes

* correct secret name from PYPI_API_TOKEN to PYPY_API_TOKEN ([0b7599d](https://github.com/adcontextprotocol/adcp-client-python/commit/0b7599d09321c8a12e934a249817816f60b92372))

## [0.1.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v0.1.0...v0.1.1) (2025-11-05)


### Bug Fixes

* remove deprecated package-name parameter from release-please config ([28d8154](https://github.com/adcontextprotocol/adcp-client-python/commit/28d8154a8185e6c841804b39e7381f6bb22bde03))

## 0.1.0 (2025-11-05)


### Features

* add automated versioning and PyPI publishing ([e7f8bbb](https://github.com/adcontextprotocol/adcp-client-python/commit/e7f8bbba5169a642f05b99d018c17491f4a86982))


### Documentation

* add comprehensive PyPI publishing setup guide ([dcc8135](https://github.com/adcontextprotocol/adcp-client-python/commit/dcc81354ca322eed92b879c3aa26a78d1f8ba3de))
