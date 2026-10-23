"""Types the AdCP ``enums`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.enums import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.enums.<schema>``.
Nothing here is renamed.

This facade is LAZY (PEP 562). It binds no name at import time, because an
eager one makes every leaf of this domain depend on every other leaf: a
submodule import runs this ``__init__``, so one leaf's dependencies become
the whole domain's. That turned a benign submodule-level cycle between
``core`` and ``formats.canonical`` -- two domains whose schemas reference
each other, which Python handles -- into a package-level one that made all
16 modules under ``formats/canonical/`` unimportable on their own.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-16 22:14:49 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The whole importable surface, so type checkers and IDEs see it. Runtime
    # resolution is ``__getattr__`` below; mypy never sees that, so a typo'd
    # import is still an error rather than ``object``.
    from adcp.types.domains.enums.account_currency_mode import AccountCurrencyMode
    from adcp.types.domains.enums.account_scope import AccountScope
    from adcp.types.domains.enums.account_status import AccountStatus
    from adcp.types.domains.enums.action_not_allowed_reason import ActionNotAllowedReason
    from adcp.types.domains.enums.action_source import ActionSource
    from adcp.types.domains.enums.activation_method import CreativeActivationMethod
    from adcp.types.domains.enums.adcp_protocol import AdcpProtocol
    from adcp.types.domains.enums.adjustment_kind import PriceAdjustmentKind
    from adcp.types.domains.enums.advertiser_industry import AdvertiserIndustry
    from adcp.types.domains.enums.age_determination_basis import AgeDeterminationBasis
    from adcp.types.domains.enums.age_verification_method import AgeVerificationMethod
    from adcp.types.domains.enums.assessment_status import AssessmentStatus
    from adcp.types.domains.enums.asset_content_type import AssetContentType
    from adcp.types.domains.enums.attestation_claim import AttestationClaim
    from adcp.types.domains.enums.attribution_methodology import AttributionMethodology
    from adcp.types.domains.enums.attribution_model import AttributionModel
    from adcp.types.domains.enums.audience_evidence_methodology import AudienceEvidenceMethodology
    from adcp.types.domains.enums.audience_resolution_method import AudienceResolutionMethod
    from adcp.types.domains.enums.audience_source import AudienceSource
    from adcp.types.domains.enums.audience_status import AudienceStatus
    from adcp.types.domains.enums.audience_subject_type import AudienceSubjectType
    from adcp.types.domains.enums.audio_channel_layout import AudioChannelLayout
    from adcp.types.domains.enums.audio_distribution_type import AudioDistributionType
    from adcp.types.domains.enums.auth_scheme import AuthenticationScheme
    from adcp.types.domains.enums.availability_status import AvailabilityStatus
    from adcp.types.domains.enums.available_metric import AvailableMetric
    from adcp.types.domains.enums.billing_party import BillingParty
    from adcp.types.domains.enums.binary_verdict import BinaryVerdict
    from adcp.types.domains.enums.brand_agent_type import BrandAgentType
    from adcp.types.domains.enums.browser_family import BrowserFamily
    from adcp.types.domains.enums.c2pa_watermark_action import C2PaWatermarkAction
    from adcp.types.domains.enums.canceled_by import CanceledBy
    from adcp.types.domains.enums.canonical_media_buy_action import CanonicalMediaBuyActionName
    from adcp.types.domains.enums.canonical_media_buy_action_mode import CanonicalMediaBuyActionMode
    from adcp.types.domains.enums.catalog_action import CatalogAction
    from adcp.types.domains.enums.catalog_item_status import CatalogItemStatus
    from adcp.types.domains.enums.catalog_type import CatalogType
    from adcp.types.domains.enums.channels import MediaChannel
    from adcp.types.domains.enums.cloud_storage_protocol import CloudStorageProtocol
    from adcp.types.domains.enums.co_branding_requirement import CoBrandingRequirement
    from adcp.types.domains.enums.collection_cadence import CollectionCadence
    from adcp.types.domains.enums.collection_kind import CollectionKind
    from adcp.types.domains.enums.collection_relationship import CollectionRelationship
    from adcp.types.domains.enums.collection_status import CollectionStatus
    from adcp.types.domains.enums.completion_source import CompletionSource
    from adcp.types.domains.enums.consent_basis import ConsentBasis
    from adcp.types.domains.enums.content_id_type import ContentIdType
    from adcp.types.domains.enums.content_rating_system import ContentRatingSystem
    from adcp.types.domains.enums.creative_action import CreativeAction
    from adcp.types.domains.enums.creative_agent_capability import CreativeAgentCapability
    from adcp.types.domains.enums.creative_approval_status import CreativeApprovalStatus
    from adcp.types.domains.enums.creative_event_reason_code import CreativeEventReasonCode
    from adcp.types.domains.enums.creative_identifier_type import CreativeIdentifierType
    from adcp.types.domains.enums.creative_quality import CreativeQuality
    from adcp.types.domains.enums.creative_selection_strategy import CreativeSelectionStrategy
    from adcp.types.domains.enums.creative_sort_field import CreativeSortField
    from adcp.types.domains.enums.creative_status import CreativeStatus
    from adcp.types.domains.enums.ctv_ad_experience import CtvAdExperience
    from adcp.types.domains.enums.daast_tracking_event import DaastTrackingEvent
    from adcp.types.domains.enums.daast_version import DaastVersion
    from adcp.types.domains.enums.day_of_week import DayOfWeek
    from adcp.types.domains.enums.daypart_timezone_mode import DaypartTimezoneMode
    from adcp.types.domains.enums.delegation_authority import DelegationAuthority
    from adcp.types.domains.enums.delivery_recipient_cloud import DeliveryRecipientCloud
    from adcp.types.domains.enums.delivery_status import DeliveryStatus
    from adcp.types.domains.enums.delivery_type import DeliveryType
    from adcp.types.domains.enums.demographic_system import DemographicSystem
    from adcp.types.domains.enums.derivative_type import DerivativeType
    from adcp.types.domains.enums.device_platform import DevicePlatform
    from adcp.types.domains.enums.device_type import DeviceType
    from adcp.types.domains.enums.digital_source_type import DigitalSourceType
    from adcp.types.domains.enums.dimension_unit import DimensionUnit
    from adcp.types.domains.enums.disclosure_persistence import DisclosurePersistence
    from adcp.types.domains.enums.disclosure_position import DisclosurePosition
    from adcp.types.domains.enums.distance_unit import DistanceUnit
    from adcp.types.domains.enums.distribution_identifier_type import DistributionIdentifierType
    from adcp.types.domains.enums.dooh_motion_type import DoohMotionType
    from adcp.types.domains.enums.embedded_provenance_method import EmbeddedProvenanceMethod
    from adcp.types.domains.enums.error_code import ErrorCode
    from adcp.types.domains.enums.error_scope import ErrorScope
    from adcp.types.domains.enums.escalation_severity import EscalationSeverity
    from adcp.types.domains.enums.event_type import EventType
    from adcp.types.domains.enums.exclusivity import Exclusivity
    from adcp.types.domains.enums.feature_check_status import FeatureCheckStatus
    from adcp.types.domains.enums.feed_format import FeedFormat
    from adcp.types.domains.enums.feedback_source import FeedbackSource
    from adcp.types.domains.enums.forecast_method import ForecastMethod
    from adcp.types.domains.enums.forecast_range_unit import ForecastRangeUnit
    from adcp.types.domains.enums.forecastable_metric import ForecastableMetric
    from adcp.types.domains.enums.format_id_parameter import FormatIdParameter
    from adcp.types.domains.enums.frame_rate_type import FrameRateType
    from adcp.types.domains.enums.frequency_cap_control_mode import FrequencyCapControlMode
    from adcp.types.domains.enums.frequency_cap_mutable_field import FrequencyCapMutableField
    from adcp.types.domains.enums.frequency_cap_scope import FrequencyCapScope
    from adcp.types.domains.enums.genre_taxonomy import GenreTaxonomy
    from adcp.types.domains.enums.geo_level import GeographicTargetingLevel
    from adcp.types.domains.enums.gop_type import GopType
    from adcp.types.domains.enums.governance_decision import GovernanceDecision
    from adcp.types.domains.enums.governance_domain import GovernanceDomain
    from adcp.types.domains.enums.governance_mode import GovernanceMode
    from adcp.types.domains.enums.governance_phase import GovernancePhase
    from adcp.types.domains.enums.history_entry_type import HistoryEntryType
    from adcp.types.domains.enums.http_method import HttpMethod
    from adcp.types.domains.enums.identifier_types import PropertyIdentifierTypes
    from adcp.types.domains.enums.impairment_offline_state import ImpairmentOfflineState
    from adcp.types.domains.enums.impairment_reason_code import ImpairmentReasonCode
    from adcp.types.domains.enums.indicator_type import IndicatorType
    from adcp.types.domains.enums.installment_status import InstallmentStatus
    from adcp.types.domains.enums.javascript_module_type import JavascriptModuleType
    from adcp.types.domains.enums.landing_page_requirement import LandingPageRequirement
    from adcp.types.domains.enums.legacy_postal_system import CountryFusedPostalCodeSystem
    from adcp.types.domains.enums.lift_dimension import LiftDimension
    from adcp.types.domains.enums.logo_slot import LogoSlot
    from adcp.types.domains.enums.macro_dialect import MacroDialectFamily
    from adcp.types.domains.enums.macro_mapping_status import MacroMappingStatus
    from adcp.types.domains.enums.macro_processing_operation import MacroProcessingOperation
    from adcp.types.domains.enums.macro_resolution_reason import MacroResolutionReason
    from adcp.types.domains.enums.macro_resolver import MacroProcessingActor
    from adcp.types.domains.enums.macro_value_context import MacroValueContext
    from adcp.types.domains.enums.makegood_remedy import MakegoodRemedy
    from adcp.types.domains.enums.markdown_flavor import MarkdownFlavor
    from adcp.types.domains.enums.match_id_type import MatchIdType
    from adcp.types.domains.enums.match_type import MatchType
    from adcp.types.domains.enums.media_buy_action_mode import MediaBuyActionMode
    from adcp.types.domains.enums.media_buy_frequency_cap_control_mode import (
        MediaBuyFrequencyCapControlMode,
    )
    from adcp.types.domains.enums.media_buy_health import MediaBuyHealth
    from adcp.types.domains.enums.media_buy_status import MediaBuyStatus
    from adcp.types.domains.enums.media_buy_valid_action import MediaBuyValidAction
    from adcp.types.domains.enums.metric_scope import MetricScope
    from adcp.types.domains.enums.metric_type import MetricType
    from adcp.types.domains.enums.metro_system import MetroAreaSystem
    from adcp.types.domains.enums.moov_atom_position import MoovAtomPosition
    from adcp.types.domains.enums.motion_level import CreativeMotionLevel
    from adcp.types.domains.enums.notification_type import NotificationType
    from adcp.types.domains.enums.offering_availability_status import OfferingAvailabilityStatus
    from adcp.types.domains.enums.outcome_target_cost_strength import OutcomeTargetCostStrength
    from adcp.types.domains.enums.outcome_type import OutcomeType
    from adcp.types.domains.enums.pacing import Pacing
    from adcp.types.domains.enums.payment_terms import PaymentTerms
    from adcp.types.domains.enums.performance_baseline import PerformanceBaseline
    from adcp.types.domains.enums.performance_standard_metric import PerformanceStandardMetric
    from adcp.types.domains.enums.pixel_tracking_event import PixelTrackingEvent
    from adcp.types.domains.enums.policy_category import PolicyCategory
    from adcp.types.domains.enums.policy_enforcement import PolicyEnforcementLevel
    from adcp.types.domains.enums.postal_system import PostalCodeSystem
    from adcp.types.domains.enums.preview_output_format import PreviewOutputFormat
    from adcp.types.domains.enums.pricing_model import PricingModel
    from adcp.types.domains.enums.pricing_structure import PricingStructure
    from adcp.types.domains.enums.principal_kind import PrincipalKind
    from adcp.types.domains.enums.production_quality import ProductionQuality
    from adcp.types.domains.enums.property_type import PropertyType
    from adcp.types.domains.enums.proposal_decline_reason import ProposalDeclineReason
    from adcp.types.domains.enums.proposal_refinement_reason import ProposalRefinementReason
    from adcp.types.domains.enums.proposal_status import ProposalStatus
    from adcp.types.domains.enums.publisher_identifier_types import PublisherIdentifierTypes
    from adcp.types.domains.enums.purchase_type import PurchaseType
    from adcp.types.domains.enums.reach_aggregation import ReachAggregation
    from adcp.types.domains.enums.reach_unit import ReachUnit
    from adcp.types.domains.enums.reporting_destination_setup_state import (
        ReportingDestinationSetupState,
    )
    from adcp.types.domains.enums.reporting_finality import ReportingFinality
    from adcp.types.domains.enums.reporting_frequency import ReportingFrequency
    from adcp.types.domains.enums.reporting_health import ReportingHealth
    from adcp.types.domains.enums.representation_selection_strategy import (
        RepresentationSelectionStrategy,
    )
    from adcp.types.domains.enums.request_signing_error_code import RequestSigningErrorCode
    from adcp.types.domains.enums.response_type import TmpResponseType
    from adcp.types.domains.enums.restricted_attribute import RestrictedAttribute
    from adcp.types.domains.enums.right_type import RightType
    from adcp.types.domains.enums.right_use import RightUse
    from adcp.types.domains.enums.rights_billing_period import RightsBillingPeriod
    from adcp.types.domains.enums.scan_type import ScanType
    from adcp.types.domains.enums.seller_policy_decline_reason import SellerPolicyDeclineReason
    from adcp.types.domains.enums.si_session_status import SiSessionStatus
    from adcp.types.domains.enums.signal_catalog_type import (
        SignalAvailabilityType,
        SignalCatalogType,
    )
    from adcp.types.domains.enums.signal_source import SignalSource
    from adcp.types.domains.enums.signal_value_type import SignalValueType
    from adcp.types.domains.enums.snapshot_unavailable_reason import SnapshotUnavailableReason
    from adcp.types.domains.enums.social_placement_surface import SocialPlacementSurface
    from adcp.types.domains.enums.sort_direction import SortDirection
    from adcp.types.domains.enums.sort_metric import SortMetric
    from adcp.types.domains.enums.special_category import SpecialCategory
    from adcp.types.domains.enums.specialism import AdcpSpecialism
    from adcp.types.domains.enums.sponsored_placement_type import SponsoredPlacementType
    from adcp.types.domains.enums.talent_role import TalentRole
    from adcp.types.domains.enums.task_status import TaskStatus
    from adcp.types.domains.enums.task_type import TaskType
    from adcp.types.domains.enums.tracker_execution_actor import TrackerExecutionActor
    from adcp.types.domains.enums.tracker_firing_path import TrackerFiringPath
    from adcp.types.domains.enums.transport_mode import TransportMode
    from adcp.types.domains.enums.travel_time_unit import TravelTimeUnit
    from adcp.types.domains.enums.uid_type import UidType
    from adcp.types.domains.enums.universal_macro import UniversalMacro
    from adcp.types.domains.enums.update_frequency import UpdateFrequency
    from adcp.types.domains.enums.url_asset_type import UrlAssetType
    from adcp.types.domains.enums.validation_mode import ValidationMode
    from adcp.types.domains.enums.vast_media_delivery_method import VastMediaDeliveryMethod
    from adcp.types.domains.enums.vast_tracking_event import VastTrackingEvent
    from adcp.types.domains.enums.vast_version import VastVersion
    from adcp.types.domains.enums.vendor_relationship import VendorRelationship
    from adcp.types.domains.enums.video_placement_type import VideoPlacementType
    from adcp.types.domains.enums.view_threshold_basis import ViewThresholdBasis
    from adcp.types.domains.enums.viewability_standard import ViewabilityStandard
    from adcp.types.domains.enums.warning_code import WarningCode
    from adcp.types.domains.enums.watermark_media_type import WatermarkMediaType
    from adcp.types.domains.enums.wcag_level import WcagLevel
    from adcp.types.domains.enums.webhook_response_type import WebhookResponseType
    from adcp.types.domains.enums.webhook_security_method import WebhookSecurityMethod

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "enums.account_currency_mode": ("AccountCurrencyMode",),
    "enums.account_scope": ("AccountScope",),
    "enums.account_status": ("AccountStatus",),
    "enums.action_not_allowed_reason": ("ActionNotAllowedReason",),
    "enums.action_source": ("ActionSource",),
    "enums.activation_method": ("CreativeActivationMethod",),
    "enums.adcp_protocol": ("AdcpProtocol",),
    "enums.adjustment_kind": ("PriceAdjustmentKind",),
    "enums.advertiser_industry": ("AdvertiserIndustry",),
    "enums.age_determination_basis": ("AgeDeterminationBasis",),
    "enums.age_verification_method": ("AgeVerificationMethod",),
    "enums.assessment_status": ("AssessmentStatus",),
    "enums.asset_content_type": ("AssetContentType",),
    "enums.attestation_claim": ("AttestationClaim",),
    "enums.attribution_methodology": ("AttributionMethodology",),
    "enums.attribution_model": ("AttributionModel",),
    "enums.audience_evidence_methodology": ("AudienceEvidenceMethodology",),
    "enums.audience_resolution_method": ("AudienceResolutionMethod",),
    "enums.audience_source": ("AudienceSource",),
    "enums.audience_status": ("AudienceStatus",),
    "enums.audience_subject_type": ("AudienceSubjectType",),
    "enums.audio_channel_layout": ("AudioChannelLayout",),
    "enums.audio_distribution_type": ("AudioDistributionType",),
    "enums.auth_scheme": ("AuthenticationScheme",),
    "enums.availability_status": ("AvailabilityStatus",),
    "enums.available_metric": ("AvailableMetric",),
    "enums.billing_party": ("BillingParty",),
    "enums.binary_verdict": ("BinaryVerdict",),
    "enums.brand_agent_type": ("BrandAgentType",),
    "enums.browser_family": ("BrowserFamily",),
    "enums.c2pa_watermark_action": ("C2PaWatermarkAction",),
    "enums.canceled_by": ("CanceledBy",),
    "enums.canonical_media_buy_action": ("CanonicalMediaBuyActionName",),
    "enums.canonical_media_buy_action_mode": ("CanonicalMediaBuyActionMode",),
    "enums.catalog_action": ("CatalogAction",),
    "enums.catalog_item_status": ("CatalogItemStatus",),
    "enums.catalog_type": ("CatalogType",),
    "enums.channels": ("MediaChannel",),
    "enums.cloud_storage_protocol": ("CloudStorageProtocol",),
    "enums.co_branding_requirement": ("CoBrandingRequirement",),
    "enums.collection_cadence": ("CollectionCadence",),
    "enums.collection_kind": ("CollectionKind",),
    "enums.collection_relationship": ("CollectionRelationship",),
    "enums.collection_status": ("CollectionStatus",),
    "enums.completion_source": ("CompletionSource",),
    "enums.consent_basis": ("ConsentBasis",),
    "enums.content_id_type": ("ContentIdType",),
    "enums.content_rating_system": ("ContentRatingSystem",),
    "enums.creative_action": ("CreativeAction",),
    "enums.creative_agent_capability": ("CreativeAgentCapability",),
    "enums.creative_approval_status": ("CreativeApprovalStatus",),
    "enums.creative_event_reason_code": ("CreativeEventReasonCode",),
    "enums.creative_identifier_type": ("CreativeIdentifierType",),
    "enums.creative_quality": ("CreativeQuality",),
    "enums.creative_selection_strategy": ("CreativeSelectionStrategy",),
    "enums.creative_sort_field": ("CreativeSortField",),
    "enums.creative_status": ("CreativeStatus",),
    "enums.ctv_ad_experience": ("CtvAdExperience",),
    "enums.daast_tracking_event": ("DaastTrackingEvent",),
    "enums.daast_version": ("DaastVersion",),
    "enums.day_of_week": ("DayOfWeek",),
    "enums.daypart_timezone_mode": ("DaypartTimezoneMode",),
    "enums.delegation_authority": ("DelegationAuthority",),
    "enums.delivery_recipient_cloud": ("DeliveryRecipientCloud",),
    "enums.delivery_status": ("DeliveryStatus",),
    "enums.delivery_type": ("DeliveryType",),
    "enums.demographic_system": ("DemographicSystem",),
    "enums.derivative_type": ("DerivativeType",),
    "enums.device_platform": ("DevicePlatform",),
    "enums.device_type": ("DeviceType",),
    "enums.digital_source_type": ("DigitalSourceType",),
    "enums.dimension_unit": ("DimensionUnit",),
    "enums.disclosure_persistence": ("DisclosurePersistence",),
    "enums.disclosure_position": ("DisclosurePosition",),
    "enums.distance_unit": ("DistanceUnit",),
    "enums.distribution_identifier_type": ("DistributionIdentifierType",),
    "enums.dooh_motion_type": ("DoohMotionType",),
    "enums.embedded_provenance_method": ("EmbeddedProvenanceMethod",),
    "enums.error_code": ("ErrorCode",),
    "enums.error_scope": ("ErrorScope",),
    "enums.escalation_severity": ("EscalationSeverity",),
    "enums.event_type": ("EventType",),
    "enums.exclusivity": ("Exclusivity",),
    "enums.feature_check_status": ("FeatureCheckStatus",),
    "enums.feed_format": ("FeedFormat",),
    "enums.feedback_source": ("FeedbackSource",),
    "enums.forecast_method": ("ForecastMethod",),
    "enums.forecast_range_unit": ("ForecastRangeUnit",),
    "enums.forecastable_metric": ("ForecastableMetric",),
    "enums.format_id_parameter": ("FormatIdParameter",),
    "enums.frame_rate_type": ("FrameRateType",),
    "enums.frequency_cap_control_mode": ("FrequencyCapControlMode",),
    "enums.frequency_cap_mutable_field": ("FrequencyCapMutableField",),
    "enums.frequency_cap_scope": ("FrequencyCapScope",),
    "enums.genre_taxonomy": ("GenreTaxonomy",),
    "enums.geo_level": ("GeographicTargetingLevel",),
    "enums.gop_type": ("GopType",),
    "enums.governance_decision": ("GovernanceDecision",),
    "enums.governance_domain": ("GovernanceDomain",),
    "enums.governance_mode": ("GovernanceMode",),
    "enums.governance_phase": ("GovernancePhase",),
    "enums.history_entry_type": ("HistoryEntryType",),
    "enums.http_method": ("HttpMethod",),
    "enums.identifier_types": ("PropertyIdentifierTypes",),
    "enums.impairment_offline_state": ("ImpairmentOfflineState",),
    "enums.impairment_reason_code": ("ImpairmentReasonCode",),
    "enums.indicator_type": ("IndicatorType",),
    "enums.installment_status": ("InstallmentStatus",),
    "enums.javascript_module_type": ("JavascriptModuleType",),
    "enums.landing_page_requirement": ("LandingPageRequirement",),
    "enums.legacy_postal_system": ("CountryFusedPostalCodeSystem",),
    "enums.lift_dimension": ("LiftDimension",),
    "enums.logo_slot": ("LogoSlot",),
    "enums.macro_dialect": ("MacroDialectFamily",),
    "enums.macro_mapping_status": ("MacroMappingStatus",),
    "enums.macro_processing_operation": ("MacroProcessingOperation",),
    "enums.macro_resolution_reason": ("MacroResolutionReason",),
    "enums.macro_resolver": ("MacroProcessingActor",),
    "enums.macro_value_context": ("MacroValueContext",),
    "enums.makegood_remedy": ("MakegoodRemedy",),
    "enums.markdown_flavor": ("MarkdownFlavor",),
    "enums.match_id_type": ("MatchIdType",),
    "enums.match_type": ("MatchType",),
    "enums.media_buy_action_mode": ("MediaBuyActionMode",),
    "enums.media_buy_frequency_cap_control_mode": ("MediaBuyFrequencyCapControlMode",),
    "enums.media_buy_health": ("MediaBuyHealth",),
    "enums.media_buy_status": ("MediaBuyStatus",),
    "enums.media_buy_valid_action": ("MediaBuyValidAction",),
    "enums.metric_scope": ("MetricScope",),
    "enums.metric_type": ("MetricType",),
    "enums.metro_system": ("MetroAreaSystem",),
    "enums.moov_atom_position": ("MoovAtomPosition",),
    "enums.motion_level": ("CreativeMotionLevel",),
    "enums.notification_type": ("NotificationType",),
    "enums.offering_availability_status": ("OfferingAvailabilityStatus",),
    "enums.outcome_target_cost_strength": ("OutcomeTargetCostStrength",),
    "enums.outcome_type": ("OutcomeType",),
    "enums.pacing": ("Pacing",),
    "enums.payment_terms": ("PaymentTerms",),
    "enums.performance_baseline": ("PerformanceBaseline",),
    "enums.performance_standard_metric": ("PerformanceStandardMetric",),
    "enums.pixel_tracking_event": ("PixelTrackingEvent",),
    "enums.policy_category": ("PolicyCategory",),
    "enums.policy_enforcement": ("PolicyEnforcementLevel",),
    "enums.postal_system": ("PostalCodeSystem",),
    "enums.preview_output_format": ("PreviewOutputFormat",),
    "enums.pricing_model": ("PricingModel",),
    "enums.pricing_structure": ("PricingStructure",),
    "enums.principal_kind": ("PrincipalKind",),
    "enums.production_quality": ("ProductionQuality",),
    "enums.property_type": ("PropertyType",),
    "enums.proposal_decline_reason": ("ProposalDeclineReason",),
    "enums.proposal_refinement_reason": ("ProposalRefinementReason",),
    "enums.proposal_status": ("ProposalStatus",),
    "enums.publisher_identifier_types": ("PublisherIdentifierTypes",),
    "enums.purchase_type": ("PurchaseType",),
    "enums.reach_aggregation": ("ReachAggregation",),
    "enums.reach_unit": ("ReachUnit",),
    "enums.reporting_destination_setup_state": ("ReportingDestinationSetupState",),
    "enums.reporting_finality": ("ReportingFinality",),
    "enums.reporting_frequency": ("ReportingFrequency",),
    "enums.reporting_health": ("ReportingHealth",),
    "enums.representation_selection_strategy": ("RepresentationSelectionStrategy",),
    "enums.request_signing_error_code": ("RequestSigningErrorCode",),
    "enums.response_type": ("TmpResponseType",),
    "enums.restricted_attribute": ("RestrictedAttribute",),
    "enums.right_type": ("RightType",),
    "enums.right_use": ("RightUse",),
    "enums.rights_billing_period": ("RightsBillingPeriod",),
    "enums.scan_type": ("ScanType",),
    "enums.seller_policy_decline_reason": ("SellerPolicyDeclineReason",),
    "enums.si_session_status": ("SiSessionStatus",),
    "enums.signal_catalog_type": (
        "SignalAvailabilityType",
        "SignalCatalogType",
    ),
    "enums.signal_source": ("SignalSource",),
    "enums.signal_value_type": ("SignalValueType",),
    "enums.snapshot_unavailable_reason": ("SnapshotUnavailableReason",),
    "enums.social_placement_surface": ("SocialPlacementSurface",),
    "enums.sort_direction": ("SortDirection",),
    "enums.sort_metric": ("SortMetric",),
    "enums.special_category": ("SpecialCategory",),
    "enums.specialism": ("AdcpSpecialism",),
    "enums.sponsored_placement_type": ("SponsoredPlacementType",),
    "enums.talent_role": ("TalentRole",),
    "enums.task_status": ("TaskStatus",),
    "enums.task_type": ("TaskType",),
    "enums.tracker_execution_actor": ("TrackerExecutionActor",),
    "enums.tracker_firing_path": ("TrackerFiringPath",),
    "enums.transport_mode": ("TransportMode",),
    "enums.travel_time_unit": ("TravelTimeUnit",),
    "enums.uid_type": ("UidType",),
    "enums.universal_macro": ("UniversalMacro",),
    "enums.update_frequency": ("UpdateFrequency",),
    "enums.url_asset_type": ("UrlAssetType",),
    "enums.validation_mode": ("ValidationMode",),
    "enums.vast_media_delivery_method": ("VastMediaDeliveryMethod",),
    "enums.vast_tracking_event": ("VastTrackingEvent",),
    "enums.vast_version": ("VastVersion",),
    "enums.vendor_relationship": ("VendorRelationship",),
    "enums.video_placement_type": ("VideoPlacementType",),
    "enums.view_threshold_basis": ("ViewThresholdBasis",),
    "enums.viewability_standard": ("ViewabilityStandard",),
    "enums.warning_code": ("WarningCode",),
    "enums.watermark_media_type": ("WatermarkMediaType",),
    "enums.wcag_level": ("WcagLevel",),
    "enums.webhook_response_type": ("WebhookResponseType",),
    "enums.webhook_security_method": ("WebhookSecurityMethod",),
}

if not TYPE_CHECKING:
    import importlib

    _DECLARING_MODULE = {name: module for module, names in _DECLARED_BY.items() for name in names}

    def __getattr__(name: str) -> object:
        """Resolve a re-exported name by importing the ONE module declaring it."""
        module = _DECLARING_MODULE.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(f"adcp.types.domains.{module}"), name)
        globals()[name] = value  # cache: __getattr__ fires once per name
        return value

    def __dir__() -> list[str]:
        return sorted(set(__all__) | set(globals()))


# Explicit exports
__all__ = [
    "AccountCurrencyMode",
    "AccountScope",
    "AccountStatus",
    "ActionNotAllowedReason",
    "ActionSource",
    "AdcpProtocol",
    "AdcpSpecialism",
    "AdvertiserIndustry",
    "AgeDeterminationBasis",
    "AgeVerificationMethod",
    "AssessmentStatus",
    "AssetContentType",
    "AttestationClaim",
    "AttributionMethodology",
    "AttributionModel",
    "AudienceEvidenceMethodology",
    "AudienceResolutionMethod",
    "AudienceSource",
    "AudienceStatus",
    "AudienceSubjectType",
    "AudioChannelLayout",
    "AudioDistributionType",
    "AuthenticationScheme",
    "AvailabilityStatus",
    "AvailableMetric",
    "BillingParty",
    "BinaryVerdict",
    "BrandAgentType",
    "BrowserFamily",
    "C2PaWatermarkAction",
    "CanceledBy",
    "CanonicalMediaBuyActionMode",
    "CanonicalMediaBuyActionName",
    "CatalogAction",
    "CatalogItemStatus",
    "CatalogType",
    "CloudStorageProtocol",
    "CoBrandingRequirement",
    "CollectionCadence",
    "CollectionKind",
    "CollectionRelationship",
    "CollectionStatus",
    "CompletionSource",
    "ConsentBasis",
    "ContentIdType",
    "ContentRatingSystem",
    "CountryFusedPostalCodeSystem",
    "CreativeAction",
    "CreativeActivationMethod",
    "CreativeAgentCapability",
    "CreativeApprovalStatus",
    "CreativeEventReasonCode",
    "CreativeIdentifierType",
    "CreativeMotionLevel",
    "CreativeQuality",
    "CreativeSelectionStrategy",
    "CreativeSortField",
    "CreativeStatus",
    "CtvAdExperience",
    "DaastTrackingEvent",
    "DaastVersion",
    "DayOfWeek",
    "DaypartTimezoneMode",
    "DelegationAuthority",
    "DeliveryRecipientCloud",
    "DeliveryStatus",
    "DeliveryType",
    "DemographicSystem",
    "DerivativeType",
    "DevicePlatform",
    "DeviceType",
    "DigitalSourceType",
    "DimensionUnit",
    "DisclosurePersistence",
    "DisclosurePosition",
    "DistanceUnit",
    "DistributionIdentifierType",
    "DoohMotionType",
    "EmbeddedProvenanceMethod",
    "ErrorCode",
    "ErrorScope",
    "EscalationSeverity",
    "EventType",
    "Exclusivity",
    "FeatureCheckStatus",
    "FeedFormat",
    "FeedbackSource",
    "ForecastMethod",
    "ForecastRangeUnit",
    "ForecastableMetric",
    "FormatIdParameter",
    "FrameRateType",
    "FrequencyCapControlMode",
    "FrequencyCapMutableField",
    "FrequencyCapScope",
    "GenreTaxonomy",
    "GeographicTargetingLevel",
    "GopType",
    "GovernanceDecision",
    "GovernanceDomain",
    "GovernanceMode",
    "GovernancePhase",
    "HistoryEntryType",
    "HttpMethod",
    "ImpairmentOfflineState",
    "ImpairmentReasonCode",
    "IndicatorType",
    "InstallmentStatus",
    "JavascriptModuleType",
    "LandingPageRequirement",
    "LiftDimension",
    "LogoSlot",
    "MacroDialectFamily",
    "MacroMappingStatus",
    "MacroProcessingActor",
    "MacroProcessingOperation",
    "MacroResolutionReason",
    "MacroValueContext",
    "MakegoodRemedy",
    "MarkdownFlavor",
    "MatchIdType",
    "MatchType",
    "MediaBuyActionMode",
    "MediaBuyFrequencyCapControlMode",
    "MediaBuyHealth",
    "MediaBuyStatus",
    "MediaBuyValidAction",
    "MediaChannel",
    "MetricScope",
    "MetricType",
    "MetroAreaSystem",
    "MoovAtomPosition",
    "NotificationType",
    "OfferingAvailabilityStatus",
    "OutcomeTargetCostStrength",
    "OutcomeType",
    "Pacing",
    "PaymentTerms",
    "PerformanceBaseline",
    "PerformanceStandardMetric",
    "PixelTrackingEvent",
    "PolicyCategory",
    "PolicyEnforcementLevel",
    "PostalCodeSystem",
    "PreviewOutputFormat",
    "PriceAdjustmentKind",
    "PricingModel",
    "PricingStructure",
    "PrincipalKind",
    "ProductionQuality",
    "PropertyIdentifierTypes",
    "PropertyType",
    "ProposalDeclineReason",
    "ProposalRefinementReason",
    "ProposalStatus",
    "PublisherIdentifierTypes",
    "PurchaseType",
    "ReachAggregation",
    "ReachUnit",
    "ReportingDestinationSetupState",
    "ReportingFinality",
    "ReportingFrequency",
    "ReportingHealth",
    "RepresentationSelectionStrategy",
    "RequestSigningErrorCode",
    "RestrictedAttribute",
    "RightType",
    "RightUse",
    "RightsBillingPeriod",
    "ScanType",
    "SellerPolicyDeclineReason",
    "SiSessionStatus",
    "SignalAvailabilityType",
    "SignalCatalogType",
    "SignalSource",
    "SignalValueType",
    "SnapshotUnavailableReason",
    "SocialPlacementSurface",
    "SortDirection",
    "SortMetric",
    "SpecialCategory",
    "SponsoredPlacementType",
    "TalentRole",
    "TaskStatus",
    "TaskType",
    "TmpResponseType",
    "TrackerExecutionActor",
    "TrackerFiringPath",
    "TransportMode",
    "TravelTimeUnit",
    "UidType",
    "UniversalMacro",
    "UpdateFrequency",
    "UrlAssetType",
    "ValidationMode",
    "VastMediaDeliveryMethod",
    "VastTrackingEvent",
    "VastVersion",
    "VendorRelationship",
    "VideoPlacementType",
    "ViewThresholdBasis",
    "ViewabilityStandard",
    "WarningCode",
    "WatermarkMediaType",
    "WcagLevel",
    "WebhookResponseType",
    "WebhookSecurityMethod",
]
