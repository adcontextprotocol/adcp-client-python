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
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus, WebhookMetadata
from adcp.types.generated import (
    # Request/Response types
    ActivateSignalRequest,
    ActivateSignalResponse,
    ActivateSignalSuccess,
    ActivateSignalError,
    BuildCreativeRequest,
    BuildCreativeResponse,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    CreateMediaBuySuccess,
    CreateMediaBuyError,
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
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    SyncCreativesRequest,
    SyncCreativesResponse,
    SyncCreativesSuccess,
    SyncCreativesError,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
    UpdateMediaBuySuccess,
    UpdateMediaBuyError,
    # Core domain types
    MediaBuy,
    Product,
    Package,
    Error,
    # Creative types
    CreativeAsset,
    CreativeManifest,
    CreativeAssignment,
    CreativePolicy,
    Format,
    FormatId,
    # Property and placement types
    Property,
    Placement,
    # Targeting types
    Targeting,
    FrequencyCap,
    Pacing,
    # Brand types
    BrandManifest,
    BrandManifestRef,
    # Metrics types
    DeliveryMetrics,
    Measurement,
    PerformanceFeedback,
    # Status enums
    MediaBuyStatus,
    PackageStatus,
    # Pricing types
    PricingOption,
    PricingModel,
    # Delivery types
    DeliveryType,
    StartTiming,
    # Channel types
    Channels,
    StandardFormatIds,
    # Protocol types
    WebhookPayload,
    ProtocolEnvelope,
    Response,
    PromotedProducts,
    # Deployment types
    Destination,
    Deployment,
    PlatformDestination,
    AgentDestination,
    PlatformDeployment,
    AgentDeployment,
    # Sub-asset types
    SubAsset,
    # Task types
    TaskType,
    TaskStatus as GeneratedTaskStatus,
)

__version__ = "2.0.0"

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
    # Deployment types
    "Destination",
    "Deployment",
    "PlatformDestination",
    "AgentDestination",
    "PlatformDeployment",
    "AgentDeployment",
    # Sub-asset types
    "SubAsset",
    # Task types
    "TaskType",
    "GeneratedTaskStatus",
]
