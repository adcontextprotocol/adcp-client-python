# Generated-types delta

## Field changes

- `core/catalog.py`
  - **classes removed**: Category, Gtins, Ids, Query, Tags, Type
- `core/catalog_selection.py`
  - **classes added**: Gtin
- `core/product_offer_filters.py`
  - **classes added**: GeoProximityItem, Geometry, Radius, TravelTime, Type
- `core/targeting.py`
  - **classes removed**: AudienceExclude, AudienceInclude, AxeExcludeSegment, AxeIncludeSegment, Browser, BrowserExclude, DaypartTargets, DevicePlatform, DevicePlatformExclude, DeviceType, DeviceTypeExclude, GeoCountries, GeoCountriesExclude, GeoMetrosExclude, GeoPlaces, GeoPlacesExclude, GeoPostalAreas, GeoPostalAreasExclude, GeoProximity, GeoProximityItem2, GeoRegions, GeoRegionsExclude, PlacementSelection, StoreCatchments
- `core/targeting_input.py`
  - **classes added**: GeoCountry, GeoMetrosExcludeItem, GeoProximityItem, GeoRegion, Geometry, Radius, StoreCatchment, TravelTime, Type
- `core/version_envelope.py`
  - **classes removed**: AdcpMajorVersion, AdcpVersion
- `core/wholesale_feed_event.py`
  - **classes added**: Range
- `error_details/vast_version_mismatch.py`
  - **classes removed**: ObservedDocumentVastVersion
- `media_buy/product_purchase.py`
  - **classes removed**: AgencyEstimateNumber, AudienceEvidencePins, Budget, CatalogIds, DailyBudgetCap, EndTime, FormatOptionRefs, MinSpendTarget, OptimizationGoals, Pacing, PerformanceStandards, StartTime
- `media_buy/product_purchase_input.py`
  - **classes added**: CatalogId
