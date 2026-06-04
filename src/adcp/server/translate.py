"""Error translation and request normalization for proxy and custom-transport servers.

Standard servers using ``serve()`` or ``ADCPAgentExecutor`` do not need these
helpers — the framework handles error translation and request normalization
internally.

These are for **proxy servers** that catch ``ADCPError`` from a downstream
agent call and need to format it for their own transport, or custom
multi-transport servers that bypass the standard framework.

Not exported from ``adcp.server`` — import directly::

    from adcp.server.translate import translate_error, normalize_request

    # In a proxy catching errors from a downstream agent:
    try:
        result = await downstream_client.create_media_buy(params)
    except ADCPError as e:
        raise translate_error(e, protocol="a2a")
        # Raises: InternalError(message="...", data={...})

    # Normalize deprecated field names from older callers:
    params = normalize_request(params, task_name="create_media_buy")
"""

from __future__ import annotations

import json
from typing import Any, Literal, cast
from urllib.parse import urlparse

from a2a.utils.errors import A2AError, InternalError, InvalidParamsError
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from adcp.error_sanitization import sanitize_error_details
from adcp.exceptions import (
    ADCPAuthenticationError,
    ADCPConnectionError,
    ADCPError,
    ADCPTaskError,
    ADCPTimeoutError,
)
from adcp.server.helpers import STANDARD_ERROR_CODES
from adcp.types import Error
from adcp.types.core import Protocol

# ============================================================================
# Error Translation
# ============================================================================

# Maps Python exception types to ADCP standard error codes.
_EXCEPTION_CODE_MAP: dict[type[ADCPError], str] = {
    ADCPAuthenticationError: "AUTH_REQUIRED",
    ADCPTimeoutError: "SERVICE_UNAVAILABLE",
    ADCPConnectionError: "SERVICE_UNAVAILABLE",
}

# A2A JSON-RPC error codes for correctable vs non-correctable errors.
_A2A_CORRECTABLE_CODE = -32602  # InvalidParamsError
_A2A_INTERNAL_CODE = -32603  # InternalError


def _error_code_for_exception(exc: ADCPError) -> str:
    """Derive a structured ADCP error code from an exception type."""
    # ADCPTaskError carries the original error codes from the response
    if isinstance(exc, ADCPTaskError) and exc.error_codes:
        return str(exc.error_codes[0])
    return _EXCEPTION_CODE_MAP.get(type(exc), "INTERNAL_ERROR")


def _recovery_for_code(code: str) -> str:
    """Look up recovery classification for an error code."""
    std = STANDARD_ERROR_CODES.get(code)
    if std:
        return std["recovery"]
    return "terminal"


def _build_error_data(
    code: str,
    message: str,
    *,
    recovery: str | None = None,
    suggestion: str | None = None,
    details: dict[str, Any] | None = None,
    errors: list[Any] | None = None,
) -> dict[str, Any]:
    """Build the structured data payload for protocol error responses."""
    data: dict[str, Any] = {
        "error_code": code,
        "recovery": recovery or _recovery_for_code(code),
    }
    if suggestion:
        data["suggestion"] = suggestion
    if details:
        data["details"] = details
    if errors:
        data["errors"] = [
            e.model_dump(exclude_none=True) if hasattr(e, "model_dump") else e for e in errors
        ]
    return data


def _extract_structured_fields(
    exc: ADCPError | Error | Any,
) -> tuple[str, str, str, str | None, str | None, dict[str, Any] | None, list[Any] | None]:
    """Extract (code, message, recovery, field, suggestion, details, errors).

    Handles three input shapes:
    - ``adcp.types.Error`` (Pydantic model)
    - ``adcp.decisioning.types.AdcpError`` (decisioning-layer exception)
    - ``adcp.exceptions.ADCPError`` (client-side exception, including ADCPTaskError)

    Used by both ``translate_error`` and ``build_mcp_error_result`` so the
    field-extraction logic stays in one place.
    """
    # Lazy import — ``adcp.decisioning.types`` pulls in the decisioning
    # graph, which translate.py shouldn't load at module-import time.
    try:
        from adcp.decisioning.types import AdcpError as DecisioningAdcpError  # noqa: N813
    except Exception:
        decisioning_error_types: tuple[type[BaseException], ...] = ()
    else:
        decisioning_error_types = (DecisioningAdcpError,)

    field: str | None = None
    if isinstance(exc, Error):
        code = exc.code
        message = exc.message
        suggestion = exc.suggestion
        details = exc.details
        # Error.recovery is an Optional Recovery enum; unwrap to a string
        # for downstream wire projection. Falls back to the recovery
        # classification looked up from the code when unset.
        recovery_val = exc.recovery
        if recovery_val is None:
            recovery = _recovery_for_code(code)
        elif hasattr(recovery_val, "value"):
            recovery = recovery_val.value
        else:
            recovery = str(recovery_val)
        errors = None
        field = exc.field
    elif isinstance(exc, decisioning_error_types):
        decisioning_exc = cast(Any, exc)
        code = decisioning_exc.code
        message = decisioning_exc.args[0] if decisioning_exc.args else ""
        suggestion = decisioning_exc.suggestion
        recovery = decisioning_exc.recovery
        details = decisioning_exc.details or None
        errors = None
        field = decisioning_exc.field
    elif isinstance(exc, ADCPError):
        code = _error_code_for_exception(exc)
        message = exc.message
        suggestion = exc.suggestion
        recovery = _recovery_for_code(code)
        details = None
        errors = getattr(exc, "errors", None)
        if errors:
            first = errors[0]
            field = getattr(first, "field", None)
            details = getattr(first, "details", None)
    else:
        raise TypeError(f"Expected ADCPError or Error, got {type(exc).__name__}")

    if details:
        details = sanitize_error_details(code, details)

    return code, message, recovery, field, suggestion, details, errors


def build_mcp_error_result(
    exc: ADCPError | Error | Any,
    *,
    params: dict[str, Any] | None = None,
) -> CallToolResult:
    """Build an MCP ``CallToolResult`` carrying the structured ``adcp_error`` envelope.

    The framework dispatcher returns this when a platform method raises a
    structured AdCP error. The result has ``isError=True`` AND
    ``structuredContent={"adcp_error": {...}}`` on the same envelope —
    matching the spec's transport-errors.mdx §MCP Binding shape that the
    storyboard runner's ``/adcp_error/code`` JSON-pointer assertion
    expects.

    The text fallback in ``content[]`` preserves human-readable display
    for clients that do not consume ``structuredContent`` (LLM tool-use
    surfaces, log viewers).

    Buyer agents read the structured envelope first; the text fallback
    is only consulted when ``structuredContent`` is absent, per the
    spec's structured-error precedence rules.

    When ``params`` is supplied and carries a ``context`` field, that
    field is echoed onto the structuredContent envelope alongside
    ``adcp_error`` — symmetric with the success path's
    :func:`adcp.server.helpers.inject_context` call. Without this echo,
    error responses violate the AdCP context-passthrough contract and
    buyers lose correlation IDs and idempotency hints across the
    raise-AdcpError boundary.
    """
    from adcp.server.helpers import inject_context

    code, message, recovery, field, suggestion, details, _errors = _extract_structured_fields(exc)

    adcp_error: dict[str, Any] = {
        "code": code,
        "message": message,
        "recovery": recovery,
    }
    if field is not None:
        adcp_error["field"] = field
    if suggestion is not None:
        adcp_error["suggestion"] = suggestion
    # ``retry_after`` lives on decisioning AdcpError; project it when present.
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        adcp_error["retry_after"] = retry_after
    if details:
        adcp_error["details"] = dict(details)

    # Text fallback for clients that don't read structuredContent.
    if field:
        text = f"{code}[{field}]: {message}"
    else:
        text = f"{code}: {message}"
    if suggestion:
        text += f"\nSuggestion: {suggestion}"

    structured: dict[str, Any] = {"adcp_error": adcp_error}
    if params is not None:
        inject_context(params, structured)

    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=structured,
        isError=True,
    )


def translate_error(
    exc: ADCPError | Error,
    protocol: Literal["mcp", "a2a"] | Protocol,
) -> ToolError | A2AError:
    """Translate an AdCP error to a protocol SDK error type.

    Returns an error that can be directly raised in a protocol handler::

        try:
            result = await handler.create_media_buy(params)
        except ADCPError as e:
            raise translate_error(e, protocol="mcp")

    For MCP, returns ``ToolError`` (from ``mcp.server.fastmcp``).
    For A2A, returns an :class:`~a2a.utils.errors.A2AError` subclass:
    :class:`~a2a.utils.errors.InvalidParamsError` for correctable errors
    (client can fix) or :class:`~a2a.utils.errors.InternalError` for
    transient/terminal (server-side or unfixable).

    The ``data`` field on A2A errors preserves recovery classification,
    error_code, suggestion, and details so buyer agents can make
    retry/fix/abandon decisions.

    Args:
        exc: An ADCPError exception or an Error Pydantic model.
        protocol: Target protocol - ``"mcp"`` or ``"a2a"``.

    Returns:
        ``ToolError`` for MCP, :class:`~a2a.utils.errors.A2AError`
        subclass for A2A. Raise the result.

    Raises:
        ValueError: If protocol is not ``"mcp"`` or ``"a2a"``.

    Warning:
        Error details are passed through to the caller. Do not include
        internal state (stack traces, SQL queries, internal URLs) in
        Error objects passed to this function.
    """
    proto = protocol.value if isinstance(protocol, Protocol) else str(protocol)
    proto = proto.lower()
    if proto not in ("mcp", "a2a"):
        raise ValueError(f"protocol must be 'mcp' or 'a2a', got {protocol!r}")

    code, message, recovery, field, suggestion, details, errors = _extract_structured_fields(exc)

    if proto == "mcp":
        return _to_mcp(code, message, suggestion=suggestion, field=field, details=details)
    return _to_a2a(
        code,
        message,
        recovery=recovery,
        suggestion=suggestion,
        details=details,
        errors=errors,
    )


def _to_mcp(
    code: str,
    message: str,
    *,
    suggestion: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> ToolError:
    """Format error as a ToolError for MCP servers.

    MCP's ``ToolError`` is a flat text payload — there's no structured
    ``data`` channel equivalent to A2A's. To give MCP clients a
    programmatic handle on the offending field, the field path is
    embedded in the code prefix: ``INVALID_REQUEST[packages[0].budget]:
    …``. Clients can parse the bracketed form with a simple regex
    (``^([A-Z_]+)(?:\\[([^\\]]+)\\])?:``) to recover both the AdCP code
    and the field path — same shape the spec suggests for the JS
    client.

    When ``details`` is non-empty, the JSON-serialised payload is
    appended after a ``\\nDetails: `` line. Buyer agents can split on
    that prefix and ``json.loads`` the rest — the result is ALWAYS
    valid JSON (truncation/serialization failures emit a sentinel
    object, never a bare ``...``). AudioStack Emma P0: pre-fix the
    wire said "see details for cause" but the dispatch's
    ``details.caused_by`` (and #341's ``validation_errors``) never
    reached MCP buyers — only A2A. Now both transports surface the
    structured breadcrumb.

    **For proxy / custom-transport callers only.** The standard
    framework path (``serve()`` / ``ADCPAgentExecutor``) projects the
    structured envelope via :func:`build_mcp_error_result` directly,
    bypassing FastMCP's ``_make_error_result`` (which drops
    ``structuredContent`` for error results). This text-payload
    ``ToolError`` shape exists for adopters running custom MCP servers
    that catch ``ADCPError`` and need a single value to ``raise`` —
    the field-bracket prefix gives clients a programmatic handle even
    on the text-only channel.
    """
    if field:
        text = f"{code}[{field}]: {message}"
    else:
        text = f"{code}: {message}"
    if suggestion:
        text += f"\nSuggestion: {suggestion}"
    if details:
        text += f"\nDetails: {_serialize_details_for_mcp(details)}"
    return ToolError(text)


#: Cap on the JSON-serialised ``details`` payload appended to MCP
#: ToolError text. Generous enough for typical
#: ``caused_by`` + ``validation_errors`` shapes (under 2 KB) and
#: bounded against an adopter who fills ``details`` with raw repr
#: or DB query strings. Not configurable today; if an adopter
#: needs more, an env-var escape hatch is the right next step.
_MCP_DETAILS_MAX_BYTES = 8192


def _serialize_details_for_mcp(details: dict[str, Any]) -> str:
    """Serialise ``details`` to a JSON string suitable for embedding
    in the MCP ToolError text payload.

    The output is ALWAYS valid JSON — even when truncation fires
    or ``json.dumps`` raises. Buyer agents can split on the
    ``\\nDetails: `` prefix and ``json.loads`` the tail without
    branching on serialization quality. Truncation is signalled via
    the ``_truncated`` field on the sentinel object so buyers can
    surface a "details elided; see server logs" UX hint.

    Pre-fix (PR #341 ship): truncation emitted a bare ``...`` suffix
    on the JSON tail, which made the buyer's ``json.loads`` throw
    ``JSONDecodeError`` with no signal that the cause was
    server-side truncation. ad-tech-protocol-expert called this
    out as a follow-up before the wire shape became de-facto
    convention.
    """
    try:
        details_json = json.dumps(details, separators=(",", ":"), default=str)
    except Exception:
        # Non-serializable details (rare — ``default=str`` catches
        # most). Emit a sentinel so the buyer's parse still
        # succeeds. ``str(details)`` may also raise on circular refs
        # — guarded with try/except + a fallback empty partial.
        try:
            partial = str(details)[: _MCP_DETAILS_MAX_BYTES // 2]
        except Exception:
            partial = ""
        return json.dumps(
            {
                "_truncated": True,
                "_reason": "non_serializable",
                "_partial": partial,
            },
            separators=(",", ":"),
        )
    if len(details_json) <= _MCP_DETAILS_MAX_BYTES:
        return details_json
    # Truncation: emit a sentinel object (always valid JSON).
    # ``_partial`` carries as much of the original payload as fits
    # inside the cap; buyers that want the full payload pull it
    # from server logs (still in ``logger.exception`` traces).
    #
    # Iterate to fit: JSON encoding of ``_partial`` adds backslash
    # escaping (each `"` becomes `\\"`, each `\\` becomes `\\\\`),
    # so a naive headroom calc undershoots when the partial contains
    # quotes or backslashes. Halve until the encoded sentinel fits.
    cut = _MCP_DETAILS_MAX_BYTES - 64  # rough headroom for sentinel keys
    while cut > 0:
        encoded = json.dumps(
            {
                "_truncated": True,
                "_reason": "size",
                "_partial": details_json[:cut],
            },
            separators=(",", ":"),
        )
        if len(encoded) <= _MCP_DETAILS_MAX_BYTES:
            return encoded
        cut = max(0, cut - max(64, (len(encoded) - _MCP_DETAILS_MAX_BYTES) * 2))
    # Cut hit zero — emit a no-partial sentinel so the buyer still
    # sees that something was truncated.
    return json.dumps(
        {"_truncated": True, "_reason": "size", "_partial": ""},
        separators=(",", ":"),
    )


def _to_a2a(
    code: str,
    message: str,
    *,
    recovery: str | None = None,
    suggestion: str | None = None,
    details: dict[str, Any] | None = None,
    errors: list[Any] | None = None,
) -> A2AError:
    """Format error as an A2AError subclass for A2A servers.

    The a2a-sdk 1.0 request handler catches :class:`A2AError` subclasses
    and maps them onto JSON-RPC error responses directly — there is no
    ``ServerError`` wrapper anymore.
    """
    data = _build_error_data(
        code,
        message,
        recovery=recovery,
        suggestion=suggestion,
        details=details,
        errors=errors,
    )

    # Use InvalidParamsError for correctable errors (client can fix),
    # InternalError for transient/terminal (server-side or unfixable).
    effective_recovery = recovery or _recovery_for_code(code)
    if effective_recovery == "correctable":
        return InvalidParamsError(message=message, data=data)
    return InternalError(message=message, data=data)


# ============================================================================
# Request Normalization
# ============================================================================

# Global field renames (apply to all task types).
_GLOBAL_RENAMES: dict[str, str] = {
    "promoted_offerings": "catalogs",
}

# Tool-scoped field renames (apply only to specific task types).
_TOOL_RENAMES: dict[str, dict[str, str]] = {
    "create_media_buy": {
        "campaign_ref": "buyer_campaign_ref",
    },
}


def _normalize_account(params: dict[str, Any]) -> None:
    """Reshape account_id string to account object.

    Old format: ``account_id: "123"``
    New format: ``account: {account_id: "123"}``
    """
    if "account_id" not in params:
        return
    if "account" not in params:
        params["account"] = {"account_id": params.pop("account_id")}
    else:
        del params["account_id"]


def _normalize_brand_manifest(params: dict[str, Any]) -> None:
    """Reshape brand_manifest URL string to brand object.

    Old format: ``brand_manifest: "https://example.com/brand.json"``
    New format: ``brand: {domain: "example.com"}``

    Kept as a wire-level shim so 3.x clients can keep talking to 4.x servers.
    The field is removed from the SDK type system; only tool-boundary
    translation accepts the legacy name.
    """
    if "brand_manifest" not in params:
        return
    if "brand" not in params:
        manifest = params.pop("brand_manifest")
        if isinstance(manifest, str):
            parsed = urlparse(manifest)
            params["brand"] = {"domain": parsed.hostname or manifest}
        else:
            # Already an object, just rename the key
            params["brand"] = manifest
    else:
        del params["brand_manifest"]


def _normalize_packages(params: dict[str, Any]) -> None:
    """Normalize package-level fields: scalar-to-array wraps.

    - ``optimization_goal`` (str) → ``optimization_goals`` (list[str])
    - ``catalog`` (str) → ``catalogs`` (list[str])
    """
    packages = params.get("packages")
    if not isinstance(packages, list):
        return
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        # optimization_goal → optimization_goals
        if "optimization_goal" in pkg and "optimization_goals" not in pkg:
            pkg["optimization_goals"] = [pkg.pop("optimization_goal")]
        elif "optimization_goal" in pkg:
            del pkg["optimization_goal"]
        # catalog → catalogs
        if "catalog" in pkg and "catalogs" not in pkg:
            pkg["catalogs"] = [pkg.pop("catalog")]
        elif "catalog" in pkg:
            del pkg["catalog"]


def normalize_request(
    params: dict[str, Any],
    task_name: str | None = None,
) -> dict[str, Any]:
    """Normalize deprecated field names and structures in request params.

    Applies known transforms so servers can accept both old and new field
    formats without duplicating normalization logic in every handler.

    Transforms applied:

    - ``account_id: "123"`` → ``account: {account_id: "123"}`` (structural)
    - ``brand_manifest: "https://..."`` → ``brand: {domain: "..."}`` (URL parse)
    - ``promoted_offerings`` → ``catalogs`` (rename)
    - ``campaign_ref`` → ``buyer_campaign_ref`` (create_media_buy only)
    - Package-level ``optimization_goal`` → ``optimization_goals`` (scalar→array)
    - Package-level ``catalog`` → ``catalogs`` (scalar→array)

    If both the deprecated and current field name are present, the current
    name takes precedence and the deprecated name is removed.

    Args:
        params: Request parameters dict.
        task_name: ADCP task/tool name (e.g. ``"create_media_buy"``).
            Enables tool-scoped renames when provided.

    Returns:
        New dict with deprecated field names replaced by current names.
        Original dict is not mutated (top-level copy; packages list is
        copied if package-level transforms apply).
    """
    result = dict(params)

    # Structural transforms
    _normalize_account(result)
    _normalize_brand_manifest(result)

    # Package-level transforms (deep copy the packages list)
    if "packages" in result and isinstance(result["packages"], list):
        result["packages"] = [
            dict(pkg) if isinstance(pkg, dict) else pkg for pkg in result["packages"]
        ]
        _normalize_packages(result)

    # Global renames
    for old_name, new_name in _GLOBAL_RENAMES.items():
        if old_name in result:
            if new_name not in result:
                result[new_name] = result.pop(old_name)
            else:
                del result[old_name]

    # Tool-scoped renames
    if task_name:
        tool_renames = _TOOL_RENAMES.get(task_name, {})
        for old_name, new_name in tool_renames.items():
            if old_name in result:
                if new_name not in result:
                    result[new_name] = result.pop(old_name)
                else:
                    del result[old_name]

    return result
