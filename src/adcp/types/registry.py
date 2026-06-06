"""Registry API types generated from OpenAPI spec.

DO NOT EDIT — regenerate with:
    python scripts/generate_registry_types.py

Source: schemas/registry-openapi.yaml
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from pydantic import AnyUrl, AwareDatetime, Field, RootModel

from adcp.types.base import RegistryBaseModel

__all__ = [
    "KellerType",
    "BrandSource",
    "RegistryApiError",
    "BrandRegistrySource",
    "BrandRegistryItem",
    "ActivityRevision",
    "BrandActivity",
    "PropertyActivity",
    "PropertySource",
    "AuthorizedAgent",
    "AgentContact",
    "PropertyIdentifier",
    "PropertyRegistrySource",
    "PropertyRegistryItem",
    "ValidationResult",
    "AgentType",
    "AgentProtocol",
    "AgentDetailedContact",
    "AgentMember",
    "AgentHealth",
    "AgentStats",
    "AgentTool",
    "AgentStandardOperations",
    "AgentCreativeCapabilities",
    "SignalsCapabilities",
    "VerifiedByAao",
    "Accreditation",
    "Metric",
    "MeasurementCapabilities",
    "AgentCapabilities",
    "ComplianceStatus",
    "AgentLifecycleStage",
    "TrackDetail",
    "VerifiedRole",
    "AgentCompliance",
    "PropertySummary",
    "FederatedPublisher",
    "DomainAuthorizedAgent",
    "SalesAgentClaim",
    "DomainLookupResult",
    "AgentSource",
    "AuthorizedByItem",
    "Agent",
    "OperatorLookupResult",
    "DiscoveryMethod",
    "Mode",
    "Hosting",
    "Status1",
    "AdagentsJson",
    "Status2",
    "BrandJson",
    "Files",
    "Source5",
    "DelegationType",
    "Property1",
    "BrandSummary",
    "FormatSummary",
    "Source6",
    "AuthorizedAgent2",
    "RollupTruncated",
    "PublisherLookupResult",
    "PublisherPropertySelector",
    "SuccessLiteral",
    "AdagentsDiscoveryMethod",
    "AdagentsValidationSeverity",
    "AdagentsValidationIssue",
    "AdagentsValidationWarning",
    "AdagentsAuthorizationType",
    "PublisherPropertySelectionType",
    "AdagentsPublisherProperty",
    "CollectionRef",
    "AdagentsAuthorizedAgent",
    "CommunityMirrorSummary",
    "RateLimitError",
    "CommunityMirrorAdagentsJson",
    "CommunityMirrorPublishResponse",
    "CommunityMirrorPublishError",
    "CommunityMirrorPublishFormatsRequest",
    "CommunityMirrorPublishPropertiesRequest",
    "CommunityMirrorPublishPlacementsRequest",
    "CommunityMirrorPublishCollectionsRequest",
    "CommunityMirrorPublishSignalsRequest",
    "CommunityMirrorPublishRequest",
    "CommunityMirrorDeleteResponse",
    "PolicyCategory",
    "PolicyEnforcement",
    "PolicySourceType",
    "PolicyReviewStatus",
    "PolicySummary",
    "PolicyExemplarPass",
    "PolicyExemplarFail",
    "PolicyExemplars",
    "Policy",
    "PolicyRevision",
    "PolicyHistory",
    "Status3",
    "SpecialismStatus",
    "Status4",
    "StoryboardStatus",
    "Observation",
    "VerdictSource",
    "Role",
    "VerifiedSpecialism",
    "VerificationMode",
    "VerificationBadge",
    "AgentVerification",
    "StoryboardStatus1",
    "ComplianceRun",
    "RegistryMetadata",
    "MonitoringSettings",
    "ComplianceStepDiagnostic",
    "OutboundRequest",
    "AuthType",
    "AgentAuthStatus",
    "Code",
    "FieldModel",
    "CredentialSaveValidationError",
    "StoryboardSummary",
    "Agent1",
    "Step",
    "Phase",
    "StoryboardDetail",
    "CompanySearchResult",
    "MemberAgentVisibility",
    "MemberAgentType",
    "Code1",
    "Requested",
    "Applied",
    "Reason",
    "MemberAgentVisibilityWarning",
    "MemberAgentTypeInput",
    "MemberAgentPatch",
    "Organization",
    "CreateOrganizationResponse",
    "OrganizationCompanyType",
    "OrganizationRevenueTier",
    "CommunityMirrorCatalogDocument",
    "ResolvedBrand",
    "ResolvedPropertyEntry",
    "ResolvedProperty",
    "FederatedAgentWithDetails",
    "AdagentsValidationResult",
    "CommunityMirrorListResponse",
    "CommunityMirrorGetResponse",
    "AgentComplianceDetail",
    "FindCompanyResult",
    "MemberAgent",
    "MemberAgentResponse",
    "MemberAgentInput",
    "CreateOrganizationInput",
    "CreateAdagentsData",
    "CreateAdagentsResponse",
    "MemberAgentListResponse",
    "FeedEvent",
    "FeedPage",
]


class KellerType(Enum):
    master = "master"
    sub_brand = "sub_brand"
    endorsed = "endorsed"
    independent = "independent"


class BrandSource(Enum):
    brand_json = "brand_json"
    community = "community"
    enriched = "enriched"


class RegistryApiError(RegistryBaseModel):
    error: str


class BrandRegistrySource(Enum):
    hosted = "hosted"
    brand_json = "brand_json"
    community = "community"
    enriched = "enriched"


class BrandRegistryItem(RegistryBaseModel):
    domain: Annotated[str, Field(examples=["acmecorp.com"])]
    brand_name: Annotated[str | None, Field(examples=["Acme Corp"])] = None
    source: BrandRegistrySource
    has_manifest: bool
    verified: bool
    house_domain: str | None = None
    keller_type: KellerType | None = None


class ActivityRevision(RegistryBaseModel):
    revision_number: Annotated[int, Field(examples=[3])]
    editor_name: Annotated[str, Field(examples=["Pinnacle Media"])]
    edit_summary: Annotated[str, Field(examples=["Updated logo and brand colors"])]
    source: Annotated[
        str | None,
        Field(
            description="BrandSource type of the record at the time of this revision (brand_json, enriched, community)"
        ),
    ] = None
    is_rollback: bool
    rolled_back_to: Annotated[
        int | None,
        Field(
            description="ActivityRevision number that was restored; only present when is_rollback is true"
        ),
    ] = None
    created_at: Annotated[str, Field(examples=["2026-03-01T12:34:56Z"])]


class BrandActivity(RegistryBaseModel):
    domain: Annotated[str, Field(examples=["acmecorp.com"])]
    total: Annotated[int, Field(examples=[3])]
    revisions: list[ActivityRevision]


class PropertyActivity(RegistryBaseModel):
    domain: Annotated[str, Field(examples=["examplepub.com"])]
    total: Annotated[int, Field(examples=[3])]
    revisions: list[ActivityRevision]


class PropertySource(Enum):
    adagents_json = "adagents_json"
    hosted = "hosted"
    discovered = "discovered"


class AuthorizedAgent(RegistryBaseModel):
    url: str
    authorized_for: str | None = None


class AgentContact(RegistryBaseModel):
    name: str | None = None
    email: str | None = None


class PropertyIdentifier(RegistryBaseModel):
    type: Annotated[str, Field(examples=["domain"])]
    value: Annotated[str, Field(examples=["examplepub.com"])]


class PropertyRegistrySource(Enum):
    adagents_json = "adagents_json"
    hosted = "hosted"
    community = "community"
    discovered = "discovered"
    enriched = "enriched"


class PropertyRegistryItem(RegistryBaseModel):
    domain: Annotated[str, Field(examples=["examplepub.com"])]
    source: PropertyRegistrySource
    property_count: int
    agent_count: int
    verified: bool


class ValidationResult(RegistryBaseModel):
    valid: bool
    domain: str | None = None
    url: str | None = None
    errors: list[str | dict[str, Any]] | None = None
    warnings: list[str | dict[str, Any]] | None = None
    status_code: int | None = None
    raw_data: dict[str, Any] | None = None


class AgentType(Enum):
    brand = "brand"
    rights = "rights"
    measurement = "measurement"
    governance = "governance"
    creative = "creative"
    sales = "sales"
    buying = "buying"
    signals = "signals"
    unknown = "unknown"


class AgentProtocol(Enum):
    mcp = "mcp"
    a2a = "a2a"


class AgentDetailedContact(RegistryBaseModel):
    name: str
    email: str
    website: str


class AgentMember(RegistryBaseModel):
    slug: str | None = None
    display_name: str | None = None
    membership_tier: Annotated[
        str | None,
        Field(
            description="Raw AAO membership tier enum (e.g. `individual_professional`, `company_leader`). Present only when the profile owner has set their member card to public (`is_public=true`) AND the org has a resolvable tier. Absent for private profiles and for orgs without an active tier-bearing subscription."
        ),
    ] = None
    membership_tier_label: Annotated[
        str | None,
        Field(
            description="Human-readable label for `membership_tier` (e.g. `Professional`, `Partner`, `Leader`). Matches the AAO pricing page. Use this for UI display; the raw enum is for programmatic gating. Presence rules match `membership_tier`."
        ),
    ] = None
    is_founding_member: Annotated[
        bool | None,
        Field(
            description="True when the profile owner carries the Founding Member badge (joined before the founding-cohort cutoff). Surfaced when the profile owner has set their member card to public (`is_public=true`). Absent for private profiles. Founding Member is orthogonal to tier — founding orgs typically display both (e.g. Scope3 shows `Partner` + `Founding Member`)."
        ),
    ] = None


class AgentHealth(RegistryBaseModel):
    online: bool
    checked_at: str
    response_time_ms: float | None = None
    tools_count: int | None = None
    resources_count: int | None = None
    error: str | None = None


class AgentStats(RegistryBaseModel):
    property_count: int | None = None
    publisher_count: int | None = None
    publishers: list[str] | None = None
    creative_formats: int | None = None


class AgentTool(RegistryBaseModel):
    name: str
    description: str


class AgentStandardOperations(RegistryBaseModel):
    can_search_inventory: bool
    can_get_availability: bool
    can_reserve_inventory: bool
    can_get_pricing: bool
    can_create_order: bool
    can_list_properties: bool


class AgentCreativeCapabilities(RegistryBaseModel):
    formats_supported: list[str]
    can_generate: bool
    can_validate: bool
    can_preview: bool


class SignalsCapabilities(RegistryBaseModel):
    audience_types: list[str]
    can_match: bool
    can_activate: bool
    can_get_signals: bool


class VerifiedByAao(Enum):
    boolean_False = False


class Accreditation(RegistryBaseModel):
    accrediting_body: str
    certification_id: str | None = None
    valid_until: str | None = None
    evidence_url: str | None = None
    verified_by_aao: Annotated[
        VerifiedByAao,
        Field(
            description="Always `false` — accreditation claims are vendor-asserted. AAO does not independently verify; renderers should mark these as vendor claims."
        ),
    ]


class Metric(RegistryBaseModel):
    metric_id: str
    standard_reference: str | None = None
    accreditations: list[Accreditation] | None = None
    unit: str | None = None
    description: str | None = None
    methodology_url: str | None = None
    methodology_version: str | None = None


class MeasurementCapabilities(RegistryBaseModel):
    metrics: list[Metric]


class AgentCapabilities(RegistryBaseModel):
    tools_count: int
    tools: list[AgentTool] | None = None
    standard_operations: AgentStandardOperations | None = None
    creative_capabilities: AgentCreativeCapabilities | None = None
    signals_capabilities: SignalsCapabilities | None = None
    measurement_capabilities: Annotated[
        MeasurementCapabilities | None,
        Field(
            description="Vendor-published per-metric catalog for measurement agents. Populated when the crawler successfully fetched and validated `get_adcp_capabilities.measurement` (AdCP 3.x). Mirrors the protocol shape — see the AdCP `get_adcp_capabilities` reference for field semantics."
        ),
    ] = None


class ComplianceStatus(Enum):
    passing = "passing"
    degraded = "degraded"
    failing = "failing"
    unknown = "unknown"


class AgentLifecycleStage(Enum):
    development = "development"
    testing = "testing"
    production = "production"
    deprecated = "deprecated"


class TrackDetail(RegistryBaseModel):
    track: str
    status: str
    scenario_count: int
    passed_count: int
    duration_ms: float
    has_coverage_gap_skip: bool | None = None


class VerifiedRole(Enum):
    media_buy = "media-buy"
    signals = "signals"
    governance = "governance"
    creative = "creative"
    brand = "brand"
    sponsored_intelligence = "sponsored-intelligence"
    measurement = "measurement"


class AgentCompliance(RegistryBaseModel):
    status: ComplianceStatus
    requested_compliance_target: Annotated[
        str | None,
        Field(
            description="Requested compliance target before alias resolution, e.g. 3.0 or 3.1-beta."
        ),
    ] = None
    adcp_version: Annotated[
        str | None,
        Field(
            description="Concrete AdCP compliance bundle version used for the latest run, e.g. 3.0.12."
        ),
    ] = None
    lifecycle_stage: AgentLifecycleStage
    tracks: Annotated[dict[str, str], Field(examples=[{"core": "pass", "products": "fail"}])]
    track_details: Annotated[
        list[TrackDetail] | None,
        Field(
            description="Latest-run per-track summary. Skipped tracks with has_coverage_gap_skip=true represent selected coverage gaps, such as missing_test_controller."
        ),
    ] = None
    streak_days: int
    last_checked_at: str | None
    headline: str | None
    monitoring_paused: bool | None = None
    check_interval_hours: int | None = None
    verified: bool | None = None
    verified_roles: Annotated[
        list[VerifiedRole] | None,
        Field(
            description="AdCP protocols the agent is AAO Verified for (e.g. media-buy, creative). Matches enums/adcp-protocol.json."
        ),
    ] = None


class PropertySummary(RegistryBaseModel):
    total_count: int
    count_by_type: dict[str, int]
    tags: list[str]
    publisher_count: int


class FederatedPublisher(RegistryBaseModel):
    domain: str
    member: AgentMember | None = None
    agent_count: int | None = None
    last_validated: str | None = None
    has_valid_adagents: bool | None = None


class DomainAuthorizedAgent(RegistryBaseModel):
    url: str
    authorized_for: str | None = None
    member: AgentMember | None = None


class SalesAgentClaim(RegistryBaseModel):
    url: str
    member: AgentMember | None = None


class DomainLookupResult(RegistryBaseModel):
    domain: Annotated[str, Field(examples=["examplepub.com"])]
    authorized_agents: list[DomainAuthorizedAgent]
    sales_agents_claiming: list[SalesAgentClaim]


class AgentSource(Enum):
    adagents_json = "adagents_json"
    agent_claim = "agent_claim"


class AuthorizedByItem(RegistryBaseModel):
    publisher_domain: str
    authorized_for: str | None = None
    source: AgentSource


class Agent(RegistryBaseModel):
    url: str
    name: str
    type: AgentType
    authorized_by: list[AuthorizedByItem]


class OperatorLookupResult(RegistryBaseModel):
    domain: Annotated[str, Field(examples=["pubmatic.com"])]
    member: AgentMember | None
    agents: list[Agent]


class DiscoveryMethod(Enum):
    direct = "direct"
    authoritative_location = "authoritative_location"
    ads_txt_managerdomain = "ads_txt_managerdomain"
    adagents_authoritative = "adagents_authoritative"
    community_catalog = "community_catalog"
    NoneType_None = None


class Mode(Enum):
    self = "self"
    self_invalid = "self_invalid"
    aao_hosted = "aao_hosted"
    self_redirected = "self_redirected"
    none = "none"


class Hosting(RegistryBaseModel):
    mode: Annotated[
        Mode,
        Field(
            description="Where this publisher's adagents.json lives. `self` = publisher hosts a valid file at their own /.well-known. `self_invalid` = publisher's /.well-known returns a file that fails validation (fixable misconfiguration, not absence). `aao_hosted` = the publisher hosts a stub at their own /.well-known whose `authoritative_location` points at AAO's canonical document. `self_redirected` = the publisher's stub `authoritative_location` resolves to a third-party HTTPS origin (a CDN, partner CMS, or sibling host) — verifiers should audit the TLS chain at `resolved_url`, not at the publisher's own origin. `none` = no adagents.json configured yet."
        ),
    ]
    hosted_url: Annotated[
        str | None,
        Field(
            description="Canonical AAO-hosted adagents.json URL. Present iff `mode === 'aao_hosted'`. Publishers reference this URL from their own /.well-known stub via the `authoritative_location` field (see https://docs.adcontextprotocol.org/docs/governance/property/adagents)."
        ),
    ] = None
    expected_url: Annotated[
        str,
        Field(
            description="Where adagents.json *should* live for this domain — the publisher's own /.well-known path. Always populated, regardless of `mode`."
        ),
    ]
    resolved_url: Annotated[
        str | None,
        Field(
            description="Where the canonical adagents.json document actually lives after following the publisher's `authoritative_location` stub or any HTTP-layer redirects. Populated when `mode === 'self_redirected'` (the third-party HTTPS origin verifiers should audit) and when `mode === 'aao_hosted'` AND the publisher has actively set up the redirect (`authoritative_location` in the manifest body or a network-layer redirect to AAO's hosted URL). NULL when there's no resolved-URL evidence to report."
        ),
    ] = None
    last_validated: Annotated[
        str | None,
        Field(
            description="ISO timestamp of the last successful validation crawl. Lets verifiers sanity-check freshness. NULL when never crawled."
        ),
    ] = None
    last_http_status: Annotated[
        int | None,
        Field(
            description="HTTP status code returned by AAO's most recent fetch attempt of the publisher's `/.well-known/adagents.json`. Verifier-grade chrome — lets a buy-side scraper confirm they see the same response AAO does. NULL until the first crawl records or for transient errors that never produced an HTTP response.",
            ge=100,
            le=599,
        ),
    ] = None
    last_bytes: Annotated[
        int | None,
        Field(
            description="Response body byte length from the most recent fetch (post-decompression). When `authoritative_location` was followed, measures the canonical document body, not the stub. NULL until the first crawl records.",
            ge=0,
        ),
    ] = None
    origin_verified_at: Annotated[
        str | None,
        Field(
            description="ISO timestamp of the last successful origin verification — AAO fetched the publisher's own /.well-known/adagents.json and confirmed `authoritative_location` points at our hosted URL. When set, the publisher's authorization rows have been promoted to `source='adagents_json'` (origin-attested). NULL when never verified or last attempt failed. Only populated when `mode === 'aao_hosted'`."
        ),
    ] = None
    origin_last_checked_at: Annotated[
        str | None,
        Field(
            description='ISO timestamp of the last verification attempt regardless of result. Lets a caller render "checked X minutes ago, not yet verified" vs "never checked." Only populated when `mode === \'aao_hosted\'`.'
        ),
    ] = None


class Status1(Enum):
    valid = "valid"
    community = "community"
    invalid = "invalid"
    unknown = "unknown"
    checking = "checking"


class AdagentsJson(RegistryBaseModel):
    status: Annotated[
        Status1,
        Field(
            description="What we know about the publisher's adagents.json right now. `valid` = crawler fetched a parsing-and-shape-valid file from the publisher origin. `community` = moderators approved a community adagents.json catalog for this domain. `invalid` = crawler fetched a file that failed validation. `unknown` = never crawled or last result is stale. `checking` = an auto-crawl was kicked off by this request; the page should poll for fresh data shortly."
        ),
    ]
    expected_url: Annotated[
        str,
        Field(description="Where adagents.json should live on the publisher's own origin."),
    ]
    registry_url: Annotated[
        str | None,
        Field(
            description="Registry-served adagents.json URL when the document is community or AgenticAdvertising.org hosted rather than served by the publisher origin."
        ),
    ] = None


class Status2(Enum):
    present = "present"
    unknown = "unknown"
    checking = "checking"


class BrandJson(RegistryBaseModel):
    status: Annotated[
        Status2,
        Field(
            description="What we know about the publisher's brand.json. `present` = a brand record with manifest data exists. `unknown` = no record yet. `checking` = an auto-crawl was kicked off."
        ),
    ]
    name: str | None = None


class Files(RegistryBaseModel):
    adagents_json: AdagentsJson
    brand_json: BrandJson


class Source5(Enum):
    adagents_json = "adagents_json"
    community = "community"
    discovered = "discovered"
    brand_json = "brand_json"


class DelegationType(Enum):
    direct = "direct"
    delegated = "delegated"
    ad_network = "ad_network"


class Property1(RegistryBaseModel):
    id: str | None = None
    type: str | None = None
    name: str | None = None
    identifiers: list[PropertyIdentifier] | None = None
    tags: Annotated[
        list[str] | None,
        Field(
            description="Arbitrary string tags on this property. The `relationship:` prefix tag (e.g. `relationship:owned`) is deprecated in favour of the `delegation_type` field and will be removed in a future release."
        ),
    ] = None
    source: Annotated[
        Source5 | None,
        Field(
            description="Where this property came from. `adagents_json` comes from the publisher's own adagents.json, `community` from an approved community adagents.json catalog, `discovered` from crawler or third-party signals, and `brand_json` from the publisher's brand.json when no federated-index data exists yet."
        ),
    ] = None
    delegation_type: Annotated[
        DelegationType | None,
        Field(
            description="Delegation relationship declared in brand.json. Populated only when `source` is `brand_json` — for `adagents_json` and `discovered` sources the authoritative value is on the matching `authorized_agents` entry. Mirrors adagents.json `delegation_type` for bilateral verification: `direct` = publisher treats this as a direct buying path, even if a third party operates the software; `delegated` = a rep firm or manager is authorized to sell on the publisher's behalf (operator-declared, unilateral until corroborated by the publisher's adagents.json); `ad_network` = sold as part of a network/exchange package. `owned` properties have no `delegation_type` — ownership is implicit and has no adagents.json counterpart."
        ),
    ] = None


class BrandSummary(RegistryBaseModel):
    name: Annotated[
        str | None,
        Field(description="Display name from brand.json or the registered brand row."),
    ] = None
    description: Annotated[
        str | None,
        Field(description="Short brand or house description when present in brand.json."),
    ] = None
    logo_url: Annotated[str | None, Field(description="First usable logo URL from brand.json.")] = (
        None
    )
    colors: Annotated[
        list[str] | None,
        Field(description="Representative hex colors from brand.json, capped for display."),
    ] = None
    industries: Annotated[
        list[str] | None,
        Field(description="Industry labels from brand.json when present."),
    ] = None


class FormatSummary(RegistryBaseModel):
    format_option_id: Annotated[
        str | None,
        Field(description="Stable format option identifier from adagents.json `formats[]`."),
    ] = None
    display_name: Annotated[
        str,
        Field(description="Human-readable format label for catalog and publisher UI display."),
    ]
    format_kind: Annotated[
        str,
        Field(
            description="Canonical format discriminator, such as `image`, `video_hosted`, `native_in_feed`, or `custom`."
        ),
    ]
    params: Annotated[
        dict[str, Any] | None,
        Field(
            description="Canonical format params from the publisher's adagents.json declaration."
        ),
    ] = None
    applies_to_property_ids: Annotated[
        list[str] | None,
        Field(
            description="ResolvedPropertyEntry IDs this format applies to; absent means all properties."
        ),
    ] = None
    applies_to_property_tags: Annotated[
        list[str] | None,
        Field(
            description="ResolvedPropertyEntry tags this format applies to; absent means all properties."
        ),
    ] = None
    seller_preference: Annotated[
        str | None,
        Field(description="Seller preference hint from the format declaration, when present."),
    ] = None
    experimental: Annotated[
        bool | None,
        Field(description="Whether this seller's format declaration is marked experimental."),
    ] = None


class Source6(Enum):
    adagents_json = "adagents_json"
    aao_hosted = "aao_hosted"
    agent_claim = "agent_claim"


class AuthorizedAgent2(RegistryBaseModel):
    url: str
    authorized_for: str | None = None
    source: Annotated[
        Source6,
        Field(
            description="How strongly this authorization is attested. `adagents_json`: the publisher's origin actually serves a valid adagents.json (origin-verified). `aao_hosted`: AAO is hosting the canonical document on the publisher's behalf — represents publisher intent but origin has NOT been verified to redirect to AAO. `agent_claim`: the agent claimed it; publisher has not confirmed."
        ),
    ]
    properties_authorized: Annotated[
        int | None,
        Field(
            description="Count of this publisher's properties the agent is authorized to sell. Absent when `rollup_truncated` is set (call `/api/registry/publisher/authorization` for the per-agent count) or when properties are entirely brand.json-hydrated (no adagents.json claim has actually been made about them).",
            ge=0,
        ),
    ] = None
    properties_total: Annotated[
        int | None,
        Field(
            description="Total number of properties this publisher exposes through the registry. Same value across all agents in the response. Absent when `properties_authorized` is absent.",
            ge=0,
        ),
    ] = None
    publisher_wide: Annotated[
        bool | None,
        Field(
            description="True when the agent has only a publisher-wide authorization row and `properties_authorized` was synthesized as `properties_total`. False when the agent has property-level authorization rows. Absent when the rollup is absent."
        ),
    ] = None


class RollupTruncated(RegistryBaseModel):
    cap: Annotated[
        int,
        Field(
            description="Maximum number of agents for which the rollup is computed in a single response.",
            gt=0,
        ),
    ]
    total_agents: Annotated[
        int,
        Field(
            description="Total authorized-agent count for this publisher (the full population the cap was applied to).",
            ge=0,
        ),
    ]


class PublisherLookupResult(RegistryBaseModel):
    domain: Annotated[str, Field(examples=["voxmedia.com"])]
    member: AgentMember | None
    adagents_valid: bool | None
    discovery_method: Annotated[
        DiscoveryMethod | None,
        Field(
            description="How the publisher's adagents.json was discovered on the most recent successful crawl or registry write. `direct`: publisher's own /.well-known/ served the document. `authoritative_location`: publisher's stub redirected to a canonical URL. `ads_txt_managerdomain`: manifest was discovered via ads.txt MANAGERDOMAIN delegation. `adagents_authoritative`: manager file named this publisher through publisher_properties fan-out. `community_catalog`: moderator-approved community catalog. Null until first crawl after migration 470."
        ),
    ] = None
    manager_domain: Annotated[
        str | None,
        Field(
            description="The manager domain whose adagents.json was used to authorize this publisher's agents. Non-null only when `discovery_method` is `ads_txt_managerdomain`. Matches the MANAGERDOMAIN value from the publisher's ads.txt."
        ),
    ] = None
    hosting: Hosting
    files: Annotated[
        Files | None,
        Field(
            description="Plain-English summary of what AAO has found at the publisher's origin. The publisher page leads with this — `you have a valid adagents.json` is the primary signal, not `mode === self`. Optional in the schema for backwards compatibility; the handler always populates it."
        ),
    ] = None
    properties: list[Property1]
    brand: Annotated[
        BrandSummary | None,
        Field(
            description="Display-oriented brand identity summary from brand.json. The full raw document remains available from the publisher's /.well-known/brand.json or hosted registry URL."
        ),
    ] = None
    formats: Annotated[
        list[FormatSummary] | None,
        Field(
            description="Display-oriented summary of top-level adagents.json `formats[]`, normalized for publisher pages and agent discovery clients. Each entry preserves `format_kind`, `format_option_id`, and canonical params."
        ),
    ] = None
    authorized_agents: list[AuthorizedAgent2]
    rollup_truncated: Annotated[
        RollupTruncated | None,
        Field(
            description="Set when the publisher has more authorized agents than the per-agent rollup cap. Above the cap, agents beyond `cap` are returned without `properties_authorized` / `properties_total` / `publisher_wide`; call `/api/registry/publisher/authorization?domain=X&agent=Y` for the per-agent count. Lets a caller decide whether to fan out individual calls or stop reading."
        ),
    ] = None
    auto_crawl_triggered: Annotated[
        bool | None,
        Field(
            description="Set to `true` when this request triggered a background crawl of the publisher's origin (we hadn't crawled before). The client should refetch in ~3-5s to pick up fresh data. Debounced per-domain so a tight refresh loop won't keep firing crawls."
        ),
    ] = None


class PublisherPropertySelector(RegistryBaseModel):
    publisher_domain: Annotated[str | None, Field(examples=["examplepub.com"])] = None
    property_types: list[str] | None = None
    property_ids: list[str] | None = None
    tags: list[str] | None = None


class SuccessLiteral(Enum):
    boolean_True = True


class AdagentsDiscoveryMethod(Enum):
    direct = "direct"
    authoritative_location = "authoritative_location"
    ads_txt_managerdomain = "ads_txt_managerdomain"
    adagents_authoritative = "adagents_authoritative"


class AdagentsValidationSeverity(Enum):
    error = "error"


class AdagentsValidationIssue(RegistryBaseModel):
    field: str
    message: str
    severity: AdagentsValidationSeverity


class AdagentsValidationWarning(RegistryBaseModel):
    field: str
    message: str
    suggestion: str | None = None


class AdagentsAuthorizationType(Enum):
    property_ids = "property_ids"
    property_tags = "property_tags"
    inline_properties = "inline_properties"
    publisher_properties = "publisher_properties"
    signal_ids = "signal_ids"
    signal_tags = "signal_tags"


class PublisherPropertySelectionType(Enum):
    all = "all"
    by_id = "by_id"
    by_tag = "by_tag"


class AdagentsPublisherProperty(RegistryBaseModel):
    publisher_domain: str | None = None
    publisher_domains: list[str] | None = None
    selection_type: PublisherPropertySelectionType
    property_ids: list[str] | None = None
    property_tags: list[str] | None = None


class CollectionRef(RegistryBaseModel):
    publisher_domain: str
    collection_ids: list[str]


class AdagentsAuthorizedAgent(RegistryBaseModel):
    url: Annotated[AnyUrl, Field(description="Agent endpoint URL.")]
    authorized_for: str | None = None
    authorization_type: AdagentsAuthorizationType | None = None
    property_ids: list[str] | None = None
    property_tags: list[str] | None = None
    properties: list[dict[str, Any]] | None = None
    publisher_properties: list[AdagentsPublisherProperty] | None = None
    collections: list[CollectionRef] | None = None
    placement_ids: list[str] | None = None
    placement_tags: list[str] | None = None
    delegation_type: DelegationType | None = None
    exclusive: bool | None = None
    countries: list[str] | None = None
    effective_from: str | None = None
    effective_until: str | None = None
    signal_ids: list[str] | None = None
    signal_tags: list[str] | None = None
    signing_keys: list[dict[str, Any]] | None = None


class CommunityMirrorSummary(RegistryBaseModel):
    platform: Annotated[
        str,
        Field(
            description="Lowercase platform identifier, normalized by the service.",
            examples=["example_platform"],
            pattern="^[a-z0-9_-]{1,64}$",
        ),
    ]
    catalog_etag: str | None
    superseded_by: Annotated[
        str | None,
        Field(
            description="HTTPS successor document URL, when this mirror has been superseded.",
            pattern="^https:\\/\\/",
        ),
    ]
    updated_at: AwareDatetime


class RateLimitError(RegistryBaseModel):
    error: str
    message: str | None = None
    retryAfter: Annotated[int | None, Field(description="Seconds to wait before retrying.")] = None


class CommunityMirrorAdagentsJson(RegistryBaseModel):
    field_schema: Annotated[AnyUrl | None, Field(alias="$schema")] = None
    authorized_agents: Annotated[
        list[AdagentsAuthorizedAgent],
        Field(
            description="Always empty for community mirrors; these catalogs never assert sales authorization.",
            max_length=0,
        ),
    ]
    properties: list[dict[str, Any]] | None = None
    catalog_etag: str | None = None
    formats: list[dict[str, Any]] | None = None
    placements: list[dict[str, Any]] | None = None
    placement_tags: dict[str, Any] | None = None
    collections: list[dict[str, Any]] | None = None
    signals: list[dict[str, Any]] | None = None
    signal_tags: dict[str, Any] | None = None
    contact: Any | None = None
    superseded_by: Annotated[
        str | None,
        Field(
            description="HTTPS URL for the canonical successor adagents.json document. Clients should re-fetch the successor and update cached mirror references before retiring use of this mirror.",
            pattern="^https:\\/\\/",
        ),
    ] = None
    last_updated: AwareDatetime | None = None


class CommunityMirrorPublishResponse(RegistryBaseModel):
    success: SuccessLiteral
    platform: Annotated[
        str,
        Field(
            description="Lowercase platform identifier, normalized by the service.",
            examples=["example_platform"],
            pattern="^[a-z0-9_-]{1,64}$",
        ),
    ]
    catalog_etag: str | None
    superseded_by: Annotated[
        str | None,
        Field(
            description="HTTPS successor document URL, when this mirror has been superseded.",
            pattern="^https:\\/\\/",
        ),
    ]
    publisher_domains: Annotated[
        list[str],
        Field(description="Publisher domains updated from this community mirror catalog."),
    ]
    updated_at: AwareDatetime


class CommunityMirrorPublishError(RegistryBaseModel):
    error: str
    details: Annotated[
        list[Any] | None,
        Field(
            description="Validation details for request-body parse failures or adagents.json conformance errors."
        ),
    ] = None


class CommunityMirrorPublishFormatsRequest(RegistryBaseModel):
    catalog_etag: Annotated[str | None, Field(max_length=255, min_length=1)] = None
    formats: Annotated[list[dict[str, Any]], Field(min_length=1)]
    properties: list[dict[str, Any]] | None = None
    placements: list[dict[str, Any]] | None = None
    placement_tags: dict[str, Any] | None = None
    collections: list[dict[str, Any]] | None = None
    signals: list[dict[str, Any]] | None = None
    signal_tags: dict[str, Any] | None = None
    contact: Any | None = None
    superseded_by: Annotated[
        str | None,
        Field(
            description="HTTPS URL for the canonical successor adagents.json document. Set this before deleting a mirror so buyers can migrate cached references.",
            pattern="^https:\\/\\/",
        ),
    ] = None


class CommunityMirrorPublishPropertiesRequest(RegistryBaseModel):
    catalog_etag: Annotated[str | None, Field(max_length=255, min_length=1)] = None
    formats: list[dict[str, Any]] | None = None
    properties: Annotated[list[dict[str, Any]], Field(min_length=1)]
    placements: list[dict[str, Any]] | None = None
    placement_tags: dict[str, Any] | None = None
    collections: list[dict[str, Any]] | None = None
    signals: list[dict[str, Any]] | None = None
    signal_tags: dict[str, Any] | None = None
    contact: Any | None = None
    superseded_by: Annotated[
        str | None,
        Field(
            description="HTTPS URL for the canonical successor adagents.json document. Set this before deleting a mirror so buyers can migrate cached references.",
            pattern="^https:\\/\\/",
        ),
    ] = None


class CommunityMirrorPublishPlacementsRequest(RegistryBaseModel):
    catalog_etag: Annotated[str | None, Field(max_length=255, min_length=1)] = None
    formats: list[dict[str, Any]] | None = None
    properties: list[dict[str, Any]] | None = None
    placements: Annotated[list[dict[str, Any]], Field(min_length=1)]
    placement_tags: dict[str, Any] | None = None
    collections: list[dict[str, Any]] | None = None
    signals: list[dict[str, Any]] | None = None
    signal_tags: dict[str, Any] | None = None
    contact: Any | None = None
    superseded_by: Annotated[
        str | None,
        Field(
            description="HTTPS URL for the canonical successor adagents.json document. Set this before deleting a mirror so buyers can migrate cached references.",
            pattern="^https:\\/\\/",
        ),
    ] = None


class CommunityMirrorPublishCollectionsRequest(RegistryBaseModel):
    catalog_etag: Annotated[str | None, Field(max_length=255, min_length=1)] = None
    formats: list[dict[str, Any]] | None = None
    properties: list[dict[str, Any]] | None = None
    placements: list[dict[str, Any]] | None = None
    placement_tags: dict[str, Any] | None = None
    collections: Annotated[list[dict[str, Any]], Field(min_length=1)]
    signals: list[dict[str, Any]] | None = None
    signal_tags: dict[str, Any] | None = None
    contact: Any | None = None
    superseded_by: Annotated[
        str | None,
        Field(
            description="HTTPS URL for the canonical successor adagents.json document. Set this before deleting a mirror so buyers can migrate cached references.",
            pattern="^https:\\/\\/",
        ),
    ] = None


class CommunityMirrorPublishSignalsRequest(RegistryBaseModel):
    catalog_etag: Annotated[str | None, Field(max_length=255, min_length=1)] = None
    formats: list[dict[str, Any]] | None = None
    properties: list[dict[str, Any]] | None = None
    placements: list[dict[str, Any]] | None = None
    placement_tags: dict[str, Any] | None = None
    collections: list[dict[str, Any]] | None = None
    signals: Annotated[list[dict[str, Any]], Field(min_length=1)]
    signal_tags: dict[str, Any] | None = None
    contact: Any | None = None
    superseded_by: Annotated[
        str | None,
        Field(
            description="HTTPS URL for the canonical successor adagents.json document. Set this before deleting a mirror so buyers can migrate cached references.",
            pattern="^https:\\/\\/",
        ),
    ] = None


class CommunityMirrorPublishRequest(
    RootModel[
        CommunityMirrorPublishFormatsRequest
        | CommunityMirrorPublishPropertiesRequest
        | CommunityMirrorPublishPlacementsRequest
        | CommunityMirrorPublishCollectionsRequest
        | CommunityMirrorPublishSignalsRequest
    ]
):
    root: Annotated[
        CommunityMirrorPublishFormatsRequest
        | CommunityMirrorPublishPropertiesRequest
        | CommunityMirrorPublishPlacementsRequest
        | CommunityMirrorPublishCollectionsRequest
        | CommunityMirrorPublishSignalsRequest,
        Field(
            description="Catalog-only adagents.json body for a community mirror. At least one of `formats`, `properties`, `placements`, `collections`, or `signals` must be present and non-empty. The service regenerates `$schema` and `last_updated` before persisting."
        ),
    ]


class CommunityMirrorDeleteResponse(RegistryBaseModel):
    success: SuccessLiteral
    platform: Annotated[
        str,
        Field(
            description="Lowercase platform identifier, normalized by the service.",
            examples=["example_platform"],
            pattern="^[a-z0-9_-]{1,64}$",
        ),
    ]


class PolicyCategory(Enum):
    regulation = "regulation"
    standard = "standard"


class PolicyEnforcement(Enum):
    must = "must"
    should = "should"
    may = "may"


class PolicySourceType(Enum):
    registry = "registry"
    community = "community"


class PolicyReviewStatus(Enum):
    pending = "pending"
    approved = "approved"


class PolicySummary(RegistryBaseModel):
    policy_id: Annotated[str, Field(examples=["gdpr_consent"])]
    version: Annotated[str, Field(examples=["1.0.0"])]
    name: Annotated[str, Field(examples=["GDPR Consent Requirements"])]
    description: Annotated[
        str | None, Field(examples=["Requirements for valid consent under GDPR"])
    ]
    category: PolicyCategory
    enforcement: PolicyEnforcement
    jurisdictions: Annotated[list[str], Field(examples=[["EU", "EEA"]])]
    region_aliases: Annotated[dict[str, list[str]], Field(examples=[{"EU": ["DE", "FR", "IT"]}])]
    policy_categories: Annotated[
        list[str], Field(examples=[["age_restricted", "pharmaceutical_advertising"]])
    ]
    channels: Annotated[list[str] | None, Field(examples=[["display", "video"]])]
    governance_domains: Annotated[list[str], Field(examples=[["campaign", "creative"]])]
    effective_date: Annotated[str | None, Field(examples=["2025-05-25"])]
    sunset_date: str | None
    source_url: Annotated[
        str | None, Field(examples=["https://eur-lex.europa.eu/eli/reg/2016/679/oj"])
    ]
    source_name: Annotated[str | None, Field(examples=["EUR-Lex"])]
    source_type: PolicySourceType
    review_status: PolicyReviewStatus
    created_at: Annotated[str, Field(examples=["2026-03-01T12:00:00.000Z"])]
    updated_at: Annotated[str, Field(examples=["2026-03-01T12:00:00.000Z"])]


class PolicyExemplarPass(RegistryBaseModel):
    scenario: Annotated[str, Field(examples=["Ad for alcohol shown during children's programming"])]
    explanation: Annotated[
        str, Field(examples=["Violates watershed timing rules for alcohol advertising"])
    ]


class PolicyExemplarFail(RegistryBaseModel):
    scenario: Annotated[str, Field(examples=["Ad for alcohol shown during children's programming"])]
    explanation: Annotated[
        str, Field(examples=["Violates watershed timing rules for alcohol advertising"])
    ]


class PolicyExemplars(RegistryBaseModel):
    pass_: Annotated[list[PolicyExemplarPass] | None, Field(alias="pass")] = None
    fail: list[PolicyExemplarFail] | None = None


class Policy(RegistryBaseModel):
    policy_id: Annotated[str, Field(examples=["gdpr_consent"])]
    version: Annotated[str, Field(examples=["1.0.0"])]
    name: Annotated[str, Field(examples=["GDPR Consent Requirements"])]
    description: Annotated[
        str | None, Field(examples=["Requirements for valid consent under GDPR"])
    ]
    category: PolicyCategory
    enforcement: PolicyEnforcement
    jurisdictions: Annotated[list[str], Field(examples=[["EU", "EEA"]])]
    region_aliases: Annotated[dict[str, list[str]], Field(examples=[{"EU": ["DE", "FR", "IT"]}])]
    policy_categories: Annotated[
        list[str], Field(examples=[["age_restricted", "pharmaceutical_advertising"]])
    ]
    channels: Annotated[list[str] | None, Field(examples=[["display", "video"]])]
    governance_domains: Annotated[list[str], Field(examples=[["campaign", "creative"]])]
    effective_date: Annotated[str | None, Field(examples=["2025-05-25"])]
    sunset_date: str | None
    source_url: Annotated[
        str | None, Field(examples=["https://eur-lex.europa.eu/eli/reg/2016/679/oj"])
    ]
    source_name: Annotated[str | None, Field(examples=["EUR-Lex"])]
    policy: Annotated[
        str,
        Field(
            examples=[
                "CreateAdagentsData subjects must provide freely given, specific, informed and unambiguous consent..."
            ]
        ),
    ]
    guidance: str | None
    exemplars: PolicyExemplars | None
    ext: dict[str, Any] | None
    source_type: PolicySourceType
    review_status: PolicyReviewStatus
    created_at: Annotated[str, Field(examples=["2026-03-01T12:00:00.000Z"])]
    updated_at: Annotated[str, Field(examples=["2026-03-01T12:00:00.000Z"])]


class PolicyRevision(RegistryBaseModel):
    revision_number: Annotated[int, Field(examples=[2])]
    editor_name: Annotated[str, Field(examples=["Pinnacle Media"])]
    edit_summary: Annotated[str, Field(examples=["Clarified consent requirements for minors"])]
    is_rollback: bool
    rolled_back_to: Annotated[
        int | None,
        Field(
            description="ActivityRevision number that was restored; only present when is_rollback is true"
        ),
    ] = None
    created_at: Annotated[str, Field(examples=["2026-03-01T12:34:56Z"])]


class PolicyHistory(RegistryBaseModel):
    policy_id: Annotated[str, Field(examples=["gdpr_consent"])]
    total: Annotated[int, Field(examples=[3])]
    revisions: list[PolicyRevision]


class Status3(Enum):
    passing = "passing"
    degraded = "degraded"
    failing = "failing"
    unknown = "unknown"
    opted_out = "opted_out"


class SpecialismStatus(Enum):
    passing = "passing"
    failing = "failing"
    untested = "untested"
    unknown = "unknown"


class Status4(Enum):
    passing = "passing"
    failing = "failing"
    partial = "partial"
    untested = "untested"


class StoryboardStatus(RegistryBaseModel):
    storyboard_id: str
    requested_compliance_target: str | None = None
    adcp_version: str | None = None
    title: str
    category: str | None
    track: str | None
    status: Status4
    steps_passed: int
    steps_total: int
    last_tested_at: str | None
    last_passed_at: str | None


class Observation(RegistryBaseModel):
    category: str
    severity: str
    message: str


class VerdictSource(Enum):
    heartbeat = "heartbeat"
    owner_test = "owner_test"
    manual = "manual"
    webhook = "webhook"
    NoneType_None = None


class Role(Enum):
    media_buy = "media-buy"
    signals = "signals"
    governance = "governance"
    creative = "creative"
    brand = "brand"
    sponsored_intelligence = "sponsored-intelligence"
    measurement = "measurement"


class VerifiedSpecialism(Enum):
    audience_sync = "audience-sync"
    brand_rights = "brand-rights"
    collection_lists = "collection-lists"
    content_standards = "content-standards"
    creative_ad_server = "creative-ad-server"
    creative_generative = "creative-generative"
    creative_template = "creative-template"
    creative_transformers = "creative-transformers"
    governance_aware_seller = "governance-aware-seller"
    governance_delivery_monitor = "governance-delivery-monitor"
    governance_spend_authority = "governance-spend-authority"
    property_lists = "property-lists"
    sales_broadcast_tv = "sales-broadcast-tv"
    sales_catalog_driven = "sales-catalog-driven"
    sales_guaranteed = "sales-guaranteed"
    sales_non_guaranteed = "sales-non-guaranteed"
    sales_proposal_mode = "sales-proposal-mode"
    sales_social = "sales-social"
    signal_marketplace = "signal-marketplace"
    signal_owned = "signal-owned"
    signed_requests = "signed-requests"
    sponsored_intelligence = "sponsored-intelligence"


class VerificationMode(Enum):
    spec = "spec"
    live = "live"


class VerificationBadge(RegistryBaseModel):
    role: Annotated[
        Role,
        Field(description="AdCP protocol this badge covers (enums/adcp-protocol.json)."),
    ]
    adcp_version: Annotated[
        str,
        Field(
            description="AdCP release this badge was issued against, MAJOR.MINOR (e.g. '3.0', '3.1'). Load-bearing for badge identity — pairs with the (agent_url, role, adcp_version) PK."
        ),
    ]
    verified_at: str
    verified_specialisms: Annotated[
        list[VerifiedSpecialism],
        Field(
            description="Specialisms demonstrably passed (enums/specialism.json). Preview specialisms are excluded from stable badges."
        ),
    ]
    verification_modes: Annotated[
        list[VerificationMode],
        Field(
            description="Verification axes earned. 'spec' = AdCP storyboards pass for the declared specialisms. 'live' = AAO has observed real production traffic via canonical campaigns. Always non-empty when a badge is present; an absent badge is conveyed by the parent record being omitted, not by an empty array.",
            min_length=1,
        ),
    ]
    verified_protocol_version: str | None
    badge_url: Annotated[
        str | None,
        Field(
            description="Legacy URL — auto-upgrades to the highest active version. For version-pinned embedding, derive `/api/registry/agents/{encoded_url}/badge/{role}/{adcp_version}.svg` where `{encoded_url}` is `encodeURIComponent(agent_url)`."
        ),
    ] = None


class AgentVerification(RegistryBaseModel):
    agent_url: str
    verified: bool
    badges: list[VerificationBadge]
    registry_url: str | None = None


class StoryboardStatus1(RegistryBaseModel):
    storyboard_id: str
    requested_compliance_target: Annotated[
        str | None,
        Field(
            description="Requested compliance target from the run that produced this storyboard verdict, e.g. 3.0 or 3.1-beta."
        ),
    ] = None
    adcp_version: Annotated[
        str | None,
        Field(
            description="Concrete AdCP compliance bundle version from the run that produced this storyboard verdict."
        ),
    ] = None
    title: str
    category: str | None
    track: str | None
    status: Status4
    steps_passed: int
    steps_total: int
    last_tested_at: str | None
    last_passed_at: str | None


class ComplianceRun(RegistryBaseModel):
    id: str
    requested_compliance_target: str | None = None
    adcp_version: str | None = None
    overall_status: str
    headline: str | None
    tracks_passed: int
    tracks_failed: int
    tracks_skipped: int
    tracks_partial: int
    tracks_json: Any | None = None
    total_duration_ms: float | None
    triggered_by: str
    tested_at: str


class RegistryMetadata(RegistryBaseModel):
    agent_url: str
    lifecycle_stage: AgentLifecycleStage
    compliance_opt_out: bool
    monitoring_paused: bool
    check_interval_hours: int
    monitoring_paused_at: str | None
    created_at: str
    updated_at: str


class MonitoringSettings(RegistryBaseModel):
    monitoring_paused: bool
    check_interval_hours: int
    monitoring_paused_at: str | None


class ComplianceStepDiagnostic(RegistryBaseModel):
    id: float | str
    run_id: str
    agent_url: str
    storyboard_id: str
    phase_id: str
    step_id: str
    task: str
    step_passed: bool
    duration_ms: float | None = None
    request_url: str | None = None
    request_jsonb: Any | None = None
    response_status: float | None = None
    response_headers_jsonb: dict[str, Any] | None = None
    response_jsonb: Any | None = None
    extraction_path: str | None = None
    extraction_note: str | None = None
    error_text: str | None = None
    adcp_error_jsonb: Any | None = None
    failed_validations_jsonb: Any | None = None
    served_by_agent_url: str | None = None
    captured_at: str


class OutboundRequest(RegistryBaseModel):
    id: str
    agent_url: str
    request_type: str
    user_agent: str
    response_time_ms: float | None
    success: bool
    error_message: str | None
    created_at: str


class AuthType(Enum):
    bearer = "bearer"
    basic = "basic"
    oauth = "oauth"
    oauth_client_credentials = "oauth_client_credentials"
    NoneType_None = None


class AgentAuthStatus(RegistryBaseModel):
    has_auth: bool
    agent_context_id: str | None
    auth_type: AuthType | None
    has_oauth_token: bool
    has_valid_oauth: bool
    oauth_token_expires_at: str | None
    has_oauth_client_credentials: bool


class Code(Enum):
    invalid_blob_shape = "invalid_blob_shape"
    missing_field = "missing_field"
    invalid_field_type = "invalid_field_type"
    field_too_long = "field_too_long"
    invalid_url = "invalid_url"
    invalid_env_reference = "invalid_env_reference"
    invalid_auth_method_value = "invalid_auth_method_value"


class FieldModel(Enum):
    oauth_client_credentials = "oauth_client_credentials"
    token_endpoint = "token_endpoint"
    client_id = "client_id"
    client_secret = "client_secret"
    scope = "scope"
    resource = "resource"
    audience = "audience"
    auth_method = "auth_method"


class CredentialSaveValidationError(RegistryBaseModel):
    error: str
    code: Annotated[
        Code,
        Field(description="Stable rejection tag. UI maps this to operator-friendly prose."),
    ]
    field: Annotated[FieldModel, Field(description="Field the UI should scroll to + highlight.")]


class StoryboardSummary(RegistryBaseModel):
    id: str
    title: str
    category: str
    summary: str
    interaction_model: str
    examples: list[str]
    phase_count: int
    step_count: int


class Agent1(RegistryBaseModel):
    interaction_model: str
    examples: list[str] | None = None


class Step(RegistryBaseModel):
    id: str
    title: str
    description: str
    expected_output: str


class Phase(RegistryBaseModel):
    title: str
    steps: list[Step]


class StoryboardDetail(RegistryBaseModel):
    id: str
    title: str
    category: str
    summary: str
    agent: Agent1
    phases: list[Phase]
    prerequisites: Any | None = None
    required_tools: list[str] | None = None
    track: str | None = None


class CompanySearchResult(RegistryBaseModel):
    domain: Annotated[str, Field(examples=["coca-cola.com"])]
    canonical_domain: Annotated[str, Field(examples=["coca-cola.com"])]
    brand_name: Annotated[str, Field(examples=["The Coca-Cola Company"])]
    house_domain: Annotated[str | None, Field(examples=["coca-cola.com"])] = None
    keller_type: KellerType | None = None
    parent_brand: str | None = None
    brand_agent_url: str | None = None
    source: Annotated[str, Field(examples=["community"])]


class MemberAgentVisibility(Enum):
    private = "private"
    members_only = "members_only"
    public = "public"


class MemberAgentType(Enum):
    brand = "brand"
    rights = "rights"
    measurement = "measurement"
    governance = "governance"
    creative = "creative"
    sales = "sales"
    buying = "buying"
    signals = "signals"
    unknown = "unknown"


class Code1(Enum):
    visibility_downgraded = "visibility_downgraded"


class Requested(Enum):
    public = "public"


class Applied(Enum):
    members_only = "members_only"


class Reason(Enum):
    tier_required = "tier_required"


class MemberAgentVisibilityWarning(RegistryBaseModel):
    code: Code1
    agent_url: str
    requested: Requested
    applied: Applied
    reason: Reason
    message: str


class MemberAgentTypeInput(Enum):
    brand = "brand"
    rights = "rights"
    measurement = "measurement"
    governance = "governance"
    creative = "creative"
    sales = "sales"
    buying = "buying"
    signals = "signals"


class MemberAgentPatch(RegistryBaseModel):
    name: str | None = None
    visibility: MemberAgentVisibility | None = None
    type: MemberAgentTypeInput | None = None
    health_check_url: AnyUrl | None = None


class Organization(RegistryBaseModel):
    id: Annotated[str, Field(examples=["org_01HXZAB123"])]
    name: Annotated[str, Field(examples=["Acme Media"])]


class CreateOrganizationResponse(RegistryBaseModel):
    success: bool | None = None
    organization: Organization | None = None
    id: Annotated[
        str | None,
        Field(
            description="Set on the **prospect-adoption** path: when an org with the user's email domain already exists in a `prospect` state (i.e. the registry pre-recorded it from a brand crawl but no human had claimed it yet), this call adopts that org for the caller instead of creating a new one."
        ),
    ] = None
    name: str | None = None
    adopted: Annotated[
        bool | None,
        Field(
            description="`true` when the response is the prospect-adoption path. When `true`, no new WorkOS organization was created — the caller is now the owner of an existing prospect record."
        ),
    ] = None


class OrganizationCompanyType(Enum):
    adtech = "adtech"
    agency = "agency"
    brand = "brand"
    publisher = "publisher"
    data = "data"
    ai = "ai"
    other = "other"


class OrganizationRevenueTier(Enum):
    under_1m = "under_1m"
    field_1m_5m = "1m_5m"
    field_5m_50m = "5m_50m"
    field_50m_250m = "50m_250m"
    field_250m_1b = "250m_1b"
    field_1b_plus = "1b_plus"


class CommunityMirrorCatalogDocument(RegistryBaseModel):
    field_schema: Annotated[AnyUrl | None, Field(alias="$schema")] = None
    authorized_agents: list[AdagentsAuthorizedAgent]
    properties: list[dict[str, Any]] | None = None
    catalog_etag: str | None = None
    formats: list[dict[str, Any]] | None = None
    placements: list[dict[str, Any]] | None = None
    placement_tags: dict[str, Any] | None = None
    collections: list[dict[str, Any]] | None = None
    signals: list[dict[str, Any]] | None = None
    signal_tags: dict[str, Any] | None = None
    contact: Any | None = None
    superseded_by: Annotated[
        AnyUrl | None,
        Field(
            description="HTTPS URL for the canonical successor adagents.json document. Clients should re-fetch the successor and update cached mirror references before retiring use of this mirror."
        ),
    ] = None
    last_updated: AwareDatetime | None = None


class ResolvedBrand(RegistryBaseModel):
    canonical_id: Annotated[str, Field(examples=["acmecorp.com"])]
    canonical_domain: Annotated[str, Field(examples=["acmecorp.com"])]
    brand_name: Annotated[str, Field(examples=["Acme Corp"])]
    names: list[dict[str, str]] | None = None
    keller_type: KellerType | None = None
    parent_brand: str | None = None
    house_domain: str | None = None
    house_name: str | None = None
    brand_agent_url: str | None = None
    brand_manifest: dict[str, Any] | None = None
    source: BrandSource


class ResolvedPropertyEntry(RegistryBaseModel):
    id: str | None = None
    type: str | None = None
    name: str | None = None
    identifiers: list[PropertyIdentifier] | None = None
    tags: list[str] | None = None


class ResolvedProperty(RegistryBaseModel):
    publisher_domain: Annotated[str, Field(examples=["examplepub.com"])]
    source: PropertySource
    authorized_agents: list[AuthorizedAgent] | None = None
    properties: list[ResolvedPropertyEntry] | None = None
    contact: AgentContact | None = None
    verified: bool


class FederatedAgentWithDetails(RegistryBaseModel):
    url: str
    name: str
    type: AgentType
    protocol: AgentProtocol | None = None
    description: str | None = None
    mcp_endpoint: str | None = None
    contact: AgentDetailedContact | None = None
    added_date: str | None = None
    member: Annotated[
        AgentMember | None,
        Field(
            description="AAO member that owns this agent record. The registry contains only agents that members have explicitly enrolled on their member profile."
        ),
    ] = None
    health: AgentHealth | None = None
    stats: AgentStats | None = None
    capabilities: AgentCapabilities | None = None
    compliance: AgentCompliance | None = None
    publisher_domains: list[str] | None = None
    property_summary: PropertySummary | None = None


class AdagentsValidationResult(RegistryBaseModel):
    valid: bool
    errors: list[AdagentsValidationIssue]
    warnings: list[AdagentsValidationWarning]
    domain: str
    url: str
    status_code: int | None = None
    response_bytes: Annotated[int | None, Field(ge=0)] = None
    resolved_url: str | None = None
    raw_data: Any | None = None
    discovery_method: AdagentsDiscoveryMethod
    manager_domain: str | None = None


class CommunityMirrorListResponse(RegistryBaseModel):
    mirrors: list[CommunityMirrorSummary]
    total: Annotated[int, Field(ge=0)]


class CommunityMirrorGetResponse(RegistryBaseModel):
    platform: Annotated[
        str,
        Field(
            description="Lowercase platform identifier, normalized by the service.",
            examples=["example_platform"],
            pattern="^[a-z0-9_-]{1,64}$",
        ),
    ]
    catalog_etag: str | None
    superseded_by: Annotated[
        str | None,
        Field(
            description="HTTPS successor document URL, when this mirror has been superseded.",
            pattern="^https:\\/\\/",
        ),
    ]
    adagents_json: CommunityMirrorAdagentsJson
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AgentComplianceDetail(RegistryBaseModel):
    agent_url: str
    requested_compliance_target: Annotated[
        str | None,
        Field(
            description="Requested compliance target before alias resolution, e.g. 3.0 or 3.1-beta. Null for legacy rows before target recording."
        ),
    ] = None
    adcp_version: Annotated[
        str | None,
        Field(
            description="Concrete AdCP compliance bundle version used for the latest run, e.g. 3.0.12. Null for legacy rows before version recording."
        ),
    ] = None
    status: Status3
    lifecycle_stage: AgentLifecycleStage
    compliance_opt_out: bool | None = None
    tracks: dict[str, str] | None = None
    track_details: Annotated[
        list[TrackDetail] | None,
        Field(
            description="Latest-run per-track summary. Skipped tracks with has_coverage_gap_skip=true represent selected coverage gaps, such as missing_test_controller."
        ),
    ] = None
    streak_days: int | None = None
    last_checked_at: str | None = None
    last_passed_at: str | None = None
    last_failed_at: str | None = None
    headline: str | None = None
    status_changed_at: str | None = None
    storyboards_passing: int | None = None
    storyboards_total: int | None = None
    check_interval_hours: Annotated[
        int | None,
        Field(description="How often the heartbeat re-tests this agent, in hours"),
    ] = None
    declared_specialisms: Annotated[
        list[str] | None,
        Field(
            description="Specialisms the agent declared in get_adcp_capabilities, from the latest run"
        ),
    ] = None
    specialism_status: Annotated[
        dict[str, SpecialismStatus] | None,
        Field(
            description="Per-specialism pass/fail/untested status — keyed on declared specialism, derived from the matching storyboard's status"
        ),
    ] = None
    storyboard_statuses: Annotated[
        list[StoryboardStatus] | None,
        Field(
            description="Owner-scoped per-storyboard diagnostics used by the dashboard. Empty for non-owners."
        ),
    ] = None
    notices: Annotated[
        list[Any] | None,
        Field(
            description="Run-summary notices from the latest non-dry-run compliance run. Unknown codes/severities are preserved verbatim."
        ),
    ] = None
    observations: Annotated[
        list[Observation] | None,
        Field(
            description="Public-safe advisory observations from the latest non-dry-run compliance run. Raw evidence is intentionally omitted; this array is not merged across runs, so cleared advisories disappear on the next fresh run."
        ),
    ] = None
    membership_tier: Annotated[
        str | None,
        Field(
            description="Owner-scoped: the agent owner's membership tier. Populated only when the authenticated viewer owns the agent; null otherwise. Field is always present so response shape doesn't reveal ownership."
        ),
    ] = None
    membership_tier_label: Annotated[
        str | None,
        Field(
            description="Owner-scoped: human-readable label for membership_tier (e.g. 'Builder'). Null for non-owners."
        ),
    ] = None
    subscription_status: Annotated[
        str | None,
        Field(
            description="Owner-scoped: the agent owner's subscription status (active, past_due, trialing, etc.). Null for non-owners."
        ),
    ] = None
    is_api_access_tier: Annotated[
        bool | None,
        Field(
            description="Owner-scoped: true when the owner's tier and subscription status grant badge eligibility. False for non-owners. Single source of truth — UI should not re-derive."
        ),
    ] = None
    verdict_source: Annotated[
        VerdictSource | None,
        Field(
            description="Owner-scoped: triggered_by value of the most recent non-dry-run compliance check. Null for non-owners and when no run has been recorded. Operators use this as a UX cue ('did this verdict come from my recent test or the system heartbeat?')."
        ),
    ] = None
    verified: bool | None = None
    verified_badges: list[VerificationBadge] | None = None


class FindCompanyResult(RegistryBaseModel):
    results: list[CompanySearchResult]


class MemberAgent(RegistryBaseModel):
    url: Annotated[AnyUrl, Field(examples=["https://agent.example.com/mcp"])]
    visibility: MemberAgentVisibility
    type: MemberAgentType
    name: str | None = None
    health_check_url: Annotated[
        AnyUrl | None,
        Field(
            description="Optional fallback liveness URL used by the health probe when the protocol handshake fails."
        ),
    ] = None


class MemberAgentResponse(RegistryBaseModel):
    agent: MemberAgent
    warnings: list[MemberAgentVisibilityWarning] | None = None
    org_auto_created: Annotated[
        bool | None,
        Field(
            description="Set to `true` when this `POST` was the caller's first interaction with the registry and the server auto-created the organization (display name derived from the user's email domain for corporate emails, or `<First Last>'s Workspace` for free-email providers). Combined with `profile_auto_created`, this is the one-call storefront experience: a third-party app holding only an OAuth token gets the org, profile, and registered agent in a single request."
        ),
    ] = None
    profile_auto_created: Annotated[
        bool | None,
        Field(
            description='Set to `true` when this `POST` was the first agent registration on the caller\'s organization and the server auto-created a private member profile (display name = organization name, `is_public: false`). Absent on subsequent calls and on update-in-place. Surfaced so storefront-style integrations can show a "we set up your profile" hint without needing to detect the prior 404 → bootstrap → retry shape.'
        ),
    ] = None


class MemberAgentInput(RegistryBaseModel):
    url: Annotated[AnyUrl, Field(examples=["https://agent.example.com/mcp"])]
    type: MemberAgentTypeInput
    name: str | None = None
    visibility: MemberAgentVisibility | None = None
    health_check_url: AnyUrl | None = None


class CreateOrganizationInput(RegistryBaseModel):
    organization_name: Annotated[
        str,
        Field(
            description="Display name for the organization. Used both as the org row name and (when auto-bootstrapping a member profile via the first agent registration) as the profile's `display_name`.",
            examples=["Acme Media"],
            max_length=200,
            min_length=1,
        ),
    ]
    is_personal: Annotated[
        bool | None,
        Field(
            description="Set to `true` to create a personal workspace instead of a corporate organization. Personal workspaces skip corporate-domain verification, are limited to one per user, and cannot host the `company_*` membership tiers."
        ),
    ] = False
    company_type: OrganizationCompanyType | None = None
    revenue_tier: OrganizationRevenueTier | None = None
    marketing_opt_in: Annotated[
        bool | None,
        Field(
            description="Whether the caller opted in to AAO marketing communications. Recorded once per user (not overwritten on subsequent calls). Independent of Terms-of-Service consent, which is recorded server-side from the request context."
        ),
    ] = False


class CreateAdagentsData(RegistryBaseModel):
    success: SuccessLiteral
    adagents_json: Annotated[
        str,
        Field(description="Pretty-printed adagents.json document generated by the service."),
    ]
    validation: AdagentsValidationResult


class CreateAdagentsResponse(RegistryBaseModel):
    success: SuccessLiteral
    data: CreateAdagentsData
    timestamp: AwareDatetime


class MemberAgentListResponse(RegistryBaseModel):
    agents: list[MemberAgent]


class FeedEvent(RegistryBaseModel):
    event_id: str
    event_type: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any]
    actor: str
    created_at: str


class FeedPage(RegistryBaseModel):
    events: list[FeedEvent]
    cursor: str | None
    has_more: bool
