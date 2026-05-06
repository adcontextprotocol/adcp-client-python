"""Core type definitions."""

from __future__ import annotations

from enum import Enum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    mcp_transport: str = (
        "streamable_http"  # "streamable_http" (default, modern) or "sse" (legacy fallback)
    )
    debug: bool = False  # Enable debug mode to capture request/response details
    extra_headers: dict[str, str] = Field(default_factory=dict)
    """Additional HTTP headers sent on every request to this agent.

    This is a **transport-layer escape hatch**, not an AdCP protocol
    extension point — protocol-defined fields belong in the request
    envelope or ``RequestContext.metadata``. Use this for vendor or
    deployment-specific routing headers (e.g. tenant routing on a
    multi-tenant server).

    Reserved: the configured ``auth_header`` (default ``x-adcp-auth``)
    and the standard ``Authorization`` header — set credentials via
    ``auth_token``/``auth_header`` instead. Header names are rejected
    if they contain CR/LF or other control characters.

    Persisted plaintext at ``~/.adcp/config.json`` when saved via the
    CLI — do not store credentials here.
    """

    @field_validator("agent_uri")
    @classmethod
    def validate_agent_uri(cls, v: str) -> str:
        """Validate agent URI format."""
        if not v:
            raise ValueError("agent_uri cannot be empty")

        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"agent_uri must start with http:// or https://, got: {v}\n"
                "Example: https://agent.example.com"
            )

        return v

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: float) -> float:
        """Validate timeout is reasonable."""
        if v <= 0:
            raise ValueError(f"timeout must be positive, got: {v}")

        if v > 300:  # 5 minutes
            raise ValueError(
                f"timeout is very large ({v}s). Consider a value under 300 seconds.\n"
                "Large timeouts can cause long hangs if agent is unresponsive."
            )

        return v

    @field_validator("mcp_transport")
    @classmethod
    def validate_mcp_transport(cls, v: str) -> str:
        """Validate MCP transport type."""
        valid_transports = ["streamable_http", "sse"]
        if v not in valid_transports:
            raise ValueError(
                f"mcp_transport must be one of {valid_transports}, got: {v}\n"
                "Use 'streamable_http' for modern agents (recommended)"
            )
        return v

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, v: str) -> str:
        """Validate auth type."""
        valid_types = ["token", "bearer"]
        if v not in valid_types:
            raise ValueError(
                f"auth_type must be one of {valid_types}, got: {v}\n"
                "Use 'bearer' for OAuth2/standard Authorization header"
            )
        return v

    @model_validator(mode="after")
    def _validate_extra_headers(self) -> AgentConfig:
        if not self.extra_headers:
            return self
        reserved = {self.auth_header.lower(), "authorization"}
        for key, value in self.extra_headers.items():
            if not key:
                raise ValueError("extra_headers contains an empty header name")
            if any(c in key for c in ("\r", "\n", "\x00")) or any(ord(c) < 0x20 for c in key):
                raise ValueError(f"extra_headers key contains control character: {key!r}")
            if any(c in value for c in ("\r", "\n", "\x00")):
                raise ValueError(f"extra_headers value for {key!r} contains CR/LF/NUL")
            if key.lower() in reserved:
                raise ValueError(
                    f"extra_headers may not override reserved auth header "
                    f"{key!r} (collides with auth_header={self.auth_header!r} "
                    f"or 'Authorization'); set credentials via auth_token + "
                    f"auth_header instead"
                )
        return self


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

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: TaskStatus
    data: T | None = None
    message: str | None = None  # Human-readable message from agent (e.g., MCP content text)
    submitted: SubmittedInfo | None = None
    needs_input: NeedsInputInfo | None = None
    error: str | None = None
    success: bool = Field(default=True)
    metadata: dict[str, Any] | None = None
    debug_info: DebugInfo | None = None
    # The full idempotency_key the SDK used for this request — echoed here so
    # buyers can correlate against their own records. SENSITIVE inside the
    # seller's replay_ttl_seconds window (serves as a retry-pattern oracle);
    # do not emit to shared logs. The SDK's debug capture redacts keys by
    # default; avoid ``model_dump_json()``-ing a TaskResult into shared sinks.
    idempotency_key: str | None = None
    # True when the seller returned a cached response for a replayed key.
    # Agents that emit side effects on success (notifications, memory writes,
    # downstream tool calls) must check this flag and suppress duplicates.
    replayed: bool = False


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


class ResolvedBrand(BaseModel):
    """Brand identity resolved from the AdCP registry."""

    model_config = ConfigDict(extra="allow")

    canonical_id: str
    canonical_domain: str
    brand_name: str
    names: list[dict[str, str]] | None = None
    keller_type: str | None = None
    parent_brand: str | None = None
    house_domain: str | None = None
    house_name: str | None = None
    brand_agent_url: str | None = None
    brand: dict[str, Any] | None = None
    source: str


class Member(BaseModel):
    """An organization registered in the AAO member directory."""

    model_config = ConfigDict(extra="allow")

    id: str
    slug: str
    display_name: str
    description: str | None = None
    tagline: str | None = None
    logo_url: str | None = None
    logo_light_url: str | None = None
    logo_dark_url: str | None = None
    contact_email: str | None = None
    contact_website: str | None = None
    offerings: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)
    agents: list[dict[str, Any]] = Field(default_factory=list)
    brands: list[dict[str, Any]] = Field(default_factory=list)
    is_public: bool = True
    is_founding_member: bool = False
    featured: bool = False
    si_enabled: bool = False


class ResolvedProperty(BaseModel):
    """Property information resolved from the AdCP registry."""

    model_config = ConfigDict(extra="allow")

    publisher_domain: str
    source: str
    authorized_agents: list[dict[str, Any]]
    properties: list[dict[str, Any]]
    verified: bool


# ========================================================================
# Policy Registry Types
# ========================================================================


class PolicyExemplar(BaseModel):
    """A pass/fail scenario used to calibrate governance agent interpretation."""

    model_config = ConfigDict(extra="allow")

    scenario: str
    explanation: str


class PolicyExemplars(BaseModel):
    """Collection of pass/fail exemplars for a policy."""

    model_config = ConfigDict(extra="allow")

    pass_: list[PolicyExemplar] = Field(default_factory=list, alias="pass")
    fail: list[PolicyExemplar] = Field(default_factory=list)


class PolicySummary(BaseModel):
    """Summary of a governance policy from the registry."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    policy_id: str
    version: str
    name: str
    description: str | None = None
    category: str
    enforcement: str
    jurisdictions: list[str] = Field(default_factory=list)
    region_aliases: dict[str, list[str]] = Field(default_factory=dict)
    verticals: list[str] = Field(default_factory=list)
    channels: list[str] | None = None
    governance_domains: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    sunset_date: str | None = None
    source_url: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    review_status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class Policy(PolicySummary):
    """Full governance policy including policy text and calibration exemplars."""

    policy: str
    guidance: str | None = None
    exemplars: PolicyExemplars | None = None
    ext: dict[str, Any] | None = None


class PolicyRevision(BaseModel):
    """A single revision in a policy's edit history."""

    model_config = ConfigDict(extra="allow")

    revision_number: int
    editor_name: str
    edit_summary: str
    is_rollback: bool
    rolled_back_to: int | None = None
    created_at: str


class PolicyHistory(BaseModel):
    """Edit history for a policy."""

    model_config = ConfigDict(extra="allow")

    policy_id: str
    total: int
    revisions: list[PolicyRevision] = Field(default_factory=list)
