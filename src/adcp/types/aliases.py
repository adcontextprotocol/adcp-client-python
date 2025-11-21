"""Semantic type aliases for generated AdCP types.

This module provides user-friendly aliases for generated types where the
auto-generated names don't match user expectations from reading the spec.

The code generator (datamodel-code-generator) creates numbered suffixes for
discriminated union variants (e.g., Response1, Response2), but users expect
semantic names (e.g., SuccessResponse, ErrorResponse).

Categories of aliases:

1. Discriminated Union Response Variants
   - Success/Error cases for API responses
   - Named to match the semantic meaning from the spec

2. Preview/Render Types
   - Input/Output/Request/Response variants
   - Numbered types mapped to their semantic purpose

3. Activation Keys
   - Signal activation key variants

DO NOT EDIT the generated types directly - they are regenerated from schemas.
Add aliases here for any types where the generated name is unclear.

Validation:
This module will raise ImportError at import time if any of the referenced
generated types do not exist. This ensures that schema changes are caught
immediately rather than at runtime when users try to use the aliases.
"""

from __future__ import annotations

from adcp.types._generated import (
    # Activation responses
    ActivateSignalResponse1,
    ActivateSignalResponse2,
    # Activation keys
    ActivationKey1,
    ActivationKey2,
    # Authorized agents
    AuthorizedAgents,
    AuthorizedAgents1,
    AuthorizedAgents2,
    AuthorizedAgents3,
    # Build creative responses
    BuildCreativeResponse1,
    BuildCreativeResponse2,
    # Create media buy responses
    CreateMediaBuyResponse1,
    CreateMediaBuyResponse2,
    # DAAST assets
    DaastAsset1,
    DaastAsset2,
    # Deployment types
    Deployment1,
    Deployment2,
    # Destination types
    Destination1,
    Destination2,
    # Preview creative requests
    PreviewCreativeRequest1,
    PreviewCreativeRequest2,
    # Preview creative responses
    PreviewCreativeResponse1,
    PreviewCreativeResponse2,
    # Preview renders
    PreviewRender1,
    PreviewRender2,
    PreviewRender3,
    # Publisher properties types
    PropertyId,
    PropertyTag,
    # Performance feedback responses
    ProvidePerformanceFeedbackResponse1,
    ProvidePerformanceFeedbackResponse2,
    # SubAssets
    SubAsset1,
    SubAsset2,
    # Sync creatives responses
    SyncCreativesResponse1,
    SyncCreativesResponse2,
    # Update media buy requests
    UpdateMediaBuyRequest1,
    UpdateMediaBuyRequest2,
    # Update media buy responses
    UpdateMediaBuyResponse1,
    UpdateMediaBuyResponse2,
    # VAST assets
    VastAsset1,
    VastAsset2,
)
from adcp.types._generated import (
    PublisherPropertySelector1 as PublisherPropertiesInternal,
)
from adcp.types._generated import (
    PublisherPropertySelector2 as PublisherPropertiesByIdInternal,
)
from adcp.types._generated import (
    PublisherPropertySelector3 as PublisherPropertiesByTagInternal,
)

# Note: Package collision resolved by PR #223
# Both create_media_buy and update_media_buy now return full Package objects
# No more separate reference type needed
# Import Package from _generated (still uses qualified name for internal reasons)
from adcp.types._generated import _PackageFromPackage as Package

# ============================================================================
# RESPONSE TYPE ALIASES - Success/Error Discriminated Unions
# ============================================================================
# These are atomic operations where the response is EITHER success OR error,
# never both. The numbered suffixes from the generator don't convey this
# critical semantic distinction.

# Activate Signal Response Variants
ActivateSignalSuccessResponse = ActivateSignalResponse1
"""Success response - signal activation succeeded."""

ActivateSignalErrorResponse = ActivateSignalResponse2
"""Error response - signal activation failed."""

# Build Creative Response Variants
BuildCreativeSuccessResponse = BuildCreativeResponse1
"""Success response - creative built successfully, manifest returned."""

BuildCreativeErrorResponse = BuildCreativeResponse2
"""Error response - creative build failed, no manifest created."""

# Create Media Buy Response Variants
CreateMediaBuySuccessResponse = CreateMediaBuyResponse1
"""Success response - media buy created successfully with media_buy_id."""

CreateMediaBuyErrorResponse = CreateMediaBuyResponse2
"""Error response - media buy creation failed, no media buy created."""

# Performance Feedback Response Variants
ProvidePerformanceFeedbackSuccessResponse = ProvidePerformanceFeedbackResponse1
"""Success response - performance feedback accepted."""

ProvidePerformanceFeedbackErrorResponse = ProvidePerformanceFeedbackResponse2
"""Error response - performance feedback rejected."""

# Sync Creatives Response Variants
SyncCreativesSuccessResponse = SyncCreativesResponse1
"""Success response - sync operation processed creatives."""

SyncCreativesErrorResponse = SyncCreativesResponse2
"""Error response - sync operation failed."""

# Update Media Buy Response Variants
UpdateMediaBuySuccessResponse = UpdateMediaBuyResponse1
"""Success response - media buy updated successfully."""

UpdateMediaBuyErrorResponse = UpdateMediaBuyResponse2
"""Error response - media buy update failed, no changes applied."""

# ============================================================================
# REQUEST TYPE ALIASES - Operation Variants
# ============================================================================

# Preview Creative Request Variants
PreviewCreativeFormatRequest = PreviewCreativeRequest1
"""Preview request using format_id to identify creative format."""

PreviewCreativeManifestRequest = PreviewCreativeRequest2
"""Preview request using creative_manifest_url to identify creative."""

# Update Media Buy Request Variants
UpdateMediaBuyPackagesRequest = UpdateMediaBuyRequest1
"""Update request modifying packages in the media buy."""

UpdateMediaBuyPropertiesRequest = UpdateMediaBuyRequest2
"""Update request modifying media buy properties (not packages)."""

# ============================================================================
# ACTIVATION KEY ALIASES
# ============================================================================

PropertyIdActivationKey = ActivationKey1
"""Activation key using property_id for identification."""

PropertyTagActivationKey = ActivationKey2
"""Activation key using property_tags for identification."""

# ============================================================================
# PREVIEW/RENDER TYPE ALIASES
# ============================================================================

# Preview Creative Response Variants
PreviewCreativeStaticResponse = PreviewCreativeResponse1
"""Preview response with static renders (image/HTML snapshots)."""

PreviewCreativeInteractiveResponse = PreviewCreativeResponse2
"""Preview response with interactive renders (iframe embedding)."""

# Preview Render Variants (discriminated by output_format)
UrlPreviewRender = PreviewRender1
"""Preview render with output_format='url' - provides preview_url for iframe embedding."""

HtmlPreviewRender = PreviewRender2
"""Preview render with output_format='html' - provides preview_html for direct embedding."""

BothPreviewRender = PreviewRender3
"""Preview render with output_format='both' - provides both preview_url and preview_html."""

# ============================================================================
# ASSET TYPE ALIASES - Delivery & Kind Discriminated Unions
# ============================================================================

# VAST Asset Variants (discriminated by delivery_type)
UrlVastAsset = VastAsset1
"""VAST asset delivered via URL endpoint - delivery_type='url'."""

InlineVastAsset = VastAsset2
"""VAST asset with inline XML content - delivery_type='inline'."""

# DAAST Asset Variants (discriminated by delivery_type)
UrlDaastAsset = DaastAsset1
"""DAAST asset delivered via URL endpoint - delivery_type='url'."""

InlineDaastAsset = DaastAsset2
"""DAAST asset with inline XML content - delivery_type='inline'."""

# SubAsset Variants (discriminated by asset_kind)
MediaSubAsset = SubAsset1
"""SubAsset for media content (images, videos) - asset_kind='media', provides content_uri."""

TextSubAsset = SubAsset2
"""SubAsset for text content (headlines, body text) - asset_kind='text', provides content."""

# ============================================================================
# PACKAGE TYPE ALIASES - Resolving Type Name Collisions
# ============================================================================
# The AdCP schemas define two genuinely different types both named "Package":
#
# 1. Full Package (from package.json schema):
#    - Complete operational package with all fields (budget, pricing_option_id, etc.)
#    - Used in MediaBuy, update operations, and package management
#    - Has 12+ fields for full package configuration
#
# Package collision resolved by PR #223:
# - create-media-buy-response.json now returns full Package objects (not minimal refs)
# - update-media-buy-response.json already returned full Package objects
# - Both operations return identical Package structures
# - Single Package type imported above, no aliases needed

# ============================================================================
# PUBLISHER PROPERTIES ALIASES - Selection Type Discriminated Unions
# ============================================================================
# The AdCP schemas define PublisherProperties as a discriminated union with
# three variants based on the `selection_type` field:
#
# 1. All Properties (selection_type='all'):
#    - Includes all properties from the publisher
#    - Only requires publisher_domain
#
# 2. By ID (selection_type='by_id'):
#    - Specific properties selected by property_id
#    - Requires publisher_domain + property_ids array
#
# 3. By Tag (selection_type='by_tag'):
#    - Properties selected by tags
#    - Requires publisher_domain + property_tags array
#
# These semantic aliases match the discriminator values and make code more
# readable when constructing or pattern-matching publisher properties.

PublisherPropertiesAll = PublisherPropertiesInternal
"""Publisher properties covering all properties from the publisher.

This variant uses selection_type='all' and includes all properties listed
in the publisher's adagents.json file.

Fields:
- publisher_domain: Domain where adagents.json is hosted
- selection_type: Literal['all']

Example:
    ```python
    from adcp import PublisherPropertiesAll

    props = PublisherPropertiesAll(
        publisher_domain="example.com",
        selection_type="all"
    )
    ```
"""

PublisherPropertiesById = PublisherPropertiesByIdInternal
"""Publisher properties selected by specific property IDs.

This variant uses selection_type='by_id' and specifies an explicit list
of property IDs from the publisher's adagents.json file.

Fields:
- publisher_domain: Domain where adagents.json is hosted
- selection_type: Literal['by_id']
- property_ids: List of PropertyId (non-empty)

Example:
    ```python
    from adcp import PublisherPropertiesById, PropertyId

    props = PublisherPropertiesById(
        publisher_domain="example.com",
        selection_type="by_id",
        property_ids=[PropertyId("homepage"), PropertyId("sports_section")]
    )
    ```
"""

PublisherPropertiesByTag = PublisherPropertiesByTagInternal
"""Publisher properties selected by tags.

This variant uses selection_type='by_tag' and specifies property tags.
The product covers all properties in the publisher's adagents.json that
have these tags.

Fields:
- publisher_domain: Domain where adagents.json is hosted
- selection_type: Literal['by_tag']
- property_tags: List of PropertyTag (non-empty)

Example:
    ```python
    from adcp import PublisherPropertiesByTag, PropertyTag

    props = PublisherPropertiesByTag(
        publisher_domain="example.com",
        selection_type="by_tag",
        property_tags=[PropertyTag("premium"), PropertyTag("video")]
    )
    ```
"""

# ============================================================================
# DEPLOYMENT & DESTINATION ALIASES - Signal Deployment Type Discriminated Unions
# ============================================================================
# The AdCP schemas define Deployment and Destination as discriminated unions
# with two variants based on the `type` field:
#
# Deployment (where a signal is activated):
# - Platform (type='platform'): DSP platform with platform ID
# - Agent (type='agent'): Sales agent with agent URL
#
# Destination (where a signal can be activated):
# - Platform (type='platform'): Target DSP platform
# - Agent (type='agent'): Target sales agent
#
# These are used in GetSignalsResponse to describe signal availability and
# activation status across different advertising platforms and agents.

PlatformDeployment = Deployment1
"""Signal deployment to a DSP platform.

This variant uses type='platform' for platform-based signal deployments
like The Trade Desk, Amazon DSP, etc.

Fields:
- type: Literal['platform']
- platform: Platform identifier (e.g., 'the-trade-desk')
- account: Optional account identifier
- is_live: Whether signal is currently active
- deployed_at: Activation timestamp if live
- activation_key: Targeting key if live and accessible
- estimated_activation_duration_minutes: Time to complete activation

Example:
    ```python
    from adcp import PlatformDeployment

    deployment = PlatformDeployment(
        type="platform",
        platform="the-trade-desk",
        account="advertiser-123",
        is_live=True,
        deployed_at=datetime.now(timezone.utc)
    )
    ```
"""

AgentDeployment = Deployment2
"""Signal deployment to a sales agent.

This variant uses type='agent' for agent-based signal deployments
using agent URLs.

Fields:
- type: Literal['agent']
- agent_url: URL identifying the destination agent
- account: Optional account identifier
- is_live: Whether signal is currently active
- deployed_at: Activation timestamp if live
- activation_key: Targeting key if live and accessible
- estimated_activation_duration_minutes: Time to complete activation

Example:
    ```python
    from adcp import AgentDeployment

    deployment = AgentDeployment(
        type="agent",
        agent_url="https://agent.example.com",
        is_live=False,
        estimated_activation_duration_minutes=30.0
    )
    ```
"""

PlatformDestination = Destination1
"""Available signal destination on a DSP platform.

This variant uses type='platform' for platform-based signal destinations.

Fields:
- type: Literal['platform']
- platform: Platform identifier (e.g., 'the-trade-desk', 'amazon-dsp')
- account: Optional account identifier on the platform

Example:
    ```python
    from adcp import PlatformDestination

    destination = PlatformDestination(
        type="platform",
        platform="the-trade-desk",
        account="advertiser-123"
    )
    ```
"""

AgentDestination = Destination2
"""Available signal destination via a sales agent.

This variant uses type='agent' for agent-based signal destinations.

Fields:
- type: Literal['agent']
- agent_url: URL identifying the destination agent
- account: Optional account identifier on the agent

Example:
    ```python
    from adcp import AgentDestination

    destination = AgentDestination(
        type="agent",
        agent_url="https://agent.example.com",
        account="partner-456"
    )
    ```
"""

# ============================================================================
# AUTHORIZED AGENTS ALIASES - Authorization Type Discriminated Unions
# ============================================================================
# The AdCP adagents.json schema defines AuthorizedAgents as a discriminated
# union with four variants based on the `authorization_type` field:
#
# 1. Property IDs (authorization_type='property_ids'):
#    - Agent authorized for specific property IDs
#    - Requires property_ids array
#
# 2. Property Tags (authorization_type='property_tags'):
#    - Agent authorized for properties matching tags
#    - Requires property_tags array
#
# 3. Inline Properties (authorization_type='inline_properties'):
#    - Agent authorized with inline property definitions
#    - Requires properties array with full Property objects
#
# 4. Publisher Properties (authorization_type='publisher_properties'):
#    - Agent authorized for properties from other publisher domains
#    - Requires publisher_properties array
#
# These define which sales agents are authorized to sell inventory and which
# properties they can access.

AuthorizedAgentsByPropertyId = AuthorizedAgents
"""Authorized agent with specific property IDs.

This variant uses authorization_type='property_ids' for agents authorized
to sell specific properties identified by their IDs.

Fields:
- authorization_type: Literal['property_ids']
- authorized_for: Human-readable description
- property_ids: List of PropertyId (non-empty)
- url: Agent's API endpoint URL

Example:
    ```python
    from adcp.types.aliases import AuthorizedAgentsByPropertyId, PropertyId

    agent = AuthorizedAgentsByPropertyId(
        authorization_type="property_ids",
        authorized_for="Premium display inventory",
        property_ids=[PropertyId("homepage"), PropertyId("sports")],
        url="https://agent.example.com"
    )
    ```
"""

AuthorizedAgentsByPropertyTag = AuthorizedAgents1
"""Authorized agent with property tags.

This variant uses authorization_type='property_tags' for agents authorized
to sell properties identified by matching tags.

Fields:
- authorization_type: Literal['property_tags']
- authorized_for: Human-readable description
- property_tags: List of PropertyTag (non-empty)
- url: Agent's API endpoint URL

Example:
    ```python
    from adcp.types.aliases import AuthorizedAgentsByPropertyTag, PropertyTag

    agent = AuthorizedAgentsByPropertyTag(
        authorization_type="property_tags",
        authorized_for="Video inventory",
        property_tags=[PropertyTag("video"), PropertyTag("premium")],
        url="https://agent.example.com"
    )
    ```
"""

AuthorizedAgentsByInlineProperties = AuthorizedAgents2
"""Authorized agent with inline property definitions.

This variant uses authorization_type='inline_properties' for agents with
inline Property objects rather than references to the top-level properties array.

Fields:
- authorization_type: Literal['inline_properties']
- authorized_for: Human-readable description
- properties: List of Property objects (non-empty)
- url: Agent's API endpoint URL

Example:
    ```python
    from adcp.types.aliases import AuthorizedAgentsByInlineProperties
    from adcp.types.stable import Property

    agent = AuthorizedAgentsByInlineProperties(
        authorization_type="inline_properties",
        authorized_for="Custom inventory bundle",
        properties=[...],  # Full Property objects
        url="https://agent.example.com"
    )
    ```
"""

AuthorizedAgentsByPublisherProperties = AuthorizedAgents3
"""Authorized agent for properties from other publishers.

This variant uses authorization_type='publisher_properties' for agents
authorized to sell inventory from other publisher domains.

Fields:
- authorization_type: Literal['publisher_properties']
- authorized_for: Human-readable description
- publisher_properties: List of PublisherPropertySelector variants (non-empty)
- url: Agent's API endpoint URL

Example:
    ```python
    from adcp.types.aliases import (
        AuthorizedAgentsByPublisherProperties,
        PublisherPropertiesAll
    )

    agent = AuthorizedAgentsByPublisherProperties(
        authorization_type="publisher_properties",
        authorized_for="Network inventory across publishers",
        publisher_properties=[
            PublisherPropertiesAll(
                publisher_domain="publisher1.com",
                selection_type="all"
            )
        ],
        url="https://agent.example.com"
    )
    ```
"""

# ============================================================================
# UNION TYPE ALIASES - For Type Hints and Pattern Matching
# ============================================================================
# These union aliases provide convenient types for function signatures,
# type hints, and pattern matching without having to manually construct
# the union each time.

# Deployment union (for signals)
Deployment = PlatformDeployment | AgentDeployment
"""Union type for all deployment variants.

Use this for type hints when a function accepts any deployment type:

Example:
    ```python
    def process_deployment(deployment: Deployment) -> None:
        if isinstance(deployment, PlatformDeployment):
            print(f"Platform: {deployment.platform}")
        elif isinstance(deployment, AgentDeployment):
            print(f"Agent: {deployment.agent_url}")
    ```
"""

# Destination union (for signals)
Destination = PlatformDestination | AgentDestination
"""Union type for all destination variants.

Use this for type hints when a function accepts any destination type:

Example:
    ```python
    def format_destination(dest: Destination) -> str:
        if isinstance(dest, PlatformDestination):
            return f"Platform: {dest.platform}"
        elif isinstance(dest, AgentDestination):
            return f"Agent: {dest.agent_url}"
    ```
"""

# Authorized agent union (for adagents.json)
AuthorizedAgent = (
    AuthorizedAgentsByPropertyId
    | AuthorizedAgentsByPropertyTag
    | AuthorizedAgentsByInlineProperties
    | AuthorizedAgentsByPublisherProperties
)
"""Union type for all authorized agent variants.

Use this for type hints when processing agents from adagents.json:

Example:
    ```python
    def validate_agent(agent: AuthorizedAgent) -> bool:
        match agent.authorization_type:
            case "property_ids":
                return len(agent.property_ids) > 0
            case "property_tags":
                return len(agent.property_tags) > 0
            case "inline_properties":
                return len(agent.properties) > 0
            case "publisher_properties":
                return len(agent.publisher_properties) > 0
    ```
"""

# Publisher properties union (for product requests)
PublisherProperties = (
    PublisherPropertiesAll | PublisherPropertiesById | PublisherPropertiesByTag
)
"""Union type for all publisher properties variants.

Use this for type hints in product filtering:

Example:
    ```python
    def filter_products(props: PublisherProperties) -> None:
        match props.selection_type:
            case "all":
                print("All properties from publisher")
            case "by_id":
                print(f"Properties: {props.property_ids}")
            case "by_tag":
                print(f"Tags: {props.property_tags}")
    ```
"""

# ============================================================================
# FILTER TYPE ALIASES - Request Filter Name Collision Resolution
# ============================================================================
# The AdCP schemas define multiple `Filters` classes with the same name but
# different fields for different request types. The generator creates these
# as local classes within each request module.
#
# These aliases resolve the name collision by providing semantic names that
# clearly indicate which request type each filter belongs to.

from adcp.types.generated_poc.get_products_request import Filters as ProductFiltersInternal
from adcp.types.generated_poc.get_signals_request import Filters as SignalFiltersInternal
from adcp.types.generated_poc.list_creatives_request import Filters as CreativeFiltersInternal

ProductFilters = ProductFiltersInternal
"""Filters for GetProductsRequest.

Used when discovering available advertising products to narrow down results
based on delivery type, pricing model, format requirements, etc.

Fields:
- delivery_type: Filter by delivery type (guaranteed/non_guaranteed)
- is_fixed_price: Filter for fixed price vs auction products
- format_types: Filter by format types (video, display, audio)
- format_ids: Filter by specific format IDs
- standard_formats_only: Only return products accepting IAB standard formats
- min_exposures: Minimum exposures/impressions needed (≥1)

Example:
    ```python
    from adcp import ProductFilters, GetProductsRequest

    filters = ProductFilters(
        delivery_type="guaranteed",
        format_types=["video", "display"],
        min_exposures=10000
    )

    request = GetProductsRequest(
        brief="Premium video inventory",
        filters=filters
    )
    ```
"""

CreativeFilters = CreativeFiltersInternal
"""Filters for ListCreativesRequest.

Used when querying creative assets from the centralized library to filter
by format, status, tags, assignment status, and date ranges.

Fields:
- format: Filter by single creative format type
- formats: Filter by multiple creative format types
- status: Filter by approval status (processing, pending_review, approved, rejected)
- statuses: Filter by multiple approval statuses
- tags: Filter by tags (all tags must match)
- tags_any: Filter by tags (any tag must match)
- name_contains: Filter by creative names containing text (case-insensitive)
- creative_ids: Filter by specific creative IDs (max 100)
- created_after: Filter creatives created after date (ISO 8601)
- created_before: Filter creatives created before date
- updated_after: Filter creatives updated after date
- updated_before: Filter creatives updated before date
- assigned_to_package: Filter creatives assigned to specific package
- assigned_to_packages: Filter creatives assigned to any of these packages
- unassigned: Filter for unassigned (true) or assigned (false) creatives
- has_performance_data: Filter creatives with performance data

Example:
    ```python
    from adcp import CreativeFilters, ListCreativesRequest

    filters = CreativeFilters(
        status="approved",
        formats=["video", "display"],
        tags=["holiday", "2024"],
        has_performance_data=True
    )

    request = ListCreativesRequest(filters=filters)
    ```
"""

SignalFilters = SignalFiltersInternal
"""Filters for GetSignalsRequest.

Used when discovering audience signals to refine results based on
catalog type, data providers, pricing, and coverage requirements.

Fields:
- catalog_types: Filter by catalog type (marketplace, custom, owned)
- data_providers: Filter by specific data providers
- max_cpm: Maximum CPM price filter (≥0)
- min_coverage_percentage: Minimum coverage requirement (0-100)

Example:
    ```python
    from adcp import SignalFilters, GetSignalsRequest

    filters = SignalFilters(
        catalog_types=["marketplace", "owned"],
        max_cpm=5.00,
        min_coverage_percentage=80.0
    )

    request = GetSignalsRequest(
        signal_spec="Coffee enthusiasts in major metros",
        deliver_to={...},
        filters=filters
    )
    ```
"""

# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # Filter type aliases
    "ProductFilters",
    "CreativeFilters",
    "SignalFilters",
    # Activation responses
    "ActivateSignalSuccessResponse",
    "ActivateSignalErrorResponse",
    # Activation keys
    "PropertyIdActivationKey",
    "PropertyTagActivationKey",
    # Asset type aliases
    "BothPreviewRender",
    "HtmlPreviewRender",
    "InlineDaastAsset",
    "InlineVastAsset",
    "MediaSubAsset",
    "TextSubAsset",
    "UrlDaastAsset",
    "UrlPreviewRender",
    "UrlVastAsset",
    # Authorized agent variants
    "AuthorizedAgentsByPropertyId",
    "AuthorizedAgentsByPropertyTag",
    "AuthorizedAgentsByInlineProperties",
    "AuthorizedAgentsByPublisherProperties",
    # Authorized agent union
    "AuthorizedAgent",
    # Build creative responses
    "BuildCreativeSuccessResponse",
    "BuildCreativeErrorResponse",
    # Create media buy responses
    "CreateMediaBuySuccessResponse",
    "CreateMediaBuyErrorResponse",
    # Performance feedback responses
    "ProvidePerformanceFeedbackSuccessResponse",
    "ProvidePerformanceFeedbackErrorResponse",
    # Preview creative requests
    "PreviewCreativeFormatRequest",
    "PreviewCreativeManifestRequest",
    # Preview creative responses
    "PreviewCreativeStaticResponse",
    "PreviewCreativeInteractiveResponse",
    # Sync creatives responses
    "SyncCreativesSuccessResponse",
    "SyncCreativesErrorResponse",
    # Update media buy requests
    "UpdateMediaBuyPackagesRequest",
    "UpdateMediaBuyPropertiesRequest",
    # Update media buy responses
    "UpdateMediaBuySuccessResponse",
    "UpdateMediaBuyErrorResponse",
    # Package type aliases
    "Package",
    # Publisher properties types
    "PropertyId",
    "PropertyTag",
    # Publisher properties variants
    "PublisherPropertiesAll",
    "PublisherPropertiesById",
    "PublisherPropertiesByTag",
    # Publisher properties union
    "PublisherProperties",
    # Deployment variants
    "PlatformDeployment",
    "AgentDeployment",
    # Deployment union
    "Deployment",
    # Destination variants
    "PlatformDestination",
    "AgentDestination",
    # Destination union
    "Destination",
]
