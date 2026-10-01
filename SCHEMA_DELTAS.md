# Generated-types delta

## Files added

- `core/outcome_target_cost_per.py` — OutcomeTargetCostPer
- `core/product_execution_requirement.py` — Connection, ProductExecutionRequirement, ProductExecutionRequirement1, ProductExecutionRequirement2, ProductExecutionRequirement3, RequiredForItem, Status
- `core/reporting_delivery_offering_id.py` — ReportingDeliveryOfferingId
- `enums/outcome_target_cost_strength.py` — OutcomeTargetCostStrength
- `error_details/execution_requirement_unmet.py` — ExecutionRequirementUnmetDetails, Reason, UnmetRequirement

## Field changes

- `bundled/protocol/get_adcp_capabilities_response.py`
  - `Features1`: `+seller_optimized_min_spend_targets`, `+seller_optimized_package_budgets`, `+seller_optimized_package_pacing`
- `core/canonical_media_buy_features.py`
  - `CanonicalMediaBuyFeatures`: `+seller_optimized_min_spend_targets`, `+seller_optimized_package_budgets`, `+seller_optimized_package_pacing`
- `core/canonical_product.py`
  - `CanonicalProduct`: `+collection_targeting_allowed`, `+collections`, `+execution_requirements`, `+targeting_resolution`
- `core/canonical_reporting_capabilities.py`
  - `CanonicalReportingCapabilities`: `+reporting_delivery_offering_ids`
- `core/media_buy_features.py`
  - `MediaBuyFeatures`: `+seller_optimized_min_spend_targets`, `+seller_optimized_package_budgets`, `+seller_optimized_package_pacing`
- `core/product.py`
  - `Product`: `+execution_requirements`
- `core/reporting_capabilities.py`
  - **classes removed**: ReportingDeliveryOfferingId
- `governance/sync_plans_response.py`
  - **classes added**: Status51
  - **classes removed**: Status50
- `media_buy/get_media_buy_delivery_response.py`
  - `ReportingPeriod`: `+timezone`
- `media_buy/get_products_request.py`
  - `Field1`: `+execution_requirements`
  - `Fields`: `-collection_targeting_allowed`, `-collections`, `-targeting_resolution`
- `media_buy/media_buy_delivery_webhook_result.py`
  - `ReportingPeriod`: `+timezone`
- `media_buy/outcome_target.py`
  - `OutcomeTarget`: `+cost_per`
- `media_buy/product_fields.py`
  - `ProductResponseField`: `+collection_targeting_allowed`, `+collections`, `+execution_requirements`, `+targeting_resolution`
- `sponsored_intelligence/si_sponsored_context_receipt.py`
  - **classes added**: Status46
  - **classes removed**: Status45
