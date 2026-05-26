"""Client-side hooks that run the schema validator around every AdCP tool
call. Pre-send validation blocks malformed requests; post-receive
validation catches field-name drift from agents (issue #249)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, TypedDict

from adcp.validation.schema_errors import build_validation_error
from adcp.validation.schema_validator import (
    ValidationOutcome,
    format_issues,
    validate_request,
    validate_response,
)

logger = logging.getLogger(__name__)

ValidationMode = Literal["strict", "warn", "off"]


class UnknownFieldPolicy(str, Enum):
    """Server-side policy for unknown top-level tool arguments.

    Runs at the transport boundary before Pydantic request-model coercion
    can silently accept or drop extra fields.
    """

    REJECT = "reject"
    STRIP = "strip"
    IGNORE = "ignore"


@dataclass(frozen=True)
class ValidationHookConfig:
    """Per-side client validation modes.

    Defaults match the TS port (adcontextprotocol/adcp-client#694):

    * ``requests``: ``"warn"`` — strict would break callers that
      intentionally send partial payloads (error-path tests, exploratory
      probes). Storyboards and compliance runners that want hard-stop
      enforcement pass ``requests="strict"`` explicitly.
    * ``responses``: ``"strict"`` in dev/test, ``"warn"`` when
      ``ADCP_ENV`` is set to ``production`` / ``prod``. Strict-by-default
      makes the SDK a compliance harness: drift from an agent fails the
      task on the first call, not the Nth storyboard run.

    Resolution order for both sides at call time:

    1. Explicit value on this config (``requests=`` / ``responses=``).
    2. ``ADCP_VALIDATION_MODE`` env var (``strict`` / ``warn`` / ``off``)
       — applies to both sides unless overridden by an explicit value.
       Matches the TS port (adcontextprotocol/adcp-client).
    3. ``ADCP_ENV=prod|production`` flips the response default to
       ``warn``; requests fall back to the type default.
    4. Defaults: ``requests="warn"``, ``responses="strict"``.

    Only ``ADCP_ENV`` and ``ADCP_VALIDATION_MODE`` are consulted —
    generic ``ENV`` / ``ENVIRONMENT`` would collide with unrelated
    tooling (rails, postgres, 12-factor) and silently flip the SDK's
    default.
    """

    requests: ValidationMode | None = None
    responses: ValidationMode | None = None
    #: Server-side policy for unsupported top-level tool arguments.
    #: ``None`` preserves existing permissive behavior.
    unknown_fields: UnknownFieldPolicy | Literal["reject", "strip", "ignore"] | None = None


#: Server-side default — strict on both request and response sides.
#: Used by :func:`adcp.server.serve` and the underlying ``create_*_server``
#: factories when the adopter does not pass ``validation=`` explicitly.
#: Strict-by-default makes the SDK enforce wire conformance: a malformed
#: request fails before the handler runs (``VALIDATION_ERROR``); a
#: spec-divergent response fails after the handler returns. Catches the
#: class of bug that ``extra="allow"`` Pydantic models silently swallow
#: (e.g. the ``pricing_options`` regression). Adopters opt out via
#: ``ValidationHookConfig(responses="warn")`` (warn-only) or
#: ``validation=None`` (off entirely).
SERVER_DEFAULT_VALIDATION: ValidationHookConfig = ValidationHookConfig(
    requests="strict", responses="strict"
)


class DebugLogEntry(TypedDict, total=False):
    """Append-only entry shape for the ``debug_logs`` list threaded by
    the client and server call paths. ``total=False`` so callers can
    still construct partial entries."""

    type: str
    message: str
    timestamp: str
    schema_variant: str
    issues: list[dict[str, Any]]


_VALID_MODES: frozenset[str] = frozenset({"strict", "warn", "off"})


def _env_validation_mode() -> ValidationMode | None:
    """Read ``ADCP_VALIDATION_MODE`` at call time.

    Returns ``None`` when the env var is unset, empty, or holds a value
    that isn't one of the three valid modes. Unrecognized values are
    ignored rather than raising — keeps the SDK robust against typos in
    deploy environments where misreads would silently change validation
    posture (better to fall back to the documented defaults than blow
    up on the next request).
    """
    val = os.environ.get("ADCP_VALIDATION_MODE")
    if not val:
        return None
    normalized = val.strip().lower()
    if normalized in _VALID_MODES:
        return normalized  # type: ignore[return-value]
    return None


def _default_response_mode() -> ValidationMode:
    """Response default: ``strict`` unless ``ADCP_ENV`` declares production.

    Read at call time (not import time) so tests that ``patch.dict`` the
    environment work without a module-level reset hook.
    """
    val = os.environ.get("ADCP_ENV")
    if val and val.lower() in {"prod", "production"}:
        return "warn"
    return "strict"


def resolve_validation_modes(
    config: ValidationHookConfig | None = None,
) -> tuple[ValidationMode, ValidationMode]:
    """Return the effective ``(requests, responses)`` modes.

    Resolution order (per side):

    1. Explicit ``config.requests`` / ``config.responses`` (when set).
    2. ``ADCP_VALIDATION_MODE`` env var — applies to both sides.
    3. ``ADCP_ENV=prod|production`` flips the response default to
       ``warn``; requests fall back to ``warn`` (the type default).
    4. Hard defaults: ``requests="warn"``, ``responses="strict"``.

    Read at call time (not import time) so tests that mutate env vars
    via ``patch.dict`` work without a module-level reset hook.
    """
    explicit_req = config.requests if config is not None else None
    explicit_resp = config.responses if config is not None else None
    env_mode = _env_validation_mode()

    req: ValidationMode = explicit_req or env_mode or "warn"
    resp: ValidationMode = explicit_resp or env_mode or _default_response_mode()
    return req, resp


def _log_warning(
    debug_logs: list[DebugLogEntry] | None,
    tool_name: str,
    side: str,
    outcome: ValidationOutcome,
) -> None:
    # Issue messages are sanitized (see schema_validator._safe_message) so
    # this summary is safe to emit to logs without leaking user values.
    summary = format_issues(outcome.issues)
    logger.warning("Schema validation warning (%s) for %s: %s", side, tool_name, summary)
    if debug_logs is None:
        return
    debug_logs.append(
        DebugLogEntry(
            type="warning",
            message=f"Schema validation warning for {tool_name}: {summary}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            schema_variant=outcome.variant,
            issues=[
                {
                    "pointer": i.pointer,
                    "message": i.message,
                    "keyword": i.keyword,
                    "schema_path": i.schema_path,
                }
                for i in outcome.issues
            ],
        )
    )


def validate_outgoing_request(
    tool_name: str,
    params: Any,
    mode: ValidationMode,
    debug_logs: list[DebugLogEntry] | None = None,
) -> ValidationOutcome | None:
    """Run request validation per the configured mode.

    * ``off`` — no-op (returns ``None``; validator is not consulted).
    * ``warn`` — log + continue; returns the outcome.
    * ``strict`` — raise :class:`SchemaValidationError` on failure.
    """
    if mode == "off":
        return None
    outcome = validate_request(tool_name, params)
    if outcome.valid:
        return outcome
    if mode == "warn":
        _log_warning(debug_logs, tool_name, "request", outcome)
        return outcome
    raise build_validation_error(tool_name, "request", outcome.issues)


def validate_incoming_response(
    tool_name: str,
    data: Any,
    mode: ValidationMode,
    debug_logs: list[DebugLogEntry] | None = None,
) -> ValidationOutcome:
    """Run response validation per the configured mode.

    * ``off`` — no-op (returns a valid skipped outcome).
    * ``warn`` — log + return the invalid outcome so the caller can
      surface details without failing the task.
    * ``strict`` — return the invalid outcome so the caller fails the task.

    Never raises — matches the existing Python response contract where a
    validation failure turns a task into ``status=FAILED`` rather than
    raising out of the adapter.
    """
    if mode == "off":
        return ValidationOutcome(valid=True, issues=[], variant="skipped")
    outcome = validate_response(tool_name, data)
    if not outcome.valid and mode == "warn":
        _log_warning(debug_logs, tool_name, "response", outcome)
    return outcome
