"""Generated nested types and field templates; no model construction."""
from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any, Literal
from typing_extensions import NotRequired, Required, TypedDict
if TYPE_CHECKING:
    from typing_extensions import Never
else:
    from ._runtime import _Never as Never
from pydantic import ConfigDict, with_config

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreBrandRef(TypedDict, total=False):
    domain: Required[builtins.str]
    brand_id: NotRequired[builtins.str]
    industries: NotRequired[builtins.list[builtins.str]]
    data_subject_contestation: NotRequired[_ExternalCoreBrandRefDataSubjectContestation]

@with_config(ConfigDict(extra="allow"))
class _AcquireRightsRequestBaseCampaign(TypedDict, total=False):
    description: Required[builtins.str]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    countries: NotRequired[builtins.list[builtins.str]]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    estimated_impressions: NotRequired[builtins.int]
    start_date: NotRequired[builtins.str]
    end_date: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePushNotificationConfig(TypedDict, total=False):
    url: Required[builtins.str]
    token: NotRequired[builtins.str]
    authentication: NotRequired[_ExternalCorePushNotificationConfigAuthentication]

@with_config(ConfigDict(extra="allow"))
class _ExternalBrandRightsTerms(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    amount: Required[builtins.float]
    currency: Required[builtins.str]
    period: NotRequired[Literal['daily', 'weekly', 'monthly', 'quarterly', 'annual', 'one_time']]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    impression_cap: NotRequired[builtins.int]
    overage_cpm: NotRequired[builtins.float]
    start_date: NotRequired[builtins.str]
    end_date: NotRequired[builtins.str]
    exclusivity: NotRequired[_ExternalBrandRightsTermsExclusivity]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreGenerationCredential(TypedDict, total=False):
    provider: Required[builtins.str]
    rights_key: Required[builtins.str]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    expires_at: NotRequired[builtins.str]
    endpoint: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _AcquireRightsResponseBaseDisclosure(TypedDict, total=False):
    required: Required[builtins.bool]
    text: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRightsConstraint(TypedDict, total=False):
    rights_id: Required[builtins.str]
    rights_agent: Required[_ExternalCoreRightsConstraintRightsAgent]
    valid_from: NotRequired[builtins.str]
    valid_until: NotRequired[builtins.str]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    countries: NotRequired[builtins.list[builtins.str]]
    excluded_countries: NotRequired[builtins.list[builtins.str]]
    impression_cap: NotRequired[builtins.int]
    right_type: NotRequired[Literal['talent', 'character', 'brand_ip', 'music', 'stock_media']]
    approval_status: NotRequired[Literal['pending', 'approved', 'rejected']]
    verification_url: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreError(TypedDict, total=False):
    code: Required[builtins.str]
    message: Required[builtins.str]
    field: NotRequired[builtins.str]
    suggestion: NotRequired[builtins.str]
    retry_after: NotRequired[builtins.float]
    issues: NotRequired[builtins.list[_ExternalCoreErrorIssuesItem]]
    details: NotRequired[builtins.dict[builtins.str, Any]]
    recovery: NotRequired[Literal['transient', 'correctable', 'terminal']]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalRequestBaseDestinationsItemVariant1(TypedDict, total=False):
    type: Required[Literal['platform']]
    platform: Required[builtins.str]
    account: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalRequestBaseDestinationsItemVariant2(TypedDict, total=False):
    type: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    account: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalResponseBaseDeploymentsItemVariant1(TypedDict, total=False):
    type: Required[Literal['platform']]
    platform: Required[builtins.str]
    account: NotRequired[builtins.str]
    is_live: Required[builtins.bool]
    activation_key: NotRequired[_ActivateSignalResponseBaseDeploymentsItemVariant1ActivationKeyVariant1 | _ActivateSignalResponseBaseDeploymentsItemVariant1ActivationKeyVariant2]
    estimated_activation_duration_minutes: NotRequired[builtins.float]
    deployed_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalResponseBaseDeploymentsItemVariant2(TypedDict, total=False):
    type: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    account: NotRequired[builtins.str]
    is_live: Required[builtins.bool]
    activation_key: NotRequired[_ActivateSignalResponseBaseDeploymentsItemVariant2ActivationKeyVariant1 | _ActivateSignalResponseBaseDeploymentsItemVariant2ActivationKeyVariant2]
    estimated_activation_duration_minutes: NotRequired[builtins.float]
    deployed_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeManifest(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    assets: Required[builtins.dict[builtins.str, Any]]
    rights: NotRequired[builtins.list[_ExternalCoreRightsConstraint]]
    industry_identifiers: NotRequired[builtins.list[_ExternalCoreIndustryIdentifier]]
    provenance: NotRequired[_ExternalCoreProvenance]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatId(TypedDict, total=False):
    agent_url: Required[builtins.str]
    id: Required[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    duration_ms: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeRequestBasePreviewInputsItem(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview(TypedDict, total=False):
    previews: Required[builtins.list[_BuildCreativeResponseBasePreviewPreviewsItem]]
    interactive_url: NotRequired[builtins.str]
    expires_at: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2(TypedDict, total=False):
    previews: Required[builtins.list[_BuildCreativeResponseBasePreview2PreviewsItem]]
    interactive_url: NotRequired[builtins.str]
    expires_at: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeConsumption(TypedDict, total=False):
    tokens: NotRequired[builtins.int]
    images_generated: NotRequired[builtins.int]
    renders: NotRequired[builtins.int]
    duration_seconds: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifact(TypedDict, total=False):
    property_rid: Required[builtins.str]
    artifact_id: Required[builtins.str]
    variant_id: NotRequired[builtins.str]
    format_id: NotRequired[_ExternalCoreFormatId]
    url: NotRequired[builtins.str]
    published_time: NotRequired[builtins.str]
    last_update_time: NotRequired[builtins.str]
    assets: Required[builtins.list[_ExternalContentStandardsArtifactAssetsItemVariant1 | _ExternalContentStandardsArtifactAssetsItemVariant2 | _ExternalContentStandardsArtifactAssetsItemVariant3 | _ExternalContentStandardsArtifactAssetsItemVariant4]]
    metadata: NotRequired[_ExternalContentStandardsArtifactMetadata]
    provenance: NotRequired[_ExternalCoreProvenance]
    identifiers: NotRequired[_ExternalContentStandardsArtifactIdentifiers]

@with_config(ConfigDict(extra="allow"))
class _CalibrateContentResponseBaseFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    status: Required[Literal['passed', 'failed', 'warning', 'unevaluated']]
    policy_id: NotRequired[builtins.str]
    explanation: NotRequired[builtins.str]
    confidence: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDelivery(TypedDict, total=False):
    geo: NotRequired[_ExternalCorePlannedDeliveryGeo]
    channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    frequency_cap: NotRequired[_ExternalCoreFrequencyCap]
    audience_summary: NotRequired[builtins.str]
    audience_targeting: NotRequired[builtins.list[_ExternalCorePlannedDeliveryAudienceTargetingItemVariant1 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant4]]
    total_budget: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]
    enforced_policies: NotRequired[builtins.list[builtins.str]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _CheckGovernanceRequestBaseDeliveryMetrics(TypedDict, total=False):
    reporting_period: Required[_CheckGovernanceRequestBaseDeliveryMetricsReportingPeriod]
    spend: NotRequired[builtins.float]
    cumulative_spend: NotRequired[builtins.float]
    impressions: NotRequired[builtins.int]
    cumulative_impressions: NotRequired[builtins.int]
    geo_distribution: NotRequired[builtins.dict[builtins.str, builtins.float]]
    channel_distribution: NotRequired[builtins.dict[builtins.str, builtins.float]]
    pacing: NotRequired[Literal['ahead', 'on_track', 'behind']]
    audience_distribution: NotRequired[_CheckGovernanceRequestBaseDeliveryMetricsAudienceDistribution]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreBusinessEntity(TypedDict, total=False):
    legal_name: Required[builtins.str]
    vat_id: NotRequired[builtins.str]
    tax_id: NotRequired[builtins.str]
    registration_number: NotRequired[builtins.str]
    address: NotRequired[_ExternalCoreBusinessEntityAddress]
    contacts: NotRequired[builtins.list[_ExternalCoreBusinessEntityContactsItem]]
    bank: NotRequired[_ExternalCoreBusinessEntityBank]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _CheckGovernanceResponseBaseFindingsItem(TypedDict, total=False):
    category_id: Required[builtins.str]
    policy_id: NotRequired[builtins.str]
    source_plan_id: NotRequired[builtins.str]
    severity: Required[Literal['info', 'warning', 'critical']]
    explanation: Required[builtins.str]
    details: NotRequired[builtins.dict[builtins.str, Any]]
    confidence: NotRequired[builtins.float]
    uncertainty_reason: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CheckGovernanceResponseBaseConditionsItem(TypedDict, total=False):
    field: Required[builtins.str]
    required_value: NotRequired[Any]
    reason: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ComplyTestControllerRequestBaseParams(TypedDict, total=False):
    creative_id: NotRequired[builtins.str]
    account_id: NotRequired[builtins.str]
    media_buy_id: NotRequired[builtins.str]
    session_id: NotRequired[builtins.str]
    product_id: NotRequired[builtins.str]
    pricing_option_id: NotRequired[builtins.str]
    plan_id: NotRequired[builtins.str]
    fixture: NotRequired[builtins.dict[builtins.str, Any]]
    status: NotRequired[builtins.str]
    rejection_reason: NotRequired[builtins.str]
    termination_reason: NotRequired[builtins.str]
    impressions: NotRequired[builtins.int]
    clicks: NotRequired[builtins.int]
    conversions: NotRequired[builtins.int]
    reported_spend: NotRequired[_ComplyTestControllerRequestBaseParamsReportedSpend]
    spend_percentage: NotRequired[builtins.float]
    arm: NotRequired[Literal['submitted', 'input-required']]
    task_id: NotRequired[builtins.str]
    message: NotRequired[builtins.str]
    format_id: NotRequired[builtins.str]
    result: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ComplyTestControllerResponseBaseForced(TypedDict, total=False):
    arm: Required[Literal['submitted', 'input-required']]
    task_id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ContextMatchRequestBaseArtifactRefsItem(TypedDict, total=False):
    type: Required[Literal['url', 'url_hash', 'eidr', 'gracenote', 'isrc', 'gtin', 'rss_guid', 'isbn', 'custom']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ContextMatchRequestBaseGeo(TypedDict, total=False):
    country: NotRequired[builtins.str]
    region: NotRequired[builtins.str]
    metro: NotRequired[_ContextMatchRequestBaseGeoMetro]

@with_config(ConfigDict(extra="allow"))
class _ContextMatchRequestBaseContextSignals(TypedDict, total=False):
    topics: NotRequired[builtins.list[builtins.str]]
    taxonomy_source: NotRequired[builtins.str]
    taxonomy_id: NotRequired[builtins.int]
    sentiment: NotRequired[Literal['positive', 'negative', 'neutral', 'mixed']]
    keywords: NotRequired[builtins.list[builtins.str]]
    language: NotRequired[builtins.str]
    content_policies: NotRequired[builtins.list[builtins.str]]
    summary: NotRequired[builtins.str]
    embedding: NotRequired[builtins.str]
    embedding_model: NotRequired[builtins.str]
    embedding_dims: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalTmpOffer(TypedDict, total=False):
    package_id: Required[builtins.str]
    seller_agent: NotRequired[_ExternalCoreSellerAgentRef]
    brand: NotRequired[_ExternalCoreBrandRef]
    price: NotRequired[_ExternalTmpOfferPrice]
    summary: NotRequired[builtins.str]
    creative_manifest: NotRequired[_ExternalCoreCreativeManifest]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ContextMatchResponseBaseSignals(TypedDict, total=False):
    segments: NotRequired[builtins.list[builtins.str]]
    targeting_kvs: NotRequired[builtins.list[_ContextMatchResponseBaseSignalsTargetingKvsItem]]

@with_config(ConfigDict(extra="allow"))
class _CreateCollectionListRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateCollectionListRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _CreateCollectionListRequestBaseBaseCollectionsItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['distribution_ids']]
    identifiers: Required[builtins.list[_CreateCollectionListRequestBaseBaseCollectionsItemVariant1IdentifiersItem]]

@with_config(ConfigDict(extra="allow"))
class _CreateCollectionListRequestBaseBaseCollectionsItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_collections']]
    publisher_domain: Required[builtins.str]
    collection_ids: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _CreateCollectionListRequestBaseBaseCollectionsItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['publisher_genres']]
    publisher_domain: Required[builtins.str]
    genres: Required[builtins.list[builtins.str]]
    genre_taxonomy: Required[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCollectionCollectionListFilters(TypedDict, total=False):
    content_ratings_exclude: NotRequired[builtins.list[_ExternalCoreContentRating]]
    content_ratings_include: NotRequired[builtins.list[_ExternalCoreContentRating]]
    genres_exclude: NotRequired[builtins.list[builtins.str]]
    genres_include: NotRequired[builtins.list[builtins.str]]
    genre_taxonomy: NotRequired[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]
    kinds: NotRequired[builtins.list[Literal['series', 'publication', 'event_series', 'rotation']]]
    exclude_distribution_ids: NotRequired[builtins.list[_ExternalCollectionCollectionListFiltersExcludeDistributionIdsItem]]
    production_quality: NotRequired[builtins.list[Literal['professional', 'prosumer', 'ugc']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCollectionCollectionList(TypedDict, total=False):
    list_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    account: NotRequired[_ExternalCollectionCollectionListAccountVariant1 | _ExternalCollectionCollectionListAccountVariant2]
    base_collections: NotRequired[builtins.list[_ExternalCollectionCollectionListBaseCollectionsItemVariant1 | _ExternalCollectionCollectionListBaseCollectionsItemVariant2 | _ExternalCollectionCollectionListBaseCollectionsItemVariant3]]
    filters: NotRequired[_ExternalCollectionCollectionListFilters]
    brand: NotRequired[_ExternalCoreBrandRef]
    webhook_url: NotRequired[builtins.str]
    cache_duration_hours: NotRequired[builtins.int]
    created_at: NotRequired[builtins.str]
    updated_at: NotRequired[builtins.str]
    collection_count: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseScope(TypedDict, total=False):
    countries_all: NotRequired[builtins.list[builtins.str]]
    channels_any: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    languages_any: Required[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernancePolicyEntry(TypedDict, total=False):
    policy_id: Required[builtins.str]
    source: NotRequired[Literal['registry', 'inline']]
    version: NotRequired[builtins.str]
    name: NotRequired[builtins.str]
    description: NotRequired[builtins.str]
    category: NotRequired[Literal['regulation', 'standard']]
    enforcement: Required[Literal['must', 'should', 'may']]
    requires_human_review: NotRequired[builtins.bool]
    jurisdictions: NotRequired[builtins.list[builtins.str]]
    region_aliases: NotRequired[builtins.dict[builtins.str, builtins.list[builtins.str]]]
    policy_categories: NotRequired[builtins.list[builtins.str]]
    channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    governance_domains: NotRequired[builtins.list[Literal['campaign', 'property', 'creative', 'content_standards']]]
    effective_date: NotRequired[builtins.str]
    sunset_date: NotRequired[builtins.str]
    source_url: NotRequired[builtins.str]
    source_name: NotRequired[builtins.str]
    policy: Required[builtins.str]
    guidance: NotRequired[builtins.str]
    exemplars: NotRequired[_ExternalGovernancePolicyEntryExemplars]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant1 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2]]

@with_config(ConfigDict(extra="allow"))
class _CreateMediaBuyRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateMediaBuyRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _CreateMediaBuyRequestBaseTotalBudget(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequest(TypedDict, total=False):
    adcp_major_version: NotRequired[builtins.int]
    product_id: Required[builtins.str]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    budget: Required[builtins.float]
    pacing: NotRequired[Literal['even', 'asap', 'front_loaded']]
    pricing_option_id: Required[builtins.str]
    bid_price: NotRequired[builtins.float]
    impressions: NotRequired[builtins.float]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    paused: NotRequired[builtins.bool]
    catalogs: NotRequired[builtins.list[_ExternalCoreCatalog]]
    optimization_goals: NotRequired[builtins.list[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1 | _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2]]
    targeting_overlay: NotRequired[_ExternalCoreTargeting]
    measurement_terms: NotRequired[_ExternalCoreMeasurementTerms]
    performance_standards: NotRequired[builtins.list[_ExternalCorePerformanceStandard]]
    creative_assignments: NotRequired[builtins.list[_ExternalCoreCreativeAssignment]]
    creatives: NotRequired[builtins.list[_ExternalCoreCreativeAsset]]
    agency_estimate_number: NotRequired[builtins.str]
    context: NotRequired[builtins.dict[builtins.str, Any]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _CreateMediaBuyRequestBaseIoAcceptance(TypedDict, total=False):
    io_id: Required[builtins.str]
    accepted_at: Required[builtins.str]
    signatory: Required[builtins.str]
    signature_id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreReportingWebhook(TypedDict, total=False):
    url: Required[builtins.str]
    token: NotRequired[builtins.str]
    authentication: Required[_ExternalCoreReportingWebhookAuthentication]
    reporting_frequency: Required[Literal['hourly', 'daily', 'monthly']]
    requested_metrics: NotRequired[builtins.list[Literal['impressions', 'spend', 'clicks', 'ctr', 'video_completions', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'viewability', 'engagement_rate', 'views', 'completed_views', 'leads', 'reach', 'frequency', 'grps', 'quartile_data', 'dooh_metrics', 'cost_per_click']]]

@with_config(ConfigDict(extra="allow"))
class _CreateMediaBuyRequestBaseArtifactWebhook(TypedDict, total=False):
    url: Required[builtins.str]
    token: NotRequired[builtins.str]
    authentication: Required[_CreateMediaBuyRequestBaseArtifactWebhookAuthentication]
    delivery_mode: Required[Literal['realtime', 'batched']]
    batch_frequency: NotRequired[Literal['hourly', 'daily']]
    sampling_rate: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAccount(TypedDict, total=False):
    account_id: Required[builtins.str]
    name: Required[builtins.str]
    advertiser: NotRequired[builtins.str]
    billing_proxy: NotRequired[builtins.str]
    status: Required[Literal['active', 'pending_approval', 'rejected', 'payment_required', 'suspended', 'closed']]
    brand: NotRequired[_ExternalCoreBrandRef]
    operator: NotRequired[builtins.str]
    billing: NotRequired[Literal['operator', 'agent', 'advertiser']]
    billing_entity: NotRequired[_ExternalCoreBusinessEntity]
    rate_card: NotRequired[builtins.str]
    payment_terms: NotRequired[Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay']]
    credit_limit: NotRequired[_ExternalCoreAccountCreditLimit]
    setup: NotRequired[_ExternalCoreAccountSetup]
    account_scope: NotRequired[Literal['operator', 'brand', 'operator_brand', 'agent']]
    governance_agents: NotRequired[builtins.list[_ExternalCoreAccountGovernanceAgentsItem]]
    reporting_bucket: NotRequired[_ExternalCoreAccountReportingBucket]
    sandbox: NotRequired[builtins.bool]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackage(TypedDict, total=False):
    package_id: Required[builtins.str]
    product_id: NotRequired[builtins.str]
    budget: NotRequired[builtins.float]
    pacing: NotRequired[Literal['even', 'asap', 'front_loaded']]
    pricing_option_id: NotRequired[builtins.str]
    bid_price: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    impressions: NotRequired[builtins.float]
    catalogs: NotRequired[builtins.list[_ExternalCoreCatalog]]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    targeting_overlay: NotRequired[_ExternalCoreTargeting]
    measurement_terms: NotRequired[_ExternalCoreMeasurementTerms]
    performance_standards: NotRequired[builtins.list[_ExternalCorePerformanceStandard]]
    creative_assignments: NotRequired[builtins.list[_ExternalCoreCreativeAssignment]]
    format_ids_to_provide: NotRequired[builtins.list[_ExternalCoreFormatId]]
    optimization_goals: NotRequired[builtins.list[_ExternalCorePackageOptimizationGoalsItemVariant1 | _ExternalCorePackageOptimizationGoalsItemVariant2]]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    paused: NotRequired[builtins.bool]
    canceled: NotRequired[builtins.bool]
    cancellation: NotRequired[_ExternalCorePackageCancellation]
    agency_estimate_number: NotRequired[builtins.str]
    creative_deadline: NotRequired[builtins.str]
    context: NotRequired[builtins.dict[builtins.str, Any]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _CreatePropertyListRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreatePropertyListRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _CreatePropertyListRequestBaseBasePropertiesItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['publisher_tags']]
    publisher_domain: Required[builtins.str]
    tags: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _CreatePropertyListRequestBaseBasePropertiesItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_ids']]
    publisher_domain: Required[builtins.str]
    property_ids: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _CreatePropertyListRequestBaseBasePropertiesItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['identifiers']]
    identifiers: Required[builtins.list[_ExternalCoreIdentifier]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListFilters(TypedDict, total=False):
    countries_all: NotRequired[builtins.list[builtins.str]]
    channels_any: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    property_types: NotRequired[builtins.list[Literal['website', 'mobile_app', 'ctv_app', 'desktop_app', 'dooh', 'podcast', 'radio', 'linear_tv', 'streaming_audio', 'ai_assistant']]]
    feature_requirements: NotRequired[builtins.list[_ExternalCoreFeatureRequirement]]
    exclude_identifiers: NotRequired[builtins.list[_ExternalCoreIdentifier]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyList(TypedDict, total=False):
    list_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    account: NotRequired[_ExternalPropertyPropertyListAccountVariant1 | _ExternalPropertyPropertyListAccountVariant2]
    base_properties: NotRequired[builtins.list[_ExternalPropertyPropertyListBasePropertiesItemVariant1 | _ExternalPropertyPropertyListBasePropertiesItemVariant2 | _ExternalPropertyPropertyListBasePropertiesItemVariant3]]
    filters: NotRequired[_ExternalPropertyPropertyListFilters]
    brand: NotRequired[_ExternalCoreBrandRef]
    webhook_url: NotRequired[builtins.str]
    cache_duration_hours: NotRequired[builtins.int]
    created_at: NotRequired[builtins.str]
    updated_at: NotRequired[builtins.str]
    property_count: NotRequired[builtins.int]
    pricing_options: NotRequired[builtins.list[_ExternalPropertyPropertyListPricingOptionsItemVariant1 | _ExternalPropertyPropertyListPricingOptionsItemVariant2 | _ExternalPropertyPropertyListPricingOptionsItemVariant3 | _ExternalPropertyPropertyListPricingOptionsItemVariant4 | _ExternalPropertyPropertyListPricingOptionsItemVariant5]]

@with_config(ConfigDict(extra="allow"))
class _DeleteCollectionListRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _DeleteCollectionListRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _DeletePropertyListRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _DeletePropertyListRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAccountFinancialsRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAccountFinancialsRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDateRange(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAccountFinancialsResponseBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAccountFinancialsResponseBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAccountFinancialsResponseBaseSpend(TypedDict, total=False):
    total_spend: Required[builtins.float]
    media_buy_count: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetAccountFinancialsResponseBaseCredit(TypedDict, total=False):
    credit_limit: Required[builtins.float]
    available_credit: Required[builtins.float]
    utilization_percent: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetAccountFinancialsResponseBaseBalance(TypedDict, total=False):
    available: Required[builtins.float]
    last_top_up: NotRequired[_GetAccountFinancialsResponseBaseBalanceLastTopUp]

@with_config(ConfigDict(extra="allow"))
class _GetAccountFinancialsResponseBaseInvoicesItem(TypedDict, total=False):
    invoice_id: Required[builtins.str]
    period: NotRequired[_ExternalCoreDateRange]
    amount: Required[builtins.float]
    status: Required[Literal['draft', 'issued', 'paid', 'past_due', 'void']]
    due_date: NotRequired[builtins.str]
    paid_date: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseAdcp(TypedDict, total=False):
    major_versions: Required[builtins.list[builtins.int]]
    idempotency: Required[_GetAdcpCapabilitiesResponseBaseAdcpIdempotencyVariant1 | _GetAdcpCapabilitiesResponseBaseAdcpIdempotencyVariant2]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseAccount(TypedDict, total=False):
    require_operator_auth: NotRequired[builtins.bool]
    authorization_endpoint: NotRequired[builtins.str]
    supported_billing: NotRequired[builtins.list[Literal['operator', 'agent', 'advertiser']]]
    required_for_products: NotRequired[builtins.bool]
    account_financials: NotRequired[builtins.bool]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuy(TypedDict, total=False):
    supported_pricing_models: NotRequired[builtins.list[Literal['cpm', 'vcpm', 'cpc', 'cpcv', 'cpv', 'cpp', 'cpa', 'flat_rate', 'time']]]
    reporting_delivery_methods: NotRequired[builtins.list[Literal['webhook', 'offline']]]
    offline_delivery_protocols: NotRequired[builtins.list[Literal['s3', 'gcs', 'azure_blob']]]
    features: NotRequired[_ExternalCoreMediaBuyFeatures]
    execution: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecution]
    audience_targeting: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyAudienceTargeting]
    conversion_tracking: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyConversionTracking]
    content_standards: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyContentStandards]
    portfolio: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyPortfolio]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseSignals(TypedDict, total=False):
    data_provider_domains: NotRequired[builtins.list[builtins.str]]
    features: NotRequired[_GetAdcpCapabilitiesResponseBaseSignalsFeatures]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseGovernance(TypedDict, total=False):
    aggregation_window_days: NotRequired[builtins.int]
    property_features: NotRequired[builtins.list[_GetAdcpCapabilitiesResponseBaseGovernancePropertyFeaturesItem]]
    creative_features: NotRequired[builtins.list[_GetAdcpCapabilitiesResponseBaseGovernanceCreativeFeaturesItem]]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseSponsoredIntelligence(TypedDict, total=False):
    endpoint: Required[_GetAdcpCapabilitiesResponseBaseSponsoredIntelligenceEndpoint]
    capabilities: Required[_ExternalSponsoredIntelligenceSiCapabilities]
    brand_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseBrand(TypedDict, total=False):
    rights: NotRequired[builtins.bool]
    right_types: NotRequired[builtins.list[Literal['talent', 'character', 'brand_ip', 'music', 'stock_media']]]
    available_uses: NotRequired[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    generation_providers: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseCreative(TypedDict, total=False):
    supports_compliance: NotRequired[builtins.bool]
    has_creative_library: NotRequired[builtins.bool]
    supports_generation: NotRequired[builtins.bool]
    supports_transformation: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseRequestSigning(TypedDict, total=False):
    supported: Required[builtins.bool]
    covers_content_digest: NotRequired[Literal['required', 'forbidden', 'either']]
    required_for: NotRequired[builtins.list[builtins.str]]
    warn_for: NotRequired[builtins.list[builtins.str]]
    supported_for: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseWebhookSigning(TypedDict, total=False):
    supported: Required[builtins.bool]
    profile: NotRequired[Literal['adcp/webhook-signing/v1']]
    algorithms: NotRequired[builtins.list[Literal['ed25519', 'ecdsa-p256-sha256']]]
    legacy_hmac_fallback: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseIdentity(TypedDict, total=False):
    per_principal_key_isolation: NotRequired[builtins.bool]
    key_origins: NotRequired[_GetAdcpCapabilitiesResponseBaseIdentityKeyOrigins]
    compromise_notification: NotRequired[_GetAdcpCapabilitiesResponseBaseIdentityCompromiseNotification]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseComplianceTesting(TypedDict, total=False):
    scenarios: Required[builtins.list[Literal['force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend']]]

@with_config(ConfigDict(extra="allow"))
class _GetBrandIdentityResponseBaseHouse(TypedDict, total=False):
    domain: Required[builtins.str]
    name: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetBrandIdentityResponseBaseLogosItem(TypedDict, total=False):
    url: Required[builtins.str]
    orientation: NotRequired[Literal['square', 'horizontal', 'vertical', 'stacked']]
    background: NotRequired[Literal['dark-bg', 'light-bg', 'transparent-bg']]
    variant: NotRequired[Literal['primary', 'secondary', 'icon', 'wordmark', 'full-lockup']]
    tags: NotRequired[builtins.list[builtins.str]]
    usage: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetBrandIdentityResponseBaseColors(TypedDict, total=False):
    primary: NotRequired[builtins.str | builtins.list[builtins.str]]
    secondary: NotRequired[builtins.str | builtins.list[builtins.str]]
    accent: NotRequired[builtins.str | builtins.list[builtins.str]]
    background: NotRequired[builtins.str | builtins.list[builtins.str]]
    text: NotRequired[builtins.str | builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _GetBrandIdentityResponseBaseFonts(TypedDict, total=False):
    primary: NotRequired[builtins.str | _FontRoleVariant2]
    secondary: NotRequired[builtins.str | _FontRoleVariant2]

@with_config(ConfigDict(extra="allow"))
class _GetBrandIdentityResponseBaseTone(TypedDict, total=False):
    voice: NotRequired[builtins.str]
    attributes: NotRequired[builtins.list[builtins.str]]
    dos: NotRequired[builtins.list[builtins.str]]
    donts: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _GetBrandIdentityResponseBaseVoiceSynthesis(TypedDict, total=False):
    provider: NotRequired[builtins.str]
    voice_id: NotRequired[builtins.str]
    settings: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetBrandIdentityResponseBaseAssetsItem(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_type: Required[Literal['image', 'video', 'audio', 'text', 'markdown', 'html', 'css', 'javascript', 'vast', 'daast', 'url', 'webhook', 'brief', 'catalog']]
    url: Required[builtins.str]
    tags: NotRequired[builtins.list[builtins.str]]
    name: NotRequired[builtins.str]
    description: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    duration_seconds: NotRequired[builtins.float]
    file_size_bytes: NotRequired[builtins.int]
    format: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetBrandIdentityResponseBaseRights(TypedDict, total=False):
    available_uses: NotRequired[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    countries: NotRequired[builtins.list[builtins.str]]
    excluded_countries: NotRequired[builtins.list[builtins.str]]
    exclusivity_model: NotRequired[builtins.str]
    content_restrictions: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _GetCollectionListRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetCollectionListRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetCollectionListRequestBasePagination(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetCollectionListResponseBaseCollectionsItem(TypedDict, total=False):
    collection_rid: NotRequired[builtins.str]
    name: Required[builtins.str]
    distribution_ids: NotRequired[builtins.list[_GetCollectionListResponseBaseCollectionsItemDistributionIdsItem]]
    content_rating: NotRequired[_ExternalCoreContentRating]
    genre: NotRequired[builtins.list[builtins.str]]
    genre_taxonomy: NotRequired[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]
    kind: NotRequired[Literal['series', 'publication', 'event_series', 'rotation']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePaginationResponse(TypedDict, total=False):
    has_more: Required[builtins.bool]
    cursor: NotRequired[builtins.str]
    total_count: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetCollectionListResponseBaseCoverageGapsValueItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetContentStandardsResponseBaseCalibrationExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_ExternalContentStandardsArtifact]]

@with_config(ConfigDict(extra="allow"))
class _GetContentStandardsResponseBasePricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetContentStandardsResponseBasePricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetContentStandardsResponseBasePricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetContentStandardsResponseBasePricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetContentStandardsResponseBasePricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_GetContentStandardsResponseBasePricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetCreativeDeliveryRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetCreativeDeliveryRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePaginationRequest(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetCreativeDeliveryResponseBaseReportingPeriod(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]
    timezone: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetCreativeDeliveryResponseBaseCreativesItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    media_buy_id: NotRequired[builtins.str]
    format_id: NotRequired[_ExternalCoreFormatId]
    totals: NotRequired[_ExternalCoreDeliveryMetrics]
    variant_count: NotRequired[builtins.int]
    variants: Required[builtins.list[_ExternalCoreCreativeVariant]]

@with_config(ConfigDict(extra="allow"))
class _GetCreativeDeliveryResponseBasePagination(TypedDict, total=False):
    limit: Required[builtins.int]
    offset: Required[builtins.int]
    has_more: Required[builtins.bool]
    total: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetCreativeFeaturesRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetCreativeFeaturesRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCreativeCreativeFeatureResult(TypedDict, total=False):
    feature_id: Required[builtins.str]
    value: Required[builtins.bool | builtins.float | builtins.str]
    unit: NotRequired[builtins.str]
    confidence: NotRequired[builtins.float]
    measured_at: NotRequired[builtins.str]
    expires_at: NotRequired[builtins.str]
    methodology_version: NotRequired[builtins.str]
    details: NotRequired[builtins.dict[builtins.str, Any]]
    policy_id: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyArtifactsRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyArtifactsRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyArtifactsRequestBaseTimeRange(TypedDict, total=False):
    start: NotRequired[builtins.str]
    end: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyArtifactsRequestBasePagination(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyArtifactsResponseBaseArtifactsItem(TypedDict, total=False):
    record_id: Required[builtins.str]
    timestamp: NotRequired[builtins.str]
    package_id: NotRequired[builtins.str]
    artifact: Required[_ExternalContentStandardsArtifact]
    country: NotRequired[builtins.str]
    channel: NotRequired[builtins.str]
    brand_context: NotRequired[_GetMediaBuyArtifactsResponseBaseArtifactsItemBrandContext]
    local_verdict: NotRequired[Literal['pass', 'fail', 'unevaluated']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyArtifactsResponseBaseCollectionInfo(TypedDict, total=False):
    total_deliveries: NotRequired[builtins.int]
    total_collected: NotRequired[builtins.int]
    returned_count: NotRequired[builtins.int]
    effective_rate: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseAttributionWindow(TypedDict, total=False):
    post_click: NotRequired[_GetMediaBuyDeliveryRequestBaseAttributionWindowPostClick]
    post_view: NotRequired[_GetMediaBuyDeliveryRequestBaseAttributionWindowPostView]
    model: NotRequired[Literal['last_touch', 'first_touch', 'linear', 'time_decay', 'data_driven']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseReportingDimensions(TypedDict, total=False):
    geo: NotRequired[_GetMediaBuyDeliveryRequestBaseReportingDimensionsGeo]
    device_type: NotRequired[_GetMediaBuyDeliveryRequestBaseReportingDimensionsDeviceType]
    device_platform: NotRequired[_GetMediaBuyDeliveryRequestBaseReportingDimensionsDevicePlatform]
    audience: NotRequired[_GetMediaBuyDeliveryRequestBaseReportingDimensionsAudience]
    placement: NotRequired[_GetMediaBuyDeliveryRequestBaseReportingDimensionsPlacement]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseReportingPeriod(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAttributionWindow(TypedDict, total=False):
    post_click: NotRequired[_ExternalCoreAttributionWindowPostClick]
    post_view: NotRequired[_ExternalCoreAttributionWindowPostView]
    model: Required[Literal['last_touch', 'first_touch', 'linear', 'time_decay', 'data_driven']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseAggregatedTotals(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    media_buy_count: Required[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItem(TypedDict, total=False):
    media_buy_id: Required[builtins.str]
    status: Required[Literal['pending_creatives', 'pending_start', 'pending', 'active', 'paused', 'completed', 'rejected', 'canceled', 'failed', 'reporting_delayed']]
    expected_availability: NotRequired[builtins.str]
    is_adjusted: NotRequired[builtins.bool]
    pricing_model: NotRequired[Literal['cpm', 'vcpm', 'cpc', 'cpcv', 'cpv', 'cpp', 'cpa', 'flat_rate', 'time']]
    totals: Required[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotals]
    by_package: Required[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItem]]
    daily_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemDailyBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuysRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuysRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuysResponseBaseMediaBuysItem(TypedDict, total=False):
    media_buy_id: Required[builtins.str]
    account: NotRequired[_ExternalCoreAccount]
    invoice_recipient: NotRequired[_ExternalCoreBusinessEntity]
    status: Required[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']]
    currency: Required[builtins.str]
    total_budget: Required[builtins.float]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    creative_deadline: NotRequired[builtins.str]
    confirmed_at: NotRequired[builtins.str]
    cancellation: NotRequired[_GetMediaBuysResponseBaseMediaBuysItemCancellation]
    revision: NotRequired[builtins.int]
    created_at: NotRequired[builtins.str]
    updated_at: NotRequired[builtins.str]
    valid_actions: NotRequired[builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']]]
    history: NotRequired[builtins.list[_GetMediaBuysResponseBaseMediaBuysItemHistoryItem]]
    packages: Required[builtins.list[_GetMediaBuysResponseBaseMediaBuysItemPackagesItem]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItem(TypedDict, total=False):
    plan_id: Required[builtins.str]
    plan_version: Required[builtins.int]
    status: Required[Literal['active', 'suspended', 'completed']]
    budget: Required[_GetPlanAuditLogsResponseBasePlansItemBudget]
    channel_allocation: NotRequired[builtins.dict[builtins.str, _GetPlanAuditLogsResponseBasePlansItemChannelAllocationValue]]
    summary: Required[_GetPlanAuditLogsResponseBasePlansItemSummary]
    entries: NotRequired[builtins.list[_GetPlanAuditLogsResponseBasePlansItemEntriesItem]]
    governed_actions: Required[builtins.list[_GetPlanAuditLogsResponseBasePlansItemGovernedActionsItem]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProduct(TypedDict, total=False):
    product_id: Required[builtins.str]
    name: Required[builtins.str]
    description: Required[builtins.str]
    publisher_properties: Required[builtins.list[_ExternalCoreProductPublisherPropertiesItemVariant1 | _ExternalCoreProductPublisherPropertiesItemVariant2 | _ExternalCoreProductPublisherPropertiesItemVariant3]]
    channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    format_ids: Required[builtins.list[_ExternalCoreFormatId]]
    placements: NotRequired[builtins.list[_ExternalCorePlacement]]
    delivery_type: Required[Literal['guaranteed', 'non_guaranteed']]
    exclusivity: NotRequired[Literal['none', 'category', 'exclusive']]
    pricing_options: Required[builtins.list[_ExternalCoreProductPricingOptionsItemVariant1 | _ExternalCoreProductPricingOptionsItemVariant2 | _ExternalCoreProductPricingOptionsItemVariant3 | _ExternalCoreProductPricingOptionsItemVariant4 | _ExternalCoreProductPricingOptionsItemVariant5 | _ExternalCoreProductPricingOptionsItemVariant6 | _ExternalCoreProductPricingOptionsItemVariant7 | _ExternalCoreProductPricingOptionsItemVariant8 | _ExternalCoreProductPricingOptionsItemVariant9]]
    forecast: NotRequired[_ExternalCoreDeliveryForecast]
    outcome_measurement: NotRequired[_ExternalCoreOutcomeMeasurement]
    delivery_measurement: NotRequired[_ExternalCoreProductDeliveryMeasurement]
    measurement_terms: NotRequired[_ExternalCoreMeasurementTerms]
    performance_standards: NotRequired[builtins.list[_ExternalCorePerformanceStandard]]
    cancellation_policy: NotRequired[_ExternalCoreCancellationPolicy]
    reporting_capabilities: Required[_ExternalCoreReportingCapabilities]
    creative_policy: NotRequired[_ExternalCoreCreativePolicy]
    is_custom: NotRequired[builtins.bool]
    property_targeting_allowed: NotRequired[builtins.bool]
    data_provider_signals: NotRequired[builtins.list[_ExternalCoreProductDataProviderSignalsItemVariant1 | _ExternalCoreProductDataProviderSignalsItemVariant2 | _ExternalCoreProductDataProviderSignalsItemVariant3]]
    signal_targeting_allowed: NotRequired[builtins.bool]
    catalog_types: NotRequired[builtins.list[Literal['offering', 'product', 'inventory', 'store', 'promotion', 'hotel', 'flight', 'job', 'vehicle', 'real_estate', 'education', 'destination', 'app']]]
    metric_optimization: NotRequired[_ExternalCoreProductMetricOptimization]
    max_optimization_goals: NotRequired[builtins.int]
    measurement_readiness: NotRequired[_ExternalCoreMeasurementReadiness]
    conversion_tracking: NotRequired[_ExternalCoreProductConversionTracking]
    catalog_match: NotRequired[_ExternalCoreProductCatalogMatch]
    brief_relevance: NotRequired[builtins.str]
    expires_at: NotRequired[builtins.str]
    product_card: NotRequired[_ExternalCoreProductProductCard]
    product_card_detailed: NotRequired[_ExternalCoreProductProductCardDetailed]
    collections: NotRequired[builtins.list[_ExternalCoreCollectionSelector]]
    collection_targeting_allowed: NotRequired[builtins.bool]
    installments: NotRequired[builtins.list[_ExternalCoreInstallment]]
    enforced_policies: NotRequired[builtins.list[builtins.str]]
    trusted_match: NotRequired[_ExternalCoreProductTrustedMatch]
    material_submission: NotRequired[_ExternalCoreProductMaterialSubmission]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetProductsRequestBaseRefineItemVariant1(TypedDict, total=False):
    scope: Required[Literal['request']]
    ask: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetProductsRequestBaseRefineItemVariant2(TypedDict, total=False):
    scope: Required[Literal['product']]
    product_id: Required[builtins.str]
    action: NotRequired[Literal['include', 'omit', 'more_like_this']]
    ask: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetProductsRequestBaseRefineItemVariant3(TypedDict, total=False):
    scope: Required[Literal['proposal']]
    proposal_id: Required[builtins.str]
    action: NotRequired[Literal['include', 'omit', 'finalize']]
    ask: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCatalog(TypedDict, total=False):
    catalog_id: NotRequired[builtins.str]
    name: NotRequired[builtins.str]
    type: Required[Literal['offering', 'product', 'inventory', 'store', 'promotion', 'hotel', 'flight', 'job', 'vehicle', 'real_estate', 'education', 'destination', 'app']]
    url: NotRequired[builtins.str]
    feed_format: NotRequired[Literal['google_merchant_center', 'facebook_catalog', 'shopify', 'linkedin_jobs', 'custom']]
    update_frequency: NotRequired[Literal['realtime', 'hourly', 'daily', 'weekly']]
    items: NotRequired[builtins.list[builtins.dict[builtins.str, Any]]]
    ids: NotRequired[builtins.list[builtins.str]]
    gtins: NotRequired[builtins.list[builtins.str]]
    tags: NotRequired[builtins.list[builtins.str]]
    category: NotRequired[builtins.str]
    query: NotRequired[builtins.str]
    conversion_events: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    content_id_type: NotRequired[Literal['sku', 'gtin', 'offering_id', 'job_id', 'hotel_id', 'flight_id', 'vehicle_id', 'listing_id', 'store_id', 'program_id', 'destination_id', 'app_id']]
    feed_field_mappings: NotRequired[builtins.list[_ExternalCoreCatalogFieldMapping]]

@with_config(ConfigDict(extra="allow"))
class _GetProductsRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetProductsRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFilters(TypedDict, total=False):
    delivery_type: NotRequired[Literal['guaranteed', 'non_guaranteed']]
    exclusivity: NotRequired[Literal['none', 'category', 'exclusive']]
    is_fixed_price: NotRequired[builtins.bool]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    standard_formats_only: NotRequired[builtins.bool]
    min_exposures: NotRequired[builtins.int]
    start_date: NotRequired[builtins.str]
    end_date: NotRequired[builtins.str]
    budget_range: NotRequired[_ExternalCoreProductFiltersBudgetRange]
    countries: NotRequired[builtins.list[builtins.str]]
    regions: NotRequired[builtins.list[builtins.str]]
    metros: NotRequired[builtins.list[_ExternalCoreProductFiltersMetrosItem]]
    channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    required_axe_integrations: NotRequired[builtins.list[builtins.str]]
    trusted_match: NotRequired[_ExternalCoreProductFiltersTrustedMatch]
    required_features: NotRequired[_ExternalCoreMediaBuyFeatures]
    required_geo_targeting: NotRequired[builtins.list[_ExternalCoreProductFiltersRequiredGeoTargetingItem]]
    signal_targeting: NotRequired[builtins.list[_ExternalCoreProductFiltersSignalTargetingItemVariant1 | _ExternalCoreProductFiltersSignalTargetingItemVariant2 | _ExternalCoreProductFiltersSignalTargetingItemVariant3]]
    postal_areas: NotRequired[builtins.list[_ExternalCoreProductFiltersPostalAreasItem]]
    geo_proximity: NotRequired[builtins.list[_ExternalCoreProductFiltersGeoProximityItemVariant1 | _ExternalCoreProductFiltersGeoProximityItemVariant2 | _ExternalCoreProductFiltersGeoProximityItemVariant3]]
    required_performance_standards: NotRequired[builtins.list[_ExternalCorePerformanceStandard]]
    keywords: NotRequired[builtins.list[_ExternalCoreProductFiltersKeywordsItem]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePropertyListRef(TypedDict, total=False):
    agent_url: Required[builtins.str]
    list_id: Required[builtins.str]
    auth_token: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetProductsRequestBaseTimeBudget(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProposal(TypedDict, total=False):
    proposal_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    allocations: Required[builtins.list[_ExternalCoreProductAllocation]]
    proposal_status: NotRequired[Literal['draft', 'committed']]
    expires_at: NotRequired[builtins.str]
    insertion_order: NotRequired[_ExternalCoreInsertionOrder]
    total_budget_guidance: NotRequired[_ExternalCoreProposalTotalBudgetGuidance]
    brief_alignment: NotRequired[builtins.str]
    forecast: NotRequired[_ExternalCoreDeliveryForecast]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetProductsResponseBaseRefinementAppliedItemVariant1(TypedDict, total=False):
    scope: Required[Literal['request']]
    status: Required[Literal['applied', 'partial', 'unable']]
    notes: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetProductsResponseBaseRefinementAppliedItemVariant2(TypedDict, total=False):
    scope: Required[Literal['product']]
    product_id: Required[builtins.str]
    status: Required[Literal['applied', 'partial', 'unable']]
    notes: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetProductsResponseBaseRefinementAppliedItemVariant3(TypedDict, total=False):
    scope: Required[Literal['proposal']]
    proposal_id: Required[builtins.str]
    status: Required[Literal['applied', 'partial', 'unable']]
    notes: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetProductsResponseBaseIncompleteItem(TypedDict, total=False):
    scope: Required[Literal['products', 'pricing', 'forecast', 'proposals']]
    description: Required[builtins.str]
    estimated_wait: NotRequired[_GetProductsResponseBaseIncompleteItemEstimatedWait]

@with_config(ConfigDict(extra="allow"))
class _GetPropertyListRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetPropertyListRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetPropertyListRequestBasePagination(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreIdentifier(TypedDict, total=False):
    type: Required[Literal['domain', 'subdomain', 'network_id', 'ios_bundle', 'android_package', 'apple_app_store_id', 'google_play_id', 'roku_store_id', 'fire_tv_asin', 'samsung_app_id', 'apple_tv_bundle', 'bundle_id', 'venue_id', 'screen_id', 'openooh_venue_type', 'rss_url', 'apple_podcast_id', 'spotify_collection_id', 'podcast_guid', 'station_id', 'facility_id']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetRightsResponseBaseRightsItem(TypedDict, total=False):
    rights_id: Required[builtins.str]
    brand_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    right_type: NotRequired[Literal['talent', 'character', 'brand_ip', 'music', 'stock_media']]
    match_score: NotRequired[builtins.float]
    match_reasons: NotRequired[builtins.list[builtins.str]]
    available_uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    countries: NotRequired[builtins.list[builtins.str]]
    excluded_countries: NotRequired[builtins.list[builtins.str]]
    exclusivity_status: NotRequired[_GetRightsResponseBaseRightsItemExclusivityStatus]
    pricing_options: Required[builtins.list[_ExternalBrandRightsPricingOption]]
    content_restrictions: NotRequired[builtins.list[builtins.str]]
    preview_assets: NotRequired[builtins.list[_GetRightsResponseBaseRightsItemPreviewAssetsItem]]

@with_config(ConfigDict(extra="allow"))
class _GetRightsResponseBaseExcludedItem(TypedDict, total=False):
    brand_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    reason: Required[builtins.str]
    suggestions: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsRequestBaseSignalIdsItemVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsRequestBaseSignalIdsItemVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsRequestBaseDestinationsItemVariant1(TypedDict, total=False):
    type: Required[Literal['platform']]
    platform: Required[builtins.str]
    account: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsRequestBaseDestinationsItemVariant2(TypedDict, total=False):
    type: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    account: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreSignalFilters(TypedDict, total=False):
    catalog_types: NotRequired[builtins.list[Literal['marketplace', 'custom', 'owned']]]
    data_providers: NotRequired[builtins.list[builtins.str]]
    max_cpm: NotRequired[builtins.float]
    max_percent: NotRequired[builtins.float]
    min_coverage_percentage: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItem(TypedDict, total=False):
    signal_id: Required[_GetSignalsResponseBaseSignalsItemSignalIdVariant1 | _GetSignalsResponseBaseSignalsItemSignalIdVariant2]
    signal_agent_segment_id: Required[builtins.str]
    name: Required[builtins.str]
    description: Required[builtins.str]
    value_type: NotRequired[Literal['binary', 'categorical', 'numeric']]
    categories: NotRequired[builtins.list[builtins.str]]
    range: NotRequired[_GetSignalsResponseBaseSignalsItemRange]
    signal_type: Required[Literal['marketplace', 'custom', 'owned']]
    data_provider: Required[builtins.str]
    coverage_percentage: Required[builtins.float]
    deployments: Required[builtins.list[_GetSignalsResponseBaseSignalsItemDeploymentsItemVariant1 | _GetSignalsResponseBaseSignalsItemDeploymentsItemVariant2]]
    pricing_options: Required[builtins.list[_GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant1 | _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant2 | _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant3 | _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant4 | _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant5]]

@with_config(ConfigDict(extra="allow"))
class _IdentityMatchRequestBaseIdentitiesItem(TypedDict, total=False):
    user_token: Required[builtins.str]
    uid_type: Required[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]

@with_config(ConfigDict(extra="allow"))
class _IdentityMatchRequestBaseConsent(TypedDict, total=False):
    gdpr: NotRequired[builtins.bool]
    tcf_consent: NotRequired[builtins.str]
    gpp: NotRequired[builtins.str]
    us_privacy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ListCollectionListsRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ListCollectionListsRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsContentStandards(TypedDict, total=False):
    standards_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    countries_all: NotRequired[builtins.list[builtins.str]]
    channels_any: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    languages_any: NotRequired[builtins.list[builtins.str]]
    policies: NotRequired[builtins.list[_ExternalGovernancePolicyEntry]]
    calibration_exemplars: NotRequired[_ExternalContentStandardsContentStandardsCalibrationExemplars]
    pricing_options: NotRequired[builtins.list[_ExternalContentStandardsContentStandardsPricingOptionsItemVariant1 | _ExternalContentStandardsContentStandardsPricingOptionsItemVariant2 | _ExternalContentStandardsContentStandardsPricingOptionsItemVariant3 | _ExternalContentStandardsContentStandardsPricingOptionsItemVariant4 | _ExternalContentStandardsContentStandardsPricingOptionsItemVariant5]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ListCreativeFormatsRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ListCreativeFormatsRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormat(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    example_url: NotRequired[builtins.str]
    accepts_parameters: NotRequired[builtins.list[Literal['dimensions', 'duration']]]
    renders: NotRequired[builtins.list[_ExternalCoreFormatRendersItemVariant1 | _ExternalCoreFormatRendersItemVariant2]]
    assets: NotRequired[builtins.list[_ExternalCoreFormatAssetsItemVariant1 | _ExternalCoreFormatAssetsItemVariant2 | _ExternalCoreFormatAssetsItemVariant3 | _ExternalCoreFormatAssetsItemVariant4 | _ExternalCoreFormatAssetsItemVariant5 | _ExternalCoreFormatAssetsItemVariant6 | _ExternalCoreFormatAssetsItemVariant7 | _ExternalCoreFormatAssetsItemVariant8 | _ExternalCoreFormatAssetsItemVariant9 | _ExternalCoreFormatAssetsItemVariant10 | _ExternalCoreFormatAssetsItemVariant11 | _ExternalCoreFormatAssetsItemVariant12 | _ExternalCoreFormatAssetsItemVariant13 | _ExternalCoreFormatAssetsItemVariant14 | _ExternalCoreFormatAssetsItemVariant15]]
    delivery: NotRequired[builtins.dict[builtins.str, Any]]
    supported_macros: NotRequired[builtins.list[Literal['MEDIA_BUY_ID', 'PACKAGE_ID', 'CREATIVE_ID', 'CACHEBUSTER', 'TIMESTAMP', 'CLICK_URL', 'GDPR', 'GDPR_CONSENT', 'US_PRIVACY', 'GPP_STRING', 'GPP_SID', 'IP_ADDRESS', 'LIMIT_AD_TRACKING', 'DEVICE_TYPE', 'OS', 'OS_VERSION', 'DEVICE_MAKE', 'DEVICE_MODEL', 'USER_AGENT', 'APP_BUNDLE', 'APP_NAME', 'COUNTRY', 'REGION', 'CITY', 'ZIP', 'DMA', 'LAT', 'LONG', 'DEVICE_ID', 'DEVICE_ID_TYPE', 'DOMAIN', 'PAGE_URL', 'REFERRER', 'KEYWORDS', 'PLACEMENT_ID', 'FOLD_POSITION', 'AD_WIDTH', 'AD_HEIGHT', 'VIDEO_ID', 'VIDEO_TITLE', 'VIDEO_DURATION', 'VIDEO_CATEGORY', 'CONTENT_GENRE', 'CONTENT_RATING', 'PLAYER_WIDTH', 'PLAYER_HEIGHT', 'POD_POSITION', 'POD_SIZE', 'AD_BREAK_ID', 'STATION_ID', 'COLLECTION_NAME', 'INSTALLMENT_ID', 'AUDIO_DURATION', 'TMPX', 'AXEM', 'CATALOG_ID', 'SKU', 'GTIN', 'OFFERING_ID', 'JOB_ID', 'HOTEL_ID', 'FLIGHT_ID', 'VEHICLE_ID', 'LISTING_ID', 'STORE_ID', 'PROGRAM_ID', 'DESTINATION_ID', 'CREATIVE_VARIANT_ID', 'APP_ITEM_ID'] | builtins.str]]
    input_format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    output_format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    format_card: NotRequired[_ExternalCoreFormatFormatCard]
    accessibility: NotRequired[_ExternalCoreFormatAccessibility]
    supported_disclosure_positions: NotRequired[builtins.list[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']]]
    disclosure_capabilities: NotRequired[builtins.list[_ExternalCoreFormatDisclosureCapabilitiesItem]]
    format_card_detailed: NotRequired[_ExternalCoreFormatFormatCardDetailed]
    reported_metrics: NotRequired[builtins.list[Literal['impressions', 'spend', 'clicks', 'ctr', 'video_completions', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'viewability', 'engagement_rate', 'views', 'completed_views', 'leads', 'reach', 'frequency', 'grps', 'quartile_data', 'dooh_metrics', 'cost_per_click']]]
    pricing_options: NotRequired[builtins.list[_ExternalCoreFormatPricingOptionsItemVariant1 | _ExternalCoreFormatPricingOptionsItemVariant2 | _ExternalCoreFormatPricingOptionsItemVariant3 | _ExternalCoreFormatPricingOptionsItemVariant4 | _ExternalCoreFormatPricingOptionsItemVariant5]]

@with_config(ConfigDict(extra="allow"))
class _ListCreativeFormatsResponseBaseCreativeAgentsItem(TypedDict, total=False):
    agent_url: Required[builtins.str]
    agent_name: NotRequired[builtins.str]
    capabilities: NotRequired[builtins.list[Literal['validation', 'assembly', 'generation', 'preview', 'delivery']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeFilters(TypedDict, total=False):
    accounts: NotRequired[builtins.list[_ExternalCoreCreativeFiltersAccountsItemVariant1 | _ExternalCoreCreativeFiltersAccountsItemVariant2]]
    statuses: NotRequired[builtins.list[Literal['processing', 'pending_review', 'approved', 'rejected', 'archived']]]
    tags: NotRequired[builtins.list[builtins.str]]
    tags_any: NotRequired[builtins.list[builtins.str]]
    name_contains: NotRequired[builtins.str]
    creative_ids: NotRequired[builtins.list[builtins.str]]
    created_after: NotRequired[builtins.str]
    created_before: NotRequired[builtins.str]
    updated_after: NotRequired[builtins.str]
    updated_before: NotRequired[builtins.str]
    assigned_to_packages: NotRequired[builtins.list[builtins.str]]
    media_buy_ids: NotRequired[builtins.list[builtins.str]]
    unassigned: NotRequired[builtins.bool]
    has_served: NotRequired[builtins.bool]
    concept_ids: NotRequired[builtins.list[builtins.str]]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    has_variables: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesRequestBaseSort(TypedDict, total=False):
    field: NotRequired[Literal['created_date', 'updated_date', 'name', 'status', 'assignment_count']]
    direction: NotRequired[Literal['asc', 'desc']]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseQuerySummary(TypedDict, total=False):
    total_matching: Required[builtins.int]
    returned: Required[builtins.int]
    filters_applied: NotRequired[builtins.list[builtins.str]]
    sort_applied: NotRequired[_ListCreativesResponseBaseQuerySummarySortApplied]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    account: NotRequired[_ExternalCoreAccount]
    name: Required[builtins.str]
    format_id: Required[_ExternalCoreFormatId]
    status: Required[Literal['processing', 'pending_review', 'approved', 'rejected', 'archived']]
    created_date: Required[builtins.str]
    updated_date: Required[builtins.str]
    assets: NotRequired[builtins.dict[builtins.str, Any]]
    tags: NotRequired[builtins.list[builtins.str]]
    concept_id: NotRequired[builtins.str]
    concept_name: NotRequired[builtins.str]
    variables: NotRequired[builtins.list[_ExternalCoreCreativeVariable]]
    assignments: NotRequired[_ListCreativesResponseBaseCreativesItemAssignments]
    snapshot: NotRequired[_ListCreativesResponseBaseCreativesItemSnapshot]
    snapshot_unavailable_reason: NotRequired[Literal['SNAPSHOT_UNSUPPORTED', 'SNAPSHOT_TEMPORARILY_UNAVAILABLE', 'SNAPSHOT_PERMISSION_DENIED']]
    items: NotRequired[builtins.list[_ListCreativesResponseBaseCreativesItemItemsItemVariant1 | _ListCreativesResponseBaseCreativesItemItemsItemVariant2]]
    pricing_options: NotRequired[builtins.list[_ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant1 | _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant2 | _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant3 | _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant4 | _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant5]]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseStatusSummary(TypedDict, total=False):
    processing: NotRequired[builtins.int]
    approved: NotRequired[builtins.int]
    pending_review: NotRequired[builtins.int]
    rejected: NotRequired[builtins.int]
    archived: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ListPropertyListsRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ListPropertyListsRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreEvent(TypedDict, total=False):
    event_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_time: Required[builtins.str]
    user_match: NotRequired[_ExternalCoreUserMatch]
    custom_data: NotRequired[_ExternalCoreEventCustomData]
    action_source: NotRequired[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_url: NotRequired[builtins.str]
    custom_event_name: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _LogEventResponseBasePartialFailuresItem(TypedDict, total=False):
    event_id: Required[builtins.str]
    code: Required[builtins.str]
    message: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['metric']]
    metric: Required[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    target_frequency: NotRequired[_PackageRequestBaseOptimizationGoalsItemVariant1TargetFrequency]
    view_duration_seconds: NotRequired[builtins.float]
    target: NotRequired[_PackageRequestBaseOptimizationGoalsItemVariant1TargetVariant1 | _PackageRequestBaseOptimizationGoalsItemVariant1TargetVariant2]
    priority: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['event']]
    event_sources: Required[builtins.list[_PackageRequestBaseOptimizationGoalsItemVariant2EventSourcesItem]]
    target: NotRequired[_PackageRequestBaseOptimizationGoalsItemVariant2TargetVariant1 | _PackageRequestBaseOptimizationGoalsItemVariant2TargetVariant2 | _PackageRequestBaseOptimizationGoalsItemVariant2TargetVariant3]
    attribution_window: NotRequired[_PackageRequestBaseOptimizationGoalsItemVariant2AttributionWindow]
    priority: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargeting(TypedDict, total=False):
    geo_countries: NotRequired[builtins.list[builtins.str]]
    geo_countries_exclude: NotRequired[builtins.list[builtins.str]]
    geo_regions: NotRequired[builtins.list[builtins.str]]
    geo_regions_exclude: NotRequired[builtins.list[builtins.str]]
    geo_metros: NotRequired[builtins.list[_ExternalCoreTargetingGeoMetrosItem]]
    geo_metros_exclude: NotRequired[builtins.list[_ExternalCoreTargetingGeoMetrosExcludeItem]]
    geo_postal_areas: NotRequired[builtins.list[_ExternalCoreTargetingGeoPostalAreasItem]]
    geo_postal_areas_exclude: NotRequired[builtins.list[_ExternalCoreTargetingGeoPostalAreasExcludeItem]]
    daypart_targets: NotRequired[builtins.list[_ExternalCoreDaypartTarget]]
    axe_include_segment: NotRequired[builtins.str]
    axe_exclude_segment: NotRequired[builtins.str]
    audience_include: NotRequired[builtins.list[builtins.str]]
    audience_exclude: NotRequired[builtins.list[builtins.str]]
    frequency_cap: NotRequired[_ExternalCoreFrequencyCap]
    property_list: NotRequired[_ExternalCorePropertyListRef]
    collection_list: NotRequired[_ExternalCoreCollectionListRef]
    collection_list_exclude: NotRequired[_ExternalCoreCollectionListRef]
    age_restriction: NotRequired[_ExternalCoreTargetingAgeRestriction]
    device_platform: NotRequired[builtins.list[Literal['ios', 'android', 'windows', 'macos', 'linux', 'chromeos', 'tvos', 'tizen', 'webos', 'fire_os', 'roku_os', 'unknown']]]
    device_type: NotRequired[builtins.list[Literal['desktop', 'mobile', 'tablet', 'ctv', 'dooh', 'unknown']]]
    device_type_exclude: NotRequired[builtins.list[Literal['desktop', 'mobile', 'tablet', 'ctv', 'dooh', 'unknown']]]
    store_catchments: NotRequired[builtins.list[_ExternalCoreTargetingStoreCatchmentsItem]]
    geo_proximity: NotRequired[builtins.list[_ExternalCoreTargetingGeoProximityItemVariant1 | _ExternalCoreTargetingGeoProximityItemVariant2 | _ExternalCoreTargetingGeoProximityItemVariant3]]
    language: NotRequired[builtins.list[builtins.str]]
    keyword_targets: NotRequired[builtins.list[_ExternalCoreTargetingKeywordTargetsItem]]
    negative_keywords: NotRequired[builtins.list[_ExternalCoreTargetingNegativeKeywordsItem]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreMeasurementTerms(TypedDict, total=False):
    billing_measurement: NotRequired[_ExternalCoreMeasurementTermsBillingMeasurement]
    makegood_policy: NotRequired[_ExternalCoreMeasurementTermsMakegoodPolicy]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePerformanceStandard(TypedDict, total=False):
    metric: Required[Literal['viewability', 'ivt', 'completion_rate', 'brand_safety', 'attention_score']]
    threshold: Required[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]
    vendor: Required[_ExternalCoreBrandRef]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeAssignment(TypedDict, total=False):
    creative_id: Required[builtins.str]
    weight: NotRequired[builtins.float]
    placement_ids: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeAsset(TypedDict, total=False):
    creative_id: Required[builtins.str]
    name: Required[builtins.str]
    format_id: Required[_ExternalCoreFormatId]
    assets: Required[builtins.dict[builtins.str, Any]]
    inputs: NotRequired[builtins.list[_ExternalCoreCreativeAssetInputsItem]]
    tags: NotRequired[builtins.list[builtins.str]]
    status: NotRequired[Literal['processing', 'pending_review', 'approved', 'rejected', 'archived']]
    weight: NotRequired[builtins.float]
    placement_ids: NotRequired[builtins.list[builtins.str]]
    industry_identifiers: NotRequired[builtins.list[_ExternalCoreIndustryIdentifier]]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeRequestBaseInputsItem(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeRequestBaseRequestsItem(TypedDict, total=False):
    format_id: NotRequired[_ExternalCoreFormatId]
    creative_manifest: Required[_ExternalCoreCreativeManifest]
    inputs: NotRequired[builtins.list[_PreviewCreativeRequestBaseRequestsItemInputsItem]]
    template_id: NotRequired[builtins.str]
    quality: NotRequired[Literal['draft', 'production']]
    output_format: NotRequired[Literal['url', 'html']]
    item_limit: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_PreviewCreativeResponseBasePreviewsItemRendersItemVariant1 | _PreviewCreativeResponseBasePreviewsItemRendersItemVariant2 | _PreviewCreativeResponseBasePreviewsItemRendersItemVariant3]]
    input: Required[_PreviewCreativeResponseBasePreviewsItemInput]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_PreviewCreativeResponseBasePreviewsItem2RendersItemVariant1 | _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant2 | _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant3]]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1(TypedDict, total=False):
    success: Required[Literal[True]]
    creative_id: Required[builtins.str]
    response: Required[_PreviewCreativeResponseBaseResultsItemVariant1Response]
    errors: NotRequired[builtins.list[_ExternalCoreError]]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2(TypedDict, total=False):
    success: Required[Literal[False]]
    creative_id: Required[builtins.str]
    response: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant2Response]
    errors: Required[builtins.list[_ExternalCoreError]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDatetimeRange(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ReportPlanOutcomeRequestBaseSellerResponse(TypedDict, total=False):
    seller_reference: NotRequired[builtins.str]
    committed_budget: NotRequired[builtins.float]
    packages: NotRequired[builtins.list[_ReportPlanOutcomeRequestBaseSellerResponsePackagesItem]]
    planned_delivery: NotRequired[_ExternalCorePlannedDelivery]
    creative_deadline: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ReportPlanOutcomeRequestBaseDelivery(TypedDict, total=False):
    reporting_period: NotRequired[_ReportPlanOutcomeRequestBaseDeliveryReportingPeriod]
    impressions: NotRequired[builtins.int]
    spend: NotRequired[builtins.float]
    cpm: NotRequired[builtins.float]
    viewability_rate: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ReportPlanOutcomeRequestBaseError(TypedDict, total=False):
    code: NotRequired[builtins.str]
    message: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ReportPlanOutcomeResponseBaseFindingsItem(TypedDict, total=False):
    category_id: Required[builtins.str]
    severity: Required[Literal['info', 'warning', 'critical']]
    explanation: Required[builtins.str]
    details: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ReportPlanOutcomeResponseBasePlanSummary(TypedDict, total=False):
    total_committed: NotRequired[builtins.float]
    budget_remaining: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ReportUsageRequestBaseUsageItem(TypedDict, total=False):
    account: Required[_ReportUsageRequestBaseUsageItemAccountVariant1 | _ReportUsageRequestBaseUsageItemAccountVariant2]
    media_buy_id: NotRequired[builtins.str]
    vendor_cost: Required[builtins.float]
    currency: Required[builtins.str]
    pricing_option_id: NotRequired[builtins.str]
    impressions: NotRequired[builtins.int]
    media_spend: NotRequired[builtins.float]
    signal_agent_segment_id: NotRequired[builtins.str]
    standards_id: NotRequired[builtins.str]
    rights_id: NotRequired[builtins.str]
    creative_id: NotRequired[builtins.str]
    property_list_id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SiGetOfferingResponseBaseOffering(TypedDict, total=False):
    offering_id: NotRequired[builtins.str]
    title: NotRequired[builtins.str]
    summary: NotRequired[builtins.str]
    tagline: NotRequired[builtins.str]
    expires_at: NotRequired[builtins.str]
    price_hint: NotRequired[builtins.str]
    image_url: NotRequired[builtins.str]
    landing_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SiGetOfferingResponseBaseMatchingProductsItem(TypedDict, total=False):
    product_id: Required[builtins.str]
    name: Required[builtins.str]
    price: NotRequired[builtins.str]
    original_price: NotRequired[builtins.str]
    image_url: NotRequired[builtins.str]
    availability_summary: NotRequired[builtins.str]
    url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiIdentity(TypedDict, total=False):
    consent_granted: Required[builtins.bool]
    consent_timestamp: NotRequired[builtins.str]
    consent_scope: NotRequired[builtins.list[Literal['name', 'email', 'shipping_address', 'phone', 'locale']]]
    privacy_policy_acknowledged: NotRequired[_ExternalSponsoredIntelligenceSiIdentityPrivacyPolicyAcknowledged]
    user: NotRequired[_ExternalSponsoredIntelligenceSiIdentityUser]
    anonymous_session_id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiCapabilities(TypedDict, total=False):
    modalities: NotRequired[_ExternalSponsoredIntelligenceSiCapabilitiesModalities]
    components: NotRequired[_ExternalSponsoredIntelligenceSiCapabilitiesComponents]
    commerce: NotRequired[_ExternalSponsoredIntelligenceSiCapabilitiesCommerce]
    a2ui: NotRequired[_ExternalSponsoredIntelligenceSiCapabilitiesA2ui]
    mcp_apps: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _SiInitiateSessionResponseBaseResponse(TypedDict, total=False):
    message: NotRequired[builtins.str]
    ui_elements: NotRequired[builtins.list[_ExternalSponsoredIntelligenceSiUiElement]]

@with_config(ConfigDict(extra="allow"))
class _SiSendMessageRequestBaseActionResponse(TypedDict, total=False):
    action: NotRequired[builtins.str]
    payload: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _SiSendMessageResponseBaseResponse(TypedDict, total=False):
    message: NotRequired[builtins.str]
    surface: NotRequired[_ExternalA2uiSurface]
    ui_elements: NotRequired[builtins.list[_ExternalSponsoredIntelligenceSiUiElement]]

@with_config(ConfigDict(extra="allow"))
class _SiSendMessageResponseBaseHandoff(TypedDict, total=False):
    type: NotRequired[Literal['transaction', 'complete']]
    intent: NotRequired[_SiSendMessageResponseBaseHandoffIntent]
    context_for_checkout: NotRequired[_SiSendMessageResponseBaseHandoffContextForCheckout]

@with_config(ConfigDict(extra="allow"))
class _SiTerminateSessionRequestBaseTerminationContext(TypedDict, total=False):
    summary: NotRequired[builtins.str]
    transaction_intent: NotRequired[_SiTerminateSessionRequestBaseTerminationContextTransactionIntent]
    cause: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SiTerminateSessionResponseBaseAcpHandoff(TypedDict, total=False):
    checkout_url: NotRequired[builtins.str]
    checkout_token: NotRequired[builtins.str]
    payload: NotRequired[builtins.dict[builtins.str, Any]]
    expires_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SiTerminateSessionResponseBaseFollowUp(TypedDict, total=False):
    action: NotRequired[Literal['save_for_later', 'set_reminder', 'subscribe_updates', 'none']]
    data: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _SyncAccountsRequestBaseAccountsItem(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    billing: Required[Literal['operator', 'agent', 'advertiser']]
    billing_entity: NotRequired[_ExternalCoreBusinessEntity]
    payment_terms: NotRequired[Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay']]
    sandbox: NotRequired[builtins.bool]
    preferred_reporting_protocol: NotRequired[Literal['s3', 'gcs', 'azure_blob']]

@with_config(ConfigDict(extra="allow"))
class _SyncAccountsResponseBaseAccountsItem(TypedDict, total=False):
    account_id: NotRequired[builtins.str]
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    name: NotRequired[builtins.str]
    action: Required[Literal['created', 'updated', 'unchanged', 'failed']]
    status: Required[Literal['active', 'pending_approval', 'rejected', 'payment_required', 'suspended', 'closed']]
    billing: NotRequired[Literal['operator', 'agent', 'advertiser']]
    billing_entity: NotRequired[_ExternalCoreBusinessEntity]
    account_scope: NotRequired[Literal['operator', 'brand', 'operator_brand', 'agent']]
    setup: NotRequired[_SyncAccountsResponseBaseAccountsItemSetup]
    rate_card: NotRequired[builtins.str]
    payment_terms: NotRequired[Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay']]
    credit_limit: NotRequired[_SyncAccountsResponseBaseAccountsItemCreditLimit]
    errors: NotRequired[builtins.list[_ExternalCoreError]]
    warnings: NotRequired[builtins.list[builtins.str]]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _SyncAudiencesRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncAudiencesRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _SyncAudiencesRequestBaseAudiencesItem(TypedDict, total=False):
    audience_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    description: NotRequired[builtins.str]
    audience_type: NotRequired[Literal['crm', 'suppression', 'lookalike_seed']]
    tags: NotRequired[builtins.list[builtins.str]]
    add: NotRequired[builtins.list[_ExternalCoreAudienceMember]]
    remove: NotRequired[builtins.list[_ExternalCoreAudienceMember]]
    delete: NotRequired[builtins.bool]
    consent_basis: NotRequired[Literal['consent', 'legitimate_interest', 'contract', 'legal_obligation']]

@with_config(ConfigDict(extra="allow"))
class _SyncAudiencesResponseBaseAudiencesItem(TypedDict, total=False):
    audience_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    seller_id: NotRequired[builtins.str]
    action: Required[Literal['created', 'updated', 'unchanged', 'deleted', 'failed']]
    status: NotRequired[Literal['processing', 'ready', 'too_small']]
    uploaded_count: NotRequired[builtins.int]
    total_uploaded_count: NotRequired[builtins.int]
    matched_count: NotRequired[builtins.int]
    effective_match_rate: NotRequired[builtins.float]
    match_breakdown: NotRequired[builtins.list[_SyncAudiencesResponseBaseAudiencesItemMatchBreakdownItem]]
    last_synced_at: NotRequired[builtins.str]
    minimum_size: NotRequired[builtins.int]
    errors: NotRequired[builtins.list[_ExternalCoreError]]

@with_config(ConfigDict(extra="allow"))
class _SyncCatalogsRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncCatalogsRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _SyncCatalogsResponseBaseCatalogsItem(TypedDict, total=False):
    catalog_id: Required[builtins.str]
    action: Required[Literal['created', 'updated', 'unchanged', 'failed', 'deleted']]
    platform_id: NotRequired[builtins.str]
    item_count: NotRequired[builtins.int]
    items_approved: NotRequired[builtins.int]
    items_pending: NotRequired[builtins.int]
    items_rejected: NotRequired[builtins.int]
    item_issues: NotRequired[builtins.list[_SyncCatalogsResponseBaseCatalogsItemItemIssuesItem]]
    last_synced_at: NotRequired[builtins.str]
    next_fetch_at: NotRequired[builtins.str]
    changes: NotRequired[builtins.list[builtins.str]]
    errors: NotRequired[builtins.list[_ExternalCoreError]]
    warnings: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _SyncCreativesRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncCreativesRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _SyncCreativesRequestBaseAssignmentsItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    package_id: Required[builtins.str]
    weight: NotRequired[builtins.float]
    placement_ids: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _SyncCreativesResponseBaseCreativesItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    account: NotRequired[_ExternalCoreAccount]
    action: Required[Literal['created', 'updated', 'unchanged', 'failed', 'deleted']]
    status: NotRequired[Literal['processing', 'pending_review', 'approved', 'rejected', 'archived']]
    platform_id: NotRequired[builtins.str]
    changes: NotRequired[builtins.list[builtins.str]]
    errors: NotRequired[builtins.list[_ExternalCoreError]]
    warnings: NotRequired[builtins.list[builtins.str]]
    preview_url: NotRequired[builtins.str]
    expires_at: NotRequired[builtins.str]
    assigned_to: NotRequired[builtins.list[builtins.str]]
    assignment_errors: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _SyncEventSourcesRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncEventSourcesRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _SyncEventSourcesRequestBaseEventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    allowed_domains: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _SyncEventSourcesResponseBaseEventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    seller_id: NotRequired[builtins.str]
    event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    action_source: NotRequired[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    managed_by: NotRequired[Literal['buyer', 'seller']]
    setup: NotRequired[_SyncEventSourcesResponseBaseEventSourcesItemSetup]
    action: Required[Literal['created', 'updated', 'unchanged', 'deleted', 'failed']]
    health: NotRequired[_ExternalCoreEventSourceHealth]
    errors: NotRequired[builtins.list[_ExternalCoreError]]

@with_config(ConfigDict(extra="allow"))
class _SyncGovernanceRequestBaseAccountsItem(TypedDict, total=False):
    account: Required[_SyncGovernanceRequestBaseAccountsItemAccountVariant1 | _SyncGovernanceRequestBaseAccountsItemAccountVariant2]
    governance_agents: Required[builtins.list[_SyncGovernanceRequestBaseAccountsItemGovernanceAgentsItem]]

@with_config(ConfigDict(extra="allow"))
class _SyncGovernanceResponseBaseAccountsItem(TypedDict, total=False):
    account: Required[_SyncGovernanceResponseBaseAccountsItemAccountVariant1 | _SyncGovernanceResponseBaseAccountsItemAccountVariant2]
    status: Required[Literal['synced', 'failed']]
    governance_agents: NotRequired[builtins.list[_SyncGovernanceResponseBaseAccountsItemGovernanceAgentsItem]]
    errors: NotRequired[builtins.list[_ExternalCoreError]]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItem(TypedDict, total=False):
    plan_id: Required[builtins.str]
    brand: Required[_ExternalCoreBrandRef]
    objectives: Required[builtins.str]
    budget: Required[_SyncPlansRequestBasePlansItemBudgetVariant1 | _SyncPlansRequestBasePlansItemBudgetVariant2]
    channels: NotRequired[_SyncPlansRequestBasePlansItemChannels]
    flight: Required[_SyncPlansRequestBasePlansItemFlight]
    countries: NotRequired[builtins.list[builtins.str]]
    regions: NotRequired[builtins.list[builtins.str]]
    policy_ids: NotRequired[builtins.list[builtins.str]]
    policy_categories: NotRequired[builtins.list[builtins.str]]
    audience: NotRequired[_ExternalGovernanceAudienceConstraints]
    restricted_attributes: NotRequired[builtins.list[Literal['racial_ethnic_origin', 'political_opinions', 'religious_beliefs', 'trade_union_membership', 'health_data', 'sex_life_sexual_orientation', 'genetic_data', 'biometric_data', 'age', 'familial_status']]]
    restricted_attributes_custom: NotRequired[builtins.list[builtins.str]]
    min_audience_size: NotRequired[builtins.int]
    human_review_required: NotRequired[builtins.bool]
    custom_policies: NotRequired[builtins.list[_ExternalGovernancePolicyEntry]]
    approved_sellers: NotRequired[builtins.list[builtins.str] | None]
    delegations: NotRequired[builtins.list[_SyncPlansRequestBasePlansItemDelegationsItem]]
    portfolio: NotRequired[_SyncPlansRequestBasePlansItemPortfolio]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansResponseBasePlansItem(TypedDict, total=False):
    plan_id: Required[builtins.str]
    status: Required[Literal['active', 'error']]
    version: Required[builtins.int]
    categories: NotRequired[builtins.list[_SyncPlansResponseBasePlansItemCategoriesItem]]
    resolved_policies: NotRequired[builtins.list[_SyncPlansResponseBasePlansItemResolvedPoliciesItem]]

@with_config(ConfigDict(extra="allow"))
class _TasksGetResponseBaseProgress(TypedDict, total=False):
    percentage: NotRequired[builtins.float]
    current_step: NotRequired[builtins.str]
    total_steps: NotRequired[builtins.int]
    step_number: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _TasksGetResponseBaseError(TypedDict, total=False):
    code: Required[builtins.str]
    message: Required[builtins.str]
    details: NotRequired[_TasksGetResponseBaseErrorDetails]

@with_config(ConfigDict(extra="allow"))
class _TasksGetResponseBaseHistoryItem(TypedDict, total=False):
    timestamp: Required[builtins.str]
    type: Required[Literal['request', 'response']]
    data: Required[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _TasksListRequestBaseFilters(TypedDict, total=False):
    protocol: NotRequired[Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence']]
    protocols: NotRequired[builtins.list[Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence']]]
    status: NotRequired[Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown']]
    statuses: NotRequired[builtins.list[Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown']]]
    task_type: NotRequired[Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights']]
    task_types: NotRequired[builtins.list[Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights']]]
    created_after: NotRequired[builtins.str]
    created_before: NotRequired[builtins.str]
    updated_after: NotRequired[builtins.str]
    updated_before: NotRequired[builtins.str]
    task_ids: NotRequired[builtins.list[builtins.str]]
    context_contains: NotRequired[builtins.str]
    has_webhook: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _TasksListRequestBaseSort(TypedDict, total=False):
    field: NotRequired[Literal['created_at', 'updated_at', 'status', 'task_type', 'protocol']]
    direction: NotRequired[Literal['asc', 'desc']]

@with_config(ConfigDict(extra="allow"))
class _TasksListRequestBasePagination(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _TasksListResponseBaseQuerySummary(TypedDict, total=False):
    total_matching: NotRequired[builtins.int]
    returned: NotRequired[builtins.int]
    domain_breakdown: NotRequired[_TasksListResponseBaseQuerySummaryDomainBreakdown]
    status_breakdown: NotRequired[builtins.dict[builtins.str, builtins.int]]
    filters_applied: NotRequired[builtins.list[builtins.str]]
    sort_applied: NotRequired[_TasksListResponseBaseQuerySummarySortApplied]

@with_config(ConfigDict(extra="allow"))
class _TasksListResponseBaseTasksItem(TypedDict, total=False):
    task_id: Required[builtins.str]
    task_type: Required[Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights']]
    domain: Required[Literal['media-buy', 'signals']]
    status: Required[Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown']]
    created_at: Required[builtins.str]
    updated_at: Required[builtins.str]
    completed_at: NotRequired[builtins.str]
    has_webhook: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _TasksListResponseBasePagination(TypedDict, total=False):
    has_more: Required[builtins.bool]
    cursor: NotRequired[builtins.str]
    total_count: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _UpdateCollectionListRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateCollectionListRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _UpdateCollectionListRequestBaseBaseCollectionsItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['distribution_ids']]
    identifiers: Required[builtins.list[_UpdateCollectionListRequestBaseBaseCollectionsItemVariant1IdentifiersItem]]

@with_config(ConfigDict(extra="allow"))
class _UpdateCollectionListRequestBaseBaseCollectionsItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_collections']]
    publisher_domain: Required[builtins.str]
    collection_ids: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _UpdateCollectionListRequestBaseBaseCollectionsItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['publisher_genres']]
    publisher_domain: Required[builtins.str]
    genres: Required[builtins.list[builtins.str]]
    genre_taxonomy: Required[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseScope(TypedDict, total=False):
    countries_all: NotRequired[builtins.list[builtins.str]]
    channels_any: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    languages_any: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant1 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2]]

@with_config(ConfigDict(extra="allow"))
class _UpdateMediaBuyRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateMediaBuyRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdate(TypedDict, total=False):
    package_id: Required[builtins.str]
    budget: NotRequired[builtins.float]
    pacing: NotRequired[Literal['even', 'asap', 'front_loaded']]
    bid_price: NotRequired[builtins.float]
    impressions: NotRequired[builtins.float]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    paused: NotRequired[builtins.bool]
    canceled: NotRequired[Literal[True]]
    cancellation_reason: NotRequired[builtins.str]
    catalogs: NotRequired[builtins.list[_ExternalCoreCatalog]]
    optimization_goals: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1 | _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2]]
    targeting_overlay: NotRequired[_ExternalCoreTargeting]
    keyword_targets_add: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateKeywordTargetsAddItem]]
    keyword_targets_remove: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateKeywordTargetsRemoveItem]]
    negative_keywords_add: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateNegativeKeywordsAddItem]]
    negative_keywords_remove: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateNegativeKeywordsRemoveItem]]
    creative_assignments: NotRequired[builtins.list[_ExternalCoreCreativeAssignment]]
    creatives: NotRequired[builtins.list[_ExternalCoreCreativeAsset]]
    context: NotRequired[builtins.dict[builtins.str, Any]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _UpdatePropertyListRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdatePropertyListRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _UpdatePropertyListRequestBaseBasePropertiesItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['publisher_tags']]
    publisher_domain: Required[builtins.str]
    tags: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _UpdatePropertyListRequestBaseBasePropertiesItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_ids']]
    publisher_domain: Required[builtins.str]
    property_ids: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _UpdatePropertyListRequestBaseBasePropertiesItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['identifiers']]
    identifiers: Required[builtins.list[_ExternalCoreIdentifier]]

@with_config(ConfigDict(extra="allow"))
class _ValidateContentDeliveryRequestBaseRecordsItem(TypedDict, total=False):
    record_id: Required[builtins.str]
    media_buy_id: NotRequired[builtins.str]
    timestamp: NotRequired[builtins.str]
    artifact: Required[_ExternalContentStandardsArtifact]
    country: NotRequired[builtins.str]
    channel: NotRequired[builtins.str]
    brand_context: NotRequired[_ValidateContentDeliveryRequestBaseRecordsItemBrandContext]

@with_config(ConfigDict(extra="allow"))
class _ValidateContentDeliveryResponseBaseSummary(TypedDict, total=False):
    total_records: Required[builtins.int]
    passed_records: Required[builtins.int]
    failed_records: Required[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ValidateContentDeliveryResponseBaseResultsItem(TypedDict, total=False):
    record_id: Required[builtins.str]
    verdict: Required[Literal['pass', 'fail']]
    features: NotRequired[builtins.list[_ValidateContentDeliveryResponseBaseResultsItemFeaturesItem]]

@with_config(ConfigDict(extra="allow"))
class _ValidatePropertyDeliveryRequestBaseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ValidatePropertyDeliveryRequestBaseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyDeliveryRecord(TypedDict, total=False):
    identifier: Required[_ExternalCoreIdentifier]
    impressions: Required[builtins.int]
    record_id: NotRequired[builtins.str]
    sales_agent_url: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ValidatePropertyDeliveryResponseBaseSummary(TypedDict, total=False):
    total_records: Required[builtins.int]
    total_impressions: Required[builtins.int]
    compliant_records: Required[builtins.int]
    compliant_impressions: Required[builtins.int]
    non_compliant_records: Required[builtins.int]
    non_compliant_impressions: Required[builtins.int]
    not_covered_records: Required[builtins.int]
    not_covered_impressions: Required[builtins.int]
    unidentified_records: Required[builtins.int]
    unidentified_impressions: Required[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ValidatePropertyDeliveryResponseBaseAggregate(TypedDict, total=False):
    score: NotRequired[builtins.float]
    grade: NotRequired[builtins.str]
    label: NotRequired[builtins.str]
    methodology_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ValidatePropertyDeliveryResponseBaseAuthorizationSummary(TypedDict, total=False):
    records_checked: Required[builtins.int]
    impressions_checked: Required[builtins.int]
    authorized_records: Required[builtins.int]
    authorized_impressions: Required[builtins.int]
    unauthorized_records: Required[builtins.int]
    unauthorized_impressions: Required[builtins.int]
    unknown_records: Required[builtins.int]
    unknown_impressions: Required[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyValidationResult(TypedDict, total=False):
    identifier: Required[_ExternalCoreIdentifier]
    record_id: NotRequired[builtins.str]
    status: Required[Literal['compliant', 'non_compliant', 'not_covered', 'unidentified']]
    impressions: Required[builtins.int]
    features: NotRequired[builtins.list[_ExternalPropertyValidationResultFeaturesItem]]
    authorization: NotRequired[_ExternalPropertyAuthorizationResult]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreBrandRefDataSubjectContestation(TypedDict, total=False):
    url: NotRequired[builtins.str]
    email: NotRequired[builtins.str]
    languages: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePushNotificationConfigAuthentication(TypedDict, total=False):
    schemes: Required[builtins.list[Literal['Bearer', 'HMAC-SHA256']]]
    credentials: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalBrandRightsTermsExclusivity(TypedDict, total=False):
    scope: NotRequired[builtins.str]
    countries: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRightsConstraintRightsAgent(TypedDict, total=False):
    url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreErrorIssuesItem(TypedDict, total=False):
    pointer: Required[builtins.str]
    message: Required[builtins.str]
    keyword: Required[builtins.str]
    schemaPath: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalResponseBaseDeploymentsItemVariant1ActivationKeyVariant1(TypedDict, total=False):
    type: Required[Literal['segment_id']]
    segment_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalResponseBaseDeploymentsItemVariant1ActivationKeyVariant2(TypedDict, total=False):
    type: Required[Literal['key_value']]
    key: Required[builtins.str]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalResponseBaseDeploymentsItemVariant2ActivationKeyVariant1(TypedDict, total=False):
    type: Required[Literal['segment_id']]
    segment_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ActivateSignalResponseBaseDeploymentsItemVariant2ActivationKeyVariant2(TypedDict, total=False):
    type: Required[Literal['key_value']]
    key: Required[builtins.str]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreIndustryIdentifier(TypedDict, total=False):
    type: Required[Literal['ad_id', 'isci', 'clearcast_clock']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProvenance(TypedDict, total=False):
    digital_source_type: NotRequired[Literal['digital_capture', 'digital_creation', 'trained_algorithmic_media', 'composite_with_trained_algorithmic_media', 'algorithmic_media', 'composite_capture', 'composite_synthetic', 'human_edits', 'data_driven_media']]
    ai_tool: NotRequired[_ExternalCoreProvenanceAiTool]
    human_oversight: NotRequired[Literal['none', 'prompt_only', 'selected', 'edited', 'directed']]
    declared_by: NotRequired[_ExternalCoreProvenanceDeclaredBy]
    declared_at: NotRequired[builtins.str]
    created_time: NotRequired[builtins.str]
    c2pa: NotRequired[_ExternalCoreProvenanceC2pa]
    disclosure: NotRequired[_ExternalCoreProvenanceDisclosure]
    verification: NotRequired[builtins.list[_ExternalCoreProvenanceVerificationItem]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant1 | _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant2 | _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant3]]
    input: Required[_BuildCreativeResponseBasePreviewPreviewsItemInput]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    format_id: Required[_ExternalCoreFormatId]
    renders: Required[builtins.list[_BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant1 | _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant2 | _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant3]]
    input: Required[_BuildCreativeResponseBasePreview2PreviewsItemInput]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant1(TypedDict, total=False):
    type: Required[Literal['text']]
    role: NotRequired[Literal['title', 'paragraph', 'heading', 'caption', 'quote', 'list_item', 'description']]
    content: Required[builtins.str]
    content_format: NotRequired[Literal['text/plain', 'text/markdown', 'text/html', 'application/json']]
    language: NotRequired[builtins.str]
    heading_level: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant2(TypedDict, total=False):
    type: Required[Literal['image']]
    url: Required[builtins.str]
    access: NotRequired[_ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant1 | _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant2 | _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant3]
    alt_text: NotRequired[builtins.str]
    caption: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant3(TypedDict, total=False):
    type: Required[Literal['video']]
    url: Required[builtins.str]
    access: NotRequired[_ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant1 | _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant2 | _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'subtitles', 'closed_captions', 'dub', 'generated']]
    thumbnail_url: NotRequired[builtins.str]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant4(TypedDict, total=False):
    type: Required[Literal['audio']]
    url: Required[builtins.str]
    access: NotRequired[_ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant1 | _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant2 | _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'closed_captions', 'generated']]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactMetadata(TypedDict, total=False):
    canonical: NotRequired[builtins.str]
    author: NotRequired[builtins.str]
    keywords: NotRequired[builtins.str]
    open_graph: NotRequired[builtins.dict[builtins.str, Any]]
    twitter_card: NotRequired[builtins.dict[builtins.str, Any]]
    json_ld: NotRequired[builtins.list[builtins.dict[builtins.str, Any]]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactIdentifiers(TypedDict, total=False):
    apple_podcast_id: NotRequired[builtins.str]
    spotify_collection_id: NotRequired[builtins.str]
    podcast_guid: NotRequired[builtins.str]
    youtube_video_id: NotRequired[builtins.str]
    rss_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryGeo(TypedDict, total=False):
    countries: NotRequired[builtins.list[builtins.str]]
    regions: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFrequencyCap(TypedDict, total=False):
    suppress: NotRequired[_ExternalCoreFrequencyCapSuppress]
    suppress_minutes: NotRequired[builtins.float]
    max_impressions: NotRequired[builtins.int]
    per: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    window: NotRequired[_ExternalCoreFrequencyCapWindow]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant1(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalCorePlannedDeliveryAudienceTargetingItemVariant1SignalIdVariant1 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant1SignalIdVariant2]
    value_type: Required[Literal['binary']]
    value: Required[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalCorePlannedDeliveryAudienceTargetingItemVariant2SignalIdVariant1 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2SignalIdVariant2]
    value_type: Required[Literal['categorical']]
    values: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalCorePlannedDeliveryAudienceTargetingItemVariant3SignalIdVariant1 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3SignalIdVariant2]
    value_type: Required[Literal['numeric']]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant4(TypedDict, total=False):
    type: Required[Literal['description']]
    description: Required[builtins.str]
    category: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CheckGovernanceRequestBaseDeliveryMetricsReportingPeriod(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CheckGovernanceRequestBaseDeliveryMetricsAudienceDistribution(TypedDict, total=False):
    baseline: Required[Literal['census', 'platform', 'custom']]
    baseline_description: NotRequired[builtins.str]
    indices: Required[builtins.dict[builtins.str, builtins.float]]
    cumulative_indices: NotRequired[builtins.dict[builtins.str, builtins.float]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreBusinessEntityAddress(TypedDict, total=False):
    street: Required[builtins.str]
    city: Required[builtins.str]
    postal_code: Required[builtins.str]
    region: NotRequired[builtins.str]
    country: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreBusinessEntityContactsItem(TypedDict, total=False):
    role: Required[Literal['billing', 'legal', 'creative', 'general']]
    name: NotRequired[builtins.str]
    email: NotRequired[builtins.str]
    phone: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreBusinessEntityBank(TypedDict, total=False):
    account_holder: Required[builtins.str]
    iban: NotRequired[builtins.str]
    bic: NotRequired[builtins.str]
    routing_number: NotRequired[builtins.str]
    account_number: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ComplyTestControllerRequestBaseParamsReportedSpend(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ContextMatchRequestBaseGeoMetro(TypedDict, total=False):
    system: Required[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreSellerAgentRef(TypedDict, total=False):
    agent_url: Required[builtins.str]
    id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalTmpOfferPrice(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: NotRequired[builtins.str]
    model: Required[Literal['cpm', 'cpc', 'cpcv', 'cpa', 'flat']]

@with_config(ConfigDict(extra="allow"))
class _ContextMatchResponseBaseSignalsTargetingKvsItem(TypedDict, total=False):
    key: Required[builtins.str]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateCollectionListRequestBaseBaseCollectionsItemVariant1IdentifiersItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreContentRating(TypedDict, total=False):
    system: Required[Literal['tv_parental', 'mpaa', 'podcast', 'esrb', 'bbfc', 'fsk', 'acb', 'chvrs', 'csa', 'pegi', 'custom']]
    rating: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCollectionCollectionListFiltersExcludeDistributionIdsItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCollectionCollectionListAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCollectionCollectionListAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCollectionCollectionListBaseCollectionsItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['distribution_ids']]
    identifiers: Required[builtins.list[_ExternalCollectionCollectionListBaseCollectionsItemVariant1IdentifiersItem]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCollectionCollectionListBaseCollectionsItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_collections']]
    publisher_domain: Required[builtins.str]
    collection_ids: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCollectionCollectionListBaseCollectionsItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['publisher_genres']]
    publisher_domain: Required[builtins.str]
    genres: Required[builtins.list[builtins.str]]
    genre_taxonomy: Required[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernancePolicyEntryExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_Exemplar]]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant1(TypedDict, total=False):
    type: Required[Literal['url']]
    value: Required[builtins.str]
    language: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2(TypedDict, total=False):
    property_rid: Required[builtins.str]
    artifact_id: Required[builtins.str]
    variant_id: NotRequired[builtins.str]
    format_id: NotRequired[_ExternalCoreFormatId]
    url: NotRequired[builtins.str]
    published_time: NotRequired[builtins.str]
    last_update_time: NotRequired[builtins.str]
    assets: Required[builtins.list[_CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant1 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4]]
    metadata: NotRequired[_CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2Metadata]
    provenance: NotRequired[_ExternalCoreProvenance]
    identifiers: NotRequired[_CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2Identifiers]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['metric']]
    metric: Required[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    target_frequency: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetFrequency]
    view_duration_seconds: NotRequired[builtins.float]
    target: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetVariant1 | _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetVariant2]
    priority: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['event']]
    event_sources: Required[builtins.list[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2EventSourcesItem]]
    target: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant1 | _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant2 | _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant3]
    attribution_window: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindow]
    priority: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreReportingWebhookAuthentication(TypedDict, total=False):
    schemes: Required[builtins.list[Literal['Bearer', 'HMAC-SHA256']]]
    credentials: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateMediaBuyRequestBaseArtifactWebhookAuthentication(TypedDict, total=False):
    schemes: Required[builtins.list[Literal['Bearer', 'HMAC-SHA256']]]
    credentials: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAccountCreditLimit(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAccountSetup(TypedDict, total=False):
    url: NotRequired[builtins.str]
    message: Required[builtins.str]
    expires_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAccountGovernanceAgentsItem(TypedDict, total=False):
    url: Required[builtins.str]
    categories: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAccountReportingBucket(TypedDict, total=False):
    protocol: Required[Literal['s3', 'gcs', 'azure_blob']]
    bucket: Required[builtins.str]
    prefix: NotRequired[builtins.str]
    region: NotRequired[builtins.str]
    format: NotRequired[Literal['jsonl', 'csv', 'parquet', 'avro', 'orc']]
    compression: NotRequired[Literal['gzip', 'none']]
    file_retention_days: Required[builtins.int]
    setup_instructions: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalPricingOptionsPriceBreakdown(TypedDict, total=False):
    list_price: Required[builtins.float]
    adjustments: Required[builtins.list[_ExternalPricingOptionsPriceBreakdownAdjustmentsItemVariant1 | _ExternalPricingOptionsPriceBreakdownAdjustmentsItemVariant2]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['metric']]
    metric: Required[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    target_frequency: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant1TargetFrequency]
    view_duration_seconds: NotRequired[builtins.float]
    target: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant1TargetVariant1 | _ExternalCorePackageOptimizationGoalsItemVariant1TargetVariant2]
    priority: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['event']]
    event_sources: Required[builtins.list[_ExternalCorePackageOptimizationGoalsItemVariant2EventSourcesItem]]
    target: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant1 | _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant2 | _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant3]
    attribution_window: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindow]
    priority: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageCancellation(TypedDict, total=False):
    canceled_at: Required[builtins.str]
    canceled_by: Required[Literal['buyer', 'seller']]
    reason: NotRequired[builtins.str]
    acknowledged_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFeatureRequirement(TypedDict, total=False):
    feature_id: Required[builtins.str]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]
    allowed_values: NotRequired[builtins.list[Any]]
    if_not_covered: NotRequired[Literal['exclude', 'include']]
    policy_id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListBasePropertiesItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['publisher_tags']]
    publisher_domain: Required[builtins.str]
    tags: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListBasePropertiesItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_ids']]
    publisher_domain: Required[builtins.str]
    property_ids: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListBasePropertiesItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['identifiers']]
    identifiers: Required[builtins.list[_ExternalCoreIdentifier]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_ExternalPropertyPropertyListPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetAccountFinancialsResponseBaseBalanceLastTopUp(TypedDict, total=False):
    amount: Required[builtins.float]
    date: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseAdcpIdempotencyVariant1(TypedDict, total=False):
    supported: Required[Literal[True]]
    replay_ttl_seconds: Required[builtins.int]
    account_id_is_opaque: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseAdcpIdempotencyVariant2(TypedDict, total=False):
    supported: Required[Literal[False]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreMediaBuyFeatures(TypedDict, total=False):
    inline_creative_management: NotRequired[builtins.bool]
    property_list_filtering: NotRequired[builtins.bool]
    catalog_management: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecution(TypedDict, total=False):
    trusted_match: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTrustedMatch]
    axe_integrations: NotRequired[builtins.list[builtins.str]]
    creative_specs: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecutionCreativeSpecs]
    targeting: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargeting]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyAudienceTargeting(TypedDict, total=False):
    supported_identifier_types: Required[builtins.list[Literal['hashed_email', 'hashed_phone']]]
    supports_platform_customer_id: NotRequired[builtins.bool]
    supported_uid_types: NotRequired[builtins.list[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]]
    minimum_audience_size: Required[builtins.int]
    matching_latency_hours: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyAudienceTargetingMatchingLatencyHours]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyConversionTracking(TypedDict, total=False):
    multi_source_event_dedup: NotRequired[builtins.bool]
    supported_event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    supported_uid_types: NotRequired[builtins.list[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]]
    supported_hashed_identifiers: NotRequired[builtins.list[Literal['hashed_email', 'hashed_phone']]]
    supported_action_sources: NotRequired[builtins.list[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]]
    attribution_windows: NotRequired[builtins.list[_GetAdcpCapabilitiesResponseBaseMediaBuyConversionTrackingAttributionWindowsItem]]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyContentStandards(TypedDict, total=False):
    supports_local_evaluation: NotRequired[builtins.bool]
    supported_channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    supports_webhook_delivery: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyPortfolio(TypedDict, total=False):
    publisher_domains: Required[builtins.list[builtins.str]]
    primary_channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    primary_countries: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]
    advertising_policies: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseSignalsFeatures(TypedDict, total=False):
    catalog_signals: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseGovernancePropertyFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    type: Required[Literal['binary', 'quantitative', 'categorical']]
    range: NotRequired[_GetAdcpCapabilitiesResponseBaseGovernancePropertyFeaturesItemRange]
    categories: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]
    methodology_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseGovernanceCreativeFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    type: Required[Literal['binary', 'quantitative', 'categorical']]
    range: NotRequired[_GetAdcpCapabilitiesResponseBaseGovernanceCreativeFeaturesItemRange]
    categories: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]
    methodology_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseSponsoredIntelligenceEndpoint(TypedDict, total=False):
    transports: Required[builtins.list[_GetAdcpCapabilitiesResponseBaseSponsoredIntelligenceEndpointTransportsItem]]
    preferred: NotRequired[Literal['mcp', 'a2a']]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseIdentityKeyOrigins(TypedDict, total=False):
    governance_signing: NotRequired[builtins.str]
    request_signing: NotRequired[builtins.str]
    webhook_signing: NotRequired[builtins.str]
    tmp_signing: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseIdentityCompromiseNotification(TypedDict, total=False):
    emits: NotRequired[builtins.bool]
    accepts: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _FontRoleVariant2(TypedDict, total=False):
    family: Required[builtins.str]
    files: NotRequired[builtins.list[_FontRoleVariant2FilesItem]]
    opentype_features: NotRequired[builtins.list[builtins.str]]
    fallbacks: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _GetCollectionListResponseBaseCollectionsItemDistributionIdsItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetContentStandardsResponseBasePricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDeliveryMetrics(TypedDict, total=False):
    impressions: NotRequired[builtins.float]
    spend: NotRequired[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_ExternalCoreDeliveryMetricsByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_ExternalCoreDeliveryMetricsQuartileData]
    dooh_metrics: NotRequired[_ExternalCoreDeliveryMetricsDoohMetrics]
    viewability: NotRequired[_ExternalCoreDeliveryMetricsViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_ExternalCoreDeliveryMetricsByActionSourceItem]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariant(TypedDict, total=False):
    impressions: NotRequired[builtins.float]
    spend: NotRequired[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_ExternalCoreCreativeVariantByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_ExternalCoreCreativeVariantQuartileData]
    dooh_metrics: NotRequired[_ExternalCoreCreativeVariantDoohMetrics]
    viewability: NotRequired[_ExternalCoreCreativeVariantViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_ExternalCoreCreativeVariantByActionSourceItem]]
    variant_id: Required[builtins.str]
    manifest: NotRequired[_ExternalCoreCreativeManifest]
    generation_context: NotRequired[_ExternalCoreCreativeVariantGenerationContext]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyArtifactsResponseBaseArtifactsItemBrandContext(TypedDict, total=False):
    brand_id: NotRequired[builtins.str]
    sku_id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseAttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseAttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseReportingDimensionsGeo(TypedDict, total=False):
    geo_level: Required[Literal['country', 'region', 'metro', 'postal_area']]
    system: NotRequired[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom', 'us_zip', 'us_zip_plus_four', 'gb_outward', 'gb_full', 'ca_fsa', 'ca_full', 'de_plz', 'fr_code_postal', 'au_postcode', 'ch_plz', 'at_plz']]
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseReportingDimensionsDeviceType(TypedDict, total=False):
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseReportingDimensionsDevicePlatform(TypedDict, total=False):
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseReportingDimensionsAudience(TypedDict, total=False):
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryRequestBaseReportingDimensionsPlacement(TypedDict, total=False):
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotals(TypedDict, total=False):
    impressions: NotRequired[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsByActionSourceItem]]
    effective_rate: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItem(TypedDict, total=False):
    impressions: NotRequired[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByActionSourceItem]]
    package_id: Required[builtins.str]
    pacing_index: NotRequired[builtins.float]
    pricing_model: Required[Literal['cpm', 'vcpm', 'cpc', 'cpcv', 'cpv', 'cpp', 'cpa', 'flat_rate', 'time']]
    rate: Required[builtins.float]
    currency: Required[builtins.str]
    delivery_status: NotRequired[Literal['delivering', 'completed', 'budget_exhausted', 'flight_ended', 'goal_met']]
    paused: NotRequired[builtins.bool]
    is_final: NotRequired[builtins.bool]
    measurement_window: NotRequired[builtins.str]
    supersedes_window: NotRequired[builtins.str]
    by_catalog_item: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItem]]
    by_creative: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItem]]
    by_keyword: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItem]]
    by_geo: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItem]]
    by_geo_truncated: NotRequired[builtins.bool]
    by_device_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItem]]
    by_device_type_truncated: NotRequired[builtins.bool]
    by_device_platform: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItem]]
    by_device_platform_truncated: NotRequired[builtins.bool]
    by_audience: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItem]]
    by_audience_truncated: NotRequired[builtins.bool]
    by_placement: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItem]]
    by_placement_truncated: NotRequired[builtins.bool]
    daily_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemDailyBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemDailyBreakdownItem(TypedDict, total=False):
    date: Required[builtins.str]
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuysResponseBaseMediaBuysItemCancellation(TypedDict, total=False):
    canceled_at: Required[builtins.str]
    canceled_by: Required[Literal['buyer', 'seller']]
    reason: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuysResponseBaseMediaBuysItemHistoryItem(TypedDict, total=False):
    revision: Required[builtins.int]
    timestamp: Required[builtins.str]
    actor: NotRequired[builtins.str]
    action: Required[builtins.str]
    summary: NotRequired[builtins.str]
    package_id: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuysResponseBaseMediaBuysItemPackagesItem(TypedDict, total=False):
    package_id: Required[builtins.str]
    product_id: NotRequired[builtins.str]
    budget: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]
    bid_price: NotRequired[builtins.float]
    impressions: NotRequired[builtins.float]
    targeting_overlay: NotRequired[_ExternalCoreTargeting]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    paused: NotRequired[builtins.bool]
    canceled: NotRequired[builtins.bool]
    cancellation: NotRequired[_GetMediaBuysResponseBaseMediaBuysItemPackagesItemCancellation]
    creative_deadline: NotRequired[builtins.str]
    creative_approvals: NotRequired[builtins.list[_GetMediaBuysResponseBaseMediaBuysItemPackagesItemCreativeApprovalsItem]]
    format_ids_pending: NotRequired[builtins.list[_ExternalCoreFormatId]]
    snapshot_unavailable_reason: NotRequired[Literal['SNAPSHOT_UNSUPPORTED', 'SNAPSHOT_TEMPORARILY_UNAVAILABLE', 'SNAPSHOT_PERMISSION_DENIED']]
    snapshot: NotRequired[_GetMediaBuysResponseBaseMediaBuysItemPackagesItemSnapshot]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemBudget(TypedDict, total=False):
    authorized: NotRequired[builtins.float]
    committed: NotRequired[builtins.float]
    remaining: NotRequired[builtins.float]
    utilization_pct: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemChannelAllocationValue(TypedDict, total=False):
    committed: NotRequired[builtins.float]
    pct: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemSummary(TypedDict, total=False):
    checks_performed: NotRequired[builtins.int]
    outcomes_reported: NotRequired[builtins.int]
    statuses: NotRequired[_GetPlanAuditLogsResponseBasePlansItemSummaryStatuses]
    findings_count: NotRequired[builtins.int]
    escalations: NotRequired[builtins.list[_GetPlanAuditLogsResponseBasePlansItemSummaryEscalationsItem]]
    drift_metrics: NotRequired[_GetPlanAuditLogsResponseBasePlansItemSummaryDriftMetrics]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemEntriesItem(TypedDict, total=False):
    id: Required[builtins.str]
    type: Required[Literal['check', 'outcome']]
    timestamp: Required[builtins.str]
    plan_id: NotRequired[builtins.str]
    caller: NotRequired[builtins.str]
    tool: NotRequired[builtins.str]
    status: NotRequired[Literal['approved', 'denied', 'conditions']]
    check_type: NotRequired[Literal['intent', 'execution']]
    mode: NotRequired[Literal['audit', 'advisory', 'enforce']]
    explanation: NotRequired[builtins.str]
    policies_evaluated: NotRequired[builtins.list[builtins.str]]
    categories_evaluated: NotRequired[builtins.list[builtins.str]]
    findings: NotRequired[builtins.list[_GetPlanAuditLogsResponseBasePlansItemEntriesItemFindingsItem]]
    outcome: NotRequired[Literal['completed', 'failed', 'delivery']]
    committed_budget: NotRequired[builtins.float]
    governance_context: NotRequired[builtins.str]
    plan_hash: NotRequired[builtins.str]
    purchase_type: NotRequired[Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']]
    outcome_status: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemGovernedActionsItem(TypedDict, total=False):
    governance_context: Required[builtins.str]
    purchase_type: Required[Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']]
    status: Required[Literal['active', 'suspended', 'completed']]
    committed: Required[builtins.float]
    check_count: Required[builtins.int]
    seller_reference: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPublisherPropertiesItemVariant1(TypedDict, total=False):
    publisher_domain: Required[builtins.str]
    selection_type: Required[Literal['all']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPublisherPropertiesItemVariant2(TypedDict, total=False):
    publisher_domain: Required[builtins.str]
    selection_type: Required[Literal['by_id']]
    property_ids: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPublisherPropertiesItemVariant3(TypedDict, total=False):
    publisher_domain: Required[builtins.str]
    selection_type: Required[Literal['by_tag']]
    property_tags: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlacement(TypedDict, total=False):
    placement_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    tags: NotRequired[builtins.list[builtins.str]]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpm']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['vcpm']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpc']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpcv']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpv']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    parameters: Required[_ExternalCoreProductPricingOptionsItemVariant5Parameters]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant6(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpp']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    parameters: Required[_ExternalCoreProductPricingOptionsItemVariant6Parameters]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant7(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpa']]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    event_source_id: NotRequired[builtins.str]
    currency: Required[builtins.str]
    fixed_price: Required[builtins.float]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant8(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['flat_rate']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    parameters: NotRequired[_ExternalCoreProductPricingOptionsItemVariant8Parameters]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant9(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['time']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    parameters: Required[_ExternalCoreProductPricingOptionsItemVariant9Parameters]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDeliveryForecast(TypedDict, total=False):
    points: Required[builtins.list[_ExternalCoreForecastPoint]]
    forecast_range_unit: NotRequired[Literal['spend', 'availability', 'reach_freq', 'weekly', 'daily', 'clicks', 'conversions', 'package']]
    method: Required[Literal['estimate', 'modeled', 'guaranteed']]
    currency: Required[builtins.str]
    demographic_system: NotRequired[Literal['nielsen', 'barb', 'agf', 'oztam', 'mediametrie', 'custom']]
    demographic: NotRequired[builtins.str]
    measurement_source: NotRequired[builtins.str]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    generated_at: NotRequired[builtins.str]
    valid_until: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreOutcomeMeasurement(TypedDict, total=False):
    type: Required[builtins.str]
    attribution: Required[builtins.str]
    window: NotRequired[_ExternalCoreOutcomeMeasurementWindow]
    reporting: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductDeliveryMeasurement(TypedDict, total=False):
    provider: Required[builtins.str]
    notes: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCancellationPolicy(TypedDict, total=False):
    notice_period: Required[_ExternalCoreDuration]
    cancellation_fee: Required[_ExternalCoreCancellationPolicyCancellationFee]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreReportingCapabilities(TypedDict, total=False):
    available_reporting_frequencies: Required[builtins.list[Literal['hourly', 'daily', 'monthly']]]
    expected_delay_minutes: Required[builtins.int]
    timezone: Required[builtins.str]
    supports_webhooks: Required[builtins.bool]
    available_metrics: Required[builtins.list[Literal['impressions', 'spend', 'clicks', 'ctr', 'video_completions', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'viewability', 'engagement_rate', 'views', 'completed_views', 'leads', 'reach', 'frequency', 'grps', 'quartile_data', 'dooh_metrics', 'cost_per_click']]]
    supports_creative_breakdown: NotRequired[builtins.bool]
    supports_keyword_breakdown: NotRequired[builtins.bool]
    supports_geo_breakdown: NotRequired[_ExternalCoreGeoBreakdownSupport]
    supports_device_type_breakdown: NotRequired[builtins.bool]
    supports_device_platform_breakdown: NotRequired[builtins.bool]
    supports_audience_breakdown: NotRequired[builtins.bool]
    supports_placement_breakdown: NotRequired[builtins.bool]
    date_range_support: Required[Literal['date_range', 'lifetime_only']]
    measurement_windows: NotRequired[builtins.list[_ExternalCoreMeasurementWindow]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativePolicy(TypedDict, total=False):
    co_branding: Required[Literal['required', 'optional', 'none']]
    landing_page: Required[Literal['any', 'retailer_site_only', 'must_include_retailer']]
    templates_available: Required[builtins.bool]
    provenance_required: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductDataProviderSignalsItemVariant1(TypedDict, total=False):
    data_provider_domain: Required[builtins.str]
    selection_type: Required[Literal['all']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductDataProviderSignalsItemVariant2(TypedDict, total=False):
    data_provider_domain: Required[builtins.str]
    selection_type: Required[Literal['by_id']]
    signal_ids: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductDataProviderSignalsItemVariant3(TypedDict, total=False):
    data_provider_domain: Required[builtins.str]
    selection_type: Required[Literal['by_tag']]
    signal_tags: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductMetricOptimization(TypedDict, total=False):
    supported_metrics: Required[builtins.list[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]]
    supported_reach_units: NotRequired[builtins.list[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]]
    supported_view_durations: NotRequired[builtins.list[builtins.float]]
    supported_targets: NotRequired[builtins.list[Literal['cost_per', 'threshold_rate']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreMeasurementReadiness(TypedDict, total=False):
    status: Required[Literal['insufficient', 'minimum', 'good', 'excellent']]
    required_event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    missing_event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    issues: NotRequired[builtins.list[_ExternalCoreDiagnosticIssue]]
    notes: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductConversionTracking(TypedDict, total=False):
    action_sources: NotRequired[builtins.list[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]]
    supported_targets: NotRequired[builtins.list[Literal['cost_per', 'per_ad_spend', 'maximize_value']]]
    platform_managed: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductCatalogMatch(TypedDict, total=False):
    matched_gtins: NotRequired[builtins.list[builtins.str]]
    matched_ids: NotRequired[builtins.list[builtins.str]]
    matched_count: NotRequired[builtins.int]
    submitted_count: Required[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductProductCard(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    manifest: Required[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductProductCardDetailed(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    manifest: Required[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCollectionSelector(TypedDict, total=False):
    publisher_domain: Required[builtins.str]
    collection_ids: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreInstallment(TypedDict, total=False):
    installment_id: Required[builtins.str]
    collection_id: NotRequired[builtins.str]
    name: NotRequired[builtins.str]
    season: NotRequired[builtins.str]
    installment_number: NotRequired[builtins.str]
    scheduled_at: NotRequired[builtins.str]
    status: NotRequired[Literal['scheduled', 'tentative', 'live', 'postponed', 'cancelled', 'aired', 'published']]
    duration_seconds: NotRequired[builtins.int]
    flexible_end: NotRequired[builtins.bool]
    valid_until: NotRequired[builtins.str]
    content_rating: NotRequired[_ExternalCoreContentRating]
    topics: NotRequired[builtins.list[builtins.str]]
    special: NotRequired[_ExternalCoreSpecial]
    guest_talent: NotRequired[builtins.list[_ExternalCoreTalent]]
    ad_inventory: NotRequired[_ExternalCoreAdInventoryConfig]
    deadlines: NotRequired[_ExternalCoreInstallmentDeadlines]
    derivative_of: NotRequired[_ExternalCoreInstallmentDerivativeOf]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductTrustedMatch(TypedDict, total=False):
    context_match: Required[builtins.bool]
    identity_match: NotRequired[builtins.bool]
    response_types: NotRequired[builtins.list[Literal['activation', 'catalog_items', 'creative', 'deal']]]
    dynamic_brands: NotRequired[builtins.bool]
    providers: NotRequired[builtins.list[_ExternalCoreProductTrustedMatchProvidersItem]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductMaterialSubmission(TypedDict, total=False):
    url: NotRequired[builtins.str]
    email: NotRequired[builtins.str]
    instructions: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCatalogFieldMapping(TypedDict, total=False):
    feed_field: NotRequired[builtins.str]
    catalog_field: NotRequired[builtins.str]
    asset_group_id: NotRequired[builtins.str]
    value: NotRequired[Any]
    transform: NotRequired[Literal['date', 'divide', 'boolean', 'split']]
    format: NotRequired[builtins.str]
    timezone: NotRequired[builtins.str]
    by: NotRequired[builtins.float]
    separator: NotRequired[builtins.str]
    default: NotRequired[Any]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersBudgetRange(TypedDict, total=False):
    min: NotRequired[builtins.float]
    max: NotRequired[builtins.float]
    currency: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersMetrosItem(TypedDict, total=False):
    system: Required[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom']]
    code: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersTrustedMatch(TypedDict, total=False):
    providers: NotRequired[builtins.list[_ExternalCoreProductFiltersTrustedMatchProvidersItem]]
    response_types: NotRequired[builtins.list[Literal['activation', 'catalog_items', 'creative', 'deal']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersRequiredGeoTargetingItem(TypedDict, total=False):
    level: Required[Literal['country', 'region', 'metro', 'postal_area']]
    system: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersSignalTargetingItemVariant1(TypedDict, total=False):
    signal_id: Required[_ExternalCoreProductFiltersSignalTargetingItemVariant1SignalIdVariant1 | _ExternalCoreProductFiltersSignalTargetingItemVariant1SignalIdVariant2]
    value_type: Required[Literal['binary']]
    value: Required[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersSignalTargetingItemVariant2(TypedDict, total=False):
    signal_id: Required[_ExternalCoreProductFiltersSignalTargetingItemVariant2SignalIdVariant1 | _ExternalCoreProductFiltersSignalTargetingItemVariant2SignalIdVariant2]
    value_type: Required[Literal['categorical']]
    values: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersSignalTargetingItemVariant3(TypedDict, total=False):
    signal_id: Required[_ExternalCoreProductFiltersSignalTargetingItemVariant3SignalIdVariant1 | _ExternalCoreProductFiltersSignalTargetingItemVariant3SignalIdVariant2]
    value_type: Required[Literal['numeric']]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersPostalAreasItem(TypedDict, total=False):
    system: Required[Literal['us_zip', 'us_zip_plus_four', 'gb_outward', 'gb_full', 'ca_fsa', 'ca_full', 'de_plz', 'fr_code_postal', 'au_postcode', 'ch_plz', 'at_plz']]
    values: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant1(TypedDict, total=False):
    lat: Required[builtins.float]
    lng: Required[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: Required[_ExternalCoreProductFiltersGeoProximityItemVariant1TravelTime]
    transport_mode: Required[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant1Radius]
    geometry: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant1Geometry]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant2(TypedDict, total=False):
    lat: Required[builtins.float]
    lng: Required[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant2TravelTime]
    transport_mode: NotRequired[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: Required[_ExternalCoreProductFiltersGeoProximityItemVariant2Radius]
    geometry: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant2Geometry]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant3(TypedDict, total=False):
    lat: NotRequired[builtins.float]
    lng: NotRequired[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant3TravelTime]
    transport_mode: NotRequired[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant3Radius]
    geometry: Required[_ExternalCoreProductFiltersGeoProximityItemVariant3Geometry]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersKeywordsItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: NotRequired[Literal['broad', 'phrase', 'exact']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductAllocation(TypedDict, total=False):
    product_id: Required[builtins.str]
    allocation_percentage: Required[builtins.float]
    pricing_option_id: NotRequired[builtins.str]
    rationale: NotRequired[builtins.str]
    sequence: NotRequired[builtins.int]
    tags: NotRequired[builtins.list[builtins.str]]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    daypart_targets: NotRequired[builtins.list[_ExternalCoreDaypartTarget]]
    forecast: NotRequired[_ExternalCoreDeliveryForecast]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreInsertionOrder(TypedDict, total=False):
    io_id: Required[builtins.str]
    terms: NotRequired[_ExternalCoreInsertionOrderTerms]
    terms_url: NotRequired[builtins.str]
    signing_url: NotRequired[builtins.str]
    requires_signature: Required[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProposalTotalBudgetGuidance(TypedDict, total=False):
    min: NotRequired[builtins.float]
    recommended: NotRequired[builtins.float]
    max: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetProductsResponseBaseIncompleteItemEstimatedWait(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _GetRightsResponseBaseRightsItemExclusivityStatus(TypedDict, total=False):
    available: NotRequired[builtins.bool]
    existing_exclusives: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalBrandRightsPricingOption(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm', 'vcpm', 'cpc', 'cpcv', 'cpv', 'cpp', 'cpa', 'flat_rate', 'time']]
    price: Required[builtins.float]
    currency: Required[builtins.str]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    period: NotRequired[Literal['daily', 'weekly', 'monthly', 'quarterly', 'annual', 'one_time']]
    impression_cap: NotRequired[builtins.int]
    overage_cpm: NotRequired[builtins.float]
    description: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetRightsResponseBaseRightsItemPreviewAssetsItem(TypedDict, total=False):
    url: Required[builtins.str]
    usage: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemSignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemSignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemRange(TypedDict, total=False):
    min: Required[builtins.float]
    max: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemDeploymentsItemVariant1(TypedDict, total=False):
    type: Required[Literal['platform']]
    platform: Required[builtins.str]
    account: NotRequired[builtins.str]
    is_live: Required[builtins.bool]
    activation_key: NotRequired[_GetSignalsResponseBaseSignalsItemDeploymentsItemVariant1ActivationKeyVariant1 | _GetSignalsResponseBaseSignalsItemDeploymentsItemVariant1ActivationKeyVariant2]
    estimated_activation_duration_minutes: NotRequired[builtins.float]
    deployed_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemDeploymentsItemVariant2(TypedDict, total=False):
    type: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    account: NotRequired[builtins.str]
    is_live: Required[builtins.bool]
    activation_key: NotRequired[_GetSignalsResponseBaseSignalsItemDeploymentsItemVariant2ActivationKeyVariant1 | _GetSignalsResponseBaseSignalsItemDeploymentsItemVariant2ActivationKeyVariant2]
    estimated_activation_duration_minutes: NotRequired[builtins.float]
    deployed_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsContentStandardsCalibrationExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_ExternalContentStandardsArtifact]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_ExternalContentStandardsContentStandardsPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatRendersItemVariant1(TypedDict, total=False):
    role: Required[builtins.str]
    parameters_from_format_id: NotRequired[builtins.bool]
    dimensions: Required[_ExternalCoreFormatRendersItemVariant1Dimensions]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatRendersItemVariant2(TypedDict, total=False):
    role: Required[builtins.str]
    parameters_from_format_id: Required[Literal[True]]
    dimensions: NotRequired[_ExternalCoreFormatRendersItemVariant2Dimensions]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant1(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['image']]
    requirements: NotRequired[_ExternalCoreRequirementsImageAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant2(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['video']]
    requirements: NotRequired[_ExternalCoreRequirementsVideoAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant3(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['audio']]
    requirements: NotRequired[_ExternalCoreRequirementsAudioAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant4(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['text']]
    requirements: NotRequired[_ExternalCoreRequirementsTextAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant5(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['markdown']]
    requirements: NotRequired[_ExternalCoreRequirementsMarkdownAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant6(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['html']]
    requirements: NotRequired[_ExternalCoreRequirementsHtmlAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant7(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['css']]
    requirements: NotRequired[_ExternalCoreRequirementsCssAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant8(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['javascript']]
    requirements: NotRequired[_ExternalCoreRequirementsJavascriptAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant9(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['vast']]
    requirements: NotRequired[_ExternalCoreRequirementsVastAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant10(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['daast']]
    requirements: NotRequired[_ExternalCoreRequirementsDaastAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant11(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['url']]
    requirements: NotRequired[_ExternalCoreRequirementsUrlAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant12(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['webhook']]
    requirements: NotRequired[_ExternalCoreRequirementsWebhookAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant13(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['brief']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant14(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['catalog']]
    requirements: NotRequired[_ExternalCoreRequirementsCatalogRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15(TypedDict, total=False):
    item_type: Required[Literal['repeatable_group']]
    asset_group_id: Required[builtins.str]
    required: Required[builtins.bool]
    min_count: Required[builtins.int]
    max_count: Required[builtins.int]
    selection_mode: NotRequired[Literal['sequential', 'optimize']]
    assets: Required[builtins.list[_ExternalCoreFormatAssetsItemVariant15AssetsItemVariant1 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant2 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant3 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant4 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant5 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant6 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant7 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant8 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant9 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant10 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant11 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant12]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatFormatCard(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    manifest: Required[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAccessibility(TypedDict, total=False):
    wcag_level: Required[Literal['A', 'AA', 'AAA']]
    requires_accessible_assets: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatDisclosureCapabilitiesItem(TypedDict, total=False):
    position: Required[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']]
    persistence: Required[builtins.list[Literal['continuous', 'initial', 'flexible']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatFormatCardDetailed(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    manifest: Required[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_ExternalCoreFormatPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeFiltersAccountsItemVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeFiltersAccountsItemVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseQuerySummarySortApplied(TypedDict, total=False):
    field: NotRequired[builtins.str]
    direction: NotRequired[Literal['asc', 'desc']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariable(TypedDict, total=False):
    variable_id: Required[builtins.str]
    name: Required[builtins.str]
    variable_type: Required[Literal['text', 'image', 'video', 'audio', 'url', 'number', 'boolean', 'color', 'date']]
    default_value: NotRequired[builtins.str]
    required: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemAssignments(TypedDict, total=False):
    assignment_count: Required[builtins.int]
    assigned_packages: NotRequired[builtins.list[_ListCreativesResponseBaseCreativesItemAssignmentsAssignedPackagesItem]]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemSnapshot(TypedDict, total=False):
    as_of: Required[builtins.str]
    staleness_seconds: Required[builtins.int]
    impressions: Required[builtins.int]
    last_served: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemItemsItemVariant1(TypedDict, total=False):
    asset_kind: Required[Literal['media']]
    asset_type: Required[builtins.str]
    asset_id: Required[builtins.str]
    content_uri: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemItemsItemVariant2(TypedDict, total=False):
    asset_kind: Required[Literal['text']]
    asset_type: Required[builtins.str]
    asset_id: Required[builtins.str]
    content: Required[builtins.str | builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreUserMatch(TypedDict, total=False):
    uids: NotRequired[builtins.list[_ExternalCoreUserMatchUidsItem]]
    hashed_email: NotRequired[builtins.str]
    hashed_phone: NotRequired[builtins.str]
    click_id: NotRequired[builtins.str]
    click_id_type: NotRequired[builtins.str]
    client_ip: NotRequired[builtins.str]
    client_user_agent: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreEventCustomData(TypedDict, total=False):
    value: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]
    order_id: NotRequired[builtins.str]
    content_ids: NotRequired[builtins.list[builtins.str]]
    content_type: NotRequired[builtins.str]
    content_name: NotRequired[builtins.str]
    content_category: NotRequired[builtins.str]
    num_items: NotRequired[builtins.int]
    search_string: NotRequired[builtins.str]
    contents: NotRequired[builtins.list[_ExternalCoreEventCustomDataContentsItem]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant1TargetFrequency(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]
    window: Required[_PackageRequestBaseOptimizationGoalsItemVariant1TargetFrequencyWindow]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant1TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant1TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['threshold_rate']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant2EventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    value_field: NotRequired[builtins.str]
    value_factor: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant2TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant2TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['per_ad_spend']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant2TargetVariant3(TypedDict, total=False):
    kind: Required[Literal['maximize_value']]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant2AttributionWindow(TypedDict, total=False):
    post_click: Required[_PackageRequestBaseOptimizationGoalsItemVariant2AttributionWindowPostClick]
    post_view: NotRequired[_PackageRequestBaseOptimizationGoalsItemVariant2AttributionWindowPostView]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoMetrosItem(TypedDict, total=False):
    system: Required[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom']]
    values: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoMetrosExcludeItem(TypedDict, total=False):
    system: Required[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom']]
    values: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoPostalAreasItem(TypedDict, total=False):
    system: Required[Literal['us_zip', 'us_zip_plus_four', 'gb_outward', 'gb_full', 'ca_fsa', 'ca_full', 'de_plz', 'fr_code_postal', 'au_postcode', 'ch_plz', 'at_plz']]
    values: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoPostalAreasExcludeItem(TypedDict, total=False):
    system: Required[Literal['us_zip', 'us_zip_plus_four', 'gb_outward', 'gb_full', 'ca_fsa', 'ca_full', 'de_plz', 'fr_code_postal', 'au_postcode', 'ch_plz', 'at_plz']]
    values: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDaypartTarget(TypedDict, total=False):
    days: Required[builtins.list[Literal['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']]]
    start_hour: Required[builtins.int]
    end_hour: Required[builtins.int]
    label: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCollectionListRef(TypedDict, total=False):
    agent_url: Required[builtins.str]
    list_id: Required[builtins.str]
    auth_token: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingAgeRestriction(TypedDict, total=False):
    min: Required[builtins.int]
    verification_required: NotRequired[builtins.bool]
    accepted_methods: NotRequired[builtins.list[Literal['facial_age_estimation', 'id_document', 'digital_id', 'credit_card', 'world_id']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingStoreCatchmentsItem(TypedDict, total=False):
    catalog_id: Required[builtins.str]
    store_ids: NotRequired[builtins.list[builtins.str]]
    catchment_ids: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant1(TypedDict, total=False):
    lat: Required[builtins.float]
    lng: Required[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: Required[_ExternalCoreTargetingGeoProximityItemVariant1TravelTime]
    transport_mode: Required[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant1Radius]
    geometry: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant1Geometry]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant2(TypedDict, total=False):
    lat: Required[builtins.float]
    lng: Required[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant2TravelTime]
    transport_mode: NotRequired[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: Required[_ExternalCoreTargetingGeoProximityItemVariant2Radius]
    geometry: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant2Geometry]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant3(TypedDict, total=False):
    lat: NotRequired[builtins.float]
    lng: NotRequired[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant3TravelTime]
    transport_mode: NotRequired[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant3Radius]
    geometry: Required[_ExternalCoreTargetingGeoProximityItemVariant3Geometry]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingKeywordTargetsItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]
    bid_price: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingNegativeKeywordsItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreMeasurementTermsBillingMeasurement(TypedDict, total=False):
    vendor: Required[_ExternalCoreBrandRef]
    max_variance_percent: NotRequired[builtins.float]
    measurement_window: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreMeasurementTermsMakegoodPolicy(TypedDict, total=False):
    available_remedies: Required[builtins.list[Literal['additional_delivery', 'credit', 'invoice_adjustment']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeAssetInputsItem(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeRequestBaseRequestsItemInputsItem(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBasePreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBasePreviewsItemRendersItemVariant1Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBasePreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBasePreviewsItemRendersItemVariant2Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBasePreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBasePreviewsItemRendersItemVariant3Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBasePreviewsItem2RendersItemVariant1Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBasePreviewsItem2RendersItemVariant1Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBasePreviewsItem2RendersItemVariant2Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBasePreviewsItem2RendersItemVariant2Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBasePreviewsItem2RendersItemVariant3Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBasePreviewsItem2RendersItemVariant3Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1Response(TypedDict, total=False):
    previews: Required[builtins.list[_PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItem]]
    interactive_url: NotRequired[builtins.str]
    expires_at: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2Response(TypedDict, total=False):
    previews: Required[builtins.list[_PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItem]]
    interactive_url: NotRequired[builtins.str]
    expires_at: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ReportPlanOutcomeRequestBaseSellerResponsePackagesItem(TypedDict, total=False):
    budget: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ReportPlanOutcomeRequestBaseDeliveryReportingPeriod(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ReportUsageRequestBaseUsageItemAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ReportUsageRequestBaseUsageItemAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiIdentityPrivacyPolicyAcknowledged(TypedDict, total=False):
    brand_policy_url: NotRequired[builtins.str]
    brand_policy_version: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiIdentityUser(TypedDict, total=False):
    email: NotRequired[builtins.str]
    name: NotRequired[builtins.str]
    locale: NotRequired[builtins.str]
    phone: NotRequired[builtins.str]
    shipping_address: NotRequired[_ExternalSponsoredIntelligenceSiIdentityUserShippingAddress]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiCapabilitiesModalities(TypedDict, total=False):
    conversational: NotRequired[builtins.bool]
    voice: NotRequired[builtins.bool | _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesVoiceVariant2]
    video: NotRequired[builtins.bool | _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesVideoVariant2]
    avatar: NotRequired[builtins.bool | _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesAvatarVariant2]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiCapabilitiesComponents(TypedDict, total=False):
    standard: NotRequired[builtins.list[Literal['text', 'link', 'image', 'product_card', 'carousel', 'action_button']]]
    extensions: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiCapabilitiesCommerce(TypedDict, total=False):
    acp_checkout: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiCapabilitiesA2ui(TypedDict, total=False):
    supported: NotRequired[builtins.bool]
    catalogs: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiUiElement(TypedDict, total=False):
    type: Required[Literal['text', 'link', 'image', 'product_card', 'carousel', 'action_button', 'app_handoff', 'integration_actions']]
    data: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalA2uiSurface(TypedDict, total=False):
    surfaceId: Required[builtins.str]
    catalogId: NotRequired[builtins.str]
    components: Required[builtins.list[_ExternalA2uiComponent]]
    rootId: NotRequired[builtins.str]
    dataModel: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _SiSendMessageResponseBaseHandoffIntent(TypedDict, total=False):
    action: NotRequired[builtins.str]
    product: NotRequired[builtins.dict[builtins.str, Any]]
    price: NotRequired[_SiSendMessageResponseBaseHandoffIntentPrice]

@with_config(ConfigDict(extra="allow"))
class _SiSendMessageResponseBaseHandoffContextForCheckout(TypedDict, total=False):
    conversation_summary: NotRequired[builtins.str]
    applied_offers: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _SiTerminateSessionRequestBaseTerminationContextTransactionIntent(TypedDict, total=False):
    action: NotRequired[Literal['purchase', 'subscribe']]
    product: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _SyncAccountsResponseBaseAccountsItemSetup(TypedDict, total=False):
    url: NotRequired[builtins.str]
    message: Required[builtins.str]
    expires_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncAccountsResponseBaseAccountsItemCreditLimit(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAudienceMember(TypedDict, total=False):
    external_id: Required[builtins.str]
    hashed_email: NotRequired[builtins.str]
    hashed_phone: NotRequired[builtins.str]
    uids: NotRequired[builtins.list[_ExternalCoreAudienceMemberUidsItem]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _SyncAudiencesResponseBaseAudiencesItemMatchBreakdownItem(TypedDict, total=False):
    id_type: Required[Literal['hashed_email', 'hashed_phone', 'rampid', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'other']]
    submitted: Required[builtins.int]
    matched: Required[builtins.int]
    match_rate: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _SyncCatalogsResponseBaseCatalogsItemItemIssuesItem(TypedDict, total=False):
    item_id: Required[builtins.str]
    status: Required[Literal['approved', 'pending', 'rejected', 'warning']]
    reasons: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _SyncEventSourcesResponseBaseEventSourcesItemSetup(TypedDict, total=False):
    snippet: NotRequired[builtins.str]
    snippet_type: NotRequired[Literal['javascript', 'html', 'pixel_url', 'server_only']]
    instructions: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreEventSourceHealth(TypedDict, total=False):
    status: Required[Literal['insufficient', 'minimum', 'good', 'excellent']]
    detail: NotRequired[_ExternalCoreEventSourceHealthDetail]
    match_rate: NotRequired[builtins.float]
    last_event_at: NotRequired[builtins.str]
    evaluated_at: NotRequired[builtins.str]
    events_received_24h: NotRequired[builtins.int]
    issues: NotRequired[builtins.list[_ExternalCoreDiagnosticIssue]]

@with_config(ConfigDict(extra="allow"))
class _SyncGovernanceRequestBaseAccountsItemAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncGovernanceRequestBaseAccountsItemAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _SyncGovernanceRequestBaseAccountsItemGovernanceAgentsItem(TypedDict, total=False):
    url: Required[builtins.str]
    authentication: Required[_SyncGovernanceRequestBaseAccountsItemGovernanceAgentsItemAuthentication]
    categories: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _SyncGovernanceResponseBaseAccountsItemAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncGovernanceResponseBaseAccountsItemAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _SyncGovernanceResponseBaseAccountsItemGovernanceAgentsItem(TypedDict, total=False):
    url: Required[builtins.str]
    categories: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemBudgetVariant1(TypedDict, total=False):
    total: Required[builtins.float]
    currency: Required[builtins.str]
    per_seller_max_pct: NotRequired[builtins.float]
    reallocation_threshold: Required[builtins.float]
    reallocation_unlimited: NotRequired[builtins.bool]
    allocations: NotRequired[builtins.dict[builtins.str, _SyncPlansRequestBasePlansItemBudgetVariant1AllocationsValue]]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemBudgetVariant2(TypedDict, total=False):
    total: Required[builtins.float]
    currency: Required[builtins.str]
    per_seller_max_pct: NotRequired[builtins.float]
    reallocation_threshold: NotRequired[builtins.float]
    reallocation_unlimited: Required[Literal[True]]
    allocations: NotRequired[builtins.dict[builtins.str, _SyncPlansRequestBasePlansItemBudgetVariant2AllocationsValue]]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemChannels(TypedDict, total=False):
    required: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    allowed: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    mix_targets: NotRequired[builtins.dict[builtins.str, _SyncPlansRequestBasePlansItemChannelsMixTargetsValue]]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemFlight(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraints(TypedDict, total=False):
    include: NotRequired[builtins.list[_ExternalGovernanceAudienceConstraintsIncludeItemVariant1 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant2 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant3 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant4]]
    exclude: NotRequired[builtins.list[_ExternalGovernanceAudienceConstraintsExcludeItemVariant1 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant2 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant3 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant4]]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemDelegationsItem(TypedDict, total=False):
    agent_url: Required[builtins.str]
    authority: Required[Literal['full', 'execute_only', 'propose_only']]
    budget_limit: NotRequired[_SyncPlansRequestBasePlansItemDelegationsItemBudgetLimit]
    markets: NotRequired[builtins.list[builtins.str]]
    expires_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemPortfolio(TypedDict, total=False):
    member_plan_ids: Required[builtins.list[builtins.str]]
    total_budget_cap: NotRequired[_SyncPlansRequestBasePlansItemPortfolioTotalBudgetCap]
    shared_policy_ids: NotRequired[builtins.list[builtins.str]]
    shared_exclusions: NotRequired[builtins.list[_ExternalGovernancePolicyEntry]]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansResponseBasePlansItemCategoriesItem(TypedDict, total=False):
    category_id: Required[builtins.str]
    status: Required[Literal['active', 'inactive']]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansResponseBasePlansItemResolvedPoliciesItem(TypedDict, total=False):
    policy_id: Required[builtins.str]
    source: Required[Literal['explicit', 'auto_applied']]
    enforcement: Required[Literal['must', 'should', 'may']]
    reason: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _TasksGetResponseBaseErrorDetails(TypedDict, total=False):
    protocol: NotRequired[Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence']]
    operation: NotRequired[builtins.str]
    specific_context: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _TasksListResponseBaseQuerySummaryDomainBreakdown(TypedDict, total=False):
    signals: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _TasksListResponseBaseQuerySummarySortApplied(TypedDict, total=False):
    field: Required[builtins.str]
    direction: Required[Literal['asc', 'desc']]

@with_config(ConfigDict(extra="allow"))
class _UpdateCollectionListRequestBaseBaseCollectionsItemVariant1IdentifiersItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant1(TypedDict, total=False):
    type: Required[Literal['url']]
    value: Required[builtins.str]
    language: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2(TypedDict, total=False):
    property_rid: Required[builtins.str]
    artifact_id: Required[builtins.str]
    variant_id: NotRequired[builtins.str]
    format_id: NotRequired[_ExternalCoreFormatId]
    url: NotRequired[builtins.str]
    published_time: NotRequired[builtins.str]
    last_update_time: NotRequired[builtins.str]
    assets: Required[builtins.list[_UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant1 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4]]
    metadata: NotRequired[_UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2Metadata]
    provenance: NotRequired[_ExternalCoreProvenance]
    identifiers: NotRequired[_UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2Identifiers]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['metric']]
    metric: Required[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    target_frequency: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetFrequency]
    view_duration_seconds: NotRequired[builtins.float]
    target: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetVariant1 | _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetVariant2]
    priority: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['event']]
    event_sources: Required[builtins.list[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2EventSourcesItem]]
    target: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant1 | _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant2 | _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant3]
    attribution_window: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindow]
    priority: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateKeywordTargetsAddItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]
    bid_price: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateKeywordTargetsRemoveItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateNegativeKeywordsAddItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateNegativeKeywordsRemoveItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

@with_config(ConfigDict(extra="allow"))
class _ValidateContentDeliveryRequestBaseRecordsItemBrandContext(TypedDict, total=False):
    brand_id: NotRequired[builtins.str]
    sku_id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ValidateContentDeliveryResponseBaseResultsItemFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    status: Required[Literal['passed', 'failed', 'warning', 'unevaluated']]
    policy_id: NotRequired[builtins.str]
    explanation: NotRequired[builtins.str]
    confidence: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyValidationResultFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    status: Required[Literal['passed', 'failed', 'warning', 'unevaluated']]
    policy_id: NotRequired[builtins.str]
    explanation: NotRequired[builtins.str]
    requirement: NotRequired[_ExternalPropertyValidationResultFeaturesItemRequirement]
    confidence: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyAuthorizationResult(TypedDict, total=False):
    status: Required[Literal['authorized', 'unauthorized', 'unknown']]
    publisher_domain: NotRequired[builtins.str]
    sales_agent_url: NotRequired[builtins.str]
    violation: NotRequired[_ExternalPropertyAuthorizationResultViolation]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProvenanceAiTool(TypedDict, total=False):
    name: Required[builtins.str]
    version: NotRequired[builtins.str]
    provider: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProvenanceDeclaredBy(TypedDict, total=False):
    agent_url: NotRequired[builtins.str]
    role: Required[Literal['creator', 'advertiser', 'agency', 'platform', 'tool']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProvenanceC2pa(TypedDict, total=False):
    manifest_url: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProvenanceDisclosure(TypedDict, total=False):
    required: Required[builtins.bool]
    jurisdictions: NotRequired[builtins.list[_ExternalCoreProvenanceDisclosureJurisdictionsItem]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProvenanceVerificationItem(TypedDict, total=False):
    verified_by: Required[builtins.str]
    verified_time: NotRequired[builtins.str]
    result: Required[Literal['authentic', 'ai_generated', 'ai_modified', 'inconclusive']]
    confidence: NotRequired[builtins.float]
    details_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant1Embedding]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant2Embedding]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant3Embedding]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant1Embedding]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant2Embedding]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant3Embedding]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFrequencyCapSuppress(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFrequencyCapWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant1SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant1SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCollectionCollectionListBaseCollectionsItemVariant1IdentifiersItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _Exemplar(TypedDict, total=False):
    scenario: Required[builtins.str]
    explanation: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant1(TypedDict, total=False):
    type: Required[Literal['text']]
    role: NotRequired[Literal['title', 'paragraph', 'heading', 'caption', 'quote', 'list_item', 'description']]
    content: Required[builtins.str]
    content_format: NotRequired[Literal['text/plain', 'text/markdown', 'text/html', 'application/json']]
    language: NotRequired[builtins.str]
    heading_level: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2(TypedDict, total=False):
    type: Required[Literal['image']]
    url: Required[builtins.str]
    access: NotRequired[_CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant1 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant2 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant3]
    alt_text: NotRequired[builtins.str]
    caption: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3(TypedDict, total=False):
    type: Required[Literal['video']]
    url: Required[builtins.str]
    access: NotRequired[_CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant1 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant2 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'subtitles', 'closed_captions', 'dub', 'generated']]
    thumbnail_url: NotRequired[builtins.str]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4(TypedDict, total=False):
    type: Required[Literal['audio']]
    url: Required[builtins.str]
    access: NotRequired[_CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant1 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant2 | _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'closed_captions', 'generated']]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2Metadata(TypedDict, total=False):
    canonical: NotRequired[builtins.str]
    author: NotRequired[builtins.str]
    keywords: NotRequired[builtins.str]
    open_graph: NotRequired[builtins.dict[builtins.str, Any]]
    twitter_card: NotRequired[builtins.dict[builtins.str, Any]]
    json_ld: NotRequired[builtins.list[builtins.dict[builtins.str, Any]]]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2Identifiers(TypedDict, total=False):
    apple_podcast_id: NotRequired[builtins.str]
    spotify_collection_id: NotRequired[builtins.str]
    podcast_guid: NotRequired[builtins.str]
    youtube_video_id: NotRequired[builtins.str]
    rss_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetFrequency(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]
    window: Required[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetFrequencyWindow]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['threshold_rate']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2EventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    value_field: NotRequired[builtins.str]
    value_factor: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['per_ad_spend']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant3(TypedDict, total=False):
    kind: Required[Literal['maximize_value']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindow(TypedDict, total=False):
    post_click: Required[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindowPostClick]
    post_view: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindowPostView]

@with_config(ConfigDict(extra="allow"))
class _ExternalPricingOptionsPriceBreakdownAdjustmentsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['fee', 'discount', 'commission', 'settlement']]
    name: Required[builtins.str]
    rate: Required[builtins.float]
    amount: NotRequired[builtins.float]
    description: NotRequired[builtins.str]
    beneficiary: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalPricingOptionsPriceBreakdownAdjustmentsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['fee', 'discount', 'commission', 'settlement']]
    name: Required[builtins.str]
    rate: NotRequired[builtins.float]
    amount: Required[builtins.float]
    description: NotRequired[builtins.str]
    beneficiary: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant1TargetFrequency(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]
    window: Required[_ExternalCorePackageOptimizationGoalsItemVariant1TargetFrequencyWindow]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant1TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant1TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['threshold_rate']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant2EventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    value_field: NotRequired[builtins.str]
    value_factor: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['per_ad_spend']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant3(TypedDict, total=False):
    kind: Required[Literal['maximize_value']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindow(TypedDict, total=False):
    post_click: Required[_ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindowPostClick]
    post_view: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindowPostView]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyPropertyListPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTrustedMatch(TypedDict, total=False):
    surfaces: NotRequired[builtins.list[Literal['website', 'mobile_app', 'ctv_app', 'desktop_app', 'dooh', 'podcast', 'radio', 'streaming_audio', 'ai_assistant']]]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecutionCreativeSpecs(TypedDict, total=False):
    vast_versions: NotRequired[builtins.list[builtins.str]]
    mraid_versions: NotRequired[builtins.list[builtins.str]]
    vpaid: NotRequired[builtins.bool]
    simid: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargeting(TypedDict, total=False):
    geo_countries: NotRequired[builtins.bool]
    geo_regions: NotRequired[builtins.bool]
    geo_metros: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingGeoMetros]
    geo_postal_areas: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingGeoPostalAreas]
    age_restriction: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingAgeRestriction]
    language: NotRequired[builtins.bool]
    keyword_targets: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingKeywordTargets]
    negative_keywords: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingNegativeKeywords]
    geo_proximity: NotRequired[_GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingGeoProximity]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyAudienceTargetingMatchingLatencyHours(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyConversionTrackingAttributionWindowsItem(TypedDict, total=False):
    event_type: NotRequired[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    post_click: Required[builtins.list[_ExternalCoreDuration]]
    post_view: NotRequired[builtins.list[_ExternalCoreDuration]]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseGovernancePropertyFeaturesItemRange(TypedDict, total=False):
    min: Required[builtins.float]
    max: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseGovernanceCreativeFeaturesItemRange(TypedDict, total=False):
    min: Required[builtins.float]
    max: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseSponsoredIntelligenceEndpointTransportsItem(TypedDict, total=False):
    type: Required[Literal['mcp', 'a2a']]
    url: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _FontRoleVariant2FilesItem(TypedDict, total=False):
    url: Required[builtins.str]
    weight: NotRequired[builtins.int]
    weight_range: NotRequired[builtins.list[builtins.int]]
    style: NotRequired[Literal['normal', 'italic', 'oblique']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDeliveryMetricsByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDeliveryMetricsQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDeliveryMetricsDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_ExternalCoreDeliveryMetricsDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDeliveryMetricsViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDeliveryMetricsByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariantByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariantQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariantDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_ExternalCoreCreativeVariantDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariantViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariantByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariantGenerationContext(TypedDict, total=False):
    context_type: NotRequired[builtins.str]
    artifact: NotRequired[_ExternalCoreCreativeVariantGenerationContextArtifact]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemByActionSourceItem]]
    content_id: Required[builtins.str]
    content_id_type: NotRequired[Literal['sku', 'gtin', 'offering_id', 'job_id', 'hotel_id', 'flight_id', 'vehicle_id', 'listing_id', 'store_id', 'program_id', 'destination_id', 'app_id']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemByActionSourceItem]]
    creative_id: Required[builtins.str]
    weight: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemByActionSourceItem]]
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemByActionSourceItem]]
    geo_level: Required[Literal['country', 'region', 'metro', 'postal_area']]
    system: NotRequired[builtins.str]
    geo_code: Required[builtins.str]
    geo_name: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemByActionSourceItem]]
    device_type: Required[Literal['desktop', 'mobile', 'tablet', 'ctv', 'dooh', 'unknown']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemByActionSourceItem]]
    device_platform: Required[Literal['ios', 'android', 'windows', 'macos', 'linux', 'chromeos', 'tvos', 'tizen', 'webos', 'fire_os', 'roku_os', 'unknown']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemByActionSourceItem]]
    audience_id: Required[builtins.str]
    audience_source: Required[Literal['synced', 'platform', 'third_party', 'lookalike', 'retargeting', 'unknown']]
    audience_name: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemByActionSourceItem]]
    placement_id: Required[builtins.str]
    placement_name: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemDailyBreakdownItem(TypedDict, total=False):
    date: Required[builtins.str]
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuysResponseBaseMediaBuysItemPackagesItemCancellation(TypedDict, total=False):
    canceled_at: Required[builtins.str]
    canceled_by: Required[Literal['buyer', 'seller']]
    reason: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuysResponseBaseMediaBuysItemPackagesItemCreativeApprovalsItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    approval_status: Required[Literal['pending_review', 'approved', 'rejected']]
    rejection_reason: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuysResponseBaseMediaBuysItemPackagesItemSnapshot(TypedDict, total=False):
    as_of: Required[builtins.str]
    staleness_seconds: Required[builtins.int]
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    currency: NotRequired[builtins.str]
    clicks: NotRequired[builtins.float]
    pacing_index: NotRequired[builtins.float]
    delivery_status: NotRequired[Literal['delivering', 'not_delivering', 'completed', 'budget_exhausted', 'flight_ended', 'goal_met']]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemSummaryStatuses(TypedDict, total=False):
    approved: NotRequired[builtins.int]
    denied: NotRequired[builtins.int]
    conditions: NotRequired[builtins.int]
    human_reviewed: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemSummaryEscalationsItem(TypedDict, total=False):
    check_id: Required[builtins.str]
    reason: Required[builtins.str]
    resolution: NotRequired[builtins.str]
    resolved_at: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemSummaryDriftMetrics(TypedDict, total=False):
    escalation_rate: NotRequired[builtins.float]
    escalation_rate_trend: NotRequired[Literal['increasing', 'stable', 'declining']]
    auto_approval_rate: NotRequired[builtins.float]
    human_override_rate: NotRequired[builtins.float]
    mean_confidence: NotRequired[builtins.float]
    thresholds: NotRequired[_GetPlanAuditLogsResponseBasePlansItemSummaryDriftMetricsThresholds]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemEntriesItemFindingsItem(TypedDict, total=False):
    category_id: Required[builtins.str]
    policy_id: NotRequired[builtins.str]
    severity: Required[Literal['info', 'warning', 'critical']]
    explanation: Required[builtins.str]
    confidence: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalPricingOptionsPriceGuidance(TypedDict, total=False):
    p25: NotRequired[builtins.float]
    p50: NotRequired[builtins.float]
    p75: NotRequired[builtins.float]
    p90: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant5Parameters(TypedDict, total=False):
    view_threshold: Required[builtins.float | _ExternalCoreProductPricingOptionsItemVariant5ParametersViewThresholdVariant2]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant6Parameters(TypedDict, total=False):
    demographic_system: NotRequired[Literal['nielsen', 'barb', 'agf', 'oztam', 'mediametrie', 'custom']]
    demographic: Required[builtins.str]
    min_points: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant8Parameters(TypedDict, total=False):
    type: Required[Literal['dooh']]
    sov_percentage: NotRequired[builtins.float]
    loop_duration_seconds: NotRequired[builtins.int]
    min_plays_per_hour: NotRequired[builtins.int]
    venue_package: NotRequired[builtins.str]
    duration_hours: NotRequired[builtins.float]
    daypart: NotRequired[builtins.str]
    estimated_impressions: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant9Parameters(TypedDict, total=False):
    time_unit: Required[Literal['hour', 'day', 'week', 'month']]
    min_duration: NotRequired[builtins.int]
    max_duration: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreForecastPoint(TypedDict, total=False):
    label: NotRequired[builtins.str]
    budget: NotRequired[builtins.float]
    metrics: Required[_ExternalCoreForecastPointMetrics]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreOutcomeMeasurementWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDuration(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCancellationPolicyCancellationFee(TypedDict, total=False):
    type: Required[Literal['percent_remaining', 'full_commitment', 'fixed_fee', 'none']]
    rate: NotRequired[builtins.float]
    amount: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreGeoBreakdownSupport(TypedDict, total=False):
    country: NotRequired[builtins.bool]
    region: NotRequired[builtins.bool]
    metro: NotRequired[builtins.dict[builtins.str, builtins.bool]]
    postal_area: NotRequired[builtins.dict[builtins.str, builtins.bool]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreMeasurementWindow(TypedDict, total=False):
    window_id: Required[builtins.str]
    description: NotRequired[builtins.str]
    duration_days: Required[builtins.int]
    expected_availability_days: NotRequired[builtins.int]
    is_guarantee_basis: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDiagnosticIssue(TypedDict, total=False):
    severity: Required[Literal['error', 'warning', 'info']]
    message: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreSpecial(TypedDict, total=False):
    name: Required[builtins.str]
    category: NotRequired[Literal['awards', 'championship', 'concert', 'conference', 'election', 'festival', 'gala', 'holiday', 'premiere', 'product_launch', 'reunion', 'tribute']]
    starts: NotRequired[builtins.str]
    ends: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTalent(TypedDict, total=False):
    role: Required[Literal['host', 'guest', 'creator', 'cast', 'narrator', 'producer', 'correspondent', 'commentator', 'analyst']]
    name: Required[builtins.str]
    brand_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAdInventoryConfig(TypedDict, total=False):
    expected_breaks: Required[builtins.int]
    total_ad_seconds: NotRequired[builtins.int]
    max_ad_duration_seconds: NotRequired[builtins.int]
    unplanned_breaks: NotRequired[builtins.bool]
    supported_formats: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreInstallmentDeadlines(TypedDict, total=False):
    booking_deadline: NotRequired[builtins.str]
    cancellation_deadline: NotRequired[builtins.str]
    material_deadlines: NotRequired[builtins.list[_ExternalCoreMaterialDeadline]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreInstallmentDerivativeOf(TypedDict, total=False):
    installment_id: Required[builtins.str]
    type: Required[Literal['clip', 'highlight', 'recap', 'trailer', 'bonus']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductTrustedMatchProvidersItem(TypedDict, total=False):
    agent_url: Required[builtins.str]
    context_match: NotRequired[builtins.bool]
    identity_match: NotRequired[builtins.bool]
    countries: NotRequired[builtins.list[builtins.str]]
    uid_types: NotRequired[builtins.list[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersTrustedMatchProvidersItem(TypedDict, total=False):
    agent_url: Required[builtins.str]
    context_match: NotRequired[builtins.bool]
    identity_match: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersSignalTargetingItemVariant1SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersSignalTargetingItemVariant1SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersSignalTargetingItemVariant2SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersSignalTargetingItemVariant2SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersSignalTargetingItemVariant3SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersSignalTargetingItemVariant3SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant1TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant1Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant1Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant2TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant2Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant2Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant3TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant3Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductFiltersGeoProximityItemVariant3Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreInsertionOrderTerms(TypedDict, total=False):
    advertiser: NotRequired[builtins.str]
    publisher: NotRequired[builtins.str]
    total_budget: NotRequired[_ExternalCoreInsertionOrderTermsTotalBudget]
    flight_start: NotRequired[builtins.str]
    flight_end: NotRequired[builtins.str]
    payment_terms: NotRequired[Literal['net_30', 'net_60', 'net_90', 'prepaid', 'due_on_receipt']]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemDeploymentsItemVariant1ActivationKeyVariant1(TypedDict, total=False):
    type: Required[Literal['segment_id']]
    segment_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemDeploymentsItemVariant1ActivationKeyVariant2(TypedDict, total=False):
    type: Required[Literal['key_value']]
    key: Required[builtins.str]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemDeploymentsItemVariant2ActivationKeyVariant1(TypedDict, total=False):
    type: Required[Literal['segment_id']]
    segment_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemDeploymentsItemVariant2ActivationKeyVariant2(TypedDict, total=False):
    type: Required[Literal['key_value']]
    key: Required[builtins.str]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetSignalsResponseBaseSignalsItemPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatRendersItemVariant1Dimensions(TypedDict, total=False):
    width: NotRequired[builtins.float]
    height: NotRequired[builtins.float]
    min_width: NotRequired[builtins.float]
    min_height: NotRequired[builtins.float]
    max_width: NotRequired[builtins.float]
    max_height: NotRequired[builtins.float]
    unit: NotRequired[Literal['px', 'dp', 'inches', 'cm', 'mm', 'pt']]
    responsive: NotRequired[_ExternalCoreFormatRendersItemVariant1DimensionsResponsive]
    aspect_ratio: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatRendersItemVariant2Dimensions(TypedDict, total=False):
    width: NotRequired[builtins.float]
    height: NotRequired[builtins.float]
    min_width: NotRequired[builtins.float]
    min_height: NotRequired[builtins.float]
    max_width: NotRequired[builtins.float]
    max_height: NotRequired[builtins.float]
    unit: NotRequired[Literal['px', 'dp', 'inches', 'cm', 'mm', 'pt']]
    responsive: NotRequired[_ExternalCoreFormatRendersItemVariant2DimensionsResponsive]
    aspect_ratio: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreOverlay(TypedDict, total=False):
    id: Required[builtins.str]
    description: NotRequired[builtins.str]
    visual: NotRequired[_ExternalCoreOverlayVisual]
    bounds: Required[_ExternalCoreOverlayBounds]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsImageAssetRequirements(TypedDict, total=False):
    min_width: NotRequired[builtins.float]
    max_width: NotRequired[builtins.float]
    min_height: NotRequired[builtins.float]
    max_height: NotRequired[builtins.float]
    unit: NotRequired[Literal['px', 'dp', 'inches', 'cm', 'mm', 'pt']]
    aspect_ratio: NotRequired[builtins.str]
    formats: NotRequired[builtins.list[Literal['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'avif', 'tiff', 'pdf', 'eps']]]
    min_dpi: NotRequired[builtins.int]
    bleed: NotRequired[_ExternalCoreRequirementsImageAssetRequirementsBleedVariant1 | _ExternalCoreRequirementsImageAssetRequirementsBleedVariant2]
    color_space: NotRequired[Literal['rgb', 'cmyk', 'grayscale']]
    max_file_size_kb: NotRequired[builtins.int]
    transparency_required: NotRequired[builtins.bool]
    animation_allowed: NotRequired[builtins.bool]
    max_animation_duration_ms: NotRequired[builtins.int]
    max_weight_grams: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsVideoAssetRequirements(TypedDict, total=False):
    min_width: NotRequired[builtins.int]
    max_width: NotRequired[builtins.int]
    min_height: NotRequired[builtins.int]
    max_height: NotRequired[builtins.int]
    aspect_ratio: NotRequired[builtins.str]
    min_duration_ms: NotRequired[builtins.int]
    max_duration_ms: NotRequired[builtins.int]
    containers: NotRequired[builtins.list[Literal['mp4', 'webm', 'mov', 'avi', 'mkv']]]
    codecs: NotRequired[builtins.list[Literal['h264', 'h265', 'vp8', 'vp9', 'av1', 'prores']]]
    max_file_size_kb: NotRequired[builtins.int]
    min_bitrate_kbps: NotRequired[builtins.int]
    max_bitrate_kbps: NotRequired[builtins.int]
    frame_rates: NotRequired[builtins.list[builtins.float]]
    audio_required: NotRequired[builtins.bool]
    frame_rate_type: NotRequired[Literal['constant', 'variable']]
    scan_type: NotRequired[Literal['progressive', 'interlaced']]
    gop_type: NotRequired[Literal['closed', 'open']]
    min_gop_interval_seconds: NotRequired[builtins.float]
    max_gop_interval_seconds: NotRequired[builtins.float]
    moov_atom_position: NotRequired[Literal['start', 'end']]
    audio_codecs: NotRequired[builtins.list[Literal['aac', 'pcm', 'ac3', 'eac3', 'mp3', 'opus', 'vorbis', 'flac']]]
    audio_sample_rates: NotRequired[builtins.list[builtins.int]]
    audio_channels: NotRequired[builtins.list[Literal['mono', 'stereo', '5.1', '7.1']]]
    loudness_lufs: NotRequired[builtins.float]
    loudness_tolerance_db: NotRequired[builtins.float]
    true_peak_dbfs: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsAudioAssetRequirements(TypedDict, total=False):
    min_duration_ms: NotRequired[builtins.int]
    max_duration_ms: NotRequired[builtins.int]
    formats: NotRequired[builtins.list[Literal['mp3', 'aac', 'wav', 'ogg', 'flac']]]
    max_file_size_kb: NotRequired[builtins.int]
    sample_rates: NotRequired[builtins.list[builtins.int]]
    channels: NotRequired[builtins.list[Literal['mono', 'stereo']]]
    min_bitrate_kbps: NotRequired[builtins.int]
    max_bitrate_kbps: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsTextAssetRequirements(TypedDict, total=False):
    min_length: NotRequired[builtins.int]
    max_length: NotRequired[builtins.int]
    min_lines: NotRequired[builtins.int]
    max_lines: NotRequired[builtins.int]
    character_pattern: NotRequired[builtins.str]
    prohibited_terms: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsMarkdownAssetRequirements(TypedDict, total=False):
    max_length: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsHtmlAssetRequirements(TypedDict, total=False):
    max_file_size_kb: NotRequired[builtins.int]
    sandbox: NotRequired[Literal['none', 'iframe', 'safeframe', 'fencedframe']]
    external_resources_allowed: NotRequired[builtins.bool]
    allowed_external_domains: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsCssAssetRequirements(TypedDict, total=False):
    max_file_size_kb: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsJavascriptAssetRequirements(TypedDict, total=False):
    max_file_size_kb: NotRequired[builtins.int]
    module_type: NotRequired[Literal['script', 'module', 'iife']]
    strict_mode_required: NotRequired[builtins.bool]
    external_resources_allowed: NotRequired[builtins.bool]
    allowed_external_domains: NotRequired[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsVastAssetRequirements(TypedDict, total=False):
    vast_version: NotRequired[Literal['2.0', '3.0', '4.0', '4.1', '4.2']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsDaastAssetRequirements(TypedDict, total=False):
    daast_version: NotRequired[Literal['1.0']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsUrlAssetRequirements(TypedDict, total=False):
    role: NotRequired[Literal['clickthrough', 'landing_page', 'impression_tracker', 'click_tracker', 'viewability_tracker', 'third_party_tracker']]
    protocols: NotRequired[builtins.list[Literal['https', 'http']]]
    allowed_domains: NotRequired[builtins.list[builtins.str]]
    max_length: NotRequired[builtins.int]
    macro_support: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsWebhookAssetRequirements(TypedDict, total=False):
    methods: NotRequired[builtins.list[Literal['GET', 'POST']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsCatalogRequirements(TypedDict, total=False):
    catalog_type: Required[Literal['offering', 'product', 'inventory', 'store', 'promotion', 'hotel', 'flight', 'job', 'vehicle', 'real_estate', 'education', 'destination', 'app']]
    required: NotRequired[builtins.bool]
    min_items: NotRequired[builtins.int]
    max_items: NotRequired[builtins.int]
    required_fields: NotRequired[builtins.list[builtins.str]]
    feed_formats: NotRequired[builtins.list[Literal['google_merchant_center', 'facebook_catalog', 'shopify', 'linkedin_jobs', 'custom']]]
    offering_asset_constraints: NotRequired[builtins.list[_ExternalCoreRequirementsOfferingAssetConstraint]]
    field_bindings: NotRequired[builtins.list[_ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant1 | _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant2 | _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant1(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['image']]
    requirements: NotRequired[_ExternalCoreRequirementsImageAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant2(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['video']]
    requirements: NotRequired[_ExternalCoreRequirementsVideoAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant3(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['audio']]
    requirements: NotRequired[_ExternalCoreRequirementsAudioAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant4(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['text']]
    requirements: NotRequired[_ExternalCoreRequirementsTextAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant5(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['markdown']]
    requirements: NotRequired[_ExternalCoreRequirementsMarkdownAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant6(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['html']]
    requirements: NotRequired[_ExternalCoreRequirementsHtmlAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant7(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['css']]
    requirements: NotRequired[_ExternalCoreRequirementsCssAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant8(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['javascript']]
    requirements: NotRequired[_ExternalCoreRequirementsJavascriptAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant9(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['vast']]
    requirements: NotRequired[_ExternalCoreRequirementsVastAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant10(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['daast']]
    requirements: NotRequired[_ExternalCoreRequirementsDaastAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant11(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['url']]
    requirements: NotRequired[_ExternalCoreRequirementsUrlAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant12(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['webhook']]
    requirements: NotRequired[_ExternalCoreRequirementsWebhookAssetRequirements]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemAssignmentsAssignedPackagesItem(TypedDict, total=False):
    package_id: Required[builtins.str]
    assigned_date: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ListCreativesResponseBaseCreativesItemPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreUserMatchUidsItem(TypedDict, total=False):
    type: Required[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreEventCustomDataContentsItem(TypedDict, total=False):
    id: Required[builtins.str]
    quantity: NotRequired[builtins.int]
    price: NotRequired[builtins.float]
    brand: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant1TargetFrequencyWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant2AttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _PackageRequestBaseOptimizationGoalsItemVariant2AttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant1TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant1Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant1Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant2TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant2Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant2Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant3TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant3Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreTargetingGeoProximityItemVariant3Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBasePreviewsItem2RendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1 | _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2 | _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3]]
    input: Required[_PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemInput]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1 | _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2 | _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3]]
    input: Required[_PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemInput]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiIdentityUserShippingAddress(TypedDict, total=False):
    street: NotRequired[builtins.str]
    city: NotRequired[builtins.str]
    state: NotRequired[builtins.str]
    postal_code: NotRequired[builtins.str]
    country: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesVoiceVariant2(TypedDict, total=False):
    provider: NotRequired[builtins.str]
    voice_id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesVideoVariant2(TypedDict, total=False):
    formats: NotRequired[builtins.list[builtins.str]]
    max_duration_seconds: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesAvatarVariant2(TypedDict, total=False):
    provider: NotRequired[builtins.str]
    avatar_id: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalA2uiComponent(TypedDict, total=False):
    id: Required[builtins.str]
    parentId: NotRequired[builtins.str]
    component: Required[builtins.dict[builtins.str, builtins.dict[builtins.str, Any]]]

@with_config(ConfigDict(extra="allow"))
class _SiSendMessageResponseBaseHandoffIntentPrice(TypedDict, total=False):
    amount: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreAudienceMemberUidsItem(TypedDict, total=False):
    type: Required[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]
    value: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreEventSourceHealthDetail(TypedDict, total=False):
    score: Required[builtins.float]
    max_score: Required[builtins.float]
    label: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncGovernanceRequestBaseAccountsItemGovernanceAgentsItemAuthentication(TypedDict, total=False):
    schemes: Required[builtins.list[Literal['Bearer', 'HMAC-SHA256']]]
    credentials: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemBudgetVariant1AllocationsValue(TypedDict, total=False):
    amount: NotRequired[builtins.float]
    max_pct: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemBudgetVariant2AllocationsValue(TypedDict, total=False):
    amount: NotRequired[builtins.float]
    max_pct: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemChannelsMixTargetsValue(TypedDict, total=False):
    min_pct: NotRequired[builtins.float]
    max_pct: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant1(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsIncludeItemVariant1SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant1SignalIdVariant2]
    value_type: Required[Literal['binary']]
    value: Required[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant2(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsIncludeItemVariant2SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant2SignalIdVariant2]
    value_type: Required[Literal['categorical']]
    values: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant3(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsIncludeItemVariant3SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant3SignalIdVariant2]
    value_type: Required[Literal['numeric']]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant4(TypedDict, total=False):
    type: Required[Literal['description']]
    description: Required[builtins.str]
    category: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant1(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsExcludeItemVariant1SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant1SignalIdVariant2]
    value_type: Required[Literal['binary']]
    value: Required[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant2(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsExcludeItemVariant2SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant2SignalIdVariant2]
    value_type: Required[Literal['categorical']]
    values: Required[builtins.list[builtins.str]]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant3(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsExcludeItemVariant3SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant3SignalIdVariant2]
    value_type: Required[Literal['numeric']]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant4(TypedDict, total=False):
    type: Required[Literal['description']]
    description: Required[builtins.str]
    category: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemDelegationsItemBudgetLimit(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _SyncPlansRequestBasePlansItemPortfolioTotalBudgetCap(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant1(TypedDict, total=False):
    type: Required[Literal['text']]
    role: NotRequired[Literal['title', 'paragraph', 'heading', 'caption', 'quote', 'list_item', 'description']]
    content: Required[builtins.str]
    content_format: NotRequired[Literal['text/plain', 'text/markdown', 'text/html', 'application/json']]
    language: NotRequired[builtins.str]
    heading_level: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2(TypedDict, total=False):
    type: Required[Literal['image']]
    url: Required[builtins.str]
    access: NotRequired[_UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant1 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant2 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant3]
    alt_text: NotRequired[builtins.str]
    caption: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3(TypedDict, total=False):
    type: Required[Literal['video']]
    url: Required[builtins.str]
    access: NotRequired[_UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant1 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant2 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'subtitles', 'closed_captions', 'dub', 'generated']]
    thumbnail_url: NotRequired[builtins.str]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4(TypedDict, total=False):
    type: Required[Literal['audio']]
    url: Required[builtins.str]
    access: NotRequired[_UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant1 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant2 | _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'closed_captions', 'generated']]
    provenance: NotRequired[_ExternalCoreProvenance]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2Metadata(TypedDict, total=False):
    canonical: NotRequired[builtins.str]
    author: NotRequired[builtins.str]
    keywords: NotRequired[builtins.str]
    open_graph: NotRequired[builtins.dict[builtins.str, Any]]
    twitter_card: NotRequired[builtins.dict[builtins.str, Any]]
    json_ld: NotRequired[builtins.list[builtins.dict[builtins.str, Any]]]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2Identifiers(TypedDict, total=False):
    apple_podcast_id: NotRequired[builtins.str]
    spotify_collection_id: NotRequired[builtins.str]
    podcast_guid: NotRequired[builtins.str]
    youtube_video_id: NotRequired[builtins.str]
    rss_url: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetFrequency(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]
    window: Required[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetFrequencyWindow]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['threshold_rate']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2EventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    value_field: NotRequired[builtins.str]
    value_factor: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['per_ad_spend']]
    value: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant3(TypedDict, total=False):
    kind: Required[Literal['maximize_value']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindow(TypedDict, total=False):
    post_click: Required[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindowPostClick]
    post_view: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindowPostView]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyValidationResultFeaturesItemRequirement(TypedDict, total=False):
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]
    allowed_values: NotRequired[builtins.list[Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalPropertyAuthorizationResultViolation(TypedDict, total=False):
    code: Required[builtins.str]
    message: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProvenanceDisclosureJurisdictionsItem(TypedDict, total=False):
    country: Required[builtins.str]
    region: NotRequired[builtins.str]
    regulation: Required[builtins.str]
    label_text: NotRequired[builtins.str]
    render_guidance: NotRequired[_ExternalCoreProvenanceDisclosureJurisdictionsItemRenderGuidance]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreviewPreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _BuildCreativeResponseBasePreview2PreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _CreateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetFrequencyWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant1TargetFrequencyWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingGeoMetros(TypedDict, total=False):
    nielsen_dma: NotRequired[builtins.bool]
    uk_itl1: NotRequired[builtins.bool]
    uk_itl2: NotRequired[builtins.bool]
    eurostat_nuts2: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingGeoPostalAreas(TypedDict, total=False):
    us_zip: NotRequired[builtins.bool]
    us_zip_plus_four: NotRequired[builtins.bool]
    gb_outward: NotRequired[builtins.bool]
    gb_full: NotRequired[builtins.bool]
    ca_fsa: NotRequired[builtins.bool]
    ca_full: NotRequired[builtins.bool]
    de_plz: NotRequired[builtins.bool]
    fr_code_postal: NotRequired[builtins.bool]
    au_postcode: NotRequired[builtins.bool]
    ch_plz: NotRequired[builtins.bool]
    at_plz: NotRequired[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingAgeRestriction(TypedDict, total=False):
    supported: NotRequired[builtins.bool]
    verification_methods: NotRequired[builtins.list[Literal['facial_age_estimation', 'id_document', 'digital_id', 'credit_card', 'world_id']]]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingKeywordTargets(TypedDict, total=False):
    supported_match_types: Required[builtins.list[Literal['broad', 'phrase', 'exact']]]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingNegativeKeywords(TypedDict, total=False):
    supported_match_types: Required[builtins.list[Literal['broad', 'phrase', 'exact']]]

@with_config(ConfigDict(extra="allow"))
class _GetAdcpCapabilitiesResponseBaseMediaBuyExecutionTargetingGeoProximity(TypedDict, total=False):
    radius: NotRequired[builtins.bool]
    travel_time: NotRequired[builtins.bool]
    geometry: NotRequired[builtins.bool]
    transport_modes: NotRequired[builtins.list[Literal['walking', 'cycling', 'driving', 'public_transport']]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreDeliveryMetricsDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariantDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreCreativeVariantGenerationContextArtifact(TypedDict, total=False):
    property_id: Required[_ExternalCoreIdentifier]
    artifact_id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemTotalsDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemDoohMetricsVenueBreakdownItem]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _GetPlanAuditLogsResponseBasePlansItemSummaryDriftMetricsThresholds(TypedDict, total=False):
    escalation_rate_max: NotRequired[builtins.float]
    escalation_rate_min: NotRequired[builtins.float]
    auto_approval_rate_max: NotRequired[builtins.float]
    human_override_rate_max: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProductPricingOptionsItemVariant5ParametersViewThresholdVariant2(TypedDict, total=False):
    duration_seconds: Required[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreForecastPointMetrics(TypedDict, total=False):
    audience_size: NotRequired[_ExternalCoreForecastRange]
    reach: NotRequired[_ExternalCoreForecastRange]
    frequency: NotRequired[_ExternalCoreForecastRange]
    impressions: NotRequired[_ExternalCoreForecastRange]
    clicks: NotRequired[_ExternalCoreForecastRange]
    spend: NotRequired[_ExternalCoreForecastRange]
    views: NotRequired[_ExternalCoreForecastRange]
    completed_views: NotRequired[_ExternalCoreForecastRange]
    grps: NotRequired[_ExternalCoreForecastRange]
    engagements: NotRequired[_ExternalCoreForecastRange]
    follows: NotRequired[_ExternalCoreForecastRange]
    saves: NotRequired[_ExternalCoreForecastRange]
    profile_visits: NotRequired[_ExternalCoreForecastRange]
    measured_impressions: NotRequired[_ExternalCoreForecastRange]
    downloads: NotRequired[_ExternalCoreForecastRange]
    plays: NotRequired[_ExternalCoreForecastRange]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreMaterialDeadline(TypedDict, total=False):
    stage: Required[builtins.str]
    due_at: Required[builtins.str]
    label: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreInsertionOrderTermsTotalBudget(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatRendersItemVariant1DimensionsResponsive(TypedDict, total=False):
    width: Required[builtins.bool]
    height: Required[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreFormatRendersItemVariant2DimensionsResponsive(TypedDict, total=False):
    width: Required[builtins.bool]
    height: Required[builtins.bool]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreOverlayVisual(TypedDict, total=False):
    url: NotRequired[builtins.str]
    light: NotRequired[builtins.str]
    dark: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreOverlayBounds(TypedDict, total=False):
    x: Required[builtins.float]
    y: Required[builtins.float]
    width: Required[builtins.float]
    height: Required[builtins.float]
    unit: Required[Literal['px', 'fraction', 'inches', 'cm', 'mm', 'pt']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsImageAssetRequirementsBleedVariant1(TypedDict, total=False):
    uniform: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsImageAssetRequirementsBleedVariant2(TypedDict, total=False):
    top: Required[builtins.float]
    right: Required[builtins.float]
    bottom: Required[builtins.float]
    left: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsOfferingAssetConstraint(TypedDict, total=False):
    asset_group_id: Required[builtins.str]
    asset_type: Required[Literal['image', 'video', 'audio', 'text', 'markdown', 'html', 'css', 'javascript', 'vast', 'daast', 'url', 'webhook', 'brief', 'catalog']]
    required: NotRequired[builtins.bool]
    min_count: NotRequired[builtins.int]
    max_count: NotRequired[builtins.int]
    asset_requirements: NotRequired[builtins.dict[builtins.str, Any]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['scalar']]
    asset_id: Required[builtins.str]
    catalog_field: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['asset_pool']]
    asset_id: Required[builtins.str]
    asset_group_id: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3(TypedDict, total=False):
    kind: Required[Literal['catalog_group']]
    format_group_id: Required[builtins.str]
    catalog_item: Required[Literal[True]]
    per_item_bindings: NotRequired[builtins.list[_ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3PerItemBindingsItemVariant1 | _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3PerItemBindingsItemVariant2]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3Embedding]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant1SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant1SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant2SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant2SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant3SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsIncludeItemVariant3SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant1SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant1SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant2SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant2SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant3SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _ExternalGovernanceAudienceConstraintsExcludeItemVariant3SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _UpdateContentStandardsRequestBaseCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetFrequencyWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreProvenanceDisclosureJurisdictionsItemRenderGuidance(TypedDict, total=False):
    persistence: NotRequired[Literal['continuous', 'initial', 'flexible']]
    min_duration_ms: NotRequired[builtins.int]
    positions: NotRequired[builtins.list[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByCreativeItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByKeywordItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByGeoItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByAudienceItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItemByPackageItemByPlacementItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreForecastRange(TypedDict, total=False):
    low: NotRequired[builtins.float]
    mid: NotRequired[builtins.float]
    high: NotRequired[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3PerItemBindingsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['scalar']]
    asset_id: Required[builtins.str]
    catalog_field: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3PerItemBindingsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['asset_pool']]
    asset_id: Required[builtins.str]
    asset_group_id: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

@with_config(ConfigDict(extra="allow"))
class _PreviewCreativeResponseBaseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

FIELDS: dict[str, dict[str, tuple[Any, Any]]] = {
    'AcquireRightsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'rights_id': (builtins.str, ...),
        'pricing_option_id': (builtins.str, ...),
        'buyer': (_ExternalCoreBrandRef, ...),
        'campaign': (_AcquireRightsRequestBaseCampaign, ...),
        'revocation_webhook': (_ExternalCorePushNotificationConfig, ...),
        'push_notification_config': (_ExternalCorePushNotificationConfig | None, None),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'AcquireRightsResponseBase': {
        'rights_id': (builtins.str | None, None),
        'status': (Literal['acquired'] | Literal['pending_approval'] | Literal['rejected'] | None, None),
        'brand_id': (builtins.str | None, None),
        'terms': (_ExternalBrandRightsTerms | None, None),
        'generation_credentials': (builtins.list[_ExternalCoreGenerationCredential] | None, None),
        'restrictions': (builtins.list[builtins.str] | None, None),
        'disclosure': (_AcquireRightsResponseBaseDisclosure | None, None),
        'approval_webhook': (_ExternalCorePushNotificationConfig | None, None),
        'usage_reporting_url': (builtins.str | None, None),
        'rights_constraint': (_ExternalCoreRightsConstraint | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'detail': (builtins.str | None, None),
        'estimated_response_time': (builtins.str | None, None),
        'reason': (builtins.str | None, None),
        'suggestions': (builtins.list[builtins.str] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'ActivateSignalRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'action': (Literal['activate', 'deactivate'], 'activate'),
        'signal_agent_segment_id': (builtins.str, ...),
        'destinations': (builtins.list[_ActivateSignalRequestBaseDestinationsItemVariant1 | _ActivateSignalRequestBaseDestinationsItemVariant2], ...),
        'pricing_option_id': (builtins.str | None, None),
        'account': (_ActivateSignalRequestBaseAccountVariant1 | _ActivateSignalRequestBaseAccountVariant2 | None, None),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ActivateSignalResponseBase': {
        'deployments': (builtins.list[_ActivateSignalResponseBaseDeploymentsItemVariant1 | _ActivateSignalResponseBaseDeploymentsItemVariant2] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'BuildCreativeInputRequiredResponseBase': {
        'reason': (Literal['APPROVAL_REQUIRED', 'CREATIVE_DIRECTION_NEEDED', 'ASSET_SELECTION_NEEDED'] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'BuildCreativeRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'message': (builtins.str | None, None),
        'creative_manifest': (_ExternalCoreCreativeManifest | None, None),
        'creative_id': (builtins.str | None, None),
        'concept_id': (builtins.str | None, None),
        'media_buy_id': (builtins.str | None, None),
        'package_id': (builtins.str | None, None),
        'target_format_id': (_ExternalCoreFormatId | None, None),
        'target_format_ids': (builtins.list[_ExternalCoreFormatId] | None, None),
        'account': (_BuildCreativeRequestBaseAccountVariant1 | _BuildCreativeRequestBaseAccountVariant2 | None, None),
        'brand': (_ExternalCoreBrandRef | None, None),
        'quality': (Literal['draft', 'production'] | None, None),
        'item_limit': (builtins.int | None, None),
        'include_preview': (builtins.bool | None, None),
        'preview_inputs': (builtins.list[_BuildCreativeRequestBasePreviewInputsItem] | None, None),
        'preview_quality': (Literal['draft', 'production'] | None, None),
        'preview_output_format': (Literal['url', 'html'], 'url'),
        'macro_values': (builtins.dict[builtins.str, builtins.str] | None, None),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'BuildCreativeSubmittedResponseBase': {
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'BuildCreativeResponseBase': {
        'creative_manifest': (_ExternalCoreCreativeManifest | None, None),
        'sandbox': (builtins.bool | None, None),
        'expires_at': (builtins.str | None, None),
        'preview': (_BuildCreativeResponseBasePreview | _BuildCreativeResponseBasePreview2 | None, None),
        'preview_error': (_ExternalCoreError | None, None),
        'pricing_option_id': (builtins.str | None, None),
        'vendor_cost': (builtins.float | None, None),
        'currency': (builtins.str | None, None),
        'consumption': (_ExternalCoreCreativeConsumption | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'creative_manifests': (builtins.list[_ExternalCoreCreativeManifest] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'BuildCreativeWorkingResponseBase': {
        'percentage': (builtins.float | None, None),
        'current_step': (builtins.str | None, None),
        'total_steps': (builtins.int | None, None),
        'step_number': (builtins.int | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CalibrateContentRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'standards_id': (builtins.str, ...),
        'artifact': (_ExternalContentStandardsArtifact, ...),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CalibrateContentResponseBase': {
        'verdict': (Literal['pass', 'fail'] | None, None),
        'confidence': (builtins.float | None, None),
        'explanation': (builtins.str | None, None),
        'features': (builtins.list[_CalibrateContentResponseBaseFeaturesItem] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'CheckGovernanceRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'plan_id': (builtins.str, ...),
        'caller': (builtins.str, ...),
        'purchase_type': (Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services'], 'media_buy'),
        'tool': (builtins.str | None, None),
        'payload': (builtins.dict[builtins.str, Any] | None, None),
        'governance_context': (builtins.str | None, None),
        'phase': (Literal['purchase', 'modification', 'delivery'], 'purchase'),
        'planned_delivery': (_ExternalCorePlannedDelivery | None, None),
        'delivery_metrics': (_CheckGovernanceRequestBaseDeliveryMetrics | None, None),
        'modification_summary': (builtins.str | None, None),
        'invoice_recipient': (_ExternalCoreBusinessEntity | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CheckGovernanceResponseBase': {
        'check_id': (builtins.str, ...),
        'status': (Literal['approved', 'denied', 'conditions'], ...),
        'plan_id': (builtins.str, ...),
        'explanation': (builtins.str, ...),
        'findings': (builtins.list[_CheckGovernanceResponseBaseFindingsItem] | None, None),
        'conditions': (builtins.list[_CheckGovernanceResponseBaseConditionsItem] | None, None),
        'expires_at': (builtins.str | None, None),
        'next_check': (builtins.str | None, None),
        'categories_evaluated': (builtins.list[builtins.str] | None, None),
        'policies_evaluated': (builtins.list[builtins.str] | None, None),
        'mode': (Literal['audit', 'advisory', 'enforce'] | None, None),
        'governance_context': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ComplyTestControllerRequestBase': {
        'scenario': (Literal['list_scenarios', 'force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_create_media_buy_arm', 'force_task_completion', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend', 'seed_product', 'seed_pricing_option', 'seed_creative', 'seed_plan', 'seed_media_buy', 'seed_creative_format'], ...),
        'params': (_ComplyTestControllerRequestBaseParams | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ComplyTestControllerResponseBase': {
        'success': (Literal[True] | Literal[False], ...),
        'scenarios': (builtins.list[Literal['force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_create_media_buy_arm', 'force_task_completion', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend', 'seed_product', 'seed_pricing_option', 'seed_creative', 'seed_plan', 'seed_media_buy', 'seed_creative_format']] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'previous_state': (builtins.str | None, None),
        'current_state': (builtins.str | builtins.str | None, None),
        'message': (builtins.str | None, None),
        'simulated': (builtins.dict[builtins.str, Any] | None, None),
        'cumulative': (builtins.dict[builtins.str, Any] | None, None),
        'forced': (_ComplyTestControllerResponseBaseForced | None, None),
        'error': (Literal['INVALID_TRANSITION', 'INVALID_STATE', 'NOT_FOUND', 'UNKNOWN_SCENARIO', 'INVALID_PARAMS', 'FORBIDDEN', 'INTERNAL_ERROR'] | None, None),
        'error_detail': (builtins.str | None, None),
    },
    'ContextMatchRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        '$schema': (builtins.str | None, None),
        'type': (Literal['context_match_request'], ...),
        'protocol_version': (builtins.str, '1.0'),
        'request_id': (builtins.str, ...),
        'property_rid': (builtins.str, ...),
        'property_id': (builtins.str | None, None),
        'property_type': (Literal['website', 'mobile_app', 'ctv_app', 'desktop_app', 'dooh', 'podcast', 'radio', 'linear_tv', 'streaming_audio', 'ai_assistant'], ...),
        'placement_id': (builtins.str, ...),
        'seller_agent_url': (builtins.str, ...),
        'artifact': (_ExternalContentStandardsArtifact | None, None),
        'artifact_refs': (builtins.list[_ContextMatchRequestBaseArtifactRefsItem] | None, None),
        'geo': (_ContextMatchRequestBaseGeo | None, None),
        'context_signals': (_ContextMatchRequestBaseContextSignals | None, None),
        'package_ids': (builtins.list[builtins.str] | None, None),
    },
    'ContextMatchResponseBase': {
        'type': (Literal['context_match_response'], ...),
        'request_id': (builtins.str, ...),
        'offers': (builtins.list[_ExternalTmpOffer], ...),
        'cache_ttl': (builtins.int | None, None),
        'signals': (_ContextMatchResponseBaseSignals | None, None),
    },
    'CreateCollectionListRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_CreateCollectionListRequestBaseAccountVariant1 | _CreateCollectionListRequestBaseAccountVariant2 | None, None),
        'name': (builtins.str, ...),
        'description': (builtins.str | None, None),
        'base_collections': (builtins.list[_CreateCollectionListRequestBaseBaseCollectionsItemVariant1 | _CreateCollectionListRequestBaseBaseCollectionsItemVariant2 | _CreateCollectionListRequestBaseBaseCollectionsItemVariant3] | None, None),
        'filters': (_ExternalCollectionCollectionListFilters | None, None),
        'brand': (_ExternalCoreBrandRef | None, None),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreateCollectionListResponseBase': {
        'list': (_ExternalCollectionCollectionList, ...),
        'auth_token': (builtins.str, ...),
        'replayed': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreateContentStandardsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'scope': (_CreateContentStandardsRequestBaseScope, ...),
        'registry_policy_ids': (builtins.list[builtins.str] | None, None),
        'policies': (builtins.list[_ExternalGovernancePolicyEntry] | None, None),
        'calibration_exemplars': (_CreateContentStandardsRequestBaseCalibrationExemplars | None, None),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreateContentStandardsResponseBase': {
        'standards_id': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'conflicting_standards_id': (builtins.str | None, None),
    },
    'CreateMediaBuyInputRequiredResponseBase': {
        'reason': (Literal['APPROVAL_REQUIRED', 'BUDGET_EXCEEDS_LIMIT'] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreateMediaBuyRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'idempotency_key': (builtins.str, ...),
        'plan_id': (builtins.str | None, None),
        'account': (_CreateMediaBuyRequestBaseAccountVariant1 | _CreateMediaBuyRequestBaseAccountVariant2, ...),
        'proposal_id': (builtins.str | None, None),
        'total_budget': (_CreateMediaBuyRequestBaseTotalBudget | None, None),
        'packages': (builtins.list[_ExternalMediaBuyPackageRequest] | None, None),
        'brand': (_ExternalCoreBrandRef, ...),
        'advertiser_industry': (Literal['automotive', 'automotive.electric_vehicles', 'automotive.parts_accessories', 'automotive.luxury', 'beauty_cosmetics', 'beauty_cosmetics.skincare', 'beauty_cosmetics.fragrance', 'beauty_cosmetics.haircare', 'cannabis', 'cpg', 'cpg.personal_care', 'cpg.household', 'dating', 'education', 'education.higher_education', 'education.online_learning', 'education.k12', 'energy_utilities', 'energy_utilities.renewable', 'fashion_apparel', 'fashion_apparel.luxury', 'fashion_apparel.sportswear', 'finance', 'finance.banking', 'finance.insurance', 'finance.investment', 'finance.cryptocurrency', 'food_beverage', 'food_beverage.alcohol', 'food_beverage.restaurants', 'food_beverage.packaged_goods', 'gambling_betting', 'gambling_betting.sports_betting', 'gambling_betting.casino', 'gaming', 'gaming.mobile', 'gaming.console_pc', 'gaming.esports', 'government_nonprofit', 'government_nonprofit.political', 'government_nonprofit.charity', 'healthcare', 'healthcare.pharmaceutical', 'healthcare.medical_devices', 'healthcare.wellness', 'home_garden', 'home_garden.furniture', 'home_garden.home_improvement', 'media_entertainment', 'media_entertainment.podcasts', 'media_entertainment.music', 'media_entertainment.film_tv', 'media_entertainment.publishing', 'media_entertainment.live_events', 'pets', 'professional_services', 'professional_services.legal', 'professional_services.consulting', 'real_estate', 'real_estate.residential', 'real_estate.commercial', 'recruitment_hr', 'retail', 'retail.ecommerce', 'retail.department_stores', 'sports_fitness', 'sports_fitness.equipment', 'sports_fitness.teams_leagues', 'technology', 'technology.software', 'technology.hardware', 'technology.ai_ml', 'telecom', 'telecom.mobile_carriers', 'telecom.internet_providers', 'transportation_logistics', 'travel_hospitality', 'travel_hospitality.airlines', 'travel_hospitality.hotels', 'travel_hospitality.cruise', 'travel_hospitality.tourism'] | None, None),
        'invoice_recipient': (_ExternalCoreBusinessEntity | None, None),
        'io_acceptance': (_CreateMediaBuyRequestBaseIoAcceptance | None, None),
        'po_number': (builtins.str | None, None),
        'agency_estimate_number': (builtins.str | None, None),
        'start_time': (Literal['asap'] | builtins.str, ...),
        'end_time': (builtins.str, ...),
        'push_notification_config': (_ExternalCorePushNotificationConfig | None, None),
        'reporting_webhook': (_ExternalCoreReportingWebhook | None, None),
        'artifact_webhook': (_CreateMediaBuyRequestBaseArtifactWebhook | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreateMediaBuySubmittedResponseBase': {
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreateMediaBuyResponseBase': {
        'media_buy_id': (builtins.str | None, None),
        'account': (_ExternalCoreAccount | None, None),
        'invoice_recipient': (_ExternalCoreBusinessEntity | None, None),
        'status': (Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | Literal['submitted'] | None, None),
        'confirmed_at': (builtins.str | None, None),
        'creative_deadline': (builtins.str | None, None),
        'revision': (builtins.int | None, None),
        'valid_actions': (builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']] | None, None),
        'packages': (builtins.list[_ExternalCorePackage] | None, None),
        'planned_delivery': (_ExternalCorePlannedDelivery | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'task_id': (builtins.str | None, None),
        'message': (builtins.str | None, None),
    },
    'CreateMediaBuyWorkingResponseBase': {
        'percentage': (builtins.float | None, None),
        'current_step': (builtins.str | None, None),
        'total_steps': (builtins.int | None, None),
        'step_number': (builtins.int | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreatePropertyListRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_CreatePropertyListRequestBaseAccountVariant1 | _CreatePropertyListRequestBaseAccountVariant2 | None, None),
        'name': (builtins.str, ...),
        'description': (builtins.str | None, None),
        'base_properties': (builtins.list[_CreatePropertyListRequestBaseBasePropertiesItemVariant1 | _CreatePropertyListRequestBaseBasePropertiesItemVariant2 | _CreatePropertyListRequestBaseBasePropertiesItemVariant3] | None, None),
        'filters': (_ExternalPropertyPropertyListFilters | None, None),
        'brand': (_ExternalCoreBrandRef | None, None),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreatePropertyListResponseBase': {
        'list': (_ExternalPropertyPropertyList, ...),
        'auth_token': (builtins.str, ...),
        'replayed': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreativeApprovalRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'rights_id': (builtins.str, ...),
        'creative_id': (builtins.str | None, None),
        'creative_url': (builtins.str, ...),
        'creative_format': (_ExternalCoreFormatId | None, None),
        'description': (builtins.str | None, None),
        'metadata': (builtins.dict[builtins.str, Any] | None, None),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'CreativeApprovalResponseBase': {
        'status': (Literal['approved'] | Literal['rejected'] | Literal['pending_review'] | None, None),
        'rights_id': (builtins.str | None, None),
        'creative_id': (builtins.str | None, None),
        'creative_url': (builtins.str | None, None),
        'approved_at': (builtins.str | None, None),
        'conditions': (builtins.list[builtins.str] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'reason': (builtins.str | None, None),
        'suggestions': (builtins.list[builtins.str] | None, None),
        'estimated_response_time': (builtins.str | None, None),
        'status_url': (builtins.str | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'DeleteCollectionListRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'list_id': (builtins.str, ...),
        'account': (_DeleteCollectionListRequestBaseAccountVariant1 | _DeleteCollectionListRequestBaseAccountVariant2 | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'idempotency_key': (builtins.str, ...),
    },
    'DeleteCollectionListResponseBase': {
        'deleted': (builtins.bool, ...),
        'list_id': (builtins.str, ...),
        'replayed': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'DeletePropertyListRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'list_id': (builtins.str, ...),
        'account': (_DeletePropertyListRequestBaseAccountVariant1 | _DeletePropertyListRequestBaseAccountVariant2 | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'idempotency_key': (builtins.str, ...),
    },
    'DeletePropertyListResponseBase': {
        'deleted': (builtins.bool, ...),
        'list_id': (builtins.str, ...),
        'replayed': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetAccountFinancialsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_GetAccountFinancialsRequestBaseAccountVariant1 | _GetAccountFinancialsRequestBaseAccountVariant2, ...),
        'period': (_ExternalCoreDateRange | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetAccountFinancialsResponseBase': {
        'account': (_GetAccountFinancialsResponseBaseAccountVariant1 | _GetAccountFinancialsResponseBaseAccountVariant2 | None, None),
        'currency': (builtins.str | None, None),
        'period': (_ExternalCoreDateRange | None, None),
        'timezone': (builtins.str | None, None),
        'spend': (_GetAccountFinancialsResponseBaseSpend | None, None),
        'credit': (_GetAccountFinancialsResponseBaseCredit | None, None),
        'balance': (_GetAccountFinancialsResponseBaseBalance | None, None),
        'payment_status': (Literal['current', 'past_due', 'suspended'] | None, None),
        'payment_terms': (Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay'] | None, None),
        'invoices': (builtins.list[_GetAccountFinancialsResponseBaseInvoicesItem] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'GetAdcpCapabilitiesRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'protocols': (builtins.list[Literal['media_buy', 'signals', 'governance', 'sponsored_intelligence', 'creative']] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetAdcpCapabilitiesResponseBase': {
        'adcp': (_GetAdcpCapabilitiesResponseBaseAdcp, ...),
        'supported_protocols': (builtins.list[Literal['media_buy', 'signals', 'governance', 'sponsored_intelligence', 'creative', 'brand']], ...),
        'account': (_GetAdcpCapabilitiesResponseBaseAccount | None, None),
        'media_buy': (_GetAdcpCapabilitiesResponseBaseMediaBuy | None, None),
        'signals': (_GetAdcpCapabilitiesResponseBaseSignals | None, None),
        'governance': (_GetAdcpCapabilitiesResponseBaseGovernance | None, None),
        'sponsored_intelligence': (_GetAdcpCapabilitiesResponseBaseSponsoredIntelligence | None, None),
        'brand': (_GetAdcpCapabilitiesResponseBaseBrand | None, None),
        'creative': (_GetAdcpCapabilitiesResponseBaseCreative | None, None),
        'request_signing': (_GetAdcpCapabilitiesResponseBaseRequestSigning | None, None),
        'webhook_signing': (_GetAdcpCapabilitiesResponseBaseWebhookSigning | None, None),
        'identity': (_GetAdcpCapabilitiesResponseBaseIdentity | None, None),
        'compliance_testing': (_GetAdcpCapabilitiesResponseBaseComplianceTesting | None, None),
        'specialisms': (builtins.list[Literal['audience-sync', 'brand-rights', 'collection-lists', 'content-standards', 'creative-ad-server', 'creative-generative', 'creative-template', 'governance-aware-seller', 'governance-delivery-monitor', 'governance-spend-authority', 'property-lists', 'sales-broadcast-tv', 'sales-catalog-driven', 'sales-guaranteed', 'sales-non-guaranteed', 'sales-proposal-mode', 'sales-social', 'signal-marketplace', 'signal-owned', 'signed-requests']] | None, None),
        'extensions_supported': (builtins.list[builtins.str] | None, None),
        'experimental_features': (builtins.list[builtins.str] | None, None),
        'last_updated': (builtins.str | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetBrandIdentityRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'brand_id': (builtins.str, ...),
        'fields': (builtins.list[Literal['description', 'industries', 'keller_type', 'logos', 'colors', 'fonts', 'visual_guidelines', 'tone', 'tagline', 'voice_synthesis', 'assets', 'rights']] | None, None),
        'use_case': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetBrandIdentityResponseBase': {
        'brand_id': (builtins.str | None, None),
        'house': (_GetBrandIdentityResponseBaseHouse | None, None),
        'names': (builtins.list[builtins.dict[builtins.str, builtins.str]] | None, None),
        'description': (builtins.str | None, None),
        'industries': (builtins.list[builtins.str] | None, None),
        'keller_type': (Literal['master', 'sub_brand', 'endorsed', 'independent'] | None, None),
        'logos': (builtins.list[_GetBrandIdentityResponseBaseLogosItem] | None, None),
        'colors': (_GetBrandIdentityResponseBaseColors | None, None),
        'fonts': (_GetBrandIdentityResponseBaseFonts | None, None),
        'visual_guidelines': (builtins.dict[builtins.str, Any] | None, None),
        'tone': (_GetBrandIdentityResponseBaseTone | None, None),
        'tagline': (builtins.str | builtins.list[builtins.dict[builtins.str, builtins.str]] | None, None),
        'voice_synthesis': (_GetBrandIdentityResponseBaseVoiceSynthesis | None, None),
        'assets': (builtins.list[_GetBrandIdentityResponseBaseAssetsItem] | None, None),
        'rights': (_GetBrandIdentityResponseBaseRights | None, None),
        'available_fields': (builtins.list[Literal['description', 'industries', 'keller_type', 'logos', 'colors', 'fonts', 'visual_guidelines', 'tone', 'tagline', 'voice_synthesis', 'assets', 'rights']] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'GetCollectionListRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'list_id': (builtins.str, ...),
        'account': (_GetCollectionListRequestBaseAccountVariant1 | _GetCollectionListRequestBaseAccountVariant2 | None, None),
        'resolve': (builtins.bool, True),
        'pagination': (_GetCollectionListRequestBasePagination | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetCollectionListResponseBase': {
        'list': (_ExternalCollectionCollectionList, ...),
        'collections': (builtins.list[_GetCollectionListResponseBaseCollectionsItem] | None, None),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'resolved_at': (builtins.str | None, None),
        'cache_valid_until': (builtins.str | None, None),
        'coverage_gaps': (builtins.dict[builtins.str, builtins.list[_GetCollectionListResponseBaseCoverageGapsValueItem]] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetContentStandardsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'standards_id': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetContentStandardsResponseBase': {
        'standards_id': (builtins.str | None, None),
        'name': (builtins.str | None, None),
        'countries_all': (builtins.list[builtins.str] | None, None),
        'channels_any': (builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']] | None, None),
        'languages_any': (builtins.list[builtins.str] | None, None),
        'policies': (builtins.list[_ExternalGovernancePolicyEntry] | None, None),
        'calibration_exemplars': (_GetContentStandardsResponseBaseCalibrationExemplars | None, None),
        'pricing_options': (builtins.list[_GetContentStandardsResponseBasePricingOptionsItemVariant1 | _GetContentStandardsResponseBasePricingOptionsItemVariant2 | _GetContentStandardsResponseBasePricingOptionsItemVariant3 | _GetContentStandardsResponseBasePricingOptionsItemVariant4 | _GetContentStandardsResponseBasePricingOptionsItemVariant5] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'GetCreativeDeliveryRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_GetCreativeDeliveryRequestBaseAccountVariant1 | _GetCreativeDeliveryRequestBaseAccountVariant2 | None, None),
        'media_buy_ids': (builtins.list[builtins.str] | None, None),
        'creative_ids': (builtins.list[builtins.str] | None, None),
        'start_date': (builtins.str | None, None),
        'end_date': (builtins.str | None, None),
        'max_variants': (builtins.int | None, None),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetCreativeDeliveryResponseBase': {
        'account_id': (builtins.str | None, None),
        'media_buy_id': (builtins.str | None, None),
        'currency': (builtins.str, ...),
        'reporting_period': (_GetCreativeDeliveryResponseBaseReportingPeriod, ...),
        'creatives': (builtins.list[_GetCreativeDeliveryResponseBaseCreativesItem], ...),
        'pagination': (_GetCreativeDeliveryResponseBasePagination | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetCreativeFeaturesRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'creative_manifest': (_ExternalCoreCreativeManifest, ...),
        'feature_ids': (builtins.list[builtins.str] | None, None),
        'account': (_GetCreativeFeaturesRequestBaseAccountVariant1 | _GetCreativeFeaturesRequestBaseAccountVariant2 | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetCreativeFeaturesResponseBase': {
        'results': (builtins.list[_ExternalCreativeCreativeFeatureResult] | None, None),
        'detail_url': (builtins.str | None, None),
        'pricing_option_id': (builtins.str | None, None),
        'vendor_cost': (builtins.float | None, None),
        'currency': (builtins.str | None, None),
        'consumption': (_ExternalCoreCreativeConsumption | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'GetMediaBuyArtifactsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_GetMediaBuyArtifactsRequestBaseAccountVariant1 | _GetMediaBuyArtifactsRequestBaseAccountVariant2 | None, None),
        'media_buy_id': (builtins.str, ...),
        'package_ids': (builtins.list[builtins.str] | None, None),
        'failures_only': (builtins.bool, False),
        'time_range': (_GetMediaBuyArtifactsRequestBaseTimeRange | None, None),
        'pagination': (_GetMediaBuyArtifactsRequestBasePagination | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetMediaBuyArtifactsResponseBase': {
        'media_buy_id': (builtins.str | None, None),
        'artifacts': (builtins.list[_GetMediaBuyArtifactsResponseBaseArtifactsItem] | None, None),
        'collection_info': (_GetMediaBuyArtifactsResponseBaseCollectionInfo | None, None),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'GetMediaBuyDeliveryRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_GetMediaBuyDeliveryRequestBaseAccountVariant1 | _GetMediaBuyDeliveryRequestBaseAccountVariant2 | None, None),
        'media_buy_ids': (builtins.list[builtins.str] | None, None),
        'status_filter': (Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | builtins.list[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']] | None, None),
        'start_date': (builtins.str | None, None),
        'end_date': (builtins.str | None, None),
        'include_package_daily_breakdown': (builtins.bool, False),
        'attribution_window': (_GetMediaBuyDeliveryRequestBaseAttributionWindow | None, None),
        'reporting_dimensions': (_GetMediaBuyDeliveryRequestBaseReportingDimensions | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetMediaBuyDeliveryResponseBase': {
        'notification_type': (Literal['scheduled', 'final', 'delayed', 'adjusted', 'window_update'] | None, None),
        'partial_data': (builtins.bool | None, None),
        'unavailable_count': (builtins.int | None, None),
        'sequence_number': (builtins.int | None, None),
        'next_expected_at': (builtins.str | None, None),
        'reporting_period': (_GetMediaBuyDeliveryResponseBaseReportingPeriod, ...),
        'currency': (builtins.str, ...),
        'attribution_window': (_ExternalCoreAttributionWindow | None, None),
        'aggregated_totals': (_GetMediaBuyDeliveryResponseBaseAggregatedTotals | None, None),
        'media_buy_deliveries': (builtins.list[_GetMediaBuyDeliveryResponseBaseMediaBuyDeliveriesItem], ...),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetMediaBuysRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_GetMediaBuysRequestBaseAccountVariant1 | _GetMediaBuysRequestBaseAccountVariant2 | None, None),
        'media_buy_ids': (builtins.list[builtins.str] | None, None),
        'status_filter': (Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | builtins.list[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']] | None, None),
        'include_snapshot': (builtins.bool, False),
        'include_history': (builtins.int, 0),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetMediaBuysResponseBase': {
        'media_buys': (builtins.list[_GetMediaBuysResponseBaseMediaBuysItem], ...),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetPlanAuditLogsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'plan_ids': (builtins.list[builtins.str] | None, None),
        'portfolio_plan_ids': (builtins.list[builtins.str] | None, None),
        'governance_contexts': (builtins.list[builtins.str] | None, None),
        'purchase_types': (builtins.list[Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']] | None, None),
        'include_entries': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetPlanAuditLogsResponseBase': {
        'plans': (builtins.list[_GetPlanAuditLogsResponseBasePlansItem], ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetProductsInputRequiredResponseBase': {
        'reason': (Literal['CLARIFICATION_NEEDED', 'BUDGET_REQUIRED'] | None, None),
        'partial_results': (builtins.list[_ExternalCoreProduct] | None, None),
        'suggestions': (builtins.list[builtins.str] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetProductsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'buying_mode': (Literal['brief', 'wholesale', 'refine'], ...),
        'brief': (builtins.str | None, None),
        'refine': (builtins.list[_GetProductsRequestBaseRefineItemVariant1 | _GetProductsRequestBaseRefineItemVariant2 | _GetProductsRequestBaseRefineItemVariant3] | None, None),
        'brand': (_ExternalCoreBrandRef | None, None),
        'catalog': (_ExternalCoreCatalog | None, None),
        'account': (_GetProductsRequestBaseAccountVariant1 | _GetProductsRequestBaseAccountVariant2 | None, None),
        'preferred_delivery_types': (builtins.list[Literal['guaranteed', 'non_guaranteed']] | None, None),
        'filters': (_ExternalCoreProductFilters | None, None),
        'property_list': (_ExternalCorePropertyListRef | None, None),
        'fields': (builtins.list[Literal['product_id', 'name', 'description', 'publisher_properties', 'channels', 'format_ids', 'placements', 'delivery_type', 'exclusivity', 'pricing_options', 'forecast', 'outcome_measurement', 'delivery_measurement', 'reporting_capabilities', 'creative_policy', 'catalog_types', 'metric_optimization', 'conversion_tracking', 'data_provider_signals', 'max_optimization_goals', 'catalog_match', 'collections', 'collection_targeting_allowed', 'installments', 'brief_relevance', 'expires_at', 'product_card', 'product_card_detailed', 'enforced_policies', 'trusted_match']] | None, None),
        'time_budget': (_GetProductsRequestBaseTimeBudget | None, None),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'required_policies': (builtins.list[builtins.str] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetProductsSubmittedResponseBase': {
        'estimated_completion': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetProductsResponseBase': {
        'products': (builtins.list[_ExternalCoreProduct], ...),
        'proposals': (builtins.list[_ExternalCoreProposal] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'property_list_applied': (builtins.bool | None, None),
        'catalog_applied': (builtins.bool | None, None),
        'refinement_applied': (builtins.list[_GetProductsResponseBaseRefinementAppliedItemVariant1 | _GetProductsResponseBaseRefinementAppliedItemVariant2 | _GetProductsResponseBaseRefinementAppliedItemVariant3] | None, None),
        'incomplete': (builtins.list[_GetProductsResponseBaseIncompleteItem] | None, None),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetProductsWorkingResponseBase': {
        'percentage': (builtins.float | None, None),
        'current_step': (builtins.str | None, None),
        'total_steps': (builtins.int | None, None),
        'step_number': (builtins.int | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetPropertyListRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'list_id': (builtins.str, ...),
        'account': (_GetPropertyListRequestBaseAccountVariant1 | _GetPropertyListRequestBaseAccountVariant2 | None, None),
        'resolve': (builtins.bool, True),
        'pagination': (_GetPropertyListRequestBasePagination | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetPropertyListResponseBase': {
        'list': (_ExternalPropertyPropertyList, ...),
        'identifiers': (builtins.list[_ExternalCoreIdentifier] | None, None),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'resolved_at': (builtins.str | None, None),
        'cache_valid_until': (builtins.str | None, None),
        'coverage_gaps': (builtins.dict[builtins.str, builtins.list[_ExternalCoreIdentifier]] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetRightsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'query': (builtins.str, ...),
        'uses': (builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']], ...),
        'buyer_brand': (_ExternalCoreBrandRef | None, None),
        'countries': (builtins.list[builtins.str] | None, None),
        'brand_id': (builtins.str | None, None),
        'right_type': (Literal['talent', 'character', 'brand_ip', 'music', 'stock_media'] | None, None),
        'include_excluded': (builtins.bool, False),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetRightsResponseBase': {
        'rights': (builtins.list[_GetRightsResponseBaseRightsItem] | None, None),
        'excluded': (builtins.list[_GetRightsResponseBaseExcludedItem] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'GetSignalsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_GetSignalsRequestBaseAccountVariant1 | _GetSignalsRequestBaseAccountVariant2 | None, None),
        'signal_spec': (builtins.str | None, None),
        'signal_ids': (builtins.list[_GetSignalsRequestBaseSignalIdsItemVariant1 | _GetSignalsRequestBaseSignalIdsItemVariant2] | None, None),
        'destinations': (builtins.list[_GetSignalsRequestBaseDestinationsItemVariant1 | _GetSignalsRequestBaseDestinationsItemVariant2] | None, None),
        'countries': (builtins.list[builtins.str] | None, None),
        'filters': (_ExternalCoreSignalFilters | None, None),
        'max_results': (builtins.int | None, None),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'GetSignalsResponseBase': {
        'signals': (builtins.list[_GetSignalsResponseBaseSignalsItem], ...),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'IdentityMatchRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        '$schema': (builtins.str | None, None),
        'type': (Literal['identity_match_request'], ...),
        'protocol_version': (builtins.str, '1.0'),
        'request_id': (builtins.str, ...),
        'seller_agent_url': (builtins.str, ...),
        'identities': (builtins.list[_IdentityMatchRequestBaseIdentitiesItem], ...),
        'consent': (_IdentityMatchRequestBaseConsent | None, None),
        'package_ids': (builtins.list[builtins.str] | None, None),
        'country': (builtins.str | None, None),
    },
    'IdentityMatchResponseBase': {
        'type': (Literal['identity_match_response'], ...),
        'request_id': (builtins.str, ...),
        'eligible_package_ids': (builtins.list[builtins.str], ...),
        'serve_window_sec': (builtins.int, ...),
        'tmpx': (builtins.str | None, None),
    },
    'ListAccountsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'status': (Literal['active', 'pending_approval', 'rejected', 'payment_required', 'suspended', 'closed'] | None, None),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListAccountsResponseBase': {
        'accounts': (builtins.list[_ExternalCoreAccount], ...),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListCollectionListsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_ListCollectionListsRequestBaseAccountVariant1 | _ListCollectionListsRequestBaseAccountVariant2 | None, None),
        'name_contains': (builtins.str | None, None),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListCollectionListsResponseBase': {
        'lists': (builtins.list[_ExternalCollectionCollectionList], ...),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListContentStandardsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'channels': (builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']] | None, None),
        'languages': (builtins.list[builtins.str] | None, None),
        'countries': (builtins.list[builtins.str] | None, None),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListContentStandardsResponseBase': {
        'standards': (builtins.list[_ExternalContentStandardsContentStandards] | None, None),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'ListCreativeFormatsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'format_ids': (builtins.list[_ExternalCoreFormatId] | None, None),
        'type': (Literal['audio', 'video', 'display', 'dooh'] | None, None),
        'asset_types': (builtins.list[Literal['image', 'video', 'audio', 'text', 'html', 'javascript', 'url']] | None, None),
        'max_width': (builtins.int | None, None),
        'max_height': (builtins.int | None, None),
        'min_width': (builtins.int | None, None),
        'min_height': (builtins.int | None, None),
        'is_responsive': (builtins.bool | None, None),
        'name_search': (builtins.str | None, None),
        'wcag_level': (Literal['A', 'AA', 'AAA'] | None, None),
        'disclosure_positions': (builtins.list[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']] | None, None),
        'disclosure_persistence': (builtins.list[Literal['continuous', 'initial', 'flexible']] | None, None),
        'output_format_ids': (builtins.list[_ExternalCoreFormatId] | None, None),
        'input_format_ids': (builtins.list[_ExternalCoreFormatId] | None, None),
        'include_pricing': (builtins.bool, False),
        'account': (_ListCreativeFormatsRequestBaseAccountVariant1 | _ListCreativeFormatsRequestBaseAccountVariant2 | None, None),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListCreativeFormatsResponseBase': {
        'formats': (builtins.list[_ExternalCoreFormat], ...),
        'creative_agents': (builtins.list[_ListCreativeFormatsResponseBaseCreativeAgentsItem] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListCreativesRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'filters': (_ExternalCoreCreativeFilters | None, None),
        'sort': (_ListCreativesRequestBaseSort | None, None),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'include_assignments': (builtins.bool, True),
        'include_snapshot': (builtins.bool, False),
        'include_items': (builtins.bool, False),
        'include_variables': (builtins.bool, False),
        'include_pricing': (builtins.bool, False),
        'account': (_ListCreativesRequestBaseAccountVariant1 | _ListCreativesRequestBaseAccountVariant2 | None, None),
        'fields': (builtins.list[Literal['creative_id', 'name', 'format_id', 'status', 'created_date', 'updated_date', 'tags', 'assignments', 'snapshot', 'items', 'variables', 'concept', 'pricing_options']] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListCreativesResponseBase': {
        'query_summary': (_ListCreativesResponseBaseQuerySummary, ...),
        'pagination': (_ExternalCorePaginationResponse, ...),
        'creatives': (builtins.list[_ListCreativesResponseBaseCreativesItem], ...),
        'format_summary': (builtins.dict[builtins.str, Any] | None, None),
        'status_summary': (_ListCreativesResponseBaseStatusSummary | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListPropertyListsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_ListPropertyListsRequestBaseAccountVariant1 | _ListPropertyListsRequestBaseAccountVariant2 | None, None),
        'name_contains': (builtins.str | None, None),
        'pagination': (_ExternalCorePaginationRequest | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ListPropertyListsResponseBase': {
        'lists': (builtins.list[_ExternalPropertyPropertyList], ...),
        'pagination': (_ExternalCorePaginationResponse | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'LogEventRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'event_source_id': (builtins.str, ...),
        'test_event_code': (builtins.str | None, None),
        'events': (builtins.list[_ExternalCoreEvent], ...),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'LogEventResponseBase': {
        'events_received': (builtins.int | None, None),
        'events_processed': (builtins.int | None, None),
        'partial_failures': (builtins.list[_LogEventResponseBasePartialFailuresItem] | None, None),
        'warnings': (builtins.list[builtins.str] | None, None),
        'match_quality': (builtins.float | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'PackageRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'product_id': (builtins.str, ...),
        'format_ids': (builtins.list[_ExternalCoreFormatId] | None, None),
        'budget': (builtins.float, ...),
        'pacing': (Literal['even', 'asap', 'front_loaded'] | None, None),
        'pricing_option_id': (builtins.str, ...),
        'bid_price': (builtins.float | None, None),
        'impressions': (builtins.float | None, None),
        'start_time': (builtins.str | None, None),
        'end_time': (builtins.str | None, None),
        'paused': (builtins.bool, False),
        'catalogs': (builtins.list[_ExternalCoreCatalog] | None, None),
        'optimization_goals': (builtins.list[_PackageRequestBaseOptimizationGoalsItemVariant1 | _PackageRequestBaseOptimizationGoalsItemVariant2] | None, None),
        'targeting_overlay': (_ExternalCoreTargeting | None, None),
        'measurement_terms': (_ExternalCoreMeasurementTerms | None, None),
        'performance_standards': (builtins.list[_ExternalCorePerformanceStandard] | None, None),
        'creative_assignments': (builtins.list[_ExternalCoreCreativeAssignment] | None, None),
        'creatives': (builtins.list[_ExternalCoreCreativeAsset] | None, None),
        'agency_estimate_number': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'PreviewCreativeRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'request_type': (Literal['single', 'batch', 'variant'], ...),
        'creative_manifest': (_ExternalCoreCreativeManifest | None, None),
        'format_id': (_ExternalCoreFormatId | None, None),
        'inputs': (builtins.list[_PreviewCreativeRequestBaseInputsItem] | None, None),
        'template_id': (builtins.str | None, None),
        'quality': (Literal['draft', 'production'] | None, None),
        'output_format': (Literal['url', 'html'], 'url'),
        'item_limit': (builtins.int | None, None),
        'requests': (builtins.list[_PreviewCreativeRequestBaseRequestsItem] | None, None),
        'variant_id': (builtins.str | None, None),
        'creative_id': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'PreviewCreativeResponseBase': {
        'response_type': (Literal['single'] | Literal['batch'] | Literal['variant'], ...),
        'previews': (builtins.list[_PreviewCreativeResponseBasePreviewsItem] | builtins.list[_PreviewCreativeResponseBasePreviewsItem2] | None, None),
        'interactive_url': (builtins.str | None, None),
        'expires_at': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'results': (builtins.list[_PreviewCreativeResponseBaseResultsItemVariant1 | _PreviewCreativeResponseBaseResultsItemVariant2] | None, None),
        'variant_id': (builtins.str | None, None),
        'creative_id': (builtins.str | None, None),
        'manifest': (_ExternalCoreCreativeManifest | None, None),
    },
    'ProvidePerformanceFeedbackRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'media_buy_id': (builtins.str, ...),
        'idempotency_key': (builtins.str, ...),
        'measurement_period': (_ExternalCoreDatetimeRange, ...),
        'performance_index': (builtins.float, ...),
        'package_id': (builtins.str | None, None),
        'creative_id': (builtins.str | None, None),
        'metric_type': (Literal['overall_performance', 'conversion_rate', 'brand_lift', 'click_through_rate', 'completion_rate', 'viewability', 'brand_safety', 'cost_efficiency'], 'overall_performance'),
        'feedback_source': (Literal['buyer_attribution', 'third_party_measurement', 'platform_analytics', 'verification_partner'], 'buyer_attribution'),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ProvidePerformanceFeedbackResponseBase': {
        'success': (Literal[True] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'ReportPlanOutcomeRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'plan_id': (builtins.str, ...),
        'check_id': (builtins.str | None, None),
        'idempotency_key': (builtins.str, ...),
        'purchase_type': (Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services'], 'media_buy'),
        'outcome': (Literal['completed', 'failed', 'delivery'], ...),
        'seller_response': (_ReportPlanOutcomeRequestBaseSellerResponse | None, None),
        'delivery': (_ReportPlanOutcomeRequestBaseDelivery | None, None),
        'error': (_ReportPlanOutcomeRequestBaseError | None, None),
        'governance_context': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ReportPlanOutcomeResponseBase': {
        'outcome_id': (builtins.str, ...),
        'status': (Literal['accepted', 'findings'], ...),
        'committed_budget': (builtins.float | None, None),
        'findings': (builtins.list[_ReportPlanOutcomeResponseBaseFindingsItem] | None, None),
        'plan_summary': (_ReportPlanOutcomeResponseBasePlanSummary | None, None),
        'replayed': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ReportUsageRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'idempotency_key': (builtins.str, ...),
        'reporting_period': (_ExternalCoreDatetimeRange, ...),
        'usage': (builtins.list[_ReportUsageRequestBaseUsageItem], ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ReportUsageResponseBase': {
        'accepted': (builtins.int, ...),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SiGetOfferingRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'offering_id': (builtins.str, ...),
        'intent': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'include_products': (builtins.bool, False),
        'product_limit': (builtins.int, 5),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SiGetOfferingResponseBase': {
        'available': (builtins.bool, ...),
        'offering_token': (builtins.str | None, None),
        'ttl_seconds': (builtins.int | None, None),
        'checked_at': (builtins.str | None, None),
        'offering': (_SiGetOfferingResponseBaseOffering | None, None),
        'matching_products': (builtins.list[_SiGetOfferingResponseBaseMatchingProductsItem] | None, None),
        'total_matching': (builtins.int | None, None),
        'unavailable_reason': (builtins.str | None, None),
        'alternative_offering_ids': (builtins.list[builtins.str] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SiInitiateSessionRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'intent': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'identity': (_ExternalSponsoredIntelligenceSiIdentity, ...),
        'media_buy_id': (builtins.str | None, None),
        'placement': (builtins.str | None, None),
        'offering_id': (builtins.str | None, None),
        'supported_capabilities': (_ExternalSponsoredIntelligenceSiCapabilities | None, None),
        'offering_token': (builtins.str | None, None),
        'idempotency_key': (builtins.str, ...),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SiInitiateSessionResponseBase': {
        'session_id': (builtins.str, ...),
        'response': (_SiInitiateSessionResponseBaseResponse | None, None),
        'negotiated_capabilities': (_ExternalSponsoredIntelligenceSiCapabilities | None, None),
        'session_status': (Literal['active', 'pending_handoff', 'complete', 'terminated'], ...),
        'session_ttl_seconds': (builtins.int | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SiSendMessageRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'idempotency_key': (builtins.str, ...),
        'session_id': (builtins.str, ...),
        'message': (builtins.str | None, None),
        'action_response': (_SiSendMessageRequestBaseActionResponse | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SiSendMessageResponseBase': {
        'session_id': (builtins.str, ...),
        'response': (_SiSendMessageResponseBaseResponse | None, None),
        'mcp_resource_uri': (builtins.str | None, None),
        'session_status': (Literal['active', 'pending_handoff', 'complete', 'terminated'], ...),
        'handoff': (_SiSendMessageResponseBaseHandoff | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SiTerminateSessionRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'session_id': (builtins.str, ...),
        'reason': (Literal['handoff_transaction', 'handoff_complete', 'user_exit', 'session_timeout', 'host_terminated'], ...),
        'termination_context': (_SiTerminateSessionRequestBaseTerminationContext | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SiTerminateSessionResponseBase': {
        'session_id': (builtins.str, ...),
        'terminated': (builtins.bool, ...),
        'session_status': (Literal['active', 'pending_handoff', 'complete', 'terminated'] | None, None),
        'acp_handoff': (_SiTerminateSessionResponseBaseAcpHandoff | None, None),
        'follow_up': (_SiTerminateSessionResponseBaseFollowUp | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncAccountsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'idempotency_key': (builtins.str, ...),
        'accounts': (builtins.list[_SyncAccountsRequestBaseAccountsItem], ...),
        'delete_missing': (builtins.bool, False),
        'dry_run': (builtins.bool, False),
        'push_notification_config': (_ExternalCorePushNotificationConfig | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncAccountsResponseBase': {
        'dry_run': (builtins.bool | None, None),
        'accounts': (builtins.list[_SyncAccountsResponseBaseAccountsItem] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'SyncAudiencesRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'idempotency_key': (builtins.str, ...),
        'account': (_SyncAudiencesRequestBaseAccountVariant1 | _SyncAudiencesRequestBaseAccountVariant2, ...),
        'audiences': (builtins.list[_SyncAudiencesRequestBaseAudiencesItem] | None, None),
        'delete_missing': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncAudiencesResponseBase': {
        'audiences': (builtins.list[_SyncAudiencesResponseBaseAudiencesItem] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'SyncCatalogsInputRequiredResponseBase': {
        'reason': (Literal['APPROVAL_REQUIRED', 'FEED_VALIDATION', 'ITEM_REVIEW', 'FEED_ACCESS'] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncCatalogsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'idempotency_key': (builtins.str, ...),
        'account': (_SyncCatalogsRequestBaseAccountVariant1 | _SyncCatalogsRequestBaseAccountVariant2, ...),
        'catalogs': (builtins.list[_ExternalCoreCatalog] | None, None),
        'catalog_ids': (builtins.list[builtins.str] | None, None),
        'delete_missing': (builtins.bool, False),
        'dry_run': (builtins.bool, False),
        'validation_mode': (Literal['strict', 'lenient'], 'strict'),
        'push_notification_config': (_ExternalCorePushNotificationConfig | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncCatalogsSubmittedResponseBase': {
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncCatalogsResponseBase': {
        'dry_run': (builtins.bool | None, None),
        'catalogs': (builtins.list[_SyncCatalogsResponseBaseCatalogsItem] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'SyncCatalogsWorkingResponseBase': {
        'percentage': (builtins.float | None, None),
        'current_step': (builtins.str | None, None),
        'total_steps': (builtins.int | None, None),
        'step_number': (builtins.int | None, None),
        'catalogs_processed': (builtins.int | None, None),
        'catalogs_total': (builtins.int | None, None),
        'items_processed': (builtins.int | None, None),
        'items_total': (builtins.int | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncCreativesInputRequiredResponseBase': {
        'reason': (Literal['APPROVAL_REQUIRED', 'ASSET_CONFIRMATION', 'FORMAT_CLARIFICATION'] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncCreativesRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_SyncCreativesRequestBaseAccountVariant1 | _SyncCreativesRequestBaseAccountVariant2, ...),
        'creatives': (builtins.list[_ExternalCoreCreativeAsset], ...),
        'creative_ids': (builtins.list[builtins.str] | None, None),
        'assignments': (builtins.list[_SyncCreativesRequestBaseAssignmentsItem] | None, None),
        'idempotency_key': (builtins.str, ...),
        'delete_missing': (builtins.bool, False),
        'dry_run': (builtins.bool, False),
        'validation_mode': (Literal['strict', 'lenient'], 'strict'),
        'push_notification_config': (_ExternalCorePushNotificationConfig | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncCreativesSubmittedResponseBase': {
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncCreativesResponseBase': {
        'dry_run': (builtins.bool | None, None),
        'creatives': (builtins.list[_SyncCreativesResponseBaseCreativesItem] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'status': (Literal['submitted'] | None, None),
        'task_id': (builtins.str | None, None),
        'message': (builtins.str | None, None),
    },
    'SyncCreativesWorkingResponseBase': {
        'percentage': (builtins.float | None, None),
        'current_step': (builtins.str | None, None),
        'total_steps': (builtins.int | None, None),
        'step_number': (builtins.int | None, None),
        'creatives_processed': (builtins.int | None, None),
        'creatives_total': (builtins.int | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncEventSourcesRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'idempotency_key': (builtins.str, ...),
        'account': (_SyncEventSourcesRequestBaseAccountVariant1 | _SyncEventSourcesRequestBaseAccountVariant2, ...),
        'event_sources': (builtins.list[_SyncEventSourcesRequestBaseEventSourcesItem] | None, None),
        'delete_missing': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncEventSourcesResponseBase': {
        'event_sources': (builtins.list[_SyncEventSourcesResponseBaseEventSourcesItem] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'SyncGovernanceRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'idempotency_key': (builtins.str, ...),
        'accounts': (builtins.list[_SyncGovernanceRequestBaseAccountsItem], ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncGovernanceResponseBase': {
        'accounts': (builtins.list[_SyncGovernanceResponseBaseAccountsItem] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'SyncPlansRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'idempotency_key': (builtins.str, ...),
        'plans': (builtins.list[_SyncPlansRequestBasePlansItem], ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'SyncPlansResponseBase': {
        'plans': (builtins.list[_SyncPlansResponseBasePlansItem], ...),
        'replayed': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'TasksGetRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'task_id': (builtins.str, ...),
        'include_history': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'TasksGetResponseBase': {
        'task_id': (builtins.str, ...),
        'task_type': (Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights'], ...),
        'protocol': (Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence'], ...),
        'status': (Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown'], ...),
        'created_at': (builtins.str, ...),
        'updated_at': (builtins.str, ...),
        'completed_at': (builtins.str | None, None),
        'has_webhook': (builtins.bool | None, None),
        'progress': (_TasksGetResponseBaseProgress | None, None),
        'error': (_TasksGetResponseBaseError | None, None),
        'history': (builtins.list[_TasksGetResponseBaseHistoryItem] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'TasksListRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'filters': (_TasksListRequestBaseFilters | None, None),
        'sort': (_TasksListRequestBaseSort | None, None),
        'pagination': (_TasksListRequestBasePagination | None, None),
        'include_history': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'TasksListResponseBase': {
        'query_summary': (_TasksListResponseBaseQuerySummary, ...),
        'tasks': (builtins.list[_TasksListResponseBaseTasksItem], ...),
        'pagination': (_TasksListResponseBasePagination, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'UpdateCollectionListRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'list_id': (builtins.str, ...),
        'account': (_UpdateCollectionListRequestBaseAccountVariant1 | _UpdateCollectionListRequestBaseAccountVariant2 | None, None),
        'name': (builtins.str | None, None),
        'description': (builtins.str | None, None),
        'base_collections': (builtins.list[_UpdateCollectionListRequestBaseBaseCollectionsItemVariant1 | _UpdateCollectionListRequestBaseBaseCollectionsItemVariant2 | _UpdateCollectionListRequestBaseBaseCollectionsItemVariant3] | None, None),
        'filters': (_ExternalCollectionCollectionListFilters | None, None),
        'brand': (_ExternalCoreBrandRef | None, None),
        'webhook_url': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'idempotency_key': (builtins.str, ...),
    },
    'UpdateCollectionListResponseBase': {
        'list': (_ExternalCollectionCollectionList, ...),
        'replayed': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'UpdateContentStandardsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'standards_id': (builtins.str, ...),
        'scope': (_UpdateContentStandardsRequestBaseScope | None, None),
        'registry_policy_ids': (builtins.list[builtins.str] | None, None),
        'policies': (builtins.list[_ExternalGovernancePolicyEntry] | None, None),
        'calibration_exemplars': (_UpdateContentStandardsRequestBaseCalibrationExemplars | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'idempotency_key': (builtins.str, ...),
    },
    'UpdateContentStandardsResponseBase': {
        'success': (Literal[True] | Literal[False], ...),
        'standards_id': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
        'conflicting_standards_id': (builtins.str | None, None),
    },
    'UpdateMediaBuyInputRequiredResponseBase': {
        'reason': (Literal['APPROVAL_REQUIRED', 'CHANGE_CONFIRMATION'] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'UpdateMediaBuyRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'account': (_UpdateMediaBuyRequestBaseAccountVariant1 | _UpdateMediaBuyRequestBaseAccountVariant2, ...),
        'media_buy_id': (builtins.str, ...),
        'revision': (builtins.int | None, None),
        'paused': (builtins.bool | None, None),
        'canceled': (Literal[True] | None, None),
        'cancellation_reason': (builtins.str | None, None),
        'start_time': (Literal['asap'] | builtins.str | None, None),
        'end_time': (builtins.str | None, None),
        'packages': (builtins.list[_ExternalMediaBuyPackageUpdate] | None, None),
        'invoice_recipient': (_ExternalCoreBusinessEntity | None, None),
        'new_packages': (builtins.list[_ExternalMediaBuyPackageRequest] | None, None),
        'reporting_webhook': (_ExternalCoreReportingWebhook | None, None),
        'push_notification_config': (_ExternalCorePushNotificationConfig | None, None),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'UpdateMediaBuySubmittedResponseBase': {
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'UpdateMediaBuyResponseBase': {
        'media_buy_id': (builtins.str | None, None),
        'status': (Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | None, None),
        'revision': (builtins.int | None, None),
        'implementation_date': (builtins.str | None, None),
        'invoice_recipient': (_ExternalCoreBusinessEntity | None, None),
        'affected_packages': (builtins.list[_ExternalCorePackage] | None, None),
        'valid_actions': (builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']] | None, None),
        'sandbox': (builtins.bool | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'UpdateMediaBuyWorkingResponseBase': {
        'percentage': (builtins.float | None, None),
        'current_step': (builtins.str | None, None),
        'total_steps': (builtins.int | None, None),
        'step_number': (builtins.int | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'UpdatePropertyListRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'list_id': (builtins.str, ...),
        'account': (_UpdatePropertyListRequestBaseAccountVariant1 | _UpdatePropertyListRequestBaseAccountVariant2 | None, None),
        'name': (builtins.str | None, None),
        'description': (builtins.str | None, None),
        'base_properties': (builtins.list[_UpdatePropertyListRequestBaseBasePropertiesItemVariant1 | _UpdatePropertyListRequestBaseBasePropertiesItemVariant2 | _UpdatePropertyListRequestBaseBasePropertiesItemVariant3] | None, None),
        'filters': (_ExternalPropertyPropertyListFilters | None, None),
        'brand': (_ExternalCoreBrandRef | None, None),
        'webhook_url': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'idempotency_key': (builtins.str, ...),
    },
    'UpdatePropertyListResponseBase': {
        'list': (_ExternalPropertyPropertyList, ...),
        'replayed': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'UpdateRightsRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'rights_id': (builtins.str, ...),
        'end_date': (builtins.str | None, None),
        'impression_cap': (builtins.int | None, None),
        'pricing_option_id': (builtins.str | None, None),
        'paused': (builtins.bool | None, None),
        'push_notification_config': (_ExternalCorePushNotificationConfig | None, None),
        'idempotency_key': (builtins.str, ...),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'UpdateRightsResponseBase': {
        'rights_id': (builtins.str | None, None),
        'terms': (_ExternalBrandRightsTerms | None, None),
        'generation_credentials': (builtins.list[_ExternalCoreGenerationCredential] | None, None),
        'rights_constraint': (_ExternalCoreRightsConstraint | None, None),
        'paused': (builtins.bool | None, None),
        'implementation_date': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'ValidateContentDeliveryRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'standards_id': (builtins.str, ...),
        'records': (builtins.list[_ValidateContentDeliveryRequestBaseRecordsItem], ...),
        'feature_ids': (builtins.list[builtins.str] | None, None),
        'include_passed': (builtins.bool, True),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ValidateContentDeliveryResponseBase': {
        'summary': (_ValidateContentDeliveryResponseBaseSummary | None, None),
        'results': (builtins.list[_ValidateContentDeliveryResponseBaseResultsItem] | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
        'errors': (builtins.list[_ExternalCoreError] | None, None),
    },
    'ValidatePropertyDeliveryRequestBase': {
        'adcp_major_version': (builtins.int | None, None),
        'list_id': (builtins.str, ...),
        'account': (_ValidatePropertyDeliveryRequestBaseAccountVariant1 | _ValidatePropertyDeliveryRequestBaseAccountVariant2 | None, None),
        'records': (builtins.list[_ExternalPropertyDeliveryRecord], ...),
        'include_compliant': (builtins.bool, False),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
    'ValidatePropertyDeliveryResponseBase': {
        'compliant': (builtins.bool | None, None),
        'list_id': (builtins.str, ...),
        'summary': (_ValidatePropertyDeliveryResponseBaseSummary, ...),
        'aggregate': (_ValidatePropertyDeliveryResponseBaseAggregate | None, None),
        'authorization_summary': (_ValidatePropertyDeliveryResponseBaseAuthorizationSummary | None, None),
        'results': (builtins.list[_ExternalPropertyValidationResult], ...),
        'validated_at': (builtins.str, ...),
        'list_resolved_at': (builtins.str | None, None),
        'context': (builtins.dict[builtins.str, Any] | None, None),
        'ext': (builtins.dict[builtins.str, Any] | None, None),
    },
}
