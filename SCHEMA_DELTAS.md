# Generated-types delta

## Files added

- `core/iana_timezone.py` — IanaTimezoneIdentifier
- `core/product_identity.py` — ProductIdentity
- `enums/daypart_timezone_mode.py` — DaypartTimezoneMode

## Field changes

- `compliance/comply_test_controller_request.py`
  - **classes added**: TargetHealth
  - `Operation`: `+omit_obligation`, `+publish_zero_row`
  - `Params`: `+reach_unit`, `+target_health`
- `core/canonical_product.py`
  - `CanonicalProduct`: `+identity`, `+overlay_support`
- `core/daypart_target.py`
  - `DaypartTarget`: `+timezone`
- `core/product.py`
  - `Product`: `+identity`
- `core/targeting_overlay_requirements.py`
  - **classes added**: DaypartRequirement, DaypartRequirement1
- `core/targeting_overlay_support.py`
  - **classes added**: DaypartSupport, DaypartSupport1, IanaTimezones
- `media_buy/get_products_request.py`
  - `Field1`: `+identity`
- `media_buy/product_fields.py`
  - `ProductResponseField`: `+identity`
- `pricing_options/flat_rate_option.py`
  - `Parameters`: `+loop_position`, `+slot_span`
