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

# Import generated types modules - for internal use
# Note: Users should import specific types, not the whole module
from adcp.types import _generated as generated
from adcp.types import aliases

# Re-export commonly-used request/response types for convenience
# Users should import from main package (e.g., `from adcp import GetProductsRequest`)
# rather than internal modules for better API stability
from adcp.types._generated import (
    # Audience & Targeting
    ActivateSignalRequest,
    ActivateSignalResponse,
    # Creative Operations
    BuildCreativeRequest,
    BuildCreativeResponse,
    # Media Buy Operations
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    # Common data types
    Error,
    Format,
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
    PreviewCreativeRequest,
    PreviewCreativeResponse,
    Product,
    Property,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    SyncCreativesRequest,
    SyncCreativesResponse,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
)
from adcp.types._generated import TaskStatus as GeneratedTaskStatus

# Re-export semantic type aliases for better ergonomics
from adcp.types.aliases import (
    ActivateSignalErrorResponse,
    ActivateSignalSuccessResponse,
    BothPreviewRender,
    BuildCreativeErrorResponse,
    BuildCreativeSuccessResponse,
    CreatedPackageReference,
    CreateMediaBuyErrorResponse,
    CreateMediaBuySuccessResponse,
    HtmlPreviewRender,
    InlineDaastAsset,
    InlineVastAsset,
    MediaSubAsset,
    PreviewCreativeFormatRequest,
    PreviewCreativeInteractiveResponse,
    PreviewCreativeManifestRequest,
    PreviewCreativeStaticResponse,
    PropertyIdActivationKey,
    PropertyTagActivationKey,
    ProvidePerformanceFeedbackErrorResponse,
    ProvidePerformanceFeedbackSuccessResponse,
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

# Re-export core domain types and pricing options from stable API
# These are commonly used in typical workflows
from adcp.types.stable import (
    # Core domain types
    BrandManifest,
    # Pricing options (all 9 types for product creation)
    CpcPricingOption,
    CpcvPricingOption,
    CpmAuctionPricingOption,
    CpmFixedRatePricingOption,
    CppPricingOption,
    CpvPricingOption,
    Creative,
    CreativeManifest,
    # Status enums (for control flow)
    CreativeStatus,
    FlatRatePricingOption,
    MediaBuy,
    MediaBuyStatus,
    Package,
    PackageStatus,
    PricingModel,
    VcpmAuctionPricingOption,
    VcpmFixedRatePricingOption,
)
from adcp.validation import (
    ValidationError,
    validate_adagents,
    validate_agent_authorization,
    validate_product,
    validate_publisher_properties_item,
)

__version__ = "2.5.0"

__all__ = [
    # Client classes
    "ADCPClient",
    "ADCPMultiAgentClient",
    # Core types
    "AgentConfig",
    "Protocol",
    "TaskResult",
    "TaskStatus",
    "WebhookMetadata",
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
    "ListAuthorizedPropertiesRequest",
    "ListAuthorizedPropertiesResponse",
    "ProvidePerformanceFeedbackRequest",
    "ProvidePerformanceFeedbackResponse",
    "Error",
    "Format",
    "Product",
    "Property",
    # Core domain types (from stable API)
    "BrandManifest",
    "Creative",
    "CreativeManifest",
    "MediaBuy",
    "Package",
    # Package type aliases
    "CreatedPackageReference",
    # Status enums (for control flow)
    "CreativeStatus",
    "MediaBuyStatus",
    "PackageStatus",
    "PricingModel",
    # Pricing options (all 9 types)
    "CpcPricingOption",
    "CpcvPricingOption",
    "CpmAuctionPricingOption",
    "CpmFixedRatePricingOption",
    "CppPricingOption",
    "CpvPricingOption",
    "FlatRatePricingOption",
    "VcpmAuctionPricingOption",
    "VcpmFixedRatePricingOption",
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
    "BothPreviewRender",
    "BuildCreativeSuccessResponse",
    "BuildCreativeErrorResponse",
    "CreateMediaBuySuccessResponse",
    "CreateMediaBuyErrorResponse",
    "HtmlPreviewRender",
    "InlineDaastAsset",
    "InlineVastAsset",
    "MediaSubAsset",
    "PreviewCreativeFormatRequest",
    "PreviewCreativeManifestRequest",
    "PreviewCreativeStaticResponse",
    "PreviewCreativeInteractiveResponse",
    "PropertyIdActivationKey",
    "PropertyTagActivationKey",
    "ProvidePerformanceFeedbackSuccessResponse",
    "ProvidePerformanceFeedbackErrorResponse",
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
