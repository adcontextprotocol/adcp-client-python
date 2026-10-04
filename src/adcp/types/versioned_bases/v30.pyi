"""Generated named extension bases for the pinned bundled schema."""
from __future__ import annotations

import builtins
from typing import Any, Literal
from pydantic import Field
from adcp.types.versioned import _VersionedExtensionModel
from . import _v30 as _definitions

class AcquireRightsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    rights_id: builtins.str
    pricing_option_id: builtins.str
    buyer: _definitions._ExternalCoreBrandRef
    campaign: _definitions._AcquireRightsRequestBaseCampaign
    revocation_webhook: _definitions._ExternalCorePushNotificationConfig
    push_notification_config: _definitions._ExternalCorePushNotificationConfig | None = Field(default=None)
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class AcquireRightsResponseBase(_VersionedExtensionModel):
    rights_id: builtins.str | None = Field(default=None)
    status: Literal['acquired'] | Literal['pending_approval'] | Literal['rejected'] | None = Field(default=None)
    brand_id: builtins.str | None = Field(default=None)
    terms: _definitions._ExternalBrandRightsTerms | None = Field(default=None)
    generation_credentials: builtins.list[_definitions._ExternalCoreGenerationCredential] | None = Field(default=None)
    restrictions: builtins.list[builtins.str] | None = Field(default=None)
    disclosure: _definitions._AcquireRightsResponseBaseDisclosure | None = Field(default=None)
    approval_webhook: _definitions._ExternalCorePushNotificationConfig | None = Field(default=None)
    usage_reporting_url: builtins.str | None = Field(default=None)
    rights_constraint: _definitions._ExternalCoreRightsConstraint | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    detail: builtins.str | None = Field(default=None)
    estimated_response_time: builtins.str | None = Field(default=None)
    reason: builtins.str | None = Field(default=None)
    suggestions: builtins.list[builtins.str] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class ActivateSignalRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    action: Literal['activate', 'deactivate'] = Field(default='activate')
    signal_agent_segment_id: builtins.str
    destinations: builtins.list[_definitions._ActivateSignalRequestBaseDestinationsItemVariant1 | _definitions._ActivateSignalRequestBaseDestinationsItemVariant2]
    pricing_option_id: builtins.str | None = Field(default=None)
    account: _definitions._ActivateSignalRequestBaseAccountVariant1 | _definitions._ActivateSignalRequestBaseAccountVariant2 | None = Field(default=None)
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ActivateSignalResponseBase(_VersionedExtensionModel):
    deployments: builtins.list[_definitions._ActivateSignalResponseBaseDeploymentsItemVariant1 | _definitions._ActivateSignalResponseBaseDeploymentsItemVariant2] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class BuildCreativeInputRequiredResponseBase(_VersionedExtensionModel):
    reason: Literal['APPROVAL_REQUIRED', 'CREATIVE_DIRECTION_NEEDED', 'ASSET_SELECTION_NEEDED'] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class BuildCreativeRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    message: builtins.str | None = Field(default=None)
    creative_manifest: _definitions._ExternalCoreCreativeManifest | None = Field(default=None)
    creative_id: builtins.str | None = Field(default=None)
    concept_id: builtins.str | None = Field(default=None)
    media_buy_id: builtins.str | None = Field(default=None)
    package_id: builtins.str | None = Field(default=None)
    target_format_id: _definitions._ExternalCoreFormatId | None = Field(default=None)
    target_format_ids: builtins.list[_definitions._ExternalCoreFormatId] | None = Field(default=None)
    account: _definitions._BuildCreativeRequestBaseAccountVariant1 | _definitions._BuildCreativeRequestBaseAccountVariant2 | None = Field(default=None)
    brand: _definitions._ExternalCoreBrandRef | None = Field(default=None)
    quality: Literal['draft', 'production'] | None = Field(default=None)
    item_limit: builtins.int | None = Field(default=None)
    include_preview: builtins.bool | None = Field(default=None)
    preview_inputs: builtins.list[_definitions._BuildCreativeRequestBasePreviewInputsItem] | None = Field(default=None)
    preview_quality: Literal['draft', 'production'] | None = Field(default=None)
    preview_output_format: Literal['url', 'html'] = Field(default='url')
    macro_values: builtins.dict[builtins.str, builtins.str] | None = Field(default=None)
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class BuildCreativeSubmittedResponseBase(_VersionedExtensionModel):
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class BuildCreativeResponseBase(_VersionedExtensionModel):
    creative_manifest: _definitions._ExternalCoreCreativeManifest | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    expires_at: builtins.str | None = Field(default=None)
    preview: _definitions._BuildCreativeResponseBasePreview | _definitions._BuildCreativeResponseBasePreview2 | None = Field(default=None)
    preview_error: _definitions._ExternalCoreError | None = Field(default=None)
    pricing_option_id: builtins.str | None = Field(default=None)
    vendor_cost: builtins.float | None = Field(default=None)
    currency: builtins.str | None = Field(default=None)
    consumption: _definitions._ExternalCoreCreativeConsumption | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    creative_manifests: builtins.list[_definitions._ExternalCoreCreativeManifest] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class BuildCreativeWorkingResponseBase(_VersionedExtensionModel):
    percentage: builtins.float | None = Field(default=None)
    current_step: builtins.str | None = Field(default=None)
    total_steps: builtins.int | None = Field(default=None)
    step_number: builtins.int | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CalibrateContentRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    standards_id: builtins.str
    artifact: _definitions._ExternalContentStandardsArtifact
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CalibrateContentResponseBase(_VersionedExtensionModel):
    verdict: Literal['pass', 'fail'] | None = Field(default=None)
    confidence: builtins.float | None = Field(default=None)
    explanation: builtins.str | None = Field(default=None)
    features: builtins.list[_definitions._CalibrateContentResponseBaseFeaturesItem] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class CheckGovernanceRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    plan_id: builtins.str
    caller: builtins.str
    purchase_type: Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services'] = Field(default='media_buy')
    tool: builtins.str | None = Field(default=None)
    payload: builtins.dict[builtins.str, Any] | None = Field(default=None)
    governance_context: builtins.str | None = Field(default=None)
    phase: Literal['purchase', 'modification', 'delivery'] = Field(default='purchase')
    planned_delivery: _definitions._ExternalCorePlannedDelivery | None = Field(default=None)
    delivery_metrics: _definitions._CheckGovernanceRequestBaseDeliveryMetrics | None = Field(default=None)
    modification_summary: builtins.str | None = Field(default=None)
    invoice_recipient: _definitions._ExternalCoreBusinessEntity | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CheckGovernanceResponseBase(_VersionedExtensionModel):
    check_id: builtins.str
    status: Literal['approved', 'denied', 'conditions']
    plan_id: builtins.str
    explanation: builtins.str
    findings: builtins.list[_definitions._CheckGovernanceResponseBaseFindingsItem] | None = Field(default=None)
    conditions: builtins.list[_definitions._CheckGovernanceResponseBaseConditionsItem] | None = Field(default=None)
    expires_at: builtins.str | None = Field(default=None)
    next_check: builtins.str | None = Field(default=None)
    categories_evaluated: builtins.list[builtins.str] | None = Field(default=None)
    policies_evaluated: builtins.list[builtins.str] | None = Field(default=None)
    mode: Literal['audit', 'advisory', 'enforce'] | None = Field(default=None)
    governance_context: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ComplyTestControllerRequestBase(_VersionedExtensionModel):
    scenario: Literal['list_scenarios', 'force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_create_media_buy_arm', 'force_task_completion', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend', 'seed_product', 'seed_pricing_option', 'seed_creative', 'seed_plan', 'seed_media_buy', 'seed_creative_format']
    params: _definitions._ComplyTestControllerRequestBaseParams | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ComplyTestControllerResponseBase(_VersionedExtensionModel):
    success: Literal[True] | Literal[False]
    scenarios: builtins.list[Literal['force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_create_media_buy_arm', 'force_task_completion', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend', 'seed_product', 'seed_pricing_option', 'seed_creative', 'seed_plan', 'seed_media_buy', 'seed_creative_format']] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    previous_state: builtins.str | None = Field(default=None)
    current_state: builtins.str | builtins.str | None = Field(default=None)
    message: builtins.str | None = Field(default=None)
    simulated: builtins.dict[builtins.str, Any] | None = Field(default=None)
    cumulative: builtins.dict[builtins.str, Any] | None = Field(default=None)
    forced: _definitions._ComplyTestControllerResponseBaseForced | None = Field(default=None)
    error: Literal['INVALID_TRANSITION', 'INVALID_STATE', 'NOT_FOUND', 'UNKNOWN_SCENARIO', 'INVALID_PARAMS', 'FORBIDDEN', 'INTERNAL_ERROR'] | None = Field(default=None)
    error_detail: builtins.str | None = Field(default=None)

class ContextMatchRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    type: Literal['context_match_request']
    protocol_version: builtins.str = Field(default='1.0')
    request_id: builtins.str
    property_rid: builtins.str
    property_id: builtins.str | None = Field(default=None)
    property_type: Literal['website', 'mobile_app', 'ctv_app', 'desktop_app', 'dooh', 'podcast', 'radio', 'linear_tv', 'streaming_audio', 'ai_assistant']
    placement_id: builtins.str
    seller_agent_url: builtins.str
    artifact: _definitions._ExternalContentStandardsArtifact | None = Field(default=None)
    artifact_refs: builtins.list[_definitions._ContextMatchRequestBaseArtifactRefsItem] | None = Field(default=None)
    geo: _definitions._ContextMatchRequestBaseGeo | None = Field(default=None)
    context_signals: _definitions._ContextMatchRequestBaseContextSignals | None = Field(default=None)
    package_ids: builtins.list[builtins.str] | None = Field(default=None)

class ContextMatchResponseBase(_VersionedExtensionModel):
    type: Literal['context_match_response']
    request_id: builtins.str
    offers: builtins.list[_definitions._ExternalTmpOffer]
    cache_ttl: builtins.int | None = Field(default=None)
    signals: _definitions._ContextMatchResponseBaseSignals | None = Field(default=None)

class CreateCollectionListRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._CreateCollectionListRequestBaseAccountVariant1 | _definitions._CreateCollectionListRequestBaseAccountVariant2 | None = Field(default=None)
    name: builtins.str
    description: builtins.str | None = Field(default=None)
    base_collections: builtins.list[_definitions._CreateCollectionListRequestBaseBaseCollectionsItemVariant1 | _definitions._CreateCollectionListRequestBaseBaseCollectionsItemVariant2 | _definitions._CreateCollectionListRequestBaseBaseCollectionsItemVariant3] | None = Field(default=None)
    filters: _definitions._ExternalCollectionCollectionListFilters | None = Field(default=None)
    brand: _definitions._ExternalCoreBrandRef | None = Field(default=None)
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreateCollectionListResponseBase(_VersionedExtensionModel):
    list: _definitions._ExternalCollectionCollectionList
    auth_token: builtins.str
    replayed: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreateContentStandardsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    scope: _definitions._CreateContentStandardsRequestBaseScope
    registry_policy_ids: builtins.list[builtins.str] | None = Field(default=None)
    policies: builtins.list[_definitions._ExternalGovernancePolicyEntry] | None = Field(default=None)
    calibration_exemplars: _definitions._CreateContentStandardsRequestBaseCalibrationExemplars | None = Field(default=None)
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreateContentStandardsResponseBase(_VersionedExtensionModel):
    standards_id: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    conflicting_standards_id: builtins.str | None = Field(default=None)

class CreateMediaBuyInputRequiredResponseBase(_VersionedExtensionModel):
    reason: Literal['APPROVAL_REQUIRED', 'BUDGET_EXCEEDS_LIMIT'] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreateMediaBuyRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    idempotency_key: builtins.str
    plan_id: builtins.str | None = Field(default=None)
    account: _definitions._CreateMediaBuyRequestBaseAccountVariant1 | _definitions._CreateMediaBuyRequestBaseAccountVariant2
    proposal_id: builtins.str | None = Field(default=None)
    total_budget: _definitions._CreateMediaBuyRequestBaseTotalBudget | None = Field(default=None)
    packages: builtins.list[_definitions._ExternalMediaBuyPackageRequest] | None = Field(default=None)
    brand: _definitions._ExternalCoreBrandRef
    advertiser_industry: Literal['automotive', 'automotive.electric_vehicles', 'automotive.parts_accessories', 'automotive.luxury', 'beauty_cosmetics', 'beauty_cosmetics.skincare', 'beauty_cosmetics.fragrance', 'beauty_cosmetics.haircare', 'cannabis', 'cpg', 'cpg.personal_care', 'cpg.household', 'dating', 'education', 'education.higher_education', 'education.online_learning', 'education.k12', 'energy_utilities', 'energy_utilities.renewable', 'fashion_apparel', 'fashion_apparel.luxury', 'fashion_apparel.sportswear', 'finance', 'finance.banking', 'finance.insurance', 'finance.investment', 'finance.cryptocurrency', 'food_beverage', 'food_beverage.alcohol', 'food_beverage.restaurants', 'food_beverage.packaged_goods', 'gambling_betting', 'gambling_betting.sports_betting', 'gambling_betting.casino', 'gaming', 'gaming.mobile', 'gaming.console_pc', 'gaming.esports', 'government_nonprofit', 'government_nonprofit.political', 'government_nonprofit.charity', 'healthcare', 'healthcare.pharmaceutical', 'healthcare.medical_devices', 'healthcare.wellness', 'home_garden', 'home_garden.furniture', 'home_garden.home_improvement', 'media_entertainment', 'media_entertainment.podcasts', 'media_entertainment.music', 'media_entertainment.film_tv', 'media_entertainment.publishing', 'media_entertainment.live_events', 'pets', 'professional_services', 'professional_services.legal', 'professional_services.consulting', 'real_estate', 'real_estate.residential', 'real_estate.commercial', 'recruitment_hr', 'retail', 'retail.ecommerce', 'retail.department_stores', 'sports_fitness', 'sports_fitness.equipment', 'sports_fitness.teams_leagues', 'technology', 'technology.software', 'technology.hardware', 'technology.ai_ml', 'telecom', 'telecom.mobile_carriers', 'telecom.internet_providers', 'transportation_logistics', 'travel_hospitality', 'travel_hospitality.airlines', 'travel_hospitality.hotels', 'travel_hospitality.cruise', 'travel_hospitality.tourism'] | None = Field(default=None)
    invoice_recipient: _definitions._ExternalCoreBusinessEntity | None = Field(default=None)
    io_acceptance: _definitions._CreateMediaBuyRequestBaseIoAcceptance | None = Field(default=None)
    po_number: builtins.str | None = Field(default=None)
    agency_estimate_number: builtins.str | None = Field(default=None)
    start_time: Literal['asap'] | builtins.str
    end_time: builtins.str
    push_notification_config: _definitions._ExternalCorePushNotificationConfig | None = Field(default=None)
    reporting_webhook: _definitions._ExternalCoreReportingWebhook | None = Field(default=None)
    artifact_webhook: _definitions._CreateMediaBuyRequestBaseArtifactWebhook | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreateMediaBuySubmittedResponseBase(_VersionedExtensionModel):
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreateMediaBuyResponseBase(_VersionedExtensionModel):
    media_buy_id: builtins.str | None = Field(default=None)
    account: _definitions._ExternalCoreAccount | None = Field(default=None)
    invoice_recipient: _definitions._ExternalCoreBusinessEntity | None = Field(default=None)
    status: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | Literal['submitted'] | None = Field(default=None)
    confirmed_at: builtins.str | None = Field(default=None)
    creative_deadline: builtins.str | None = Field(default=None)
    revision: builtins.int | None = Field(default=None)
    valid_actions: builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']] | None = Field(default=None)
    packages: builtins.list[_definitions._ExternalCorePackage] | None = Field(default=None)
    planned_delivery: _definitions._ExternalCorePlannedDelivery | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    task_id: builtins.str | None = Field(default=None)
    message: builtins.str | None = Field(default=None)

class CreateMediaBuyWorkingResponseBase(_VersionedExtensionModel):
    percentage: builtins.float | None = Field(default=None)
    current_step: builtins.str | None = Field(default=None)
    total_steps: builtins.int | None = Field(default=None)
    step_number: builtins.int | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreatePropertyListRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._CreatePropertyListRequestBaseAccountVariant1 | _definitions._CreatePropertyListRequestBaseAccountVariant2 | None = Field(default=None)
    name: builtins.str
    description: builtins.str | None = Field(default=None)
    base_properties: builtins.list[_definitions._CreatePropertyListRequestBaseBasePropertiesItemVariant1 | _definitions._CreatePropertyListRequestBaseBasePropertiesItemVariant2 | _definitions._CreatePropertyListRequestBaseBasePropertiesItemVariant3] | None = Field(default=None)
    filters: _definitions._ExternalPropertyPropertyListFilters | None = Field(default=None)
    brand: _definitions._ExternalCoreBrandRef | None = Field(default=None)
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreatePropertyListResponseBase(_VersionedExtensionModel):
    list: _definitions._ExternalPropertyPropertyList
    auth_token: builtins.str
    replayed: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreativeApprovalRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    rights_id: builtins.str
    creative_id: builtins.str | None = Field(default=None)
    creative_url: builtins.str
    creative_format: _definitions._ExternalCoreFormatId | None = Field(default=None)
    description: builtins.str | None = Field(default=None)
    metadata: builtins.dict[builtins.str, Any] | None = Field(default=None)
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class CreativeApprovalResponseBase(_VersionedExtensionModel):
    status: Literal['approved'] | Literal['rejected'] | Literal['pending_review'] | None = Field(default=None)
    rights_id: builtins.str | None = Field(default=None)
    creative_id: builtins.str | None = Field(default=None)
    creative_url: builtins.str | None = Field(default=None)
    approved_at: builtins.str | None = Field(default=None)
    conditions: builtins.list[builtins.str] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    reason: builtins.str | None = Field(default=None)
    suggestions: builtins.list[builtins.str] | None = Field(default=None)
    estimated_response_time: builtins.str | None = Field(default=None)
    status_url: builtins.str | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class DeleteCollectionListRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    list_id: builtins.str
    account: _definitions._DeleteCollectionListRequestBaseAccountVariant1 | _definitions._DeleteCollectionListRequestBaseAccountVariant2 | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    idempotency_key: builtins.str

class DeleteCollectionListResponseBase(_VersionedExtensionModel):
    deleted: builtins.bool
    list_id: builtins.str
    replayed: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class DeletePropertyListRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    list_id: builtins.str
    account: _definitions._DeletePropertyListRequestBaseAccountVariant1 | _definitions._DeletePropertyListRequestBaseAccountVariant2 | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    idempotency_key: builtins.str

class DeletePropertyListResponseBase(_VersionedExtensionModel):
    deleted: builtins.bool
    list_id: builtins.str
    replayed: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetAccountFinancialsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._GetAccountFinancialsRequestBaseAccountVariant1 | _definitions._GetAccountFinancialsRequestBaseAccountVariant2
    period: _definitions._ExternalCoreDateRange | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetAccountFinancialsResponseBase(_VersionedExtensionModel):
    account: _definitions._GetAccountFinancialsResponseBaseAccountVariant1 | _definitions._GetAccountFinancialsResponseBaseAccountVariant2 | None = Field(default=None)
    currency: builtins.str | None = Field(default=None)
    period: _definitions._ExternalCoreDateRange | None = Field(default=None)
    timezone: builtins.str | None = Field(default=None)
    spend: _definitions._GetAccountFinancialsResponseBaseSpend | None = Field(default=None)
    credit: _definitions._GetAccountFinancialsResponseBaseCredit | None = Field(default=None)
    balance: _definitions._GetAccountFinancialsResponseBaseBalance | None = Field(default=None)
    payment_status: Literal['current', 'past_due', 'suspended'] | None = Field(default=None)
    payment_terms: Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay'] | None = Field(default=None)
    invoices: builtins.list[_definitions._GetAccountFinancialsResponseBaseInvoicesItem] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class GetAdcpCapabilitiesRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    protocols: builtins.list[Literal['media_buy', 'signals', 'governance', 'sponsored_intelligence', 'creative']] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetAdcpCapabilitiesResponseBase(_VersionedExtensionModel):
    adcp: _definitions._GetAdcpCapabilitiesResponseBaseAdcp
    supported_protocols: builtins.list[Literal['media_buy', 'signals', 'governance', 'sponsored_intelligence', 'creative', 'brand']]
    account: _definitions._GetAdcpCapabilitiesResponseBaseAccount | None = Field(default=None)
    media_buy: _definitions._GetAdcpCapabilitiesResponseBaseMediaBuy | None = Field(default=None)
    signals: _definitions._GetAdcpCapabilitiesResponseBaseSignals | None = Field(default=None)
    governance: _definitions._GetAdcpCapabilitiesResponseBaseGovernance | None = Field(default=None)
    sponsored_intelligence: _definitions._GetAdcpCapabilitiesResponseBaseSponsoredIntelligence | None = Field(default=None)
    brand: _definitions._GetAdcpCapabilitiesResponseBaseBrand | None = Field(default=None)
    creative: _definitions._GetAdcpCapabilitiesResponseBaseCreative | None = Field(default=None)
    request_signing: _definitions._GetAdcpCapabilitiesResponseBaseRequestSigning | None = Field(default=None)
    webhook_signing: _definitions._GetAdcpCapabilitiesResponseBaseWebhookSigning | None = Field(default=None)
    identity: _definitions._GetAdcpCapabilitiesResponseBaseIdentity | None = Field(default=None)
    compliance_testing: _definitions._GetAdcpCapabilitiesResponseBaseComplianceTesting | None = Field(default=None)
    specialisms: builtins.list[Literal['audience-sync', 'brand-rights', 'collection-lists', 'content-standards', 'creative-ad-server', 'creative-generative', 'creative-template', 'governance-aware-seller', 'governance-delivery-monitor', 'governance-spend-authority', 'property-lists', 'sales-broadcast-tv', 'sales-catalog-driven', 'sales-guaranteed', 'sales-non-guaranteed', 'sales-proposal-mode', 'sales-social', 'signal-marketplace', 'signal-owned', 'signed-requests']] | None = Field(default=None)
    extensions_supported: builtins.list[builtins.str] | None = Field(default=None)
    experimental_features: builtins.list[builtins.str] | None = Field(default=None)
    last_updated: builtins.str | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetBrandIdentityRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    brand_id: builtins.str
    fields: builtins.list[Literal['description', 'industries', 'keller_type', 'logos', 'colors', 'fonts', 'visual_guidelines', 'tone', 'tagline', 'voice_synthesis', 'assets', 'rights']] | None = Field(default=None)
    use_case: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetBrandIdentityResponseBase(_VersionedExtensionModel):
    brand_id: builtins.str | None = Field(default=None)
    house: _definitions._GetBrandIdentityResponseBaseHouse | None = Field(default=None)
    names: builtins.list[builtins.dict[builtins.str, builtins.str]] | None = Field(default=None)
    description: builtins.str | None = Field(default=None)
    industries: builtins.list[builtins.str] | None = Field(default=None)
    keller_type: Literal['master', 'sub_brand', 'endorsed', 'independent'] | None = Field(default=None)
    logos: builtins.list[_definitions._GetBrandIdentityResponseBaseLogosItem] | None = Field(default=None)
    colors: _definitions._GetBrandIdentityResponseBaseColors | None = Field(default=None)
    fonts: _definitions._GetBrandIdentityResponseBaseFonts | None = Field(default=None)
    visual_guidelines: builtins.dict[builtins.str, Any] | None = Field(default=None)
    tone: _definitions._GetBrandIdentityResponseBaseTone | None = Field(default=None)
    tagline: builtins.str | builtins.list[builtins.dict[builtins.str, builtins.str]] | None = Field(default=None)
    voice_synthesis: _definitions._GetBrandIdentityResponseBaseVoiceSynthesis | None = Field(default=None)
    assets: builtins.list[_definitions._GetBrandIdentityResponseBaseAssetsItem] | None = Field(default=None)
    rights: _definitions._GetBrandIdentityResponseBaseRights | None = Field(default=None)
    available_fields: builtins.list[Literal['description', 'industries', 'keller_type', 'logos', 'colors', 'fonts', 'visual_guidelines', 'tone', 'tagline', 'voice_synthesis', 'assets', 'rights']] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class GetCollectionListRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    list_id: builtins.str
    account: _definitions._GetCollectionListRequestBaseAccountVariant1 | _definitions._GetCollectionListRequestBaseAccountVariant2 | None = Field(default=None)
    resolve: builtins.bool = Field(default=True)
    pagination: _definitions._GetCollectionListRequestBasePagination | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetCollectionListResponseBase(_VersionedExtensionModel):
    list: _definitions._ExternalCollectionCollectionList
    collections: builtins.list[_definitions._GetCollectionListResponseBaseCollectionsItem] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    resolved_at: builtins.str | None = Field(default=None)
    cache_valid_until: builtins.str | None = Field(default=None)
    coverage_gaps: builtins.dict[builtins.str, builtins.list[_definitions._GetCollectionListResponseBaseCoverageGapsValueItem]] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetContentStandardsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    standards_id: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetContentStandardsResponseBase(_VersionedExtensionModel):
    standards_id: builtins.str | None = Field(default=None)
    name: builtins.str | None = Field(default=None)
    countries_all: builtins.list[builtins.str] | None = Field(default=None)
    channels_any: builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']] | None = Field(default=None)
    languages_any: builtins.list[builtins.str] | None = Field(default=None)
    policies: builtins.list[_definitions._ExternalGovernancePolicyEntry] | None = Field(default=None)
    calibration_exemplars: _definitions._GetContentStandardsResponseBaseCalibrationExemplars | None = Field(default=None)
    pricing_options: builtins.list[_definitions._GetContentStandardsResponseBasePricingOptionsItemVariant1 | _definitions._GetContentStandardsResponseBasePricingOptionsItemVariant2 | _definitions._GetContentStandardsResponseBasePricingOptionsItemVariant3 | _definitions._GetContentStandardsResponseBasePricingOptionsItemVariant4 | _definitions._GetContentStandardsResponseBasePricingOptionsItemVariant5] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class GetCreativeDeliveryRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._GetCreativeDeliveryRequestBaseAccountVariant1 | _definitions._GetCreativeDeliveryRequestBaseAccountVariant2 | None = Field(default=None)
    media_buy_ids: builtins.list[builtins.str] | None = Field(default=None)
    creative_ids: builtins.list[builtins.str] | None = Field(default=None)
    start_date: builtins.str | None = Field(default=None)
    end_date: builtins.str | None = Field(default=None)
    max_variants: builtins.int | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetCreativeDeliveryResponseBase(_VersionedExtensionModel):
    account_id: builtins.str | None = Field(default=None)
    media_buy_id: builtins.str | None = Field(default=None)
    currency: builtins.str
    reporting_period: _definitions._GetCreativeDeliveryResponseBaseReportingPeriod
    creatives: builtins.list[_definitions._GetCreativeDeliveryResponseBaseCreativesItem]
    pagination: _definitions._GetCreativeDeliveryResponseBasePagination | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetCreativeFeaturesRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    creative_manifest: _definitions._ExternalCoreCreativeManifest
    feature_ids: builtins.list[builtins.str] | None = Field(default=None)
    account: _definitions._GetCreativeFeaturesRequestBaseAccountVariant1 | _definitions._GetCreativeFeaturesRequestBaseAccountVariant2 | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetCreativeFeaturesResponseBase(_VersionedExtensionModel):
    results: builtins.list[_definitions._ExternalCreativeCreativeFeatureResult] | None = Field(default=None)
    detail_url: builtins.str | None = Field(default=None)
    pricing_option_id: builtins.str | None = Field(default=None)
    vendor_cost: builtins.float | None = Field(default=None)
    currency: builtins.str | None = Field(default=None)
    consumption: _definitions._ExternalCoreCreativeConsumption | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class GetMediaBuyArtifactsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._GetMediaBuyArtifactsRequestBaseAccountVariant1 | _definitions._GetMediaBuyArtifactsRequestBaseAccountVariant2 | None = Field(default=None)
    media_buy_id: builtins.str
    package_ids: builtins.list[builtins.str] | None = Field(default=None)
    failures_only: builtins.bool = Field(default=False)
    time_range: _definitions._GetMediaBuyArtifactsRequestBaseTimeRange | None = Field(default=None)
    pagination: _definitions._GetMediaBuyArtifactsRequestBasePagination | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetMediaBuyArtifactsResponseBase(_VersionedExtensionModel):
    media_buy_id: builtins.str | None = Field(default=None)
    artifacts: builtins.list[_definitions._GetMediaBuyArtifactsResponseBaseArtifactsItem] | None = Field(default=None)
    collection_info: _definitions._GetMediaBuyArtifactsResponseBaseCollectionInfo | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class GetMediaBuyDeliveryRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._GetMediaBuyDeliveryRequestBaseAccountVariant1 | _definitions._GetMediaBuyDeliveryRequestBaseAccountVariant2 | None = Field(default=None)
    media_buy_ids: builtins.list[builtins.str] | None = Field(default=None)
    status_filter: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | builtins.list[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']] | None = Field(default=None)
    start_date: builtins.str | None = Field(default=None)
    end_date: builtins.str | None = Field(default=None)
    include_package_daily_breakdown: builtins.bool = Field(default=False)
    attribution_window: _definitions._GetMediaBuyDeliveryRequestBaseAttributionWindow | None = Field(default=None)
    reporting_dimensions: _definitions._GetMediaBuyDeliveryRequestBaseReportingDimensions | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetMediaBuyDeliveryResponseBase(_VersionedExtensionModel):
    notification_type: Literal['scheduled', 'final', 'delayed', 'adjusted', 'window_update'] | None = Field(default=None)
    partial_data: builtins.bool | None = Field(default=None)
    unavailable_count: builtins.int | None = Field(default=None)
    sequence_number: builtins.int | None = Field(default=None)
    next_expected_at: builtins.str | None = Field(default=None)
    reporting_period: _definitions._GetMediaBuyDeliveryResponseBaseReportingPeriod
    currency: builtins.str
    attribution_window: _definitions._ExternalCoreAttributionWindow | None = Field(default=None)
    aggregated_totals: _definitions._GetMediaBuyDeliveryResponseBaseAggregatedTotals | None = Field(default=None)
    media_buy_deliveries: builtins.list[_definitions._GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItem]
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetMediaBuysRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._GetMediaBuysRequestBaseAccountVariant1 | _definitions._GetMediaBuysRequestBaseAccountVariant2 | None = Field(default=None)
    media_buy_ids: builtins.list[builtins.str] | None = Field(default=None)
    status_filter: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | builtins.list[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']] | None = Field(default=None)
    include_snapshot: builtins.bool = Field(default=False)
    include_history: builtins.int = Field(default=0)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetMediaBuysResponseBase(_VersionedExtensionModel):
    media_buys: builtins.list[_definitions._GetMediaBuysResponseBaseMediaBuysItem]
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetPlanAuditLogsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    plan_ids: builtins.list[builtins.str] | None = Field(default=None)
    portfolio_plan_ids: builtins.list[builtins.str] | None = Field(default=None)
    governance_contexts: builtins.list[builtins.str] | None = Field(default=None)
    purchase_types: builtins.list[Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']] | None = Field(default=None)
    include_entries: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetPlanAuditLogsResponseBase(_VersionedExtensionModel):
    plans: builtins.list[_definitions._GetPlanAuditLogsResponseBasePlansItem]
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetProductsInputRequiredResponseBase(_VersionedExtensionModel):
    reason: Literal['CLARIFICATION_NEEDED', 'BUDGET_REQUIRED'] | None = Field(default=None)
    partial_results: builtins.list[_definitions._ExternalCoreProduct] | None = Field(default=None)
    suggestions: builtins.list[builtins.str] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetProductsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    buying_mode: Literal['brief', 'wholesale', 'refine']
    brief: builtins.str | None = Field(default=None)
    refine: builtins.list[_definitions._GetProductsRequestBaseRefineItemVariant1 | _definitions._GetProductsRequestBaseRefineItemVariant2 | _definitions._GetProductsRequestBaseRefineItemVariant3] | None = Field(default=None)
    brand: _definitions._ExternalCoreBrandRef | None = Field(default=None)
    catalog: _definitions._ExternalCoreCatalog | None = Field(default=None)
    account: _definitions._GetProductsRequestBaseAccountVariant1 | _definitions._GetProductsRequestBaseAccountVariant2 | None = Field(default=None)
    preferred_delivery_types: builtins.list[Literal['guaranteed', 'non_guaranteed']] | None = Field(default=None)
    filters: _definitions._ExternalCoreProductFilters | None = Field(default=None)
    property_list: _definitions._ExternalCorePropertyListRef | None = Field(default=None)
    fields: builtins.list[Literal['product_id', 'name', 'description', 'publisher_properties', 'channels', 'format_ids', 'placements', 'delivery_type', 'exclusivity', 'pricing_options', 'forecast', 'outcome_measurement', 'delivery_measurement', 'reporting_capabilities', 'creative_policy', 'catalog_types', 'metric_optimization', 'conversion_tracking', 'data_provider_signals', 'max_optimization_goals', 'catalog_match', 'collections', 'collection_targeting_allowed', 'installments', 'brief_relevance', 'expires_at', 'product_card', 'product_card_detailed', 'enforced_policies', 'trusted_match']] | None = Field(default=None)
    time_budget: _definitions._GetProductsRequestBaseTimeBudget | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    required_policies: builtins.list[builtins.str] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetProductsSubmittedResponseBase(_VersionedExtensionModel):
    estimated_completion: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetProductsResponseBase(_VersionedExtensionModel):
    products: builtins.list[_definitions._ExternalCoreProduct]
    proposals: builtins.list[_definitions._ExternalCoreProposal] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    property_list_applied: builtins.bool | None = Field(default=None)
    catalog_applied: builtins.bool | None = Field(default=None)
    refinement_applied: builtins.list[_definitions._GetProductsResponseBaseRefinementAppliedItemVariant1 | _definitions._GetProductsResponseBaseRefinementAppliedItemVariant2 | _definitions._GetProductsResponseBaseRefinementAppliedItemVariant3] | None = Field(default=None)
    incomplete: builtins.list[_definitions._GetProductsResponseBaseIncompleteItem] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetProductsWorkingResponseBase(_VersionedExtensionModel):
    percentage: builtins.float | None = Field(default=None)
    current_step: builtins.str | None = Field(default=None)
    total_steps: builtins.int | None = Field(default=None)
    step_number: builtins.int | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetPropertyListRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    list_id: builtins.str
    account: _definitions._GetPropertyListRequestBaseAccountVariant1 | _definitions._GetPropertyListRequestBaseAccountVariant2 | None = Field(default=None)
    resolve: builtins.bool = Field(default=True)
    pagination: _definitions._GetPropertyListRequestBasePagination | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetPropertyListResponseBase(_VersionedExtensionModel):
    list: _definitions._ExternalPropertyPropertyList
    identifiers: builtins.list[_definitions._ExternalCoreIdentifier] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    resolved_at: builtins.str | None = Field(default=None)
    cache_valid_until: builtins.str | None = Field(default=None)
    coverage_gaps: builtins.dict[builtins.str, builtins.list[_definitions._ExternalCoreIdentifier]] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetRightsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    query: builtins.str
    uses: builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]
    buyer_brand: _definitions._ExternalCoreBrandRef | None = Field(default=None)
    countries: builtins.list[builtins.str] | None = Field(default=None)
    brand_id: builtins.str | None = Field(default=None)
    right_type: Literal['talent', 'character', 'brand_ip', 'music', 'stock_media'] | None = Field(default=None)
    include_excluded: builtins.bool = Field(default=False)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetRightsResponseBase(_VersionedExtensionModel):
    rights: builtins.list[_definitions._GetRightsResponseBaseRightsItem] | None = Field(default=None)
    excluded: builtins.list[_definitions._GetRightsResponseBaseExcludedItem] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class GetSignalsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._GetSignalsRequestBaseAccountVariant1 | _definitions._GetSignalsRequestBaseAccountVariant2 | None = Field(default=None)
    signal_spec: builtins.str | None = Field(default=None)
    signal_ids: builtins.list[_definitions._GetSignalsRequestBaseSignalIdsItemVariant1 | _definitions._GetSignalsRequestBaseSignalIdsItemVariant2] | None = Field(default=None)
    destinations: builtins.list[_definitions._GetSignalsRequestBaseDestinationsItemVariant1 | _definitions._GetSignalsRequestBaseDestinationsItemVariant2] | None = Field(default=None)
    countries: builtins.list[builtins.str] | None = Field(default=None)
    filters: _definitions._ExternalCoreSignalFilters | None = Field(default=None)
    max_results: builtins.int | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class GetSignalsResponseBase(_VersionedExtensionModel):
    signals: builtins.list[_definitions._GetSignalsResponseBaseSignalsItem]
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class IdentityMatchRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    type: Literal['identity_match_request']
    protocol_version: builtins.str = Field(default='1.0')
    request_id: builtins.str
    seller_agent_url: builtins.str
    identities: builtins.list[_definitions._IdentityMatchRequestBaseIdentitiesItem]
    consent: _definitions._IdentityMatchRequestBaseConsent | None = Field(default=None)
    package_ids: builtins.list[builtins.str] | None = Field(default=None)
    country: builtins.str | None = Field(default=None)

class IdentityMatchResponseBase(_VersionedExtensionModel):
    type: Literal['identity_match_response']
    request_id: builtins.str
    eligible_package_ids: builtins.list[builtins.str]
    serve_window_sec: builtins.int
    tmpx: builtins.str | None = Field(default=None)

class ListAccountsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    status: Literal['active', 'pending_approval', 'rejected', 'payment_required', 'suspended', 'closed'] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListAccountsResponseBase(_VersionedExtensionModel):
    accounts: builtins.list[_definitions._ExternalCoreAccount]
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListCollectionListsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._ListCollectionListsRequestBaseAccountVariant1 | _definitions._ListCollectionListsRequestBaseAccountVariant2 | None = Field(default=None)
    name_contains: builtins.str | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListCollectionListsResponseBase(_VersionedExtensionModel):
    lists: builtins.list[_definitions._ExternalCollectionCollectionList]
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListContentStandardsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    channels: builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']] | None = Field(default=None)
    languages: builtins.list[builtins.str] | None = Field(default=None)
    countries: builtins.list[builtins.str] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListContentStandardsResponseBase(_VersionedExtensionModel):
    standards: builtins.list[_definitions._ExternalContentStandardsContentStandards] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class ListCreativeFormatsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    format_ids: builtins.list[_definitions._ExternalCoreFormatId] | None = Field(default=None)
    type: Literal['audio', 'video', 'display', 'dooh'] | None = Field(default=None)
    asset_types: builtins.list[Literal['image', 'video', 'audio', 'text', 'html', 'javascript', 'url']] | None = Field(default=None)
    max_width: builtins.int | None = Field(default=None)
    max_height: builtins.int | None = Field(default=None)
    min_width: builtins.int | None = Field(default=None)
    min_height: builtins.int | None = Field(default=None)
    is_responsive: builtins.bool | None = Field(default=None)
    name_search: builtins.str | None = Field(default=None)
    wcag_level: Literal['A', 'AA', 'AAA'] | None = Field(default=None)
    disclosure_positions: builtins.list[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']] | None = Field(default=None)
    disclosure_persistence: builtins.list[Literal['continuous', 'initial', 'flexible']] | None = Field(default=None)
    output_format_ids: builtins.list[_definitions._ExternalCoreFormatId] | None = Field(default=None)
    input_format_ids: builtins.list[_definitions._ExternalCoreFormatId] | None = Field(default=None)
    include_pricing: builtins.bool = Field(default=False)
    account: _definitions._ListCreativeFormatsRequestBaseAccountVariant1 | _definitions._ListCreativeFormatsRequestBaseAccountVariant2 | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListCreativeFormatsResponseBase(_VersionedExtensionModel):
    formats: builtins.list[_definitions._ExternalCoreFormat]
    creative_agents: builtins.list[_definitions._ListCreativeFormatsResponseBaseCreativeAgentsItem] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListCreativesRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    filters: _definitions._ExternalCoreCreativeFilters | None = Field(default=None)
    sort: _definitions._ListCreativesRequestBaseSort | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    include_assignments: builtins.bool = Field(default=True)
    include_snapshot: builtins.bool = Field(default=False)
    include_items: builtins.bool = Field(default=False)
    include_variables: builtins.bool = Field(default=False)
    include_pricing: builtins.bool = Field(default=False)
    account: _definitions._ListCreativesRequestBaseAccountVariant1 | _definitions._ListCreativesRequestBaseAccountVariant2 | None = Field(default=None)
    fields: builtins.list[Literal['creative_id', 'name', 'format_id', 'status', 'created_date', 'updated_date', 'tags', 'assignments', 'snapshot', 'items', 'variables', 'concept', 'pricing_options']] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListCreativesResponseBase(_VersionedExtensionModel):
    query_summary: _definitions._ListCreativesResponseBaseQuerySummary
    pagination: _definitions._ExternalCorePaginationResponse
    creatives: builtins.list[_definitions._ListCreativesResponseBaseCreativesItem]
    format_summary: builtins.dict[builtins.str, Any] | None = Field(default=None)
    status_summary: _definitions._ListCreativesResponseBaseStatusSummary | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListPropertyListsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._ListPropertyListsRequestBaseAccountVariant1 | _definitions._ListPropertyListsRequestBaseAccountVariant2 | None = Field(default=None)
    name_contains: builtins.str | None = Field(default=None)
    pagination: _definitions._ExternalCorePaginationRequest | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ListPropertyListsResponseBase(_VersionedExtensionModel):
    lists: builtins.list[_definitions._ExternalPropertyPropertyList]
    pagination: _definitions._ExternalCorePaginationResponse | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class LogEventRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    event_source_id: builtins.str
    test_event_code: builtins.str | None = Field(default=None)
    events: builtins.list[_definitions._ExternalCoreEvent]
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class LogEventResponseBase(_VersionedExtensionModel):
    events_received: builtins.int | None = Field(default=None)
    events_processed: builtins.int | None = Field(default=None)
    partial_failures: builtins.list[_definitions._LogEventResponseBasePartialFailuresItem] | None = Field(default=None)
    warnings: builtins.list[builtins.str] | None = Field(default=None)
    match_quality: builtins.float | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class PackageRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    product_id: builtins.str
    format_ids: builtins.list[_definitions._ExternalCoreFormatId] | None = Field(default=None)
    budget: builtins.float
    pacing: Literal['even', 'asap', 'front_loaded'] | None = Field(default=None)
    pricing_option_id: builtins.str
    bid_price: builtins.float | None = Field(default=None)
    impressions: builtins.float | None = Field(default=None)
    start_time: builtins.str | None = Field(default=None)
    end_time: builtins.str | None = Field(default=None)
    paused: builtins.bool = Field(default=False)
    catalogs: builtins.list[_definitions._ExternalCoreCatalog] | None = Field(default=None)
    optimization_goals: builtins.list[_definitions._PackageRequestBaseOptimizationGoalsItemVariant1 | _definitions._PackageRequestBaseOptimizationGoalsItemVariant2] | None = Field(default=None)
    targeting_overlay: _definitions._ExternalCoreTargeting | None = Field(default=None)
    measurement_terms: _definitions._ExternalCoreMeasurementTerms | None = Field(default=None)
    performance_standards: builtins.list[_definitions._ExternalCorePerformanceStandard] | None = Field(default=None)
    creative_assignments: builtins.list[_definitions._ExternalCoreCreativeAssignment] | None = Field(default=None)
    creatives: builtins.list[_definitions._ExternalCoreCreativeAsset] | None = Field(default=None)
    agency_estimate_number: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class PreviewCreativeRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    request_type: Literal['single', 'batch', 'variant']
    creative_manifest: _definitions._ExternalCoreCreativeManifest | None = Field(default=None)
    format_id: _definitions._ExternalCoreFormatId | None = Field(default=None)
    inputs: builtins.list[_definitions._PreviewCreativeRequestBaseInputsItem] | None = Field(default=None)
    template_id: builtins.str | None = Field(default=None)
    quality: Literal['draft', 'production'] | None = Field(default=None)
    output_format: Literal['url', 'html'] = Field(default='url')
    item_limit: builtins.int | None = Field(default=None)
    requests: builtins.list[_definitions._PreviewCreativeRequestBaseRequestsItem] | None = Field(default=None)
    variant_id: builtins.str | None = Field(default=None)
    creative_id: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class PreviewCreativeResponseBase(_VersionedExtensionModel):
    response_type: Literal['single'] | Literal['batch'] | Literal['variant']
    previews: builtins.list[_definitions._PreviewCreativeResponseBasePreviewsItem] | builtins.list[_definitions._PreviewCreativeResponseBasePreviewsItem2] | None = Field(default=None)
    interactive_url: builtins.str | None = Field(default=None)
    expires_at: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    results: builtins.list[_definitions._PreviewCreativeResponseBaseResultsItemVariant1 | _definitions._PreviewCreativeResponseBaseResultsItemVariant2] | None = Field(default=None)
    variant_id: builtins.str | None = Field(default=None)
    creative_id: builtins.str | None = Field(default=None)
    manifest: _definitions._ExternalCoreCreativeManifest | None = Field(default=None)

class ProvidePerformanceFeedbackRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    media_buy_id: builtins.str
    idempotency_key: builtins.str
    measurement_period: _definitions._ExternalCoreDatetimeRange
    performance_index: builtins.float
    package_id: builtins.str | None = Field(default=None)
    creative_id: builtins.str | None = Field(default=None)
    metric_type: Literal['overall_performance', 'conversion_rate', 'brand_lift', 'click_through_rate', 'completion_rate', 'viewability', 'brand_safety', 'cost_efficiency'] = Field(default='overall_performance')
    feedback_source: Literal['buyer_attribution', 'third_party_measurement', 'platform_analytics', 'verification_partner'] = Field(default='buyer_attribution')
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ProvidePerformanceFeedbackResponseBase(_VersionedExtensionModel):
    success: Literal[True] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class ReportPlanOutcomeRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    plan_id: builtins.str
    check_id: builtins.str | None = Field(default=None)
    idempotency_key: builtins.str
    purchase_type: Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services'] = Field(default='media_buy')
    outcome: Literal['completed', 'failed', 'delivery']
    seller_response: _definitions._ReportPlanOutcomeRequestBaseSellerResponse | None = Field(default=None)
    delivery: _definitions._ReportPlanOutcomeRequestBaseDelivery | None = Field(default=None)
    error: _definitions._ReportPlanOutcomeRequestBaseError | None = Field(default=None)
    governance_context: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ReportPlanOutcomeResponseBase(_VersionedExtensionModel):
    outcome_id: builtins.str
    status: Literal['accepted', 'findings']
    committed_budget: builtins.float | None = Field(default=None)
    findings: builtins.list[_definitions._ReportPlanOutcomeResponseBaseFindingsItem] | None = Field(default=None)
    plan_summary: _definitions._ReportPlanOutcomeResponseBasePlanSummary | None = Field(default=None)
    replayed: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ReportUsageRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    idempotency_key: builtins.str
    reporting_period: _definitions._ExternalCoreDatetimeRange
    usage: builtins.list[_definitions._ReportUsageRequestBaseUsageItem]
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ReportUsageResponseBase(_VersionedExtensionModel):
    accepted: builtins.int
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SiGetOfferingRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    offering_id: builtins.str
    intent: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    include_products: builtins.bool = Field(default=False)
    product_limit: builtins.int = Field(default=5)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SiGetOfferingResponseBase(_VersionedExtensionModel):
    available: builtins.bool
    offering_token: builtins.str | None = Field(default=None)
    ttl_seconds: builtins.int | None = Field(default=None)
    checked_at: builtins.str | None = Field(default=None)
    offering: _definitions._SiGetOfferingResponseBaseOffering | None = Field(default=None)
    matching_products: builtins.list[_definitions._SiGetOfferingResponseBaseMatchingProductsItem] | None = Field(default=None)
    total_matching: builtins.int | None = Field(default=None)
    unavailable_reason: builtins.str | None = Field(default=None)
    alternative_offering_ids: builtins.list[builtins.str] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SiInitiateSessionRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    intent: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    identity: _definitions._ExternalSponsoredIntelligenceSiIdentity
    media_buy_id: builtins.str | None = Field(default=None)
    placement: builtins.str | None = Field(default=None)
    offering_id: builtins.str | None = Field(default=None)
    supported_capabilities: _definitions._ExternalSponsoredIntelligenceSiCapabilities | None = Field(default=None)
    offering_token: builtins.str | None = Field(default=None)
    idempotency_key: builtins.str
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SiInitiateSessionResponseBase(_VersionedExtensionModel):
    session_id: builtins.str
    response: _definitions._SiInitiateSessionResponseBaseResponse | None = Field(default=None)
    negotiated_capabilities: _definitions._ExternalSponsoredIntelligenceSiCapabilities | None = Field(default=None)
    session_status: Literal['active', 'pending_handoff', 'complete', 'terminated']
    session_ttl_seconds: builtins.int | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SiSendMessageRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    idempotency_key: builtins.str
    session_id: builtins.str
    message: builtins.str | None = Field(default=None)
    action_response: _definitions._SiSendMessageRequestBaseActionResponse | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SiSendMessageResponseBase(_VersionedExtensionModel):
    session_id: builtins.str
    response: _definitions._SiSendMessageResponseBaseResponse | None = Field(default=None)
    mcp_resource_uri: builtins.str | None = Field(default=None)
    session_status: Literal['active', 'pending_handoff', 'complete', 'terminated']
    handoff: _definitions._SiSendMessageResponseBaseHandoff | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SiTerminateSessionRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    session_id: builtins.str
    reason: Literal['handoff_transaction', 'handoff_complete', 'user_exit', 'session_timeout', 'host_terminated']
    termination_context: _definitions._SiTerminateSessionRequestBaseTerminationContext | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SiTerminateSessionResponseBase(_VersionedExtensionModel):
    session_id: builtins.str
    terminated: builtins.bool
    session_status: Literal['active', 'pending_handoff', 'complete', 'terminated'] | None = Field(default=None)
    acp_handoff: _definitions._SiTerminateSessionResponseBaseAcpHandoff | None = Field(default=None)
    follow_up: _definitions._SiTerminateSessionResponseBaseFollowUp | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncAccountsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    idempotency_key: builtins.str
    accounts: builtins.list[_definitions._SyncAccountsRequestBaseAccountsItem]
    delete_missing: builtins.bool = Field(default=False)
    dry_run: builtins.bool = Field(default=False)
    push_notification_config: _definitions._ExternalCorePushNotificationConfig | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncAccountsResponseBase(_VersionedExtensionModel):
    dry_run: builtins.bool | None = Field(default=None)
    accounts: builtins.list[_definitions._SyncAccountsResponseBaseAccountsItem] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class SyncAudiencesRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    idempotency_key: builtins.str
    account: _definitions._SyncAudiencesRequestBaseAccountVariant1 | _definitions._SyncAudiencesRequestBaseAccountVariant2
    audiences: builtins.list[_definitions._SyncAudiencesRequestBaseAudiencesItem] | None = Field(default=None)
    delete_missing: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncAudiencesResponseBase(_VersionedExtensionModel):
    audiences: builtins.list[_definitions._SyncAudiencesResponseBaseAudiencesItem] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class SyncCatalogsInputRequiredResponseBase(_VersionedExtensionModel):
    reason: Literal['APPROVAL_REQUIRED', 'FEED_VALIDATION', 'ITEM_REVIEW', 'FEED_ACCESS'] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncCatalogsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    idempotency_key: builtins.str
    account: _definitions._SyncCatalogsRequestBaseAccountVariant1 | _definitions._SyncCatalogsRequestBaseAccountVariant2
    catalogs: builtins.list[_definitions._ExternalCoreCatalog] | None = Field(default=None)
    catalog_ids: builtins.list[builtins.str] | None = Field(default=None)
    delete_missing: builtins.bool = Field(default=False)
    dry_run: builtins.bool = Field(default=False)
    validation_mode: Literal['strict', 'lenient'] = Field(default='strict')
    push_notification_config: _definitions._ExternalCorePushNotificationConfig | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncCatalogsSubmittedResponseBase(_VersionedExtensionModel):
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncCatalogsResponseBase(_VersionedExtensionModel):
    dry_run: builtins.bool | None = Field(default=None)
    catalogs: builtins.list[_definitions._SyncCatalogsResponseBaseCatalogsItem] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class SyncCatalogsWorkingResponseBase(_VersionedExtensionModel):
    percentage: builtins.float | None = Field(default=None)
    current_step: builtins.str | None = Field(default=None)
    total_steps: builtins.int | None = Field(default=None)
    step_number: builtins.int | None = Field(default=None)
    catalogs_processed: builtins.int | None = Field(default=None)
    catalogs_total: builtins.int | None = Field(default=None)
    items_processed: builtins.int | None = Field(default=None)
    items_total: builtins.int | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncCreativesInputRequiredResponseBase(_VersionedExtensionModel):
    reason: Literal['APPROVAL_REQUIRED', 'ASSET_CONFIRMATION', 'FORMAT_CLARIFICATION'] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncCreativesRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._SyncCreativesRequestBaseAccountVariant1 | _definitions._SyncCreativesRequestBaseAccountVariant2
    creatives: builtins.list[_definitions._ExternalCoreCreativeAsset]
    creative_ids: builtins.list[builtins.str] | None = Field(default=None)
    assignments: builtins.list[_definitions._SyncCreativesRequestBaseAssignmentsItem] | None = Field(default=None)
    idempotency_key: builtins.str
    delete_missing: builtins.bool = Field(default=False)
    dry_run: builtins.bool = Field(default=False)
    validation_mode: Literal['strict', 'lenient'] = Field(default='strict')
    push_notification_config: _definitions._ExternalCorePushNotificationConfig | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncCreativesSubmittedResponseBase(_VersionedExtensionModel):
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncCreativesResponseBase(_VersionedExtensionModel):
    dry_run: builtins.bool | None = Field(default=None)
    creatives: builtins.list[_definitions._SyncCreativesResponseBaseCreativesItem] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    status: Literal['submitted'] | None = Field(default=None)
    task_id: builtins.str | None = Field(default=None)
    message: builtins.str | None = Field(default=None)

class SyncCreativesWorkingResponseBase(_VersionedExtensionModel):
    percentage: builtins.float | None = Field(default=None)
    current_step: builtins.str | None = Field(default=None)
    total_steps: builtins.int | None = Field(default=None)
    step_number: builtins.int | None = Field(default=None)
    creatives_processed: builtins.int | None = Field(default=None)
    creatives_total: builtins.int | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncEventSourcesRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    idempotency_key: builtins.str
    account: _definitions._SyncEventSourcesRequestBaseAccountVariant1 | _definitions._SyncEventSourcesRequestBaseAccountVariant2
    event_sources: builtins.list[_definitions._SyncEventSourcesRequestBaseEventSourcesItem] | None = Field(default=None)
    delete_missing: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncEventSourcesResponseBase(_VersionedExtensionModel):
    event_sources: builtins.list[_definitions._SyncEventSourcesResponseBaseEventSourcesItem] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class SyncGovernanceRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    idempotency_key: builtins.str
    accounts: builtins.list[_definitions._SyncGovernanceRequestBaseAccountsItem]
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncGovernanceResponseBase(_VersionedExtensionModel):
    accounts: builtins.list[_definitions._SyncGovernanceResponseBaseAccountsItem] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class SyncPlansRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    idempotency_key: builtins.str
    plans: builtins.list[_definitions._SyncPlansRequestBasePlansItem]
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class SyncPlansResponseBase(_VersionedExtensionModel):
    plans: builtins.list[_definitions._SyncPlansResponseBasePlansItem]
    replayed: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class TasksGetRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    task_id: builtins.str
    include_history: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class TasksGetResponseBase(_VersionedExtensionModel):
    task_id: builtins.str
    task_type: Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights']
    protocol: Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence']
    status: Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown']
    created_at: builtins.str
    updated_at: builtins.str
    completed_at: builtins.str | None = Field(default=None)
    has_webhook: builtins.bool | None = Field(default=None)
    progress: _definitions._TasksGetResponseBaseProgress | None = Field(default=None)
    error: _definitions._TasksGetResponseBaseError | None = Field(default=None)
    history: builtins.list[_definitions._TasksGetResponseBaseHistoryItem] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class TasksListRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    filters: _definitions._TasksListRequestBaseFilters | None = Field(default=None)
    sort: _definitions._TasksListRequestBaseSort | None = Field(default=None)
    pagination: _definitions._TasksListRequestBasePagination | None = Field(default=None)
    include_history: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class TasksListResponseBase(_VersionedExtensionModel):
    query_summary: _definitions._TasksListResponseBaseQuerySummary
    tasks: builtins.list[_definitions._TasksListResponseBaseTasksItem]
    pagination: _definitions._TasksListResponseBasePagination
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class UpdateCollectionListRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    list_id: builtins.str
    account: _definitions._UpdateCollectionListRequestBaseAccountVariant1 | _definitions._UpdateCollectionListRequestBaseAccountVariant2 | None = Field(default=None)
    name: builtins.str | None = Field(default=None)
    description: builtins.str | None = Field(default=None)
    base_collections: builtins.list[_definitions._UpdateCollectionListRequestBaseBaseCollectionsItemVariant1 | _definitions._UpdateCollectionListRequestBaseBaseCollectionsItemVariant2 | _definitions._UpdateCollectionListRequestBaseBaseCollectionsItemVariant3] | None = Field(default=None)
    filters: _definitions._ExternalCollectionCollectionListFilters | None = Field(default=None)
    brand: _definitions._ExternalCoreBrandRef | None = Field(default=None)
    webhook_url: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    idempotency_key: builtins.str

class UpdateCollectionListResponseBase(_VersionedExtensionModel):
    list: _definitions._ExternalCollectionCollectionList
    replayed: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class UpdateContentStandardsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    standards_id: builtins.str
    scope: _definitions._UpdateContentStandardsRequestBaseScope | None = Field(default=None)
    registry_policy_ids: builtins.list[builtins.str] | None = Field(default=None)
    policies: builtins.list[_definitions._ExternalGovernancePolicyEntry] | None = Field(default=None)
    calibration_exemplars: _definitions._UpdateContentStandardsRequestBaseCalibrationExemplars | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    idempotency_key: builtins.str

class UpdateContentStandardsResponseBase(_VersionedExtensionModel):
    success: Literal[True] | Literal[False]
    standards_id: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)
    conflicting_standards_id: builtins.str | None = Field(default=None)

class UpdateMediaBuyInputRequiredResponseBase(_VersionedExtensionModel):
    reason: Literal['APPROVAL_REQUIRED', 'CHANGE_CONFIRMATION'] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class UpdateMediaBuyRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    account: _definitions._UpdateMediaBuyRequestBaseAccountVariant1 | _definitions._UpdateMediaBuyRequestBaseAccountVariant2
    media_buy_id: builtins.str
    revision: builtins.int | None = Field(default=None)
    paused: builtins.bool | None = Field(default=None)
    canceled: Literal[True] | None = Field(default=None)
    cancellation_reason: builtins.str | None = Field(default=None)
    start_time: Literal['asap'] | builtins.str | None = Field(default=None)
    end_time: builtins.str | None = Field(default=None)
    packages: builtins.list[_definitions._ExternalMediaBuyPackageUpdate] | None = Field(default=None)
    invoice_recipient: _definitions._ExternalCoreBusinessEntity | None = Field(default=None)
    new_packages: builtins.list[_definitions._ExternalMediaBuyPackageRequest] | None = Field(default=None)
    reporting_webhook: _definitions._ExternalCoreReportingWebhook | None = Field(default=None)
    push_notification_config: _definitions._ExternalCorePushNotificationConfig | None = Field(default=None)
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class UpdateMediaBuySubmittedResponseBase(_VersionedExtensionModel):
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class UpdateMediaBuyResponseBase(_VersionedExtensionModel):
    media_buy_id: builtins.str | None = Field(default=None)
    status: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | None = Field(default=None)
    revision: builtins.int | None = Field(default=None)
    implementation_date: builtins.str | None = Field(default=None)
    invoice_recipient: _definitions._ExternalCoreBusinessEntity | None = Field(default=None)
    affected_packages: builtins.list[_definitions._ExternalCorePackage] | None = Field(default=None)
    valid_actions: builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']] | None = Field(default=None)
    sandbox: builtins.bool | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class UpdateMediaBuyWorkingResponseBase(_VersionedExtensionModel):
    percentage: builtins.float | None = Field(default=None)
    current_step: builtins.str | None = Field(default=None)
    total_steps: builtins.int | None = Field(default=None)
    step_number: builtins.int | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class UpdatePropertyListRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    list_id: builtins.str
    account: _definitions._UpdatePropertyListRequestBaseAccountVariant1 | _definitions._UpdatePropertyListRequestBaseAccountVariant2 | None = Field(default=None)
    name: builtins.str | None = Field(default=None)
    description: builtins.str | None = Field(default=None)
    base_properties: builtins.list[_definitions._UpdatePropertyListRequestBaseBasePropertiesItemVariant1 | _definitions._UpdatePropertyListRequestBaseBasePropertiesItemVariant2 | _definitions._UpdatePropertyListRequestBaseBasePropertiesItemVariant3] | None = Field(default=None)
    filters: _definitions._ExternalPropertyPropertyListFilters | None = Field(default=None)
    brand: _definitions._ExternalCoreBrandRef | None = Field(default=None)
    webhook_url: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    idempotency_key: builtins.str

class UpdatePropertyListResponseBase(_VersionedExtensionModel):
    list: _definitions._ExternalPropertyPropertyList
    replayed: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class UpdateRightsRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    rights_id: builtins.str
    end_date: builtins.str | None = Field(default=None)
    impression_cap: builtins.int | None = Field(default=None)
    pricing_option_id: builtins.str | None = Field(default=None)
    paused: builtins.bool | None = Field(default=None)
    push_notification_config: _definitions._ExternalCorePushNotificationConfig | None = Field(default=None)
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class UpdateRightsResponseBase(_VersionedExtensionModel):
    rights_id: builtins.str | None = Field(default=None)
    terms: _definitions._ExternalBrandRightsTerms | None = Field(default=None)
    generation_credentials: builtins.list[_definitions._ExternalCoreGenerationCredential] | None = Field(default=None)
    rights_constraint: _definitions._ExternalCoreRightsConstraint | None = Field(default=None)
    paused: builtins.bool | None = Field(default=None)
    implementation_date: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class ValidateContentDeliveryRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    standards_id: builtins.str
    records: builtins.list[_definitions._ValidateContentDeliveryRequestBaseRecordsItem]
    feature_ids: builtins.list[builtins.str] | None = Field(default=None)
    include_passed: builtins.bool = Field(default=True)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ValidateContentDeliveryResponseBase(_VersionedExtensionModel):
    summary: _definitions._ValidateContentDeliveryResponseBaseSummary | None = Field(default=None)
    results: builtins.list[_definitions._ValidateContentDeliveryResponseBaseResultsItem] | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)
    errors: builtins.list[_definitions._ExternalCoreError] | None = Field(default=None)

class ValidatePropertyDeliveryRequestBase(_VersionedExtensionModel):
    adcp_major_version: builtins.int | None = Field(default=None)
    list_id: builtins.str
    account: _definitions._ValidatePropertyDeliveryRequestBaseAccountVariant1 | _definitions._ValidatePropertyDeliveryRequestBaseAccountVariant2 | None = Field(default=None)
    records: builtins.list[_definitions._ExternalPropertyDeliveryRecord]
    include_compliant: builtins.bool = Field(default=False)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

class ValidatePropertyDeliveryResponseBase(_VersionedExtensionModel):
    compliant: builtins.bool | None = Field(default=None)
    list_id: builtins.str
    summary: _definitions._ValidatePropertyDeliveryResponseBaseSummary
    aggregate: _definitions._ValidatePropertyDeliveryResponseBaseAggregate | None = Field(default=None)
    authorization_summary: _definitions._ValidatePropertyDeliveryResponseBaseAuthorizationSummary | None = Field(default=None)
    results: builtins.list[_definitions._ExternalPropertyValidationResult]
    validated_at: builtins.str
    list_resolved_at: builtins.str | None = Field(default=None)
    context: builtins.dict[builtins.str, Any] | None = Field(default=None)
    ext: builtins.dict[builtins.str, Any] | None = Field(default=None)

__all__ = ['AcquireRightsRequestBase', 'AcquireRightsResponseBase', 'ActivateSignalRequestBase', 'ActivateSignalResponseBase', 'BuildCreativeInputRequiredResponseBase', 'BuildCreativeRequestBase', 'BuildCreativeSubmittedResponseBase', 'BuildCreativeResponseBase', 'BuildCreativeWorkingResponseBase', 'CalibrateContentRequestBase', 'CalibrateContentResponseBase', 'CheckGovernanceRequestBase', 'CheckGovernanceResponseBase', 'ComplyTestControllerRequestBase', 'ComplyTestControllerResponseBase', 'ContextMatchRequestBase', 'ContextMatchResponseBase', 'CreateCollectionListRequestBase', 'CreateCollectionListResponseBase', 'CreateContentStandardsRequestBase', 'CreateContentStandardsResponseBase', 'CreateMediaBuyInputRequiredResponseBase', 'CreateMediaBuyRequestBase', 'CreateMediaBuySubmittedResponseBase', 'CreateMediaBuyResponseBase', 'CreateMediaBuyWorkingResponseBase', 'CreatePropertyListRequestBase', 'CreatePropertyListResponseBase', 'CreativeApprovalRequestBase', 'CreativeApprovalResponseBase', 'DeleteCollectionListRequestBase', 'DeleteCollectionListResponseBase', 'DeletePropertyListRequestBase', 'DeletePropertyListResponseBase', 'GetAccountFinancialsRequestBase', 'GetAccountFinancialsResponseBase', 'GetAdcpCapabilitiesRequestBase', 'GetAdcpCapabilitiesResponseBase', 'GetBrandIdentityRequestBase', 'GetBrandIdentityResponseBase', 'GetCollectionListRequestBase', 'GetCollectionListResponseBase', 'GetContentStandardsRequestBase', 'GetContentStandardsResponseBase', 'GetCreativeDeliveryRequestBase', 'GetCreativeDeliveryResponseBase', 'GetCreativeFeaturesRequestBase', 'GetCreativeFeaturesResponseBase', 'GetMediaBuyArtifactsRequestBase', 'GetMediaBuyArtifactsResponseBase', 'GetMediaBuyDeliveryRequestBase', 'GetMediaBuyDeliveryResponseBase', 'GetMediaBuysRequestBase', 'GetMediaBuysResponseBase', 'GetPlanAuditLogsRequestBase', 'GetPlanAuditLogsResponseBase', 'GetProductsInputRequiredResponseBase', 'GetProductsRequestBase', 'GetProductsSubmittedResponseBase', 'GetProductsResponseBase', 'GetProductsWorkingResponseBase', 'GetPropertyListRequestBase', 'GetPropertyListResponseBase', 'GetRightsRequestBase', 'GetRightsResponseBase', 'GetSignalsRequestBase', 'GetSignalsResponseBase', 'IdentityMatchRequestBase', 'IdentityMatchResponseBase', 'ListAccountsRequestBase', 'ListAccountsResponseBase', 'ListCollectionListsRequestBase', 'ListCollectionListsResponseBase', 'ListContentStandardsRequestBase', 'ListContentStandardsResponseBase', 'ListCreativeFormatsRequestBase', 'ListCreativeFormatsResponseBase', 'ListCreativesRequestBase', 'ListCreativesResponseBase', 'ListPropertyListsRequestBase', 'ListPropertyListsResponseBase', 'LogEventRequestBase', 'LogEventResponseBase', 'PackageRequestBase', 'PreviewCreativeRequestBase', 'PreviewCreativeResponseBase', 'ProvidePerformanceFeedbackRequestBase', 'ProvidePerformanceFeedbackResponseBase', 'ReportPlanOutcomeRequestBase', 'ReportPlanOutcomeResponseBase', 'ReportUsageRequestBase', 'ReportUsageResponseBase', 'SiGetOfferingRequestBase', 'SiGetOfferingResponseBase', 'SiInitiateSessionRequestBase', 'SiInitiateSessionResponseBase', 'SiSendMessageRequestBase', 'SiSendMessageResponseBase', 'SiTerminateSessionRequestBase', 'SiTerminateSessionResponseBase', 'SyncAccountsRequestBase', 'SyncAccountsResponseBase', 'SyncAudiencesRequestBase', 'SyncAudiencesResponseBase', 'SyncCatalogsInputRequiredResponseBase', 'SyncCatalogsRequestBase', 'SyncCatalogsSubmittedResponseBase', 'SyncCatalogsResponseBase', 'SyncCatalogsWorkingResponseBase', 'SyncCreativesInputRequiredResponseBase', 'SyncCreativesRequestBase', 'SyncCreativesSubmittedResponseBase', 'SyncCreativesResponseBase', 'SyncCreativesWorkingResponseBase', 'SyncEventSourcesRequestBase', 'SyncEventSourcesResponseBase', 'SyncGovernanceRequestBase', 'SyncGovernanceResponseBase', 'SyncPlansRequestBase', 'SyncPlansResponseBase', 'TasksGetRequestBase', 'TasksGetResponseBase', 'TasksListRequestBase', 'TasksListResponseBase', 'UpdateCollectionListRequestBase', 'UpdateCollectionListResponseBase', 'UpdateContentStandardsRequestBase', 'UpdateContentStandardsResponseBase', 'UpdateMediaBuyInputRequiredResponseBase', 'UpdateMediaBuyRequestBase', 'UpdateMediaBuySubmittedResponseBase', 'UpdateMediaBuyResponseBase', 'UpdateMediaBuyWorkingResponseBase', 'UpdatePropertyListRequestBase', 'UpdatePropertyListResponseBase', 'UpdateRightsRequestBase', 'UpdateRightsResponseBase', 'ValidateContentDeliveryRequestBase', 'ValidateContentDeliveryResponseBase', 'ValidatePropertyDeliveryRequestBase', 'ValidatePropertyDeliveryResponseBase']
