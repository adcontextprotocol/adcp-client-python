from __future__ import annotations

"""Core type definitions."""

from enum import Enum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field


class Protocol(str, Enum):
    """Supported protocols."""

    A2A = "a2a"
    MCP = "mcp"


class AgentConfig(BaseModel):
    """Agent configuration."""

    id: str
    agent_uri: str
    protocol: Protocol
    auth_token: str | None = None
    requires_auth: bool = False
    auth_header: str = "x-adcp-auth"  # Header name for authentication
    auth_type: str = "token"  # "token" for direct value, "bearer" for "Bearer {token}"
    timeout: float = 30.0  # Request timeout in seconds
    mcp_transport: str = "streamable_http"  # "streamable_http" (default, modern) or "sse" (legacy fallback)
    debug: bool = False  # Enable debug mode to capture request/response details


class TaskStatus(str, Enum):
    """Task execution status."""

    COMPLETED = "completed"
    SUBMITTED = "submitted"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"
    WORKING = "working"


T = TypeVar("T")


class SubmittedInfo(BaseModel):
    """Information about submitted async task."""

    webhook_url: str
    operation_id: str


class NeedsInputInfo(BaseModel):
    """Information when agent needs clarification."""

    message: str
    field: str | None = None


class DebugInfo(BaseModel):
    """Debug information for troubleshooting."""

    request: dict[str, Any]
    response: dict[str, Any]
    duration_ms: float | None = None


class TaskResult(BaseModel, Generic[T]):
    """Result from task execution."""

    status: TaskStatus
    data: T | None = None
    submitted: SubmittedInfo | None = None
    needs_input: NeedsInputInfo | None = None
    error: str | None = None
    success: bool = Field(default=True)
    metadata: dict[str, Any] | None = None
    debug_info: DebugInfo | None = None

    class Config:
        arbitrary_types_allowed = True


class ActivityType(str, Enum):
    """Types of activity events."""

    PROTOCOL_REQUEST = "protocol_request"
    PROTOCOL_RESPONSE = "protocol_response"
    WEBHOOK_RECEIVED = "webhook_received"
    HANDLER_CALLED = "handler_called"
    STATUS_CHANGE = "status_change"


class Activity(BaseModel):
    """Activity event for observability."""

    model_config = {"frozen": True}

    type: ActivityType
    operation_id: str
    agent_id: str
    task_type: str
    status: TaskStatus | None = None
    timestamp: str
    metadata: dict[str, Any] | None = None


class WebhookMetadata(BaseModel):
    """Metadata passed to webhook handlers."""

    operation_id: str
    agent_id: str
    task_type: str
    status: TaskStatus
    sequence_number: int | None = None
    notification_type: Literal["scheduled", "final", "delayed"] | None = None
    timestamp: str
