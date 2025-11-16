from __future__ import annotations

"""
AdCP Python Client Library

Official Python client for the Ad Context Protocol (AdCP).
Supports both A2A and MCP protocols with full type safety.
"""

from adcp.adagents import (
    domain_matches,
    fetch_adagents,
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

# Import all generated types - users can import what they need from adcp.types.generated
from adcp.types import aliases, generated

# Re-export semantic type aliases for better ergonomics
from adcp.types.aliases import (
    ActivateSignalErrorResponse,
    ActivateSignalSuccessResponse,
    BuildCreativeErrorResponse,
    BuildCreativeSuccessResponse,
    CreateMediaBuyErrorResponse,
    CreateMediaBuySuccessResponse,
    PreviewCreativeFormatRequest,
    PreviewCreativeInteractiveResponse,
    PreviewCreativeManifestRequest,
    PreviewCreativeStaticResponse,
    PreviewRenderHtml,
    PreviewRenderIframe,
    PreviewRenderImage,
    PropertyIdActivationKey,
    PropertyTagActivationKey,
    ProvidePerformanceFeedbackErrorResponse,
    ProvidePerformanceFeedbackSuccessResponse,
    SyncCreativesErrorResponse,
    SyncCreativesSuccessResponse,
    UpdateMediaBuyErrorResponse,
    UpdateMediaBuyPackagesRequest,
    UpdateMediaBuyPropertiesRequest,
    UpdateMediaBuySuccessResponse,
)
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus, WebhookMetadata

# Re-export commonly-used request/response types for convenience
# Users should import from main package (e.g., `from adcp import GetProductsRequest`)
# rather than internal modules for better API stability
from adcp.types.generated import (
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
from adcp.types.generated import TaskStatus as GeneratedTaskStatus
from adcp.validation import (
    ValidationError,
    validate_adagents,
    validate_agent_authorization,
    validate_product,
    validate_publisher_properties_item,
)

__version__ = "2.1.0"

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
    # Adagents validation
    "fetch_adagents",
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
    "BuildCreativeSuccessResponse",
    "BuildCreativeErrorResponse",
    "CreateMediaBuySuccessResponse",
    "CreateMediaBuyErrorResponse",
    "ProvidePerformanceFeedbackSuccessResponse",
    "ProvidePerformanceFeedbackErrorResponse",
    "SyncCreativesSuccessResponse",
    "SyncCreativesErrorResponse",
    "UpdateMediaBuySuccessResponse",
    "UpdateMediaBuyErrorResponse",
    "PreviewCreativeFormatRequest",
    "PreviewCreativeManifestRequest",
    "PreviewCreativeStaticResponse",
    "PreviewCreativeInteractiveResponse",
    "PreviewRenderImage",
    "PreviewRenderHtml",
    "PreviewRenderIframe",
    "PropertyIdActivationKey",
    "PropertyTagActivationKey",
    "UpdateMediaBuyPackagesRequest",
    "UpdateMediaBuyPropertiesRequest",
]
