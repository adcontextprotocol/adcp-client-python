# Generated-types delta: AdCP 3.2.0-beta.3 → 3.2.0-beta.4

## Added types

- `core/forecast_dimension_time.py`: `TimeForecastDimension`
- `enums/availability_status.py`: `AvailabilityStatus`
- `media_buy/outcome_target.py`: metric/event goals and `OutcomeTarget`
- `media_buy/legacy_purchase_continuation_input.py`:
  `CompatibilityPurchaseCoordinatorInput` and `AcceptedLoss`

## Field and enum changes

- `protocol/get_adcp_capabilities_response.py`: media-buy capabilities add
  `availability_horizon` and `outcome_target`.
- `core/forecast_point.py` and `core/canonical_forecast_point.py`: forecast
  points add `availability_status`; dimensions now accept time windows.
- `core/product_offer_filters.py`: offer filters add `availability_horizon`.
- `media_buy/product_discovery_criteria.py`: criteria add `outcome_target`.
- `media_buy/request_proposals_response.py`: adds the
  `products_available` outcome, partial-result `incomplete` metadata, and
  listed/legacy purchase continuations.
- `media_buy/control_media_buy_request.py`: adds mutable display `name`.
- `core/canonical_media_buy_action.py` and
  `enums/media_buy_valid_action.py`: add `update_name`.
- `core/targeting_overlay_support.py`: country include/exclude support may
  advertise `max_values_per_package`; proximity support adds the same limit.
- `extensions/extension_meta.py`: extension metadata requires `$id`.
- `core/assets/card_asset.py`: card provenance resolves to the canonical
  provenance model.
- `media_buy/package_update.py`: package cancellation precedence is clarified.
