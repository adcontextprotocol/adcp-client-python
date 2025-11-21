from __future__ import annotations

"""Type definitions for AdCP client."""

from adcp.types.aliases import (
    CreativeFilters,
    Filters,
    ProductFilters,
    SignalFilters,
)
from adcp.types.core import (
    Activity,
    ActivityType,
    AgentConfig,
    DebugInfo,
    Protocol,
    TaskResult,
    TaskStatus,
    WebhookMetadata,
)

__all__ = [
    # Core types
    "AgentConfig",
    "Protocol",
    "TaskResult",
    "TaskStatus",
    "WebhookMetadata",
    "Activity",
    "ActivityType",
    "DebugInfo",
    # Filter types
    "CreativeFilters",
    "ProductFilters",
    "SignalFilters",
    "Filters",  # Generic alias for backward compatibility
]
