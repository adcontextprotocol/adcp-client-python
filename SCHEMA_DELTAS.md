# Generated-types delta

## Files added

- `core/impression_basis_breakdown_entry.py` — ImpressionBasisBreakdownEntry
- `core/reporting_dimension_systems.py` — ReportingDimensionSystems
- `enums/impression_estimation_basis.py` — ImpressionEstimationBasis

## Field changes

- `brand_discovery.py`
  - **classes added**: ColorValue2Item
  - `ColorValue1`: `+_json_schema_extra`
- `bundled/protocol/get_adcp_capabilities_response.py`
  - **classes added**: Country32, DimensionSystems, Metro, PostalArea, PostalArea1, PostalArea2, PostalArea3, PostalArea4, PostalArea5, PostalArea6, PostalArea7, PostalArea8, PostalArea9, System, System1, System2, System3, System9
  - `Offering`: `+dimension_systems`
  - `ReportingDimension`: `+postal_area`
- `core/canonical_forecast_point.py`
  - `CanonicalForecastPoint`: `+audience_multiplier`, `+impression_basis`, `+impression_basis_breakdown`
- `core/canonical_performance_standard.py`
  - `CanonicalPerformanceStandard`: `-metric_id`
- `core/canonical_placement.py`
  - `CanonicalDoohPlacementAttributes`: `+audience_multiplier`, `+impression_basis`
- `core/delivery_metrics.py`
  - **classes added**: BasisMetric
  - `DoohMetrics1`: `+audience_multiplier`, `+basis_metric`, `+impression_basis`, `+impression_basis_breakdown`
  - `VenueBreakdownItem`: `+impression_basis`, `+plays`
- `core/forecast_point.py`
  - `ForecastPoint`: `+audience_multiplier`, `+impression_basis`, `+impression_basis_breakdown`
- `core/performance_standard.py`
  - `PerformanceStandard`: `-metric_id`
- `core/placement.py`
  - `ProductDoohPlacementAttributes`: `+audience_multiplier`, `+impression_basis`
- `core/postal_area.py`
  - **classes added**: PostalArea401, PostalArea4010, PostalArea4011, PostalArea4012, PostalArea4013, PostalArea4014, PostalArea4015, PostalArea4016, PostalArea4017, PostalArea4018, PostalArea4019, PostalArea402, PostalArea4020, PostalArea4021, PostalArea403, PostalArea404, PostalArea405, PostalArea406, PostalArea407, PostalArea408, PostalArea409, System41, System42, System43, System49
  - **classes removed**: PostalArea11, PostalArea110, PostalArea111, PostalArea112, PostalArea113, PostalArea114, PostalArea115, PostalArea116, PostalArea117, PostalArea118, PostalArea119, PostalArea12, PostalArea120, PostalArea121, PostalArea13, PostalArea14, PostalArea15, PostalArea16, PostalArea17, PostalArea18, PostalArea19, System1, System2, System3, System9
- `core/postal_country_system.py`
  - **classes added**: System51, System52, System53, System59
  - **classes removed**: System11, System12, System13, System19
- `core/product_offer_filters.py`
  - `RequiredPerformanceStandard`: `-metric_id`
- `core/reporting_delivery_offering.py`
  - `ReportingDeliveryOffering`: `+dimension_systems`
- `core/reporting_subscription.py`
  - `Selector`: `+dimension_systems`
- `core/x_entity_types.py`
  - `XEntityTypes`: `+evaluator`
- `enums/error_code.py`
  - `ErrorCode`: `+PROPOSAL_TERMS_CHANGED`
- `enums/reporting_dimension.py`
  - `ReportingDimension`: `+postal_area`
