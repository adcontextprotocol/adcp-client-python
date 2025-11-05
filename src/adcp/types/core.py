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


class TaskResult(BaseModel, Generic[T]):
    """Result from task execution."""

    status: TaskStatus
    data: T | None = None
    submitted: SubmittedInfo | None = None
    needs_input: NeedsInputInfo | None = None
    error: str | None = None
    success: bool = Field(default=True)

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
