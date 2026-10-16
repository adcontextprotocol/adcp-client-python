"""Types the AdCP ``core`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.core import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.core.<schema>``.
Nothing here is renamed.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-13 11:29:51 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.domains.core.acceptance_policy_profile_ids import (
    AcceptancePolicyProfileId,
    AcceptancePolicyProfileIds,
)
from adcp.types.domains.core.account import (
    Account,
    Compression,
    CreditLimit,
    GovernanceAgent,
    ReportingBucket,
)
from adcp.types.domains.core.account_authorization import (
    AccountAuthorization,
    AllowedTask,
    ScopeName,
)
from adcp.types.domains.core.account_change import AccountChange, Actor, Repair, UnavailableReason
from adcp.types.domains.core.account_change_recorded_webhook import AccountChangeRecordedWebhook
from adcp.types.domains.core.account_identity_change import (
    AccountIdentityChange,
    AccountIdentityChange1,
    AccountIdentityChange2,
)
from adcp.types.domains.core.account_identity_change_preview import (
    AccountIdentityChangePreview,
    AccountIdentityChangePreview1,
    AccountIdentityChangePreview2,
    AccountIdentityChangePreview3,
    BlockedImpacts,
    Blocker,
    Effect1,
    Impact,
    NonblockingImpact,
    NonblockingImpacts,
)
from adcp.types.domains.core.account_ref import (
    AccountReference,
    AccountReference1,
    AccountReference2,
)
from adcp.types.domains.core.account_status_changed_webhook import AccountStatusChangedWebhook
from adcp.types.domains.core.account_timezone_capability import (
    AccountSelection,
    AccountTimezoneCapability,
    SupportedTimezone,
)
from adcp.types.domains.core.account_with_authorization import AccountWithAuthorization
from adcp.types.domains.core.activation_key import ActivationKey, ActivationKey1, ActivationKey2
from adcp.types.domains.core.ad_inventory_config import AdInventoryConfiguration
from adcp.types.domains.core.agent_encryption_key import AgentEncryptionKey
from adcp.types.domains.core.agent_notification_config import AgentNotificationConfig
from adcp.types.domains.core.agent_notification_config_state import AgentNotificationConfigState
from adcp.types.domains.core.agent_reporting_destination import (
    AcceptedFormat,
    AgentReportingDestination,
    AgentReportingDestination1,
    AgentReportingDestination2,
    AgentReportingDestination3,
)
from adcp.types.domains.core.agent_reporting_destination_state import (
    AgentReportingDestinationState,
    PriorDestinationRef,
)
from adcp.types.domains.core.agent_signing_key import AgentSigningKey
from adcp.types.domains.core.agent_webhook_challenge import AgentWebhookChallenge
from adcp.types.domains.core.app_item import AppItem, Platform
from adcp.types.domains.core.applicable_package_id import ApplicablePackageId
from adcp.types.domains.core.asset_group_vocabulary import AdcpAssetGroupVocabularyRegistry
from adcp.types.domains.core.assets.asset_union import (
    AssetVariant,
    AudioChannelLayout,
    C2paWatermarkAction,
    CatalogType,
    ContentIdType,
    DaastAsset1,
    DaastAsset2,
    DaastTrackingEvent,
    DaastVersion,
    DigitalSourceType,
    DisclosurePersistence,
    DisclosurePosition,
    DisplayTagAsset1,
    DisplayTagAsset2,
    DisplayTagAsset3,
    EmbeddedProvenanceMethod,
    Ext,
    FeedFormat,
    FieldModel,
    FrameRateType,
    GopType,
    HttpMethod,
    JavascriptModuleType,
    Jurisdiction2,
    Location1,
    Location2,
    Location4,
    Location5,
    Location6,
    Location8,
    Location9,
    MacroBearingUrl1,
    MacroBearingUrl2,
    MacroDeclaration1,
    MacroDeclaration10,
    MacroDeclaration2,
    MacroDeclaration3,
    MacroDeclaration4,
    MacroDeclaration5,
    MacroDeclaration6,
    MacroDeclaration7,
    MacroDeclaration8,
    MacroDeclaration9,
    MacroDeclarationModel,
    MacroDialect,
    MacroMappingStatus,
    MacroProcessingOperation,
    MacroResolver,
    MacroValueContext,
    MarkdownFlavor,
    MoovAtomPosition,
    PixelTrackingEvent,
    PlatformExtensionRef,
    PublishedPostAsset1,
    PublishedPostAsset2,
    ReferenceAuthorization1,
    Role2,
    ScanType,
    Target1,
    UniversalMacro,
    UpdateFrequency,
    UrlAssetType,
    VastAsset1,
    VastAsset2,
    VastTrackingEvent,
    VastVersion,
    VerifyAgent1,
    WatermarkMediaType,
    WebhookResponseType,
    WebhookSecurityMethod,
)
from adcp.types.domains.core.assets.audio_asset import BitDepth
from adcp.types.domains.core.assets.daast_asset import (
    DaastAsset3,
    DaastAsset4,
    Location13,
    MacroDeclaration12,
)
from adcp.types.domains.core.assets.display_tag_asset import (
    DisplayTagAsset4,
    DisplayTagAsset5,
    DisplayTagAsset6,
    Location17,
    Location18,
    MacroDeclaration15,
    MacroDeclaration16,
)
from adcp.types.domains.core.assets.vast_asset import (
    Location26,
    MacroDeclaration20,
    VastAsset3,
    VastAsset4,
)
from adcp.types.domains.core.async_response_data import AdcpAsyncResponseData
from adcp.types.domains.core.async_response_refs.creative.get_creative_features_async_response_submitted import (
    GetCreativeFeaturesSubmitted,
)
from adcp.types.domains.core.async_response_refs.creative.sync_creatives_async_response_input_required import (
    SyncCreativesInputRequired,
)
from adcp.types.domains.core.async_response_refs.creative.sync_creatives_async_response_submitted import (
    SyncCreativesSubmitted,
)
from adcp.types.domains.core.async_response_refs.creative.sync_creatives_async_response_working import (
    SyncCreativesWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.accept_proposal_async_response_input_required import (
    AcceptProposalInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.accept_proposal_async_response_submitted import (
    AcceptProposalSubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.accept_proposal_async_response_working import (
    AcceptProposalWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.build_creative_async_response_input_required import (
    BuildCreativeInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.build_creative_async_response_submitted import (
    BuildCreativeSubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.build_creative_async_response_working import (
    BuildCreativeWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.buy_products_async_response_input_required import (
    BuyProductsInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.buy_products_async_response_submitted import (
    BuyProductsSubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.buy_products_async_response_working import (
    BuyProductsWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.control_media_buy_async_response_input_required import (
    ControlMediaBuyInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.control_media_buy_async_response_submitted import (
    ControlMediaBuySubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.control_media_buy_async_response_working import (
    ControlMediaBuyWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.create_media_buy_async_response_input_required import (
    CreateMediaBuyInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.create_media_buy_async_response_submitted import (
    CreateMediaBuySubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.create_media_buy_async_response_working import (
    CreateMediaBuyWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.decline_proposals_async_response_input_required import (
    DeclineProposalsInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.decline_proposals_async_response_submitted import (
    DeclineProposalsSubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.decline_proposals_async_response_working import (
    DeclineProposalsWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.get_products_async_response_input_required import (
    GetProductsInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.get_products_async_response_submitted import (
    GetProductsSubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.get_products_async_response_working import (
    GetProductsWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.refine_proposals_async_response_input_required import (
    RefineProposalsInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.refine_proposals_async_response_submitted import (
    RefineProposalsSubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.refine_proposals_async_response_working import (
    RefineProposalsWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.request_proposals_async_response_input_required import (
    RequestProposalsInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.request_proposals_async_response_submitted import (
    RequestProposalsSubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.request_proposals_async_response_working import (
    RequestProposalsWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.sync_catalogs_async_response_input_required import (
    SyncCatalogsInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.sync_catalogs_async_response_submitted import (
    SyncCatalogsSubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.sync_catalogs_async_response_working import (
    SyncCatalogsWorking,
)
from adcp.types.domains.core.async_response_refs.media_buy.update_media_buy_async_response_input_required import (
    UpdateMediaBuyInputRequired,
)
from adcp.types.domains.core.async_response_refs.media_buy.update_media_buy_async_response_submitted import (
    UpdateMediaBuySubmitted,
)
from adcp.types.domains.core.async_response_refs.media_buy.update_media_buy_async_response_working import (
    UpdateMediaBuyWorking,
)
from adcp.types.domains.core.async_response_refs.signals.get_signals_async_response_submitted import (
    GetSignalsSubmitted,
)
from adcp.types.domains.core.async_response_refs.signals.get_signals_async_response_working import (
    GetSignalsWorking,
)
from adcp.types.domains.core.attestation_capabilities import (
    AcceptedIssuer,
    AttestationCapabilities,
    CredentialOrigin,
    Resolver,
    SupportedDeliveryMethod,
)
from adcp.types.domains.core.attestation_evaluation import Outcome
from adcp.types.domains.core.attestation_issuer import (
    AttestationIssuer,
    AttestationIssuer1,
    AttestationIssuer2,
    AttestationIssuer3,
)
from adcp.types.domains.core.attestation_reference import (
    AttestationReference,
    EmbeddedCredential,
    Locator,
    Locator1,
    ValidityHint,
)
from adcp.types.domains.core.attestation_subject import (
    AttestationSubject,
    AttestationSubject1,
    AttestationSubject2,
    AttestationSubject3,
)
from adcp.types.domains.core.attribution_window import AttributionWindow
from adcp.types.domains.core.audience_activation_method import (
    AudienceActivationMethod,
    AudienceActivationMethod1,
    AudienceActivationMethod2,
    AudienceActivationMethod3,
    AudienceActivationMethod4,
    AudienceActivationMethod5,
    AudienceActivationMethod6,
    Cloud,
    ConsumerIdentity,
)
from adcp.types.domains.core.audience_characteristic import AudienceCharacteristic
from adcp.types.domains.core.audience_evidence import (
    AudienceEvidence,
    Subject11,
    Subject12,
    Subject13,
    Subject14,
    Subject15,
    Subject16,
    Subject17,
    Subject2,
    Subject21,
    Subject22,
    Subject23,
    Subject24,
    Subject25,
    Subject26,
    Subject27,
    Subject3,
    Subject31,
    Subject32,
    Subject33,
    Subject34,
    Subject35,
    Subject36,
    Subject37,
)
from adcp.types.domains.core.audience_evidence_pin import AudienceEvidencePin
from adcp.types.domains.core.audience_evidence_requirements import AudienceEvidenceRequirements
from adcp.types.domains.core.audience_evidence_selection import AudienceEvidenceSelection
from adcp.types.domains.core.audience_member import AudienceMember
from adcp.types.domains.core.audience_selector import (
    AudienceSelector,
    AudienceSelector1,
    AudienceSelector2,
    AudienceSelector3,
    AudienceSelector4,
)
from adcp.types.domains.core.audience_source import AudienceSource, AudienceSource1, AudienceSource2
from adcp.types.domains.core.authorized_agent_base import AuthorizedAgentBaseFields
from adcp.types.domains.core.bidding_policy import BiddingPolicy, CostPer, Roas, Strength, Strength1
from adcp.types.domains.core.bidding_policy_capability import (
    BiddingPolicyCapability,
    CostPerStrength,
    MaxBidWithCostPer,
    MaxBidWithRoas,
    PolicyProfile,
    RoasStrength,
    ScopeCapability,
)
from adcp.types.domains.core.brand_id import BrandId
from adcp.types.domains.core.brand_key import BrandKey
from adcp.types.domains.core.brand_ref import (
    BrandKitOverride,
    BrandReference,
    Colors,
    DataSubjectContestation,
)
from adcp.types.domains.core.brand_response_authorization_result import (
    BrandResponseAuthorizationResult,
    BrandResponseAuthorizationResult1,
    BrandResponseAuthorizationResult2,
    Trust,
)
from adcp.types.domains.core.budget_allocation import (
    BudgetAllocation,
    BudgetAllocation1,
    BudgetAllocation2,
    OptimizationGoal1,
    OptimizationGoal2,
    OptimizationGoal3,
    OptimizationGoal4,
    OptimizationGoal5,
    OptimizationGoal6,
    OptimizationGoal7,
    Target3,
    Target4,
    Target5,
    Target6,
    Target7,
    Target8,
)
from adcp.types.domains.core.business_entity import Bank, BusinessEntity, Contact
from adcp.types.domains.core.cancellation_policy import CancellationFee, CancellationPolicy
from adcp.types.domains.core.canonical_account_ref import (
    CanonicalAccountReference,
    CanonicalAccountReference1,
    CanonicalAccountReference2,
)
from adcp.types.domains.core.canonical_audience_evidence import (
    AttestationDigest,
    CanonicalAudienceEvidence,
)
from adcp.types.domains.core.canonical_audience_evidence_selection import (
    CanonicalAudienceEvidenceSelection,
    VerifiedAttestationDigest,
)
from adcp.types.domains.core.canonical_budget_allocation import (
    CanonicalBudgetAllocation,
    CanonicalBudgetAllocation1,
    CanonicalBudgetAllocation2,
)
from adcp.types.domains.core.canonical_delivery_forecast import CanonicalDeliveryForecast
from adcp.types.domains.core.canonical_forecast_point import CanonicalForecastPoint
from adcp.types.domains.core.canonical_forecast_vendor_metric_value import (
    CanonicalForecastVendorMetricValue,
)
from adcp.types.domains.core.canonical_format_kind import CanonicalFormatKind
from adcp.types.domains.core.canonical_format_option import CanonicalFormatOption, FormatKind
from adcp.types.domains.core.canonical_measurement_terms import CanonicalMeasurementTerms
from adcp.types.domains.core.canonical_media_buy_action import (
    Action3,
    Action4,
    CanonicalMediaBuyAction,
    CanonicalMediaBuyAction1,
    CanonicalMediaBuyAction2,
    CanonicalMediaBuyAction3,
)
from adcp.types.domains.core.canonical_media_buy_action_fields import CanonicalMediaBuyActionFields
from adcp.types.domains.core.canonical_media_buy_features import CanonicalMediaBuyFeatures
from adcp.types.domains.core.canonical_metric_qualifier import CanonicalMetricQualifier
from adcp.types.domains.core.canonical_optimization_goal import (
    CanonicalOptimizationGoal,
    CanonicalOptimizationGoal1,
    CanonicalOptimizationGoal2,
    CanonicalOptimizationGoal3,
    Target10,
    Target11,
)
from adcp.types.domains.core.canonical_performance_standard import CanonicalPerformanceStandard
from adcp.types.domains.core.canonical_placement import (
    CanonicalDoohPlacementAttributes,
    CanonicalDoohScreenResolution,
    CanonicalProductPlacement,
    CanonicalProductPlacement1,
    CanonicalProductPlacement2,
)
from adcp.types.domains.core.canonical_pricing_option import CanonicalPricingOption, PricingModel
from adcp.types.domains.core.canonical_product import (
    CanonicalProduct,
    PublisherProperty1,
    PublisherProperty2,
    PublisherProperty3,
    PublisherProperty4,
    PublisherProperty5,
    PublisherProperty6,
    PublisherProperty7,
)
from adcp.types.domains.core.canonical_product_action import CanonicalProductAction
from adcp.types.domains.core.canonical_projection_ref import (
    AssetSource,
    CanonicalProjectionReference,
)
from adcp.types.domains.core.canonical_projection_slot_override import (
    CanonicalProjectionSlotOverride,
)
from adcp.types.domains.core.canonical_proposal import CanonicalProposal, ProposalKind
from adcp.types.domains.core.canonical_reporting_capabilities import CanonicalReportingCapabilities
from adcp.types.domains.core.canonical_reporting_commitment import (
    CanonicalReportingCommitment,
    CanonicalReportingCommitment1,
    CanonicalReportingCommitment2,
)
from adcp.types.domains.core.canvas_constraint import CanvasConstraint, Constraint
from adcp.types.domains.core.capabilities_changed_webhook import CapabilitiesChangedWebhook
from adcp.types.domains.core.catalog_item_availability_error import CatalogItemAvailabilityError
from adcp.types.domains.core.catalog_item_availability_ref import CatalogItemAvailabilityReference
from adcp.types.domains.core.catalog_item_availability_state import CatalogItemAvailabilityState
from adcp.types.domains.core.catalog_item_availability_update import CatalogItemAvailabilityUpdate
from adcp.types.domains.core.catalog_item_availability_update_result import (
    CatalogItemAvailabilityUpdateResult,
)
from adcp.types.domains.core.catalog_item_delivery_metrics import CatalogItemDeliveryMetrics
from adcp.types.domains.core.catalog_item_reference_not_found_error import (
    CatalogItemReferenceNotFoundError,
)
from adcp.types.domains.core.catalog_selection import CatalogSelection
from adcp.types.domains.core.catchment import Catchment
from adcp.types.domains.core.collection import Collection, RelatedCollection
from adcp.types.domains.core.collection_delivery_metrics import CollectionDeliveryMetrics
from adcp.types.domains.core.collection_distribution import CollectionDistribution
from adcp.types.domains.core.collection_list_ref import CollectionListReference
from adcp.types.domains.core.collection_property_delivery_metrics import (
    CollectionPropertyDeliveryMetrics,
)
from adcp.types.domains.core.collection_ref import CollectionReference
from adcp.types.domains.core.collection_selection import (
    CollectionSelection,
    CollectionSelection1,
    CollectionSelection2,
)
from adcp.types.domains.core.collection_selector import CollectionSelector
from adcp.types.domains.core.committed_metric import (
    CommittedMetric,
    CommittedMetric1,
    CommittedMetric2,
    Qualifier1,
    QualifierModel,
)
from adcp.types.domains.core.compact_task_input_required import CompactTaskInputRequired
from adcp.types.domains.core.compact_task_submitted import CompactTaskSubmitted
from adcp.types.domains.core.compact_task_working import CompactTaskWorking
from adcp.types.domains.core.content_rating import ContentRating
from adcp.types.domains.core.context import ContextObject
from adcp.types.domains.core.creative_approval_scope import ScopedCreativeApproval
from adcp.types.domains.core.creative_asset import CreativeAsset, Input
from adcp.types.domains.core.creative_assets import CreativeAssets, CreativeAssets1
from adcp.types.domains.core.creative_assignment import CreativeAssignment, RotationMode
from adcp.types.domains.core.creative_consumption import CreativeConsumption
from adcp.types.domains.core.creative_delivery_metrics import CreativeDeliveryMetrics
from adcp.types.domains.core.creative_filters import CreativeFilters
from adcp.types.domains.core.creative_item import CreativeItem, CreativeItem1, CreativeItem2
from adcp.types.domains.core.creative_locale_policy import CreativeLocalePolicy
from adcp.types.domains.core.creative_localization import CreativeLocalization, TargetVariant
from adcp.types.domains.core.creative_localization_readback import (
    CreativeLocalizationReadback,
    ResolvedAssets,
    ResolvedAssets1,
    Variants,
    Variants1,
    Variants2,
)
from adcp.types.domains.core.creative_manifest import CreativeManifest
from adcp.types.domains.core.creative_operation_format_declaration import (
    CreativeOperationFormatDeclaration,
)
from adcp.types.domains.core.creative_policy import CreativePolicy, ProvenanceRequirements
from adcp.types.domains.core.creative_representation import CreativeRepresentation
from adcp.types.domains.core.creative_representation_set import CreativeRepresentationSet
from adcp.types.domains.core.creative_revision_id import CreativeRevisionId
from adcp.types.domains.core.creative_variable import CreativeVariable, VariableType
from adcp.types.domains.core.creative_variant import Artifact, CreativeVariant, GenerationContext
from adcp.types.domains.core.daast_tracker_constraints import (
    DaastEvent,
    DaastOffset,
    DaastTarget,
    DaastTrackerConstraints,
    DaastVersions,
)
from adcp.types.domains.core.data_provider_signal_selector import (
    DataProviderSignalSelector,
    DataProviderSignalSelector1,
    DataProviderSignalSelector2,
    DataProviderSignalSelector3,
    SignalTag,
)
from adcp.types.domains.core.date_range import DateRange
from adcp.types.domains.core.datetime_range import DatetimeRange
from adcp.types.domains.core.daypart_target import DaypartTarget
from adcp.types.domains.core.deadline_policy import DeadlinePolicy, MaterialStage
from adcp.types.domains.core.delivery_breakdown_controls import DeliveryBreakdownControls
from adcp.types.domains.core.delivery_forecast import DeliveryForecast
from adcp.types.domains.core.delivery_metric_aggregate import (
    DeliveryMetricAggregate,
    DeliveryMetricAggregate1,
    DeliveryMetricAggregate2,
    Field0,
    Qualifier3,
)
from adcp.types.domains.core.delivery_metrics import (
    ByActionSourceItem,
    ByEventTypeItem,
    DeliveryMetrics,
    DoohMetrics,
    DoohMetrics1,
    EstimationBasis,
    OohMetrics,
    Panel,
    Posting,
    QuartileData,
    ReachWindow,
    TimeBasedView,
    VenueBreakdownItem,
    Viewability1,
    ViewedSecondsHistogramItem,
    ViewedSecondsPercentiles,
)
from adcp.types.domains.core.delivery_provider import DeliveryProvider
from adcp.types.domains.core.delivery_recipient import DeliveryRecipient
from adcp.types.domains.core.demographic_age_range import DemographicAgeRange
from adcp.types.domains.core.demographic_predicate import DemographicPredicate
from adcp.types.domains.core.demographic_reporting_capability import (
    DemographicReportingCapability,
    ReportingMode,
)
from adcp.types.domains.core.demographic_targeting_capability import (
    DemographicTargetingCapability,
    ExecutionMode,
    UnknownHandling,
)
from adcp.types.domains.core.demographic_targeting_intent import DemographicTargetingIntent
from adcp.types.domains.core.demographic_targeting_resolution import (
    DemographicTargetingResolution,
    Execution,
    Execution1,
    Execution2,
    IntervalId,
)
from adcp.types.domains.core.deployment import Deployment, Deployment1, Deployment2
from adcp.types.domains.core.destination import Destination1, Destination2
from adcp.types.domains.core.destination_item import DestinationItem, DestinationType
from adcp.types.domains.core.diagnostic_issue import DiagnosticIssue, Severity
from adcp.types.domains.core.downstream_connection_requirement import (
    ConnectionType,
    DownstreamConnectionRequirement,
    ResourceRef,
)
from adcp.types.domains.core.duration import Duration
from adcp.types.domains.core.education_item import DegreeType, EducationItem, Level, Modality
from adcp.types.domains.core.error import BuyerReason, DiscriminatorItem, Issue, Recovery
from adcp.types.domains.core.evaluator_spec import (
    EvalBudget,
    EvaluatorSpec,
    EvaluatorSpec1,
    EvaluatorSpec2,
    EvaluatorSpec3,
    Exemplars,
    FeatureAgent,
    RankByItem,
    RankByItem1,
    RankByItem2,
)
from adcp.types.domains.core.event import Event
from adcp.types.domains.core.event_custom_data import Content, EventCustomData
from adcp.types.domains.core.event_source_health import Detail, EventSourceHealth
from adcp.types.domains.core.event_surface import Category, EventSurface
from adcp.types.domains.core.experimental_feature_id import ExperimentalFeatureId
from adcp.types.domains.core.ext import ExtensionObject
from adcp.types.domains.core.feature_requirement import FeatureRequirement, IfNotCovered
from adcp.types.domains.core.flight_item import FlightItem
from adcp.types.domains.core.forecast_dimension_audience import AudienceForecastDimension
from adcp.types.domains.core.forecast_dimension_device_platform import (
    DevicePlatformForecastDimension,
)
from adcp.types.domains.core.forecast_dimension_device_type import DeviceTypeForecastDimension
from adcp.types.domains.core.forecast_dimension_geo import GeoForecastDimension
from adcp.types.domains.core.forecast_dimension_placement import PlacementForecastDimension
from adcp.types.domains.core.forecast_dimension_signal import Presence, SignalForecastDimension
from adcp.types.domains.core.forecast_dimension_time import TimeForecastDimension
from adcp.types.domains.core.forecast_point import ForecastPoint, ViewableRate
from adcp.types.domains.core.forecast_point_dimensions import ForecastPointDimensions
from adcp.types.domains.core.forecast_range import ForecastRange
from adcp.types.domains.core.forecast_rate_range import ForecastRateRange
from adcp.types.domains.core.forecast_vendor_metric_value import ForecastVendorMetricValue
from adcp.types.domains.core.format import (
    Assets10,
    Assets11,
    Assets12,
    Assets13,
    Assets14,
    Assets15,
    Assets16,
    Assets17,
    Assets18,
    Assets19,
    Assets20,
    Assets21,
    Assets22,
    Assets23,
    Assets24,
    Assets25,
    Assets26,
    Assets27,
    Assets28,
    Assets29,
    Assets30,
    Assets31,
    Assets32,
    Assets33,
    Assets34,
    Assets35,
    Assets36,
    Assets37,
    Assets38,
    Assets39,
    Assets40,
    Assets41,
    Assets42,
    Assets43,
    Assets44,
    Assets45,
    Assets46,
    Assets47,
    Assets48,
    Assets49,
    Assets50,
    Assets51,
    Assets9,
    Assets94,
    BaseGroupAsset,
    BaseIndividualAsset,
    CanonicalParameters,
    CanonicalParameters1,
    CanonicalParameters10,
    CanonicalParameters11,
    CanonicalParameters12,
    CanonicalParameters13,
    CanonicalParameters14,
    CanonicalParameters15,
    CanonicalParameters16,
    CanonicalParameters17,
    CanonicalParameters18,
    CanonicalParameters19,
    CanonicalParameters2,
    CanonicalParameters20,
    CanonicalParameters21,
    CanonicalParameters22,
    CanonicalParameters23,
    CanonicalParameters24,
    CanonicalParameters25,
    CanonicalParameters26,
    CanonicalParameters27,
    CanonicalParameters28,
    CanonicalParameters29,
    CanonicalParameters3,
    CanonicalParameters30,
    CanonicalParameters31,
    CanonicalParameters32,
    CanonicalParameters33,
    CanonicalParameters4,
    CanonicalParameters5,
    CanonicalParameters6,
    CanonicalParameters7,
    CanonicalParameters8,
    CanonicalParameters9,
    Dimensions,
    Dimensions1,
    DisclosureCapability,
    FormatAssetUnion,
    FormatCard,
    FormatCardDetailed,
    GroupFormatAssetUnion,
    Renders,
    Renders1,
    Responsive,
    UnknownFormatAsset,
    UnknownGroupAsset,
)
from adcp.types.domains.core.format_id import FormatReferenceStructuredObject
from adcp.types.domains.core.format_option_ref import (
    FormatOptionReference,
    FormatOptionReference1,
    FormatOptionReference2,
)
from adcp.types.domains.core.format_shape_vocabulary import AdcpFormatShapeVocabularyRegistry
from adcp.types.domains.core.frequency_cap import FrequencyCap
from adcp.types.domains.core.frequency_cap_constraints import FrequencyCapConstraints
from adcp.types.domains.core.frequency_cap_duration_unit import FrequencyCapDurationUnit
from adcp.types.domains.core.frequency_cap_impression_constraints import (
    AllowedValue,
    FrequencyCapImpressionConstraints,
)
from adcp.types.domains.core.frequency_cap_interval_constraints import (
    AllowedInterval,
    FrequencyCapIntervalConstraints,
)
from adcp.types.domains.core.frequency_cap_requirements import FrequencyCapRequirements
from adcp.types.domains.core.generation_credential import GenerationCredential
from adcp.types.domains.core.geo_breakdown_support import GeographicBreakdownSupport
from adcp.types.domains.core.geo_delivery_metrics import GeoDeliveryMetrics
from adcp.types.domains.core.geo_metro import GeoMetro
from adcp.types.domains.core.geo_place_area import GeographicPlaceArea
from adcp.types.domains.core.geo_place_catalog_capability import (
    GeographicPlaceCatalogCapability,
    SupportedVersion,
)
from adcp.types.domains.core.geo_place_catalog_entry import (
    GeographicPlaceCatalogEntry,
    ParentLabel,
    ReplacedByValue,
)
from adcp.types.domains.core.geo_place_requirement import (
    CatalogRequirement,
    GeographicPlaceRequirement,
)
from adcp.types.domains.core.geo_place_resolver import Auth, GeographicPlaceResolver
from adcp.types.domains.core.geo_place_support import GeographicPlaceSystemSupport
from adcp.types.domains.core.geo_place_system import (
    GeographicPlaceIdentifierSystem,
    GeographicPlaceIdentifierSystem1,
    GeographicPlaceIdentifierSystem2,
)
from adcp.types.domains.core.geo_place_type import (
    GeographicPlaceType,
    GeographicPlaceType1,
    GeographicPlaceType2,
)
from adcp.types.domains.core.geo_region_requirement import Countries1, GeographicRegionRequirement
from adcp.types.domains.core.geo_region_support import Countries3, GeographicRegionSupport
from adcp.types.domains.core.get_geo_place_resolution_request import (
    GetGeographicPlaceResolutionRequest,
)
from adcp.types.domains.core.get_geo_place_resolution_response import (
    GetGeographicPlaceResolutionResponse,
)
from adcp.types.domains.core.hotel_item import HotelItem
from adcp.types.domains.core.iana_timezone import IanaTimezoneIdentifier
from adcp.types.domains.core.impairment import Impairment, Transition
from adcp.types.domains.core.indicator import Indicator
from adcp.types.domains.core.indicator_bearing import IndicatorBearingResourceState
from adcp.types.domains.core.indicator_scope import IndicatorScope
from adcp.types.domains.core.indicators_changed_webhook import (
    IndicatorsChangedWebhook,
    RelationshipKind,
)
from adcp.types.domains.core.industry_identifier import IndustryIdentifier
from adcp.types.domains.core.insertion_order import InsertionOrder, PaymentTerms, Terms, TotalBudget
from adcp.types.domains.core.installment import DerivativeOf, Installment
from adcp.types.domains.core.installment_deadlines import InstallmentDeadlines
from adcp.types.domains.core.installment_delivery_metrics import InstallmentDeliveryMetrics
from adcp.types.domains.core.installment_property_delivery_metrics import (
    InstallmentPropertyDeliveryMetrics,
)
from adcp.types.domains.core.installment_ref import InstallmentReference
from adcp.types.domains.core.inventory_list_application import (
    InventoryListApplication,
    InventoryListApplication1,
    InventoryListApplication2,
    Summary,
    Summary2,
)
from adcp.types.domains.core.job_item import EmploymentType, ExperienceLevel, JobItem, Salary
from adcp.types.domains.core.keyword_delivery_metrics import KeywordDeliveryMetrics
from adcp.types.domains.core.limited_series import LimitedSeries
from adcp.types.domains.core.locale_tag import LanguageTag
from adcp.types.domains.core.localized_creative_asset import (
    LocalizedCreativeAsset,
    LocalizedCreativeAsset1,
    LocalizedCreativeAsset10,
    LocalizedCreativeAsset11,
    LocalizedCreativeAsset12,
    LocalizedCreativeAsset13,
    LocalizedCreativeAsset14,
    LocalizedCreativeAsset15,
    LocalizedCreativeAsset16,
    LocalizedCreativeAsset17,
    LocalizedCreativeAsset18,
    LocalizedCreativeAsset19,
    LocalizedCreativeAsset2,
    LocalizedCreativeAsset20,
    LocalizedCreativeAsset21,
    LocalizedCreativeAsset22,
    LocalizedCreativeAsset3,
    LocalizedCreativeAsset4,
    LocalizedCreativeAsset5,
    LocalizedCreativeAsset7,
    LocalizedCreativeAsset8,
    LocalizedCreativeAsset9,
)
from adcp.types.domains.core.macro_bearing_url import MacroBearingUrl3, MacroBearingUrl4
from adcp.types.domains.core.macro_resolution_capability import (
    MacroProcessingCapability,
    MappingStatus,
    Operation,
)
from adcp.types.domains.core.macro_resolution_result import MacroResolutionResult
from adcp.types.domains.core.material_deadline import MaterialDeadline
from adcp.types.domains.core.mcp_webhook_payload import McpWebhookPayload
from adcp.types.domains.core.measurement_readiness import MeasurementReadiness
from adcp.types.domains.core.measurement_terms import MeasurementTerms
from adcp.types.domains.core.measurement_window import MeasurementWindow
from adcp.types.domains.core.media_buy import MediaBuy
from adcp.types.domains.core.media_buy_available_action import MediaBuyAvailableAction
from adcp.types.domains.core.media_buy_available_action_id import MediaBuyAvailableActionId
from adcp.types.domains.core.media_buy_change_term_id import MediaBuyChangeTermId
from adcp.types.domains.core.media_buy_features import MediaBuyFeatures
from adcp.types.domains.core.media_buy_frequency_cap import MediaBuyFrequencyCap
from adcp.types.domains.core.media_buy_frequency_cap_capability import (
    MediaBuyFrequencyCapCapability,
)
from adcp.types.domains.core.media_buy_frequency_cap_requirement import (
    MediaBuyFrequencyCapRequirement,
)
from adcp.types.domains.core.media_buy_frequency_cap_support import MediaBuyFrequencyCapSupport
from adcp.types.domains.core.media_buy_legacy_terms_ref import MediaBuyTermsReference
from adcp.types.domains.core.media_buy_support import ProductMediaBuySupport
from adcp.types.domains.core.media_buy_support_requirements import (
    ProductMediaBuySupportRequirements,
)
from adcp.types.domains.core.missing_metric import MissingMetric, MissingMetric1, MissingMetric2
from adcp.types.domains.core.negative_keyword import NegativeKeyword
from adcp.types.domains.core.notification_config import NotificationConfig
from adcp.types.domains.core.offering import GeoTargets, Offering
from adcp.types.domains.core.offering_asset_group import Items, OfferingAssetGroup
from adcp.types.domains.core.operator_identity import OperatorIdentity
from adcp.types.domains.core.operator_unit import OperatorUnit
from adcp.types.domains.core.opportunity_context import (
    CloseReason,
    Intent,
    OpportunityContext,
    Phase,
)
from adcp.types.domains.core.optimization_goal import (
    OptimizationGoal10,
    OptimizationGoal8,
    OptimizationGoal9,
    Target14,
    Target15,
    Target16,
    Target17,
    Target18,
    Target19,
)
from adcp.types.domains.core.outcome_measurement import OutcomeMeasurement
from adcp.types.domains.core.outcome_target_cost_per import OutcomeTargetCostPer
from adcp.types.domains.core.overlay import Bounds, Overlay, Visual
from adcp.types.domains.core.package import Package
from adcp.types.domains.core.package_delivery_metric_value import PackageDeliveryMetricValue
from adcp.types.domains.core.package_format_snapshot import (
    PackageFormatSnapshot,
    PackageFormatSnapshot1,
    PackageFormatSnapshot10,
    PackageFormatSnapshot11,
    PackageFormatSnapshot12,
    PackageFormatSnapshot13,
    PackageFormatSnapshot14,
    PackageFormatSnapshot15,
    PackageFormatSnapshot16,
    PackageFormatSnapshot17,
    PackageFormatSnapshot18,
    PackageFormatSnapshot19,
    PackageFormatSnapshot2,
    PackageFormatSnapshot20,
    PackageFormatSnapshot21,
    PackageFormatSnapshot22,
    PackageFormatSnapshot23,
    PackageFormatSnapshot24,
    PackageFormatSnapshot25,
    PackageFormatSnapshot26,
    PackageFormatSnapshot27,
    PackageFormatSnapshot28,
    PackageFormatSnapshot29,
    PackageFormatSnapshot3,
    PackageFormatSnapshot30,
    PackageFormatSnapshot31,
    PackageFormatSnapshot32,
    PackageFormatSnapshot33,
    PackageFormatSnapshot4,
    PackageFormatSnapshot5,
    PackageFormatSnapshot6,
    PackageFormatSnapshot7,
    PackageFormatSnapshot8,
    PackageFormatSnapshot9,
)
from adcp.types.domains.core.package_signal_targeting import (
    PackageSignalTargeting,
    PackageSignalTargeting1,
    PackageSignalTargeting2,
    PackageSignalTargeting3,
    PackageSignalTargeting4,
    PackageSignalTargeting5,
    PackageSignalTargeting6,
    PackageSignalTargeting7,
)
from adcp.types.domains.core.package_signal_targeting_group import (
    Operator,
    PackageSignalTargetingGroup,
)
from adcp.types.domains.core.package_signal_targeting_groups import PackageSignalTargetingGroups
from adcp.types.domains.core.package_targeting_resolution import PackageTargetingResolution
from adcp.types.domains.core.pagination_request import PaginationRequest
from adcp.types.domains.core.pagination_response import PaginationResponse
from adcp.types.domains.core.performance_feedback import (
    MeasurementPeriod,
    Metric7,
    PerformanceFeedback,
)
from adcp.types.domains.core.performance_feedback_assertion import (
    ConfidenceInterval,
    PerformanceFeedbackAssertion,
)
from adcp.types.domains.core.performance_feedback_metric import (
    PerformanceFeedbackMetric,
    PerformanceFeedbackMetric1,
    PerformanceFeedbackMetric2,
)
from adcp.types.domains.core.performance_standard import PerformanceStandard
from adcp.types.domains.core.placement import (
    Placement,
    ProductDoohPlacementAttributes,
    ProductDoohScreenResolution,
)
from adcp.types.domains.core.placement_definition import (
    FormatOptions,
    PlacementDefinition,
    PublisherDoohPlacementAttributes,
    PublisherDoohScreenResolution,
)
from adcp.types.domains.core.placement_delivery_metrics import PlacementDeliveryMetrics
from adcp.types.domains.core.placement_evidence import PlacementEvidence
from adcp.types.domains.core.placement_identity import (
    PlacementIdentity,
    PlacementIdentity1,
    PlacementIdentity2,
)
from adcp.types.domains.core.placement_presentation import (
    BoxDecoration,
    Canvas,
    Color,
    CreativeSlot,
    Fit,
    ImageDecoration,
    ImageRef,
    Layer,
    PlacementPresentationDocument,
    Rectangle,
    TextDecoration,
)
from adcp.types.domains.core.placement_property_delivery_metrics import (
    PlacementPropertyDeliveryMetrics,
)
from adcp.types.domains.core.placement_ref import PlacementReference
from adcp.types.domains.core.placement_selection import PlacementSelection1, PlacementSelection2
from adcp.types.domains.core.planned_delivery import Geo, PlannedDelivery
from adcp.types.domains.core.platform_extension_ref import PlatformExtensionReference
from adcp.types.domains.core.positive_postal_area_support import PositivePostalAreaSupport
from adcp.types.domains.core.postal_area import (
    PostalArea,
    PostalArea1,
    PostalArea11,
    PostalArea110,
    PostalArea111,
    PostalArea112,
    PostalArea113,
    PostalArea114,
    PostalArea115,
    PostalArea116,
    PostalArea117,
    PostalArea118,
    PostalArea119,
    PostalArea12,
    PostalArea120,
    PostalArea121,
    PostalArea13,
    PostalArea14,
    PostalArea15,
    PostalArea16,
    PostalArea17,
    PostalArea18,
    PostalArea19,
    PostalArea2,
    System1,
    System2,
    System3,
    System9,
)
from adcp.types.domains.core.postal_area_support import (
    CAEnum,
    GBEnum,
    ME,
    PostalAreaSupport,
    PostalAreaSupportAdditionalPropertyEnum,
)
from adcp.types.domains.core.postal_country_system import (
    PostalCountrySystem,
    PostalCountrySystem1,
    PostalCountrySystem10,
    PostalCountrySystem2,
    PostalCountrySystem3,
    PostalCountrySystem4,
    PostalCountrySystem5,
    PostalCountrySystem6,
    PostalCountrySystem7,
    PostalCountrySystem8,
    PostalCountrySystem9,
    System11,
    System12,
    System13,
    System19,
)
from adcp.types.domains.core.presentation_ref import PlacementPresentationReference
from adcp.types.domains.core.preview_provider import PublisherDesignatedPreviewProvider, Route
from adcp.types.domains.core.preview_renderer_metadata import (
    PreviewRendererMetadata,
    RenderingOrigin,
)
from adcp.types.domains.core.price import Price
from adcp.types.domains.core.pricing_option import PricingOption
from adcp.types.domains.core.principal_changed_webhook import PrincipalChangedWebhook
from adcp.types.domains.core.principal_declarations import (
    AgentDeclarations,
    AsyncAdcpVersion,
    WebhookSigningAlgorithm,
)
from adcp.types.domains.core.principal_declarations_state import (
    Axis,
    Exclusion,
    PrincipalDeclarationsState,
)
from adcp.types.domains.core.principal_state import (
    DestinationRef,
    PrincipalState,
    RetiredDestination,
)
from adcp.types.domains.core.product import (
    AudienceActivation,
    ConversionTracking,
    DeliveryMeasurement,
    MaterialSubmission,
    MetricOptimization,
    Product,
    ProductCard,
    ProductCardDetailed,
    PublisherProperty81,
    PublisherProperty82,
    PublisherProperty83,
    PublisherProperty84,
    PublisherProperty85,
    PublisherProperty86,
    PublisherProperty87,
    Specification,
    SupportedMetric,
    SupportedTarget5,
    SupportedViewDuration,
)
from adcp.types.domains.core.product_allocation import ProductAllocation
from adcp.types.domains.core.product_allowed_action import ProductAllowedAction
from adcp.types.domains.core.product_audience_evidence_requirements import (
    AcceptedAttestationIssuers,
    AcceptedAttestationIssuers1,
    AcceptedAttestationIssuers2,
    AcceptedAttestationIssuers3,
    ProductAudienceEvidenceRequirements,
)
from adcp.types.domains.core.product_card_reference_asset import ProductCardReferenceAsset
from adcp.types.domains.core.product_change_map import ProductChangeMap, ProductChangeMap1
from adcp.types.domains.core.product_execution_requirement import (
    Connection,
    ProductExecutionRequirement,
    ProductExecutionRequirement1,
    ProductExecutionRequirement2,
    ProductExecutionRequirement3,
)
from adcp.types.domains.core.product_filters import (
    AudienceActivationMethods,
    AudienceActivationMethods1,
    AudienceActivationMethods2,
    AudienceActivationMethods3,
    AudienceActivationMethods4,
    AudienceActivationMethods5,
    Keyword,
    ProductFilters,
    RequiredGeoTargetingItem,
    SignalTargetingItem,
    SignalTargetingItem1,
    SignalTargetingItem2,
    SignalTargetingItem3,
    SignalTargetingItem4,
    SignalTargetingItem5,
    SignalTargetingItem6,
    SignalTargetingItem7,
)
from adcp.types.domains.core.product_format_declaration import (
    ProductFormatDeclaration,
    ProductFormatDeclaration1,
    ProductFormatDeclaration10,
    ProductFormatDeclaration11,
    ProductFormatDeclaration12,
    ProductFormatDeclaration13,
    ProductFormatDeclaration14,
    ProductFormatDeclaration15,
    ProductFormatDeclaration16,
    ProductFormatDeclaration2,
    ProductFormatDeclaration3,
    ProductFormatDeclaration4,
    ProductFormatDeclaration5,
    ProductFormatDeclaration6,
    ProductFormatDeclaration7,
    ProductFormatDeclaration8,
    ProductFormatDeclaration9,
)
from adcp.types.domains.core.product_identity import ProductIdentity
from adcp.types.domains.core.product_offer_filters import (
    AvailabilityHorizon,
    ProductOfferFilters,
    RequiredPerformanceStandard,
)
from adcp.types.domains.core.product_signal_targeting_option import (
    ActivationStatus,
    AllowedTargetingMode,
    ProductSignalTargetingOption,
)
from adcp.types.domains.core.product_targeting_resolution import ProductTargetingResolution
from adcp.types.domains.core.property import Property
from adcp.types.domains.core.property_delivery_metrics import PropertyDeliveryMetrics
from adcp.types.domains.core.property_id import PropertyId
from adcp.types.domains.core.property_list_ref import PropertyListReference
from adcp.types.domains.core.property_ref import PropertyReference
from adcp.types.domains.core.property_tag import PropertyTag
from adcp.types.domains.core.proposal import Proposal
from adcp.types.domains.core.protocol_envelope import ProtocolEnvelope
from adcp.types.domains.core.provenance import VerifyAgent18
from adcp.types.domains.core.publisher_property_selector import (
    PublisherPropertySelector,
    PublisherPropertySelector1,
    PublisherPropertySelector2,
    PublisherPropertySelector3,
)
from adcp.types.domains.core.push_notification_config import PushNotificationConfig
from adcp.types.domains.core.real_estate_item import ListingType, PropertyType, RealEstateItem
from adcp.types.domains.core.reference_renderer import ReferenceRenderer
from adcp.types.domains.core.registry_event import (
    AgentProfilePayload,
    AuthorizationPayload,
    AuthorizationType,
    BadgeRole,
    ChangedFields,
    Classification,
    CollectionIdentifier,
    CollectionPayload,
    CompliancePayload,
    ComplianceStatus,
    DelegationType,
    GradingProfile,
    Market,
    Payload1,
    Payload10,
    Payload11,
    Payload12,
    Payload13,
    Payload14,
    Payload2,
    Payload3,
    Payload4,
    Payload5,
    Payload6,
    Payload7,
    Payload8,
    Payload9,
    PropertyPayload,
    PropertySource,
    PublisherAdagentsPayload,
    RegistryEvent,
    RegistryEvent1,
    RegistryEvent10,
    RegistryEvent11,
    RegistryEvent12,
    RegistryEvent13,
    RegistryEvent14,
    RegistryEvent15,
    RegistryEvent16,
    RegistryEvent17,
    RegistryEvent18,
    RegistryEvent19,
    RegistryEvent2,
    RegistryEvent20,
    RegistryEvent3,
    RegistryEvent4,
    RegistryEvent5,
    RegistryEvent6,
    RegistryEvent7,
    RegistryEvent8,
    RegistryEvent9,
    Storyboard,
    StoryboardStatus,
    StringArray,
    Tracks,
)
from adcp.types.domains.core.registry_feed_response import Freshness, RegistryFeedResponse
from adcp.types.domains.core.reporting_adjustment import AccountingPeriod, ReportingAdjustment
from adcp.types.domains.core.reporting_adjustment_receipt import (
    ReportingAdjustmentReceipt,
    ReportingAdjustmentRejectionCode,
)
from adcp.types.domains.core.reporting_canonical_content_digest import (
    ReportingCanonicalContentDigest,
)
from adcp.types.domains.core.reporting_canonicalization_contract import (
    AdditionalItem,
    EmptyReport,
    GoldenVectors,
    OrderingEncoding,
    ReportingCanonicalizationContract,
    ReportingPrimaryKey,
)
from adcp.types.domains.core.reporting_capabilities import ReportingCapabilities
from adcp.types.domains.core.reporting_consumer_status import (
    ConsumerStatus,
    FailureCode,
    MismatchCode,
    ReportingConsumerStatus,
)
from adcp.types.domains.core.reporting_control_total import (
    ReportingControlTotal,
    ReportingControlTotal1,
    ReportingControlTotal2,
)
from adcp.types.domains.core.reporting_coverage import (
    Limitation,
    ReportingCoverage,
    ReportingMediaBuyId,
    ReportingPackageId,
)
from adcp.types.domains.core.reporting_dataset_share_destination import (
    Recipient,
    ReportingCloud,
    ReportingDatasetShareDestination,
    ReportingDatasetShareDestination1,
    ReportingDatasetShareDestination2,
)
from adcp.types.domains.core.reporting_delivery_capabilities import (
    OperationsContact,
    ReportingDeliveryCapabilities,
)
from adcp.types.domains.core.reporting_delivery_config import (
    AuthoritativeParty,
    CoverageRequirement,
    ReportingDeliveryConfiguration,
)
from adcp.types.domains.core.reporting_delivery_config_state import (
    ReportingDeliveryConfigLifecycleState,
    ReportingDeliveryConfigurationState,
)
from adcp.types.domains.core.reporting_delivery_method import (
    ReportingDeliveryMethod,
    ReportingDeliveryMethod1,
    ReportingDeliveryMethod2,
    ReportingDeliveryMethod3,
    ReportingOrchestration,
)
from adcp.types.domains.core.reporting_delivery_offering import (
    DestinationMode,
    ProducerIdentity,
    ReportingDeliveryOffering,
    ReportingDeliveryPattern,
    ReportingFeedPurpose,
    ReportingProfile,
)
from adcp.types.domains.core.reporting_delivery_offering_id import ReportingDeliveryOfferingId
from adcp.types.domains.core.reporting_delivery_ready_webhook import (
    Readiness,
    ReportingDeliveryReadyWebhook,
)
from adcp.types.domains.core.reporting_file_compression import ReportingFileCompression
from adcp.types.domains.core.reporting_file_entry import ReportingFileEntry
from adcp.types.domains.core.reporting_file_manifest import ReportingFileManifest
from adcp.types.domains.core.reporting_file_object_ref import ReportingFileObjectReference
from adcp.types.domains.core.reporting_ledger_changed_webhook import ReportingLedgerChangedWebhook
from adcp.types.domains.core.reporting_materialization import ReportingMaterialization
from adcp.types.domains.core.reporting_native_version_ref import ReportingNativeVersionReference
from adcp.types.domains.core.reporting_obligation import (
    ProductionStatus,
    ReconciliationStatus,
    ReportingObligation,
)
from adcp.types.domains.core.reporting_receipt import RejectionCode, ReportingReceipt
from adcp.types.domains.core.reporting_reconciliation_mode import ReportingReconciliationMode
from adcp.types.domains.core.reporting_reliability_statistics import (
    AdjustmentMagnitudeItem,
    Basis,
    LatencyPercentiles,
    ReportingReliabilityMeasurementPeriod,
    ReportingReliabilityStatistics,
)
from adcp.types.domains.core.reporting_report_definition import (
    Aggregation,
    Calendar,
    ContractVersion,
    FinalityPolicies,
    FinalityPolicies1,
    FinalityPolicies2,
    ReportCalendarTimezoneBasis,
    ReportingReportDefinition,
    RestatementPolicy,
)
from adcp.types.domains.core.reporting_resource import (
    Immutability,
    ReportingReaderCompatibilityItem,
    ReportingResource,
)
from adcp.types.domains.core.reporting_revision import (
    DataThroughPrecision,
    FinalityBasis,
    ReportingRevision,
)
from adcp.types.domains.core.reporting_schedule import ReportingSchedule, ReportingScheduleAlignment
from adcp.types.domains.core.reporting_schedule_offering import (
    PeriodAnchorPolicy,
    PeriodTimezonePolicy,
    ReportingScheduleOffering,
)
from adcp.types.domains.core.reporting_status_changed_webhook import (
    IssueId,
    ReportingStatusChangedWebhook,
)
from adcp.types.domains.core.reporting_status_issue import (
    IssueState,
    RecommendedAction,
    ReportingStatusIssue,
    ReportingStatusSeverity,
    ResponsibleParty,
)
from adcp.types.domains.core.reporting_verification import (
    NativeCommitEvidence,
    ObservedThrough,
    PhysicalChecksums,
    PhysicalChecksums1,
    ReportingVerification,
    VerificationPath,
)
from adcp.types.domains.core.reporting_verification_profile import ReportingVerificationProfile
from adcp.types.domains.core.reporting_verification_profile_set import (
    ReportingVerificationProfileSet,
    ReportingVerificationProfileSetEnum,
)
from adcp.types.domains.core.reporting_webhook import ReportingFrequency, ReportingWebhook
from adcp.types.domains.core.reporting_write_destination import (
    ReportingWriteDestination,
    ReportingWriteDestination1,
    ReportingWriteDestination2,
)
from adcp.types.domains.core.representation_destination import RepresentationDestination
from adcp.types.domains.core.representation_rejection import RepresentationRejection
from adcp.types.domains.core.representation_selection import RepresentationSelection, ResolvedBy
from adcp.types.domains.core.requirements.asset_requirements import AssetRequirements
from adcp.types.domains.core.requirements.audio_asset_requirements import (
    AudioAssetRequirements,
    SampleRate,
)
from adcp.types.domains.core.requirements.catalog_field_binding import (
    AssetPoolBinding,
    CatalogFieldBinding,
    CatalogFieldBinding1,
    PerItemBindings,
    ScalarBinding,
)
from adcp.types.domains.core.requirements.catalog_requirements import CatalogRequirements
from adcp.types.domains.core.requirements.css_asset_requirements import CssAssetRequirements
from adcp.types.domains.core.requirements.daast_asset_requirements import DaastAssetRequirements
from adcp.types.domains.core.requirements.html_asset_requirements import (
    HtmlAssetRequirements,
    Sandbox,
)
from adcp.types.domains.core.requirements.image_asset_requirements import (
    Bleed,
    Bleed1,
    ImageAssetRequirements,
    PixelRatio,
)
from adcp.types.domains.core.requirements.javascript_asset_requirements import (
    JavascriptAssetRequirements,
    ModuleType,
)
from adcp.types.domains.core.requirements.markdown_asset_requirements import (
    MarkdownAssetRequirements,
)
from adcp.types.domains.core.requirements.offering_asset_constraint import OfferingAssetConstraint
from adcp.types.domains.core.requirements.text_asset_requirements import TextAssetRequirements
from adcp.types.domains.core.requirements.url_asset_requirements import (
    Protocol,
    UrlAssetRequirements,
)
from adcp.types.domains.core.requirements.vast_asset_requirements import VastAssetRequirements
from adcp.types.domains.core.requirements.video_asset_requirements import (
    AudioCodec,
    AudioSampleRate,
    FrameRate,
    VideoAssetRequirements,
)
from adcp.types.domains.core.requirements.webhook_asset_requirements import WebhookAssetRequirements
from adcp.types.domains.core.response import ProtocolResponse
from adcp.types.domains.core.response_payload_jws_envelope import (
    ResponsePayload,
    ResponsePayloadJwsEnvelope,
)
from adcp.types.domains.core.rights_attestation_evaluation import (
    Issuer5,
    Issuer6,
    Reference,
    RightsAttestationEvaluation,
    Subject10,
    Subject19,
)
from adcp.types.domains.core.rights_constraint import (
    ApprovalStatus,
    ExcludedCountry,
    GrantStatus,
    Issuer8,
    Issuer9,
    Restriction,
    RightsAgent,
    RightsConstraint,
    Subject20,
    Subject29,
)
from adcp.types.domains.core.seller_agent_ref import SellerAgentReference
from adcp.types.domains.core.signal_coverage_forecast import (
    BucketCompleteness,
    BucketSemantics,
    Point,
    SignalCoverageForecast,
)
from adcp.types.domains.core.signal_definition import AudienceScope, SignalDefinition, Tag
from adcp.types.domains.core.signal_definition_enrichment import SignalDefinitionEnrichment
from adcp.types.domains.core.signal_filters import SignalFilters
from adcp.types.domains.core.signal_id import SignalId8, SignalId9
from adcp.types.domains.core.signal_listing import SignalListing
from adcp.types.domains.core.signal_modeling_disclosure import Audience, SignalModelingDisclosure
from adcp.types.domains.core.signal_pricing import (
    VendorPricing,
    VendorPricing1,
    VendorPricing2,
    VendorPricing3,
    VendorPricing4,
    VendorPricing5,
)
from adcp.types.domains.core.signal_pricing_option import SignalPricingOption
from adcp.types.domains.core.signal_ref import SignalRef, SignalRef1, SignalRef2, SignalRef3
from adcp.types.domains.core.signal_selection_group_rule import SignalSelectionGroupRule
from adcp.types.domains.core.signal_targeting import (
    SignalTargeting,
    SignalTargeting1,
    SignalTargeting2,
    SignalTargeting3,
)
from adcp.types.domains.core.signal_targeting_expression import (
    SignalTargetingExpression,
    SignalTargetingExpression1,
    SignalTargetingExpression2,
    SignalTargetingExpression3,
)
from adcp.types.domains.core.signal_targeting_rules import ResolutionModel, SignalTargetingRules
from adcp.types.domains.core.sla_window import SlaWindow
from adcp.types.domains.core.special import Special
from adcp.types.domains.core.spot_reporting_capability import SpotReportingCapability
from adcp.types.domains.core.start_timing import StartTiming
from adcp.types.domains.core.store_item import StoreItem
from adcp.types.domains.core.talent import Talent
from adcp.types.domains.core.targeting import AgeRestriction, TargetingOverlay
from adcp.types.domains.core.targeting_input import TargetingOverlayInput
from adcp.types.domains.core.targeting_modification import (
    Path,
    Selector,
    TargetingModification,
    TargetingModification1,
    TargetingModification2,
)
from adcp.types.domains.core.targeting_overlay_requirements import (
    BrowserRequirement,
    BrowserRequirement1,
    DaypartRequirement,
    DaypartRequirement1,
    KeywordRequirement,
    KeywordRequirement1,
    MetroRequirement,
    MetroRequirement1,
    Required,
    TargetingOverlayRequirements,
)
from adcp.types.domains.core.targeting_overlay_support import (
    BrowserSupport,
    BrowserSupport1,
    CountrySupport,
    CountrySupport1,
    DaypartSupport,
    DaypartSupport1,
    IanaTimezones,
    KeywordSupport,
    KeywordSupport1,
    MetroSupport,
    MetroSupport1,
    PlaceCatalogSupport,
    PlaceSupport,
    Supported,
    TargetingOverlaySupport,
)
from adcp.types.domains.core.targeting_unknown_age_eligibility_constraint import (
    TargetingUnknownAgeEligibilityConstraint,
)
from adcp.types.domains.core.targeting_verified_age_basis_constraint import (
    TargetingVerifiedAgeBasisConstraint,
)
from adcp.types.domains.core.tasks_get_request import TasksGetRequest
from adcp.types.domains.core.tasks_get_response import (
    Details,
    HistoryItem,
    Progress,
    TasksGetResponse,
)
from adcp.types.domains.core.tasks_list_request import Filters, Sort, TasksListRequest
from adcp.types.domains.core.tasks_list_response import (
    DomainBreakdown,
    QuerySummary,
    SortApplied,
    TasksListResponse,
)
from adcp.types.domains.core.tracker_execution_contract import TrackerExecutionContract
from adcp.types.domains.core.tracker_execution_selector import (
    TrackerExecutionSelector,
    TrackerExecutionSelector1,
    TrackerExecutionSelector2,
    TrackerExecutionSelector3,
)
from adcp.types.domains.core.transformer import (
    BrandAgent,
    InputFormat,
    InputFormat1,
    InputFormat10,
    InputFormat11,
    InputFormat12,
    InputFormat13,
    InputFormat14,
    InputFormat15,
    InputFormat16,
    InputFormat17,
    InputFormat18,
    InputFormat19,
    InputFormat2,
    InputFormat20,
    InputFormat21,
    InputFormat22,
    InputFormat23,
    InputFormat24,
    InputFormat25,
    InputFormat26,
    InputFormat27,
    InputFormat28,
    InputFormat29,
    InputFormat3,
    InputFormat30,
    InputFormat31,
    InputFormat32,
    InputFormat33,
    InputFormat4,
    InputFormat5,
    InputFormat6,
    InputFormat7,
    InputFormat8,
    InputFormat9,
    Multiplicity,
    OutputCapabilityId,
    Transformer,
    VariantDimension,
    VoiceSynthesisRefItem,
)
from adcp.types.domains.core.transformer_param import Option, TransformerParam, ValueSource
from adcp.types.domains.core.truncation_sentinel import FieldTruncation, TruncationSentinel
from adcp.types.domains.core.user_match import UserMatch
from adcp.types.domains.core.vast_media_file_requirements import MimeType, VastMediafileRequirements
from adcp.types.domains.core.vast_tracker_constraints import (
    VastEvent,
    VastOffset,
    VastTarget,
    VastTrackerConstraints,
    VastVersions,
)
from adcp.types.domains.core.vehicle_item import (
    BodyStyle,
    Condition,
    FuelType,
    Mileage,
    Transmission,
    VehicleItem,
)
from adcp.types.domains.core.vendor_metric_id import VendorMetricId
from adcp.types.domains.core.vendor_metric_optimization import VendorMetricOptimization
from adcp.types.domains.core.vendor_metric_optimization_supported_metric import (
    VendorMetricOptimizationSupportedMetric,
)
from adcp.types.domains.core.vendor_metric_value import VendorMetricValue
from adcp.types.domains.core.vendor_pricing_option import (
    AppliesToOutputCapabilityId,
    VendorPricingOption,
    VendorPricingOption1,
    VendorPricingOption10,
    VendorPricingOption11,
    VendorPricingOption2,
    VendorPricingOption3,
    VendorPricingOption4,
    VendorPricingOption5,
    VendorPricingOption6,
    VendorPricingOption7,
    VendorPricingOption8,
    VendorPricingOption9,
)
from adcp.types.domains.core.verification_token_claims import (
    AgenticadvertisingOrgVerificationTokenClaims,
    VerificationTokenGradingProfile,
    VerificationTokenMode,
)
from adcp.types.domains.core.version_envelope import AdcpVersionEnvelope
from adcp.types.domains.core.warning import Warning
from adcp.types.domains.core.warning_resource import WarningAffectedResource
from adcp.types.domains.core.webhook_activity_record import WebhookActivityRecord
from adcp.types.domains.core.webhook_challenge import WebhookChallenge
from adcp.types.domains.core.webhook_challenge_response import WebhookChallengeResponse
from adcp.types.domains.core.wholesale_feed_event import (
    AffectedEntityType,
    AppliesTo,
    AppliesTo1,
    AppliesTo2,
    Payload17,
    Payload18,
    Payload20,
    Payload21,
    Payload22,
    Payload23,
    Payload24,
    Payload25,
    RemovalReason,
    Signal,
    WholesaleFeedEvent,
    WholesaleFeedEvent1,
    WholesaleFeedEvent2,
    WholesaleFeedEvent3,
    WholesaleFeedEvent4,
    WholesaleFeedEvent5,
    WholesaleFeedEvent6,
    WholesaleFeedEvent7,
    WholesaleFeedEvent8,
    WholesaleFeedEvent9,
)
from adcp.types.domains.core.wholesale_feed_webhook import (
    CacheScope,
    NotificationType,
    WholesaleFeedWebhook,
)
from adcp.types.domains.core.x_entity_types import XEntityTypes

# Explicit exports
__all__ = [
    "AcceptProposalInputRequired",
    "AcceptProposalSubmitted",
    "AcceptProposalWorking",
    "AcceptancePolicyProfileId",
    "AcceptancePolicyProfileIds",
    "AcceptedAttestationIssuers",
    "AcceptedAttestationIssuers1",
    "AcceptedAttestationIssuers2",
    "AcceptedAttestationIssuers3",
    "AcceptedFormat",
    "AcceptedIssuer",
    "Account",
    "AccountAuthorization",
    "AccountChange",
    "AccountChangeRecordedWebhook",
    "AccountIdentityChange",
    "AccountIdentityChange1",
    "AccountIdentityChange2",
    "AccountIdentityChangePreview",
    "AccountIdentityChangePreview1",
    "AccountIdentityChangePreview2",
    "AccountIdentityChangePreview3",
    "AccountReference",
    "AccountReference1",
    "AccountReference2",
    "AccountSelection",
    "AccountStatusChangedWebhook",
    "AccountTimezoneCapability",
    "AccountWithAuthorization",
    "AccountingPeriod",
    "Action3",
    "Action4",
    "ActivationKey",
    "ActivationKey1",
    "ActivationKey2",
    "ActivationStatus",
    "Actor",
    "AdInventoryConfiguration",
    "AdcpAssetGroupVocabularyRegistry",
    "AdcpAsyncResponseData",
    "AdcpFormatShapeVocabularyRegistry",
    "AdcpVersionEnvelope",
    "AdditionalItem",
    "AdjustmentMagnitudeItem",
    "AffectedEntityType",
    "AgeRestriction",
    "AgentDeclarations",
    "AgentEncryptionKey",
    "AgentNotificationConfig",
    "AgentNotificationConfigState",
    "AgentProfilePayload",
    "AgentReportingDestination",
    "AgentReportingDestination1",
    "AgentReportingDestination2",
    "AgentReportingDestination3",
    "AgentReportingDestinationState",
    "AgentSigningKey",
    "AgentWebhookChallenge",
    "AgenticadvertisingOrgVerificationTokenClaims",
    "Aggregation",
    "AllowedInterval",
    "AllowedTargetingMode",
    "AllowedTask",
    "AllowedValue",
    "AppItem",
    "ApplicablePackageId",
    "AppliesTo",
    "AppliesTo1",
    "AppliesTo2",
    "AppliesToOutputCapabilityId",
    "ApprovalStatus",
    "Artifact",
    "AssetPoolBinding",
    "AssetRequirements",
    "AssetSource",
    "AssetVariant",
    "Assets10",
    "Assets11",
    "Assets12",
    "Assets13",
    "Assets14",
    "Assets15",
    "Assets16",
    "Assets17",
    "Assets18",
    "Assets19",
    "Assets20",
    "Assets21",
    "Assets22",
    "Assets23",
    "Assets24",
    "Assets25",
    "Assets26",
    "Assets27",
    "Assets28",
    "Assets29",
    "Assets30",
    "Assets31",
    "Assets32",
    "Assets33",
    "Assets34",
    "Assets35",
    "Assets36",
    "Assets37",
    "Assets38",
    "Assets39",
    "Assets40",
    "Assets41",
    "Assets42",
    "Assets43",
    "Assets44",
    "Assets45",
    "Assets46",
    "Assets47",
    "Assets48",
    "Assets49",
    "Assets50",
    "Assets51",
    "Assets9",
    "Assets94",
    "AsyncAdcpVersion",
    "AttestationCapabilities",
    "AttestationDigest",
    "AttestationIssuer",
    "AttestationIssuer1",
    "AttestationIssuer2",
    "AttestationIssuer3",
    "AttestationReference",
    "AttestationSubject",
    "AttestationSubject1",
    "AttestationSubject2",
    "AttestationSubject3",
    "AttributionWindow",
    "Audience",
    "AudienceActivation",
    "AudienceActivationMethod",
    "AudienceActivationMethod1",
    "AudienceActivationMethod2",
    "AudienceActivationMethod3",
    "AudienceActivationMethod4",
    "AudienceActivationMethod5",
    "AudienceActivationMethod6",
    "AudienceActivationMethods",
    "AudienceActivationMethods1",
    "AudienceActivationMethods2",
    "AudienceActivationMethods3",
    "AudienceActivationMethods4",
    "AudienceActivationMethods5",
    "AudienceCharacteristic",
    "AudienceEvidence",
    "AudienceEvidencePin",
    "AudienceEvidenceRequirements",
    "AudienceEvidenceSelection",
    "AudienceForecastDimension",
    "AudienceMember",
    "AudienceScope",
    "AudienceSelector",
    "AudienceSelector1",
    "AudienceSelector2",
    "AudienceSelector3",
    "AudienceSelector4",
    "AudienceSource",
    "AudienceSource1",
    "AudienceSource2",
    "AudioAssetRequirements",
    "AudioChannelLayout",
    "AudioCodec",
    "AudioSampleRate",
    "Auth",
    "AuthoritativeParty",
    "AuthorizationPayload",
    "AuthorizationType",
    "AuthorizedAgentBaseFields",
    "AvailabilityHorizon",
    "Axis",
    "BadgeRole",
    "Bank",
    "BaseGroupAsset",
    "BaseIndividualAsset",
    "Basis",
    "BiddingPolicy",
    "BiddingPolicyCapability",
    "BitDepth",
    "Bleed",
    "Bleed1",
    "BlockedImpacts",
    "Blocker",
    "BodyStyle",
    "Bounds",
    "BoxDecoration",
    "BrandAgent",
    "BrandId",
    "BrandKey",
    "BrandKitOverride",
    "BrandReference",
    "BrandResponseAuthorizationResult",
    "BrandResponseAuthorizationResult1",
    "BrandResponseAuthorizationResult2",
    "BrowserRequirement",
    "BrowserRequirement1",
    "BrowserSupport",
    "BrowserSupport1",
    "BucketCompleteness",
    "BucketSemantics",
    "BudgetAllocation",
    "BudgetAllocation1",
    "BudgetAllocation2",
    "BuildCreativeInputRequired",
    "BuildCreativeSubmitted",
    "BuildCreativeWorking",
    "BusinessEntity",
    "BuyProductsInputRequired",
    "BuyProductsSubmitted",
    "BuyProductsWorking",
    "BuyerReason",
    "ByActionSourceItem",
    "ByEventTypeItem",
    "C2paWatermarkAction",
    "CAEnum",
    "CacheScope",
    "Calendar",
    "CancellationFee",
    "CancellationPolicy",
    "CanonicalAccountReference",
    "CanonicalAccountReference1",
    "CanonicalAccountReference2",
    "CanonicalAudienceEvidence",
    "CanonicalAudienceEvidenceSelection",
    "CanonicalBudgetAllocation",
    "CanonicalBudgetAllocation1",
    "CanonicalBudgetAllocation2",
    "CanonicalDeliveryForecast",
    "CanonicalDoohPlacementAttributes",
    "CanonicalDoohScreenResolution",
    "CanonicalForecastPoint",
    "CanonicalForecastVendorMetricValue",
    "CanonicalFormatKind",
    "CanonicalFormatOption",
    "CanonicalMeasurementTerms",
    "CanonicalMediaBuyAction",
    "CanonicalMediaBuyAction1",
    "CanonicalMediaBuyAction2",
    "CanonicalMediaBuyAction3",
    "CanonicalMediaBuyActionFields",
    "CanonicalMediaBuyFeatures",
    "CanonicalMetricQualifier",
    "CanonicalOptimizationGoal",
    "CanonicalOptimizationGoal1",
    "CanonicalOptimizationGoal2",
    "CanonicalOptimizationGoal3",
    "CanonicalParameters",
    "CanonicalParameters1",
    "CanonicalParameters10",
    "CanonicalParameters11",
    "CanonicalParameters12",
    "CanonicalParameters13",
    "CanonicalParameters14",
    "CanonicalParameters15",
    "CanonicalParameters16",
    "CanonicalParameters17",
    "CanonicalParameters18",
    "CanonicalParameters19",
    "CanonicalParameters2",
    "CanonicalParameters20",
    "CanonicalParameters21",
    "CanonicalParameters22",
    "CanonicalParameters23",
    "CanonicalParameters24",
    "CanonicalParameters25",
    "CanonicalParameters26",
    "CanonicalParameters27",
    "CanonicalParameters28",
    "CanonicalParameters29",
    "CanonicalParameters3",
    "CanonicalParameters30",
    "CanonicalParameters31",
    "CanonicalParameters32",
    "CanonicalParameters33",
    "CanonicalParameters4",
    "CanonicalParameters5",
    "CanonicalParameters6",
    "CanonicalParameters7",
    "CanonicalParameters8",
    "CanonicalParameters9",
    "CanonicalPerformanceStandard",
    "CanonicalPricingOption",
    "CanonicalProduct",
    "CanonicalProductAction",
    "CanonicalProductPlacement",
    "CanonicalProductPlacement1",
    "CanonicalProductPlacement2",
    "CanonicalProjectionReference",
    "CanonicalProjectionSlotOverride",
    "CanonicalProposal",
    "CanonicalReportingCapabilities",
    "CanonicalReportingCommitment",
    "CanonicalReportingCommitment1",
    "CanonicalReportingCommitment2",
    "Canvas",
    "CanvasConstraint",
    "CapabilitiesChangedWebhook",
    "CatalogFieldBinding",
    "CatalogFieldBinding1",
    "CatalogItemAvailabilityError",
    "CatalogItemAvailabilityReference",
    "CatalogItemAvailabilityState",
    "CatalogItemAvailabilityUpdate",
    "CatalogItemAvailabilityUpdateResult",
    "CatalogItemDeliveryMetrics",
    "CatalogItemReferenceNotFoundError",
    "CatalogRequirement",
    "CatalogRequirements",
    "CatalogSelection",
    "CatalogType",
    "Catchment",
    "Category",
    "ChangedFields",
    "Classification",
    "CloseReason",
    "Cloud",
    "Collection",
    "CollectionDeliveryMetrics",
    "CollectionDistribution",
    "CollectionIdentifier",
    "CollectionListReference",
    "CollectionPayload",
    "CollectionPropertyDeliveryMetrics",
    "CollectionReference",
    "CollectionSelection",
    "CollectionSelection1",
    "CollectionSelection2",
    "CollectionSelector",
    "Color",
    "Colors",
    "CommittedMetric",
    "CommittedMetric1",
    "CommittedMetric2",
    "CompactTaskInputRequired",
    "CompactTaskSubmitted",
    "CompactTaskWorking",
    "CompliancePayload",
    "ComplianceStatus",
    "Compression",
    "Condition",
    "ConfidenceInterval",
    "Connection",
    "ConnectionType",
    "Constraint",
    "ConsumerIdentity",
    "ConsumerStatus",
    "Contact",
    "Content",
    "ContentIdType",
    "ContentRating",
    "ContextObject",
    "ContractVersion",
    "ControlMediaBuyInputRequired",
    "ControlMediaBuySubmitted",
    "ControlMediaBuyWorking",
    "ConversionTracking",
    "CostPer",
    "CostPerStrength",
    "Countries1",
    "Countries3",
    "CountrySupport",
    "CountrySupport1",
    "CoverageRequirement",
    "CreateMediaBuyInputRequired",
    "CreateMediaBuySubmitted",
    "CreateMediaBuyWorking",
    "CreativeAsset",
    "CreativeAssets",
    "CreativeAssets1",
    "CreativeAssignment",
    "CreativeConsumption",
    "CreativeDeliveryMetrics",
    "CreativeFilters",
    "CreativeItem",
    "CreativeItem1",
    "CreativeItem2",
    "CreativeLocalePolicy",
    "CreativeLocalization",
    "CreativeLocalizationReadback",
    "CreativeManifest",
    "CreativeOperationFormatDeclaration",
    "CreativePolicy",
    "CreativeRepresentation",
    "CreativeRepresentationSet",
    "CreativeRevisionId",
    "CreativeSlot",
    "CreativeVariable",
    "CreativeVariant",
    "CredentialOrigin",
    "CreditLimit",
    "CssAssetRequirements",
    "DaastAsset1",
    "DaastAsset2",
    "DaastAsset3",
    "DaastAsset4",
    "DaastAssetRequirements",
    "DaastEvent",
    "DaastOffset",
    "DaastTarget",
    "DaastTrackerConstraints",
    "DaastTrackingEvent",
    "DaastVersion",
    "DaastVersions",
    "DataProviderSignalSelector",
    "DataProviderSignalSelector1",
    "DataProviderSignalSelector2",
    "DataProviderSignalSelector3",
    "DataSubjectContestation",
    "DataThroughPrecision",
    "DateRange",
    "DatetimeRange",
    "DaypartRequirement",
    "DaypartRequirement1",
    "DaypartSupport",
    "DaypartSupport1",
    "DaypartTarget",
    "DeadlinePolicy",
    "DeclineProposalsInputRequired",
    "DeclineProposalsSubmitted",
    "DeclineProposalsWorking",
    "DegreeType",
    "DelegationType",
    "DeliveryBreakdownControls",
    "DeliveryForecast",
    "DeliveryMeasurement",
    "DeliveryMetricAggregate",
    "DeliveryMetricAggregate1",
    "DeliveryMetricAggregate2",
    "DeliveryMetrics",
    "DeliveryProvider",
    "DeliveryRecipient",
    "DemographicAgeRange",
    "DemographicPredicate",
    "DemographicReportingCapability",
    "DemographicTargetingCapability",
    "DemographicTargetingIntent",
    "DemographicTargetingResolution",
    "Deployment",
    "Deployment1",
    "Deployment2",
    "DerivativeOf",
    "Destination1",
    "Destination2",
    "DestinationItem",
    "DestinationMode",
    "DestinationRef",
    "DestinationType",
    "Detail",
    "Details",
    "DevicePlatformForecastDimension",
    "DeviceTypeForecastDimension",
    "DiagnosticIssue",
    "DigitalSourceType",
    "Dimensions",
    "Dimensions1",
    "DisclosureCapability",
    "DisclosurePersistence",
    "DisclosurePosition",
    "DiscriminatorItem",
    "DisplayTagAsset1",
    "DisplayTagAsset2",
    "DisplayTagAsset3",
    "DisplayTagAsset4",
    "DisplayTagAsset5",
    "DisplayTagAsset6",
    "DomainBreakdown",
    "DoohMetrics",
    "DoohMetrics1",
    "DownstreamConnectionRequirement",
    "Duration",
    "EducationItem",
    "Effect1",
    "EmbeddedCredential",
    "EmbeddedProvenanceMethod",
    "EmploymentType",
    "EmptyReport",
    "EstimationBasis",
    "EvalBudget",
    "EvaluatorSpec",
    "EvaluatorSpec1",
    "EvaluatorSpec2",
    "EvaluatorSpec3",
    "Event",
    "EventCustomData",
    "EventSourceHealth",
    "EventSurface",
    "ExcludedCountry",
    "Exclusion",
    "Execution",
    "Execution1",
    "Execution2",
    "ExecutionMode",
    "Exemplars",
    "ExperienceLevel",
    "ExperimentalFeatureId",
    "Ext",
    "ExtensionObject",
    "FailureCode",
    "FeatureAgent",
    "FeatureRequirement",
    "FeedFormat",
    "Field0",
    "FieldModel",
    "FieldTruncation",
    "Filters",
    "FinalityBasis",
    "FinalityPolicies",
    "FinalityPolicies1",
    "FinalityPolicies2",
    "Fit",
    "FlightItem",
    "ForecastPoint",
    "ForecastPointDimensions",
    "ForecastRange",
    "ForecastRateRange",
    "ForecastVendorMetricValue",
    "FormatAssetUnion",
    "FormatCard",
    "FormatCardDetailed",
    "FormatKind",
    "FormatOptionReference",
    "FormatOptionReference1",
    "FormatOptionReference2",
    "FormatOptions",
    "FormatReferenceStructuredObject",
    "FrameRate",
    "FrameRateType",
    "FrequencyCap",
    "FrequencyCapConstraints",
    "FrequencyCapDurationUnit",
    "FrequencyCapImpressionConstraints",
    "FrequencyCapIntervalConstraints",
    "FrequencyCapRequirements",
    "Freshness",
    "FuelType",
    "GBEnum",
    "GenerationContext",
    "GenerationCredential",
    "Geo",
    "GeoDeliveryMetrics",
    "GeoForecastDimension",
    "GeoMetro",
    "GeoTargets",
    "GeographicBreakdownSupport",
    "GeographicPlaceArea",
    "GeographicPlaceCatalogCapability",
    "GeographicPlaceCatalogEntry",
    "GeographicPlaceIdentifierSystem",
    "GeographicPlaceIdentifierSystem1",
    "GeographicPlaceIdentifierSystem2",
    "GeographicPlaceRequirement",
    "GeographicPlaceResolver",
    "GeographicPlaceSystemSupport",
    "GeographicPlaceType",
    "GeographicPlaceType1",
    "GeographicPlaceType2",
    "GeographicRegionRequirement",
    "GeographicRegionSupport",
    "GetCreativeFeaturesSubmitted",
    "GetGeographicPlaceResolutionRequest",
    "GetGeographicPlaceResolutionResponse",
    "GetProductsInputRequired",
    "GetProductsSubmitted",
    "GetProductsWorking",
    "GetSignalsSubmitted",
    "GetSignalsWorking",
    "GoldenVectors",
    "GopType",
    "GovernanceAgent",
    "GradingProfile",
    "GrantStatus",
    "GroupFormatAssetUnion",
    "HistoryItem",
    "HotelItem",
    "HtmlAssetRequirements",
    "HttpMethod",
    "IanaTimezoneIdentifier",
    "IanaTimezones",
    "IfNotCovered",
    "ImageAssetRequirements",
    "ImageDecoration",
    "ImageRef",
    "Immutability",
    "Impact",
    "Impairment",
    "Indicator",
    "IndicatorBearingResourceState",
    "IndicatorScope",
    "IndicatorsChangedWebhook",
    "IndustryIdentifier",
    "Input",
    "InputFormat",
    "InputFormat1",
    "InputFormat10",
    "InputFormat11",
    "InputFormat12",
    "InputFormat13",
    "InputFormat14",
    "InputFormat15",
    "InputFormat16",
    "InputFormat17",
    "InputFormat18",
    "InputFormat19",
    "InputFormat2",
    "InputFormat20",
    "InputFormat21",
    "InputFormat22",
    "InputFormat23",
    "InputFormat24",
    "InputFormat25",
    "InputFormat26",
    "InputFormat27",
    "InputFormat28",
    "InputFormat29",
    "InputFormat3",
    "InputFormat30",
    "InputFormat31",
    "InputFormat32",
    "InputFormat33",
    "InputFormat4",
    "InputFormat5",
    "InputFormat6",
    "InputFormat7",
    "InputFormat8",
    "InputFormat9",
    "InsertionOrder",
    "Installment",
    "InstallmentDeadlines",
    "InstallmentDeliveryMetrics",
    "InstallmentPropertyDeliveryMetrics",
    "InstallmentReference",
    "Intent",
    "IntervalId",
    "InventoryListApplication",
    "InventoryListApplication1",
    "InventoryListApplication2",
    "Issue",
    "IssueId",
    "IssueState",
    "Issuer5",
    "Issuer6",
    "Issuer8",
    "Issuer9",
    "Items",
    "JavascriptAssetRequirements",
    "JavascriptModuleType",
    "JobItem",
    "Jurisdiction2",
    "Keyword",
    "KeywordDeliveryMetrics",
    "KeywordRequirement",
    "KeywordRequirement1",
    "KeywordSupport",
    "KeywordSupport1",
    "LanguageTag",
    "LatencyPercentiles",
    "Layer",
    "Level",
    "Limitation",
    "LimitedSeries",
    "ListingType",
    "LocalizedCreativeAsset",
    "LocalizedCreativeAsset1",
    "LocalizedCreativeAsset10",
    "LocalizedCreativeAsset11",
    "LocalizedCreativeAsset12",
    "LocalizedCreativeAsset13",
    "LocalizedCreativeAsset14",
    "LocalizedCreativeAsset15",
    "LocalizedCreativeAsset16",
    "LocalizedCreativeAsset17",
    "LocalizedCreativeAsset18",
    "LocalizedCreativeAsset19",
    "LocalizedCreativeAsset2",
    "LocalizedCreativeAsset20",
    "LocalizedCreativeAsset21",
    "LocalizedCreativeAsset22",
    "LocalizedCreativeAsset3",
    "LocalizedCreativeAsset4",
    "LocalizedCreativeAsset5",
    "LocalizedCreativeAsset7",
    "LocalizedCreativeAsset8",
    "LocalizedCreativeAsset9",
    "Location1",
    "Location13",
    "Location17",
    "Location18",
    "Location2",
    "Location26",
    "Location4",
    "Location5",
    "Location6",
    "Location8",
    "Location9",
    "Locator",
    "Locator1",
    "ME",
    "MacroBearingUrl1",
    "MacroBearingUrl2",
    "MacroBearingUrl3",
    "MacroBearingUrl4",
    "MacroDeclaration1",
    "MacroDeclaration10",
    "MacroDeclaration12",
    "MacroDeclaration15",
    "MacroDeclaration16",
    "MacroDeclaration2",
    "MacroDeclaration20",
    "MacroDeclaration3",
    "MacroDeclaration4",
    "MacroDeclaration5",
    "MacroDeclaration6",
    "MacroDeclaration7",
    "MacroDeclaration8",
    "MacroDeclaration9",
    "MacroDeclarationModel",
    "MacroDialect",
    "MacroMappingStatus",
    "MacroProcessingCapability",
    "MacroProcessingOperation",
    "MacroResolutionResult",
    "MacroResolver",
    "MacroValueContext",
    "MappingStatus",
    "MarkdownAssetRequirements",
    "MarkdownFlavor",
    "Market",
    "MaterialDeadline",
    "MaterialStage",
    "MaterialSubmission",
    "MaxBidWithCostPer",
    "MaxBidWithRoas",
    "McpWebhookPayload",
    "MeasurementPeriod",
    "MeasurementReadiness",
    "MeasurementTerms",
    "MeasurementWindow",
    "MediaBuy",
    "MediaBuyAvailableAction",
    "MediaBuyAvailableActionId",
    "MediaBuyChangeTermId",
    "MediaBuyFeatures",
    "MediaBuyFrequencyCap",
    "MediaBuyFrequencyCapCapability",
    "MediaBuyFrequencyCapRequirement",
    "MediaBuyFrequencyCapSupport",
    "MediaBuyTermsReference",
    "Metric7",
    "MetricOptimization",
    "MetroRequirement",
    "MetroRequirement1",
    "MetroSupport",
    "MetroSupport1",
    "Mileage",
    "MimeType",
    "MismatchCode",
    "MissingMetric",
    "MissingMetric1",
    "MissingMetric2",
    "Modality",
    "ModuleType",
    "MoovAtomPosition",
    "Multiplicity",
    "NativeCommitEvidence",
    "NegativeKeyword",
    "NonblockingImpact",
    "NonblockingImpacts",
    "NotificationConfig",
    "NotificationType",
    "ObservedThrough",
    "Offering",
    "OfferingAssetConstraint",
    "OfferingAssetGroup",
    "OohMetrics",
    "Operation",
    "OperationsContact",
    "Operator",
    "OperatorIdentity",
    "OperatorUnit",
    "OpportunityContext",
    "OptimizationGoal1",
    "OptimizationGoal10",
    "OptimizationGoal2",
    "OptimizationGoal3",
    "OptimizationGoal4",
    "OptimizationGoal5",
    "OptimizationGoal6",
    "OptimizationGoal7",
    "OptimizationGoal8",
    "OptimizationGoal9",
    "Option",
    "OrderingEncoding",
    "Outcome",
    "OutcomeMeasurement",
    "OutcomeTargetCostPer",
    "OutputCapabilityId",
    "Overlay",
    "Package",
    "PackageDeliveryMetricValue",
    "PackageFormatSnapshot",
    "PackageFormatSnapshot1",
    "PackageFormatSnapshot10",
    "PackageFormatSnapshot11",
    "PackageFormatSnapshot12",
    "PackageFormatSnapshot13",
    "PackageFormatSnapshot14",
    "PackageFormatSnapshot15",
    "PackageFormatSnapshot16",
    "PackageFormatSnapshot17",
    "PackageFormatSnapshot18",
    "PackageFormatSnapshot19",
    "PackageFormatSnapshot2",
    "PackageFormatSnapshot20",
    "PackageFormatSnapshot21",
    "PackageFormatSnapshot22",
    "PackageFormatSnapshot23",
    "PackageFormatSnapshot24",
    "PackageFormatSnapshot25",
    "PackageFormatSnapshot26",
    "PackageFormatSnapshot27",
    "PackageFormatSnapshot28",
    "PackageFormatSnapshot29",
    "PackageFormatSnapshot3",
    "PackageFormatSnapshot30",
    "PackageFormatSnapshot31",
    "PackageFormatSnapshot32",
    "PackageFormatSnapshot33",
    "PackageFormatSnapshot4",
    "PackageFormatSnapshot5",
    "PackageFormatSnapshot6",
    "PackageFormatSnapshot7",
    "PackageFormatSnapshot8",
    "PackageFormatSnapshot9",
    "PackageSignalTargeting",
    "PackageSignalTargeting1",
    "PackageSignalTargeting2",
    "PackageSignalTargeting3",
    "PackageSignalTargeting4",
    "PackageSignalTargeting5",
    "PackageSignalTargeting6",
    "PackageSignalTargeting7",
    "PackageSignalTargetingGroup",
    "PackageSignalTargetingGroups",
    "PackageTargetingResolution",
    "PaginationRequest",
    "PaginationResponse",
    "Panel",
    "ParentLabel",
    "Path",
    "Payload1",
    "Payload10",
    "Payload11",
    "Payload12",
    "Payload13",
    "Payload14",
    "Payload17",
    "Payload18",
    "Payload2",
    "Payload20",
    "Payload21",
    "Payload22",
    "Payload23",
    "Payload24",
    "Payload25",
    "Payload3",
    "Payload4",
    "Payload5",
    "Payload6",
    "Payload7",
    "Payload8",
    "Payload9",
    "PaymentTerms",
    "PerItemBindings",
    "PerformanceFeedback",
    "PerformanceFeedbackAssertion",
    "PerformanceFeedbackMetric",
    "PerformanceFeedbackMetric1",
    "PerformanceFeedbackMetric2",
    "PerformanceStandard",
    "PeriodAnchorPolicy",
    "PeriodTimezonePolicy",
    "Phase",
    "PhysicalChecksums",
    "PhysicalChecksums1",
    "PixelRatio",
    "PixelTrackingEvent",
    "PlaceCatalogSupport",
    "PlaceSupport",
    "Placement",
    "PlacementDefinition",
    "PlacementDeliveryMetrics",
    "PlacementEvidence",
    "PlacementForecastDimension",
    "PlacementIdentity",
    "PlacementIdentity1",
    "PlacementIdentity2",
    "PlacementPresentationDocument",
    "PlacementPresentationReference",
    "PlacementPropertyDeliveryMetrics",
    "PlacementReference",
    "PlacementSelection1",
    "PlacementSelection2",
    "PlannedDelivery",
    "Platform",
    "PlatformExtensionRef",
    "PlatformExtensionReference",
    "Point",
    "PolicyProfile",
    "PositivePostalAreaSupport",
    "PostalArea",
    "PostalArea1",
    "PostalArea11",
    "PostalArea110",
    "PostalArea111",
    "PostalArea112",
    "PostalArea113",
    "PostalArea114",
    "PostalArea115",
    "PostalArea116",
    "PostalArea117",
    "PostalArea118",
    "PostalArea119",
    "PostalArea12",
    "PostalArea120",
    "PostalArea121",
    "PostalArea13",
    "PostalArea14",
    "PostalArea15",
    "PostalArea16",
    "PostalArea17",
    "PostalArea18",
    "PostalArea19",
    "PostalArea2",
    "PostalAreaSupport",
    "PostalAreaSupportAdditionalPropertyEnum",
    "PostalCountrySystem",
    "PostalCountrySystem1",
    "PostalCountrySystem10",
    "PostalCountrySystem2",
    "PostalCountrySystem3",
    "PostalCountrySystem4",
    "PostalCountrySystem5",
    "PostalCountrySystem6",
    "PostalCountrySystem7",
    "PostalCountrySystem8",
    "PostalCountrySystem9",
    "Posting",
    "Presence",
    "PreviewRendererMetadata",
    "Price",
    "PricingModel",
    "PricingOption",
    "PrincipalChangedWebhook",
    "PrincipalDeclarationsState",
    "PrincipalState",
    "PriorDestinationRef",
    "ProducerIdentity",
    "Product",
    "ProductAllocation",
    "ProductAllowedAction",
    "ProductAudienceEvidenceRequirements",
    "ProductCard",
    "ProductCardDetailed",
    "ProductCardReferenceAsset",
    "ProductChangeMap",
    "ProductChangeMap1",
    "ProductDoohPlacementAttributes",
    "ProductDoohScreenResolution",
    "ProductExecutionRequirement",
    "ProductExecutionRequirement1",
    "ProductExecutionRequirement2",
    "ProductExecutionRequirement3",
    "ProductFilters",
    "ProductFormatDeclaration",
    "ProductFormatDeclaration1",
    "ProductFormatDeclaration10",
    "ProductFormatDeclaration11",
    "ProductFormatDeclaration12",
    "ProductFormatDeclaration13",
    "ProductFormatDeclaration14",
    "ProductFormatDeclaration15",
    "ProductFormatDeclaration16",
    "ProductFormatDeclaration2",
    "ProductFormatDeclaration3",
    "ProductFormatDeclaration4",
    "ProductFormatDeclaration5",
    "ProductFormatDeclaration6",
    "ProductFormatDeclaration7",
    "ProductFormatDeclaration8",
    "ProductFormatDeclaration9",
    "ProductIdentity",
    "ProductMediaBuySupport",
    "ProductMediaBuySupportRequirements",
    "ProductOfferFilters",
    "ProductSignalTargetingOption",
    "ProductTargetingResolution",
    "ProductionStatus",
    "Progress",
    "Property",
    "PropertyDeliveryMetrics",
    "PropertyId",
    "PropertyListReference",
    "PropertyPayload",
    "PropertyReference",
    "PropertySource",
    "PropertyTag",
    "PropertyType",
    "Proposal",
    "ProposalKind",
    "Protocol",
    "ProtocolEnvelope",
    "ProtocolResponse",
    "ProvenanceRequirements",
    "PublishedPostAsset1",
    "PublishedPostAsset2",
    "PublisherAdagentsPayload",
    "PublisherDesignatedPreviewProvider",
    "PublisherDoohPlacementAttributes",
    "PublisherDoohScreenResolution",
    "PublisherProperty1",
    "PublisherProperty2",
    "PublisherProperty3",
    "PublisherProperty4",
    "PublisherProperty5",
    "PublisherProperty6",
    "PublisherProperty7",
    "PublisherProperty81",
    "PublisherProperty82",
    "PublisherProperty83",
    "PublisherProperty84",
    "PublisherProperty85",
    "PublisherProperty86",
    "PublisherProperty87",
    "PublisherPropertySelector",
    "PublisherPropertySelector1",
    "PublisherPropertySelector2",
    "PublisherPropertySelector3",
    "PushNotificationConfig",
    "Qualifier1",
    "Qualifier3",
    "QualifierModel",
    "QuartileData",
    "QuerySummary",
    "RankByItem",
    "RankByItem1",
    "RankByItem2",
    "ReachWindow",
    "Readiness",
    "RealEstateItem",
    "Recipient",
    "RecommendedAction",
    "ReconciliationStatus",
    "Recovery",
    "Rectangle",
    "Reference",
    "ReferenceAuthorization1",
    "ReferenceRenderer",
    "RefineProposalsInputRequired",
    "RefineProposalsSubmitted",
    "RefineProposalsWorking",
    "RegistryEvent",
    "RegistryEvent1",
    "RegistryEvent10",
    "RegistryEvent11",
    "RegistryEvent12",
    "RegistryEvent13",
    "RegistryEvent14",
    "RegistryEvent15",
    "RegistryEvent16",
    "RegistryEvent17",
    "RegistryEvent18",
    "RegistryEvent19",
    "RegistryEvent2",
    "RegistryEvent20",
    "RegistryEvent3",
    "RegistryEvent4",
    "RegistryEvent5",
    "RegistryEvent6",
    "RegistryEvent7",
    "RegistryEvent8",
    "RegistryEvent9",
    "RegistryFeedResponse",
    "RejectionCode",
    "RelatedCollection",
    "RelationshipKind",
    "RemovalReason",
    "RenderingOrigin",
    "Renders",
    "Renders1",
    "Repair",
    "ReplacedByValue",
    "ReportCalendarTimezoneBasis",
    "ReportingAdjustment",
    "ReportingAdjustmentReceipt",
    "ReportingAdjustmentRejectionCode",
    "ReportingBucket",
    "ReportingCanonicalContentDigest",
    "ReportingCanonicalizationContract",
    "ReportingCapabilities",
    "ReportingCloud",
    "ReportingConsumerStatus",
    "ReportingControlTotal",
    "ReportingControlTotal1",
    "ReportingControlTotal2",
    "ReportingCoverage",
    "ReportingDatasetShareDestination",
    "ReportingDatasetShareDestination1",
    "ReportingDatasetShareDestination2",
    "ReportingDeliveryCapabilities",
    "ReportingDeliveryConfigLifecycleState",
    "ReportingDeliveryConfiguration",
    "ReportingDeliveryConfigurationState",
    "ReportingDeliveryMethod",
    "ReportingDeliveryMethod1",
    "ReportingDeliveryMethod2",
    "ReportingDeliveryMethod3",
    "ReportingDeliveryOffering",
    "ReportingDeliveryOfferingId",
    "ReportingDeliveryPattern",
    "ReportingDeliveryReadyWebhook",
    "ReportingFeedPurpose",
    "ReportingFileCompression",
    "ReportingFileEntry",
    "ReportingFileManifest",
    "ReportingFileObjectReference",
    "ReportingFrequency",
    "ReportingLedgerChangedWebhook",
    "ReportingMaterialization",
    "ReportingMediaBuyId",
    "ReportingMode",
    "ReportingNativeVersionReference",
    "ReportingObligation",
    "ReportingOrchestration",
    "ReportingPackageId",
    "ReportingPrimaryKey",
    "ReportingProfile",
    "ReportingReaderCompatibilityItem",
    "ReportingReceipt",
    "ReportingReconciliationMode",
    "ReportingReliabilityMeasurementPeriod",
    "ReportingReliabilityStatistics",
    "ReportingReportDefinition",
    "ReportingResource",
    "ReportingRevision",
    "ReportingSchedule",
    "ReportingScheduleAlignment",
    "ReportingScheduleOffering",
    "ReportingStatusChangedWebhook",
    "ReportingStatusIssue",
    "ReportingStatusSeverity",
    "ReportingVerification",
    "ReportingVerificationProfile",
    "ReportingVerificationProfileSet",
    "ReportingVerificationProfileSetEnum",
    "ReportingWebhook",
    "ReportingWriteDestination",
    "ReportingWriteDestination1",
    "ReportingWriteDestination2",
    "RepresentationDestination",
    "RepresentationRejection",
    "RepresentationSelection",
    "RequestProposalsInputRequired",
    "RequestProposalsSubmitted",
    "RequestProposalsWorking",
    "Required",
    "RequiredGeoTargetingItem",
    "RequiredPerformanceStandard",
    "ResolutionModel",
    "ResolvedAssets",
    "ResolvedAssets1",
    "ResolvedBy",
    "Resolver",
    "ResourceRef",
    "ResponsePayload",
    "ResponsePayloadJwsEnvelope",
    "ResponsibleParty",
    "Responsive",
    "RestatementPolicy",
    "Restriction",
    "RetiredDestination",
    "RightsAgent",
    "RightsAttestationEvaluation",
    "RightsConstraint",
    "Roas",
    "RoasStrength",
    "Role2",
    "RotationMode",
    "Route",
    "Salary",
    "SampleRate",
    "Sandbox",
    "ScalarBinding",
    "ScanType",
    "ScopeCapability",
    "ScopeName",
    "ScopedCreativeApproval",
    "Selector",
    "SellerAgentReference",
    "Severity",
    "Signal",
    "SignalCoverageForecast",
    "SignalDefinition",
    "SignalDefinitionEnrichment",
    "SignalFilters",
    "SignalForecastDimension",
    "SignalId8",
    "SignalId9",
    "SignalListing",
    "SignalModelingDisclosure",
    "SignalPricingOption",
    "SignalRef",
    "SignalRef1",
    "SignalRef2",
    "SignalRef3",
    "SignalSelectionGroupRule",
    "SignalTag",
    "SignalTargeting",
    "SignalTargeting1",
    "SignalTargeting2",
    "SignalTargeting3",
    "SignalTargetingExpression",
    "SignalTargetingExpression1",
    "SignalTargetingExpression2",
    "SignalTargetingExpression3",
    "SignalTargetingItem",
    "SignalTargetingItem1",
    "SignalTargetingItem2",
    "SignalTargetingItem3",
    "SignalTargetingItem4",
    "SignalTargetingItem5",
    "SignalTargetingItem6",
    "SignalTargetingItem7",
    "SignalTargetingRules",
    "SlaWindow",
    "Sort",
    "SortApplied",
    "Special",
    "Specification",
    "SpotReportingCapability",
    "StartTiming",
    "StoreItem",
    "Storyboard",
    "StoryboardStatus",
    "Strength",
    "Strength1",
    "StringArray",
    "Subject10",
    "Subject11",
    "Subject12",
    "Subject13",
    "Subject14",
    "Subject15",
    "Subject16",
    "Subject17",
    "Subject19",
    "Subject2",
    "Subject20",
    "Subject21",
    "Subject22",
    "Subject23",
    "Subject24",
    "Subject25",
    "Subject26",
    "Subject27",
    "Subject29",
    "Subject3",
    "Subject31",
    "Subject32",
    "Subject33",
    "Subject34",
    "Subject35",
    "Subject36",
    "Subject37",
    "Summary",
    "Summary2",
    "Supported",
    "SupportedDeliveryMethod",
    "SupportedMetric",
    "SupportedTarget5",
    "SupportedTimezone",
    "SupportedVersion",
    "SupportedViewDuration",
    "SyncCatalogsInputRequired",
    "SyncCatalogsSubmitted",
    "SyncCatalogsWorking",
    "SyncCreativesInputRequired",
    "SyncCreativesSubmitted",
    "SyncCreativesWorking",
    "System1",
    "System11",
    "System12",
    "System13",
    "System19",
    "System2",
    "System3",
    "System9",
    "Tag",
    "Talent",
    "Target1",
    "Target10",
    "Target11",
    "Target14",
    "Target15",
    "Target16",
    "Target17",
    "Target18",
    "Target19",
    "Target3",
    "Target4",
    "Target5",
    "Target6",
    "Target7",
    "Target8",
    "TargetVariant",
    "TargetingModification",
    "TargetingModification1",
    "TargetingModification2",
    "TargetingOverlay",
    "TargetingOverlayInput",
    "TargetingOverlayRequirements",
    "TargetingOverlaySupport",
    "TargetingUnknownAgeEligibilityConstraint",
    "TargetingVerifiedAgeBasisConstraint",
    "TasksGetRequest",
    "TasksGetResponse",
    "TasksListRequest",
    "TasksListResponse",
    "Terms",
    "TextAssetRequirements",
    "TextDecoration",
    "TimeBasedView",
    "TimeForecastDimension",
    "TotalBudget",
    "TrackerExecutionContract",
    "TrackerExecutionSelector",
    "TrackerExecutionSelector1",
    "TrackerExecutionSelector2",
    "TrackerExecutionSelector3",
    "Tracks",
    "Transformer",
    "TransformerParam",
    "Transition",
    "Transmission",
    "TruncationSentinel",
    "Trust",
    "UnavailableReason",
    "UniversalMacro",
    "UnknownFormatAsset",
    "UnknownGroupAsset",
    "UnknownHandling",
    "UpdateFrequency",
    "UpdateMediaBuyInputRequired",
    "UpdateMediaBuySubmitted",
    "UpdateMediaBuyWorking",
    "UrlAssetRequirements",
    "UrlAssetType",
    "UserMatch",
    "ValidityHint",
    "ValueSource",
    "VariableType",
    "VariantDimension",
    "Variants",
    "Variants1",
    "Variants2",
    "VastAsset1",
    "VastAsset2",
    "VastAsset3",
    "VastAsset4",
    "VastAssetRequirements",
    "VastEvent",
    "VastMediafileRequirements",
    "VastOffset",
    "VastTarget",
    "VastTrackerConstraints",
    "VastTrackingEvent",
    "VastVersion",
    "VastVersions",
    "VehicleItem",
    "VendorMetricId",
    "VendorMetricOptimization",
    "VendorMetricOptimizationSupportedMetric",
    "VendorMetricValue",
    "VendorPricing",
    "VendorPricing1",
    "VendorPricing2",
    "VendorPricing3",
    "VendorPricing4",
    "VendorPricing5",
    "VendorPricingOption",
    "VendorPricingOption1",
    "VendorPricingOption10",
    "VendorPricingOption11",
    "VendorPricingOption2",
    "VendorPricingOption3",
    "VendorPricingOption4",
    "VendorPricingOption5",
    "VendorPricingOption6",
    "VendorPricingOption7",
    "VendorPricingOption8",
    "VendorPricingOption9",
    "VenueBreakdownItem",
    "VerificationPath",
    "VerificationTokenGradingProfile",
    "VerificationTokenMode",
    "VerifiedAttestationDigest",
    "VerifyAgent1",
    "VerifyAgent18",
    "VideoAssetRequirements",
    "Viewability1",
    "ViewableRate",
    "ViewedSecondsHistogramItem",
    "ViewedSecondsPercentiles",
    "Visual",
    "VoiceSynthesisRefItem",
    "Warning",
    "WarningAffectedResource",
    "WatermarkMediaType",
    "WebhookActivityRecord",
    "WebhookAssetRequirements",
    "WebhookChallenge",
    "WebhookChallengeResponse",
    "WebhookResponseType",
    "WebhookSecurityMethod",
    "WebhookSigningAlgorithm",
    "WholesaleFeedEvent",
    "WholesaleFeedEvent1",
    "WholesaleFeedEvent2",
    "WholesaleFeedEvent3",
    "WholesaleFeedEvent4",
    "WholesaleFeedEvent5",
    "WholesaleFeedEvent6",
    "WholesaleFeedEvent7",
    "WholesaleFeedEvent8",
    "WholesaleFeedEvent9",
    "WholesaleFeedWebhook",
    "XEntityTypes",
]
