# Generated-types delta

## Files removed

- `core/geo_metro.py` — GeoMetro
- `core/negative_keyword.py` — NegativeKeyword
- `core/targeting_input.py` — KeywordTarget, TargetingOverlayInput
- `core/targeting_unknown_age_eligibility_constraint.py` — TargetingUnknownAgeEligibilityConstraint
- `core/targeting_verified_age_basis_constraint.py` — TargetingVerifiedAgeBasisConstraint
- `media_buy/product_purchase_input.py` — ProductPurchaseInput

## Field changes

- `bundled/protocol/get_adcp_capabilities_response.py`
  - **classes added**: Disclosure3, EmbeddedProvenanceItem2, VerifyAgent4, Watermark2
  - **classes removed**: Disclosure7, EmbeddedProvenanceItem6, VerifyAgent6, Watermark6
- `core/assets/asset_union.py`
  - **classes removed**: ImageAssetModel, UrlAssetModel, VideoAssetModel
- `core/assets/card_asset.py`
  - **classes removed**: AiTool, C2pa, DeclaredBy, Disclosure, EmbeddedProvenanceItem, HumanOversight, Jurisdiction, PlatformExtensionRef, Provenance, RenderGuidance, Result, Role, VerificationItem, VerifyAgent, VerifyAgent4, Watermark
- `core/assets/daast_asset.py`
  - **classes added**: Location13, MacroDeclaration12
  - **classes removed**: Location14, MacroDeclaration13
- `core/assets/display_tag_asset.py`
  - **classes added**: MacroDeclaration15
  - **classes removed**: MacroDeclaration17
- `core/assets/vast_asset.py`
  - **classes added**: Location26, MacroDeclaration20
  - **classes removed**: Location27, MacroDeclaration21
- `core/macro_declaration.py`
  - **classes removed**: Kind, MacroDialect, MacroEncoding, MacroMappingStatus, MacroProcessingOperation, MacroResolver, MacroTranslationTarget, MacroValueContext, UniversalMacro
- `core/provenance.py`
  - **classes added**: VerifyAgent18
  - **classes removed**: VerifyAgent20
- `core/targeting.py`
  - **classes added**: GeoMetro, NegativeKeyword
  - **classes removed**: AudienceExclude, AudienceInclude, AxeExcludeSegment, AxeIncludeSegment, Browser, BrowserExclude, CollectionListExclude, DaypartTargets, Demographics, DevicePlatform, DevicePlatformExclude, DeviceType, DeviceTypeExclude, GeoCountries, GeoCountriesExclude, GeoMetrosExclude, GeoPlaces, GeoPlacesExclude, GeoPostalAreas, GeoPostalAreasExclude, GeoRegions, GeoRegionsExclude, PlacementSelection, PropertyListExclude, StoreCatchments
- `media_buy/product_purchase.py`
  - **classes removed**: AgencyEstimateNumber, AudienceEvidencePins, Bidding, Budget, CatalogIds, Context, DailyBudgetCap, EndTime, Ext, FormatOptionRefs, MinSpendTarget, OptimizationGoals, Pacing, PerformanceStandards, Pricing, StartTime
- `protocol/get_principal_response.py`
  - **classes added**: Result6, Result9
  - **classes removed**: Result10, Result8
  - `Result7`: `-configuration`, `-configuration_version`
- `protocol/sync_principal_response.py`
  - **classes added**: Result17, Result19
  - **classes removed**: Result18, Result20
