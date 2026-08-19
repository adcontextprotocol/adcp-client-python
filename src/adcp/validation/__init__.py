"""AdCP validation helpers.

Two independent pieces live here:

* **Discriminator / mutual-exclusivity checks** for adagents.json and
  product.json raw-dict payloads (``legacy.py``). These complement Pydantic
  models when parsing third-party JSON that hasn't yet been coerced.

* **Schema-driven validation** against the bundled AdCP JSON schemas
  (``schema_loader``, ``schema_validator``, ``schema_errors``,
  ``client_hooks``). Used pre-send + post-receive on the client, and
  opt-in at the server dispatcher, to catch field-name drift before it
  reaches a storyboard run.
"""

from __future__ import annotations

from adcp.validation.client_hooks import (
    SERVER_DEFAULT_VALIDATION,
    DebugLogEntry,
    UnknownFieldPolicy,
    ValidationHookConfig,
    ValidationMode,
    resolve_validation_modes,
    validate_incoming_response,
    validate_outgoing_request,
)
from adcp.validation.legacy import (
    ValidationError,
    validate_adagents,
    validate_agent_authorization,
    validate_product,
    validate_publisher_properties_item,
    validate_revoked_publisher_domain_entry,
)
from adcp.validation.schema_errors import (
    AdcpValidationErrorDetails,
    ValidationErrorDetails,
    build_adcp_validation_error_payload,
    build_validation_error,
)
from adcp.validation.schema_loader import (
    Direction,
    ResponseVariant,
    get_mcp_schema,
    get_portable_schema,
    get_schema,
    get_validator,
    list_validator_keys,
)
from adcp.validation.schema_validator import (
    SchemaValidationError,
    ValidationIssue,
    ValidationOutcome,
    format_issues,
    validate_request,
    validate_response,
)

__all__ = [
    # Legacy (adagents / product)
    "ValidationError",
    "validate_adagents",
    "validate_agent_authorization",
    "validate_product",
    "validate_publisher_properties_item",
    "validate_revoked_publisher_domain_entry",
    # Schema core
    "Direction",
    "ResponseVariant",
    "SchemaValidationError",
    "ValidationIssue",
    "ValidationOutcome",
    "format_issues",
    "get_mcp_schema",
    "get_portable_schema",
    "get_schema",
    "get_validator",
    "list_validator_keys",
    "validate_request",
    "validate_response",
    # Errors
    "AdcpValidationErrorDetails",
    "ValidationErrorDetails",
    "build_adcp_validation_error_payload",
    "build_validation_error",
    # Client hooks
    "DebugLogEntry",
    "SERVER_DEFAULT_VALIDATION",
    "UnknownFieldPolicy",
    "ValidationHookConfig",
    "ValidationMode",
    "resolve_validation_modes",
    "validate_incoming_response",
    "validate_outgoing_request",
]
