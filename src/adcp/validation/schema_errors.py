"""Convert schema validation failures into thrown errors and the AdCP
``VALIDATION_ERROR`` envelope used by server middleware."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adcp.validation.schema_validator import (
    SchemaValidationError,
    ValidationIssue,
    _issue_to_wire,
)


@dataclass(frozen=True)
class ValidationErrorDetails:
    """Mirror of :attr:`SchemaValidationError.details` as a typed dataclass.

    Retained as a public type so callers can annotate their own
    intermediate structures without depending on the exception class.
    """

    tool: str
    side: str
    issues: list[ValidationIssue]


@dataclass(frozen=True)
class AdcpValidationErrorDetails:
    """Shape of ``adcp_error.details`` inside a server-side
    ``VALIDATION_ERROR`` envelope. Shipped so buyers can index every
    pointer programmatically instead of parsing the free-text message."""

    tool: str
    side: str
    issues: list[ValidationIssue]


def build_validation_error(
    tool: str, side: str, issues: list[ValidationIssue]
) -> SchemaValidationError:
    """Build a :class:`SchemaValidationError` carrying every failure.

    Strict-mode client hooks raise this so callers can inspect the full
    pointer list via ``.issues`` and the ``details`` dict.
    """
    return SchemaValidationError(tool, side, issues)


def build_adcp_validation_error_payload(
    tool: str, side: str, issues: list[ValidationIssue]
) -> dict[str, Any]:
    """Serialize issues into the kwargs expected by the AdCP ``Error`` model.

    Returns a dict with ``code`` / ``message`` / optional ``field`` /
    ``details`` keys — ready to splat into
    ``Error(**build_adcp_validation_error_payload(...))`` or into the
    server's ``adcp_error`` response envelope.

    Messages on every ``ValidationIssue`` are already sanitized (see
    :func:`adcp.validation.schema_validator._safe_message`) — they do
    not echo user-supplied values, so the wire envelope cannot leak
    bearer tokens / PII / prompt-injection strings from the offending
    payload back to the peer.
    """
    first = issues[0] if issues else None
    if first is not None:
        message = f"{tool} {side} failed schema validation at {first.pointer}: {first.message}"
    else:
        message = f"{tool} {side} failed schema validation"

    payload: dict[str, Any] = {
        "code": "VALIDATION_ERROR",
        "message": message,
        "details": {
            "tool": tool,
            "side": side,
            "issues": [_issue_to_wire(i) for i in issues],
        },
    }
    if first is not None and first.pointer:
        payload["field"] = first.pointer
    return payload
