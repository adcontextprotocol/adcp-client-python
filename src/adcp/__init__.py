from __future__ import annotations

"""
AdCP Python Client Library

Official Python client for the Ad Context Protocol (AdCP).
Supports both A2A and MCP protocols with full type safety.
"""

from adcp.adagents import (
    AuthorizationContext,
    domain_matches,
    fetch_adagents,
    fetch_agent_authorizations,
    get_all_properties,
    get_all_tags,
    get_properties_by_agent,
    identifiers_match,
    verify_agent_authorization,
    verify_agent_for_property,
)
from adcp.client import ADCPClient, ADCPMultiAgentClient
from adcp.exceptions import (
    AdagentsNotFoundError,
    AdagentsTimeoutError,
    AdagentsValidationError,
    ADCPAuthenticationError,
    ADCPConnectionError,
    ADCPError,
    ADCPProtocolError,
    ADCPTimeoutError,
    ADCPToolNotFoundError,
    ADCPWebhookError,
    ADCPWebhookSignatureError,
)

# Test helpers
from adcp.testing import (
    CREATIVE_AGENT_CONFIG,
    TEST_AGENT_A2A_CONFIG,
    TEST_AGENT_A2A_NO_AUTH_CONFIG,
    TEST_AGENT_MCP_CONFIG,
    TEST_AGENT_MCP_NO_AUTH_CONFIG,
    TEST_AGENT_TOKEN,
    create_test_agent,
    creative_agent,
    test_agent,
    test_agent_a2a,
    test_agent_a2a_no_auth,
    test_agent_client,
    test_agent_no_auth,
)

# Re-export commonly-used request/response types for convenience
# Users should import from main package (e.g., `from adcp import GetProductsRequest`)
# rather than internal modules for better API stability
# Re-export core domain types and pricing options
# These are commonly used in typical workflows
from adcp.types import (
    # Audience & Targeting
    ActivateSignalRequest,
    ActivateSignalResponse,
    # Type enums from PR #222
    AssetContentType,
    # Core domain types
    BrandManifest,
    # Creative Operations
    BuildCreativeRequest,
    BuildCreativeResponse,
    # Pricing options (all 9 types for product creation)
    CpcPricingOption,
    CpcvPricingOption,
    CpmAuctionPricingOption,
    CpmFixedRatePricingOption,
    CppPricingOption,
    CpvPricingOption,
    # Media Buy Operations
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    Creative,
    CreativeFilters,
    CreativeManifest,
    # Status enums (for control flow)
    CreativeStatus,
    # Common data types
    Error,
    FlatRatePricingOption,
    Format,
    FormatCategory,
    FormatId,
    GeneratedTaskStatus,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
    ListAuthorizedPropertiesRequest,
    ListAuthorizedPropertiesResponse,
    ListCreativeFormatsRequest,
    ListCreativeFormatsResponse,
    ListCreativesRequest,
    ListCreativesResponse,
    McpWebhookPayload,
    MediaBuy,
    MediaBuyStatus,
    Package,
    PackageRequest,
    PreviewCreativeRequest,
    PreviewCreativeResponse,
    PriceGuidance,
    PricingModel,
    Product,
    ProductFilters,
    Property,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    PushNotificationConfig,
    SignalFilters,
    SyncCreativesRequest,
    SyncCreativesResponse,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
    VcpmAuctionPricingOption,
    VcpmFixedRatePricingOption,
    aliases,
)

# Import generated types modules - for internal use
# Note: Users should import specific types, not the whole module
from adcp.types import _generated as generated

# Re-export semantic type aliases for better ergonomics
from adcp.types.aliases import (
    ActivateSignalErrorResponse,
    ActivateSignalSuccessResponse,
    AgentDeployment,
    AgentDestination,
    BothPreviewRender,
    BuildCreativeErrorResponse,
    BuildCreativeSuccessResponse,
    CreateMediaBuyErrorResponse,
    CreateMediaBuySuccessResponse,
    HtmlPreviewRender,
    InlineDaastAsset,
    InlineVastAsset,
    MediaSubAsset,
    PlatformDeployment,
    PlatformDestination,
    PreviewCreativeFormatRequest,
    PreviewCreativeInteractiveResponse,
    PreviewCreativeManifestRequest,
    PreviewCreativeStaticResponse,
    PropertyId,
    PropertyIdActivationKey,
    PropertyTag,
    PropertyTagActivationKey,
    ProvidePerformanceFeedbackErrorResponse,
    ProvidePerformanceFeedbackSuccessResponse,
    PublisherPropertiesAll,
    PublisherPropertiesById,
    PublisherPropertiesByTag,
    SyncCreativesErrorResponse,
    SyncCreativesSuccessResponse,
    TextSubAsset,
    UpdateMediaBuyErrorResponse,
    UpdateMediaBuyPackagesRequest,
    UpdateMediaBuyPropertiesRequest,
    UpdateMediaBuySuccessResponse,
    UrlDaastAsset,
    UrlPreviewRender,
    UrlVastAsset,
)
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus, WebhookMetadata
from adcp.validation import (
    ValidationError,
    validate_adagents,
    validate_agent_authorization,
    validate_product,
    validate_publisher_properties_item,
)
from adcp.webhooks import (
    create_a2a_webhook_payload,
    create_mcp_webhook_payload,
    extract_webhook_result_data,
    get_adcp_signed_headers_for_webhook,
)

__version__ = "2.14.0"


def get_adcp_version() -> str:
    """
    Get the target AdCP specification version this SDK is built for.

    This version determines which AdCP schemas are used for type generation
    and validation. The SDK is designed to work with this specific version
    of the AdCP specification.

    Returns:
        AdCP specification version (e.g., "2.5.0")

    Raises:
        FileNotFoundError: If ADCP_VERSION file is missing from package
    """
    from importlib.resources import files

    # Read from ADCP_VERSION file in package
    version_file = files("adcp") / "ADCP_VERSION"
    return version_file.read_text().strip()


__all__ = [
    # Version functions
    "get_adcp_version",
    # Client classes
    "ADCPClient",
    "ADCPMultiAgentClient",
    # Core types
    "AgentConfig",
    "Protocol",
    "TaskResult",
    "TaskStatus",
    "WebhookMetadata",
    # Webhook utilities
    "create_mcp_webhook_payload",
    "create_a2a_webhook_payload",
    "get_adcp_signed_headers_for_webhook",
    "extract_webhook_result_data",
    "McpWebhookPayload",
    # Common request/response types (re-exported for convenience)
    "CreateMediaBuyRequest",
    "CreateMediaBuyResponse",
    "GetMediaBuyDeliveryRequest",
    "GetMediaBuyDeliveryResponse",
    "GetProductsRequest",
    "GetProductsResponse",
    "UpdateMediaBuyRequest",
    "UpdateMediaBuyResponse",
    "BuildCreativeRequest",
    "BuildCreativeResponse",
    "ListCreativeFormatsRequest",
    "ListCreativeFormatsResponse",
    "ListCreativesRequest",
    "ListCreativesResponse",
    "PreviewCreativeRequest",
    "PreviewCreativeResponse",
    "SyncCreativesRequest",
    "SyncCreativesResponse",
    "ActivateSignalRequest",
    "ActivateSignalResponse",
    "GetSignalsRequest",
    "GetSignalsResponse",
    "SignalFilters",
    "ListAuthorizedPropertiesRequest",
    "ListAuthorizedPropertiesResponse",
    "ProvidePerformanceFeedbackRequest",
    "ProvidePerformanceFeedbackResponse",
    "Error",
    "Format",
    "FormatId",
    # New type enums from PR #222
    "AssetContentType",
    "FormatCategory",
    "Product",
    "ProductFilters",
    "Property",
    # Core domain types (from stable API)
    "BrandManifest",
    "Creative",
    "CreativeFilters",
    "CreativeManifest",
    "MediaBuy",
    "Package",
    "PackageRequest",
    # Status enums (for control flow)
    "CreativeStatus",
    "MediaBuyStatus",
    "PricingModel",
    # Pricing-related types
    "CpcPricingOption",
    "CpcvPricingOption",
    "CpmAuctionPricingOption",
    "CpmFixedRatePricingOption",
    "CppPricingOption",
    "CpvPricingOption",
    "FlatRatePricingOption",
    "PriceGuidance",
    "VcpmAuctionPricingOption",
    "VcpmFixedRatePricingOption",
    # Configuration types
    "PushNotificationConfig",
    # Adagents validation
    "AuthorizationContext",
    "fetch_adagents",
    "fetch_agent_authorizations",
    "verify_agent_authorization",
    "verify_agent_for_property",
    "domain_matches",
    "identifiers_match",
    "get_all_properties",
    "get_all_tags",
    "get_properties_by_agent",
    # Test helpers
    "test_agent",
    "test_agent_a2a",
    "test_agent_no_auth",
    "test_agent_a2a_no_auth",
    "creative_agent",
    "test_agent_client",
    "create_test_agent",
    "TEST_AGENT_TOKEN",
    "TEST_AGENT_MCP_CONFIG",
    "TEST_AGENT_A2A_CONFIG",
    "TEST_AGENT_MCP_NO_AUTH_CONFIG",
    "TEST_AGENT_A2A_NO_AUTH_CONFIG",
    "CREATIVE_AGENT_CONFIG",
    # Exceptions
    "ADCPError",
    "ADCPConnectionError",
    "ADCPAuthenticationError",
    "ADCPTimeoutError",
    "ADCPProtocolError",
    "ADCPToolNotFoundError",
    "ADCPWebhookError",
    "ADCPWebhookSignatureError",
    "AdagentsValidationError",
    "AdagentsNotFoundError",
    "AdagentsTimeoutError",
    # Validation utilities
    "ValidationError",
    "validate_adagents",
    "validate_agent_authorization",
    "validate_product",
    "validate_publisher_properties_item",
    # Generated types modules
    "generated",
    "aliases",
    "GeneratedTaskStatus",
    # Semantic type aliases (for better API ergonomics)
    "ActivateSignalSuccessResponse",
    "ActivateSignalErrorResponse",
    "AgentDeployment",
    "AgentDestination",
    "BothPreviewRender",
    "BuildCreativeSuccessResponse",
    "BuildCreativeErrorResponse",
    "CreateMediaBuySuccessResponse",
    "CreateMediaBuyErrorResponse",
    "HtmlPreviewRender",
    "InlineDaastAsset",
    "InlineVastAsset",
    "MediaSubAsset",
    "PlatformDeployment",
    "PlatformDestination",
    "PreviewCreativeFormatRequest",
    "PreviewCreativeManifestRequest",
    "PreviewCreativeStaticResponse",
    "PreviewCreativeInteractiveResponse",
    "PropertyId",
    "PropertyIdActivationKey",
    "PropertyTag",
    "PropertyTagActivationKey",
    "ProvidePerformanceFeedbackSuccessResponse",
    "ProvidePerformanceFeedbackErrorResponse",
    "PublisherPropertiesAll",
    "PublisherPropertiesById",
    "PublisherPropertiesByTag",
    "SyncCreativesSuccessResponse",
    "SyncCreativesErrorResponse",
    "TextSubAsset",
    "UpdateMediaBuySuccessResponse",
    "UpdateMediaBuyErrorResponse",
    "UpdateMediaBuyPackagesRequest",
    "UpdateMediaBuyPropertiesRequest",
    "UrlDaastAsset",
    "UrlPreviewRender",
    "UrlVastAsset",
]
