from __future__ import annotations

"""
AdCP Python Client Library

Official Python client for the Ad Context Protocol (AdCP).
Supports both A2A and MCP protocols with full type safety.
"""

from adcp.client import ADCPClient, ADCPMultiAgentClient
from adcp.exceptions import (
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
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus, WebhookMetadata
from adcp.types.generated import (
    ActivateSignalError,
    # Request/Response types
    ActivateSignalRequest,
    ActivateSignalResponse,
    ActivateSignalSuccess,
    ActivationKey,
    AgentDeployment,
    AgentDestination,
    BothPreviewRender,
    # Brand types
    BrandManifest,
    BrandManifestRef,
    BuildCreativeRequest,
    BuildCreativeResponse,
    # Channel types
    Channels,
    CreateMediaBuyError,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    CreateMediaBuySuccess,
    # Creative types
    CreativeAsset,
    CreativeAssignment,
    CreativeManifest,
    CreativePolicy,
    DaastAsset,
    # Metrics types
    DeliveryMetrics,
    # Delivery types
    DeliveryType,
    Deployment,
    # Deployment types
    Destination,
    Error,
    Format,
    FormatId,
    FrequencyCap,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
    HtmlPreviewRender,
    InlineDaastAsset,
    InlineVastAsset,
    Key_valueActivationKey,
    ListAuthorizedPropertiesRequest,
    ListAuthorizedPropertiesResponse,
    ListCreativeFormatsRequest,
    ListCreativeFormatsResponse,
    ListCreativesRequest,
    ListCreativesResponse,
    Measurement,
    # Core domain types
    MediaBuy,
    # Status enums
    MediaBuyStatus,
    # Sub-asset types
    MediaSubAsset,
    Pacing,
    Package,
    PackageStatus,
    PerformanceFeedback,
    Placement,
    PlatformDeployment,
    PlatformDestination,
    PreviewCreativeRequest,
    PreviewCreativeResponse,
    # Preview render types
    PreviewRender,
    PricingModel,
    # Pricing types
    PricingOption,
    Product,
    PromotedProducts,
    # Property and placement types
    Property,
    ProtocolEnvelope,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    PushNotificationConfig,
    ReportingCapabilities,
    Response,
    Segment_idActivationKey,
    StandardFormatIds,
    StartTiming,
    SubAsset,
    SyncCreativesError,
    SyncCreativesRequest,
    SyncCreativesResponse,
    SyncCreativesSuccess,
    # Targeting types
    Targeting,
    # Task types
    TaskType,
    TextSubAsset,
    UpdateMediaBuyError,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
    UpdateMediaBuySuccess,
    UrlDaastAsset,
    UrlPreviewRender,
    UrlVastAsset,
    # Asset delivery types (VAST/DAAST)
    VastAsset,
    # Protocol types
    WebhookPayload,
)
from adcp.types.generated import (
    TaskStatus as GeneratedTaskStatus,
)

__version__ = "1.4.1"

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
    # Request/Response types
    "ActivateSignalRequest",
    "ActivateSignalResponse",
    "ActivateSignalSuccess",
    "ActivateSignalError",
    "ActivationKey",
    "Segment_idActivationKey",
    "Key_valueActivationKey",
    "BuildCreativeRequest",
    "BuildCreativeResponse",
    "CreateMediaBuyRequest",
    "CreateMediaBuyResponse",
    "CreateMediaBuySuccess",
    "CreateMediaBuyError",
    "GetMediaBuyDeliveryRequest",
    "GetMediaBuyDeliveryResponse",
    "GetProductsRequest",
    "GetProductsResponse",
    "GetSignalsRequest",
    "GetSignalsResponse",
    "ListAuthorizedPropertiesRequest",
    "ListAuthorizedPropertiesResponse",
    "ListCreativeFormatsRequest",
    "ListCreativeFormatsResponse",
    "ListCreativesRequest",
    "ListCreativesResponse",
    "PreviewCreativeRequest",
    "PreviewCreativeResponse",
    "ProvidePerformanceFeedbackRequest",
    "ProvidePerformanceFeedbackResponse",
    "SyncCreativesRequest",
    "SyncCreativesResponse",
    "SyncCreativesSuccess",
    "SyncCreativesError",
    "UpdateMediaBuyRequest",
    "UpdateMediaBuyResponse",
    "UpdateMediaBuySuccess",
    "UpdateMediaBuyError",
    # Core domain types
    "MediaBuy",
    "Product",
    "Package",
    "Error",
    # Creative types
    "CreativeAsset",
    "CreativeManifest",
    "CreativeAssignment",
    "CreativePolicy",
    "Format",
    "FormatId",
    # Property and placement types
    "Property",
    "Placement",
    # Targeting types
    "Targeting",
    "FrequencyCap",
    "Pacing",
    # Brand types
    "BrandManifest",
    "BrandManifestRef",
    # Metrics types
    "DeliveryMetrics",
    "Measurement",
    "PerformanceFeedback",
    # Status enums
    "MediaBuyStatus",
    "PackageStatus",
    # Pricing types
    "PricingOption",
    "PricingModel",
    # Delivery types
    "DeliveryType",
    "StartTiming",
    # Channel types
    "Channels",
    "StandardFormatIds",
    # Protocol types
    "WebhookPayload",
    "ProtocolEnvelope",
    "Response",
    "PromotedProducts",
    "PushNotificationConfig",
    "ReportingCapabilities",
    # Deployment types
    "Destination",
    "Deployment",
    "PlatformDestination",
    "AgentDestination",
    "PlatformDeployment",
    "AgentDeployment",
    # Sub-asset types
    "MediaSubAsset",
    "SubAsset",
    "TextSubAsset",
    # Asset delivery types (VAST/DAAST)
    "VastAsset",
    "UrlVastAsset",
    "InlineVastAsset",
    "DaastAsset",
    "UrlDaastAsset",
    "InlineDaastAsset",
    # Preview render types
    "PreviewRender",
    "UrlPreviewRender",
    "HtmlPreviewRender",
    "BothPreviewRender",
    # Task types
    "TaskType",
    "GeneratedTaskStatus",
]
