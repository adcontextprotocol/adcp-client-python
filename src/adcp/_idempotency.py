"""Internal helpers for AdCP idempotency_key handling.

See adcontextprotocol/adcp#2308 and #2315 for the spec contract.
"""

from __future__ import annotations

import logging
import re
import warnings
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from adcp.types.core import TaskResult

IDEMPOTENT_TASKS: frozenset[str] = frozenset(
    {
        "accept_proposal",
        "buy_products",
        "control_media_buy",
        "create_media_buy",
        "decline_proposals",
        "refine_proposals",
        "request_proposals",
        "update_media_buy",
        "sync_creatives",
        "activate_signal",
        "acquire_rights",
        "creative_approval",
        "update_rights",
        "build_creative",
        "calibrate_content",
        "create_content_standards",
        "update_content_standards",
        "create_property_list",
        "update_property_list",
        "delete_property_list",
        "create_collection_list",
        "update_collection_list",
        "delete_collection_list",
        "log_event",
        "provide_performance_feedback",
        "report_usage",
        "report_plan_outcome",
        "report_plan_adjustment",
        "si_initiate_session",
        "sync_accounts",
        "sync_governance",
        "sync_plans",
        "sync_audiences",
        "sync_catalogs",
        "sync_event_sources",
        "sync_agent_notification_configs",
        "sync_principal",
        "sync_reporting_receipts",
        "si_send_message",
    }
)

# Text-mode fallback for MCP servers that emit error codes only in the message
# body (is_error=true with no structuredContent). FastMCP's default behavior.
_TEXT_CODE_PATTERN = re.compile(r"\b(IDEMPOTENCY_CONFLICT|IDEMPOTENCY_EXPIRED)\b")

_KEY_CHARSET = re.compile(r"^[A-Za-z0-9_.:\-]+$")

logger = logging.getLogger(__name__)

# Module-level registry of keys pinned via ``ADCPClient.use_idempotency_key``,
# keyed by the owning client's unique token. A dict (not a ContextVar) because
# ContextVars are copied into each asyncio task at creation; two gather()
# siblings inside a ``with`` block would each see their own copy of the pinned
# key and both consume it, duplicating it across requests. A shared dict with
# ``pop`` semantics gives us single-use-within-scope: the first mutating call
# takes the key, concurrent siblings fall through to fresh-UUID generation.
# Safe under asyncio because ``dict.pop`` is atomic under CPython's GIL.
_scoped_keys: dict[str, str] = {}


def generate_key() -> str:
    """Generate a fresh UUID v4 suitable for use as an idempotency_key."""
    return str(uuid4())


def validate_key(key: str) -> str:
    """Validate a caller-provided key against the spec pattern.

    Raises ``ValueError`` with a specific message for length vs charset
    violations. Returns the key unchanged on success.
    """
    if not isinstance(key, str):
        raise ValueError(f"idempotency_key must be a string, got {type(key).__name__}")
    if len(key) < 16 or len(key) > 255:
        raise ValueError(
            f"idempotency_key length must be 16-255 characters, got {len(key)}. "
            "A UUID v4 (36 chars, e.g. uuid.uuid4()) satisfies this."
        )
    if not _KEY_CHARSET.match(key):
        raise ValueError(
            "idempotency_key contains invalid characters; only "
            "letters, digits, and ._:- are allowed."
        )
    return key


def redact(key: str | None) -> str:
    """Return a short, safe representation of an idempotency_key for logging.

    Keys live inside the seller's replay TTL window and can serve as a
    retry-pattern oracle for anyone watching network traffic. Callers should
    never log the full key; use this helper to emit a prefix only.
    """
    if not key:
        return "<none>"
    if len(key) <= 8:
        return "<short>"
    return f"{key[:8]}..."


def is_mutating(tool_name: str) -> bool:
    """True if the tool is in the idempotency-required set."""
    return tool_name in IDEMPOTENT_TASKS


def redact_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a params dict with the idempotency_key replaced by its prefix.

    Use before emitting a request payload to logs or debug artifacts. Full
    keys are an observable retry-pattern oracle inside the seller's TTL window
    and should never appear in persistent logs.
    """
    if "idempotency_key" not in params:
        return params
    redacted = dict(params)
    redacted["idempotency_key"] = redact(redacted.get("idempotency_key"))
    return redacted


def deep_redact(value: Any) -> Any:
    """Recursively redact any ``idempotency_key`` field in a nested structure.

    The server echoes ``idempotency_key`` in response envelopes so buyers can
    correlate retries, which means the key can surface in debug-captured
    response bodies even after the request-side redaction in
    :func:`redact_params`. This helper walks dicts, lists, and Pydantic
    ``BaseModel`` instances, replacing any ``idempotency_key`` string value
    with its 8-char prefix. Originals are not mutated.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "idempotency_key" and isinstance(v, str):
                out[k] = redact(v)
            else:
                out[k] = deep_redact(v)
        return out
    if isinstance(value, list):
        return [deep_redact(item) for item in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return deep_redact(value.model_dump())
        except Exception:
            # Failing to dump a Pydantic model should not poison a debug path.
            return value
    return value


def inject_key(
    tool_name: str,
    params: dict[str, Any],
    client_token: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Resolve and inject an ``idempotency_key`` into a params dict for a tool call.

    Does not mutate the caller's dict. Returns a new dict (or the original when
    no injection is needed) plus the effective key (or None when the tool is
    read-only and no key was supplied).

    A caller-provided empty-string key is treated as absent (falls through to
    context-scoped or auto-generated resolution) rather than raising a confusing
    ValueError from deep in the adapter stack.

    ``client_token`` identifies the owning client so a key pinned via
    ``client_a.use_idempotency_key(...)`` is not reused by a different client
    executing inside the same ``with`` block — see :func:`resolve_key`.
    """
    existing = params.get("idempotency_key")
    if isinstance(existing, str) and existing == "":
        existing = None
    key = resolve_key(
        tool_name,
        existing if isinstance(existing, str) else None,
        client_token=client_token,
    )
    if key is None:
        return params, None
    if existing == key:
        return params, key
    new_params = dict(params)
    new_params["idempotency_key"] = key
    return new_params, key


_REPLAYED_TRUTHY = frozenset({"true", "1", "yes"})


def extract_replayed(data: Any) -> bool:
    """Pull the envelope-level ``replayed`` flag from a response payload.

    Strictly the spec says this is a boolean. Real servers have been observed
    to stringify booleans or use integer 1; we accept those permissively
    because the alternative (False) risks the caller re-emitting side effects
    on a replay. Unexpected shapes warn so incompliance is visible during rollout.
    """
    if not isinstance(data, dict) or "replayed" not in data:
        return False
    val = data.get("replayed")
    if isinstance(val, bool):
        return val
    if isinstance(val, int):  # includes 0/1; bool handled above
        return val != 0
    if isinstance(val, str):
        logger.warning(
            "Server returned replayed=%r as a string; spec requires a "
            "JSON boolean. Treating as %s.",
            val,
            val.strip().lower() in _REPLAYED_TRUTHY,
        )
        return val.strip().lower() in _REPLAYED_TRUTHY
    logger.warning(
        "Server returned replayed=%r with unexpected type %s; treating as False.",
        val,
        type(val).__name__,
    )
    return False


def collect_error_codes(data: Any) -> list[str]:
    """Return the ``code`` values from a response's ``errors`` array, if present."""
    if not isinstance(data, dict):
        return []
    errors = data.get("errors")
    if not isinstance(errors, list):
        return []
    codes: list[str] = []
    for err in errors:
        code = None
        if isinstance(err, dict):
            code = err.get("code")
        else:
            code = getattr(err, "code", None)
        if isinstance(code, str):
            codes.append(code)
    return codes


def raise_for_idempotency_error(
    tool_name: str,
    data: Any,
    agent_id: str | None,
) -> None:
    """Raise the matching typed exception if the response carries an idempotency error code.

    Inspects the ``errors`` array on the response payload. No-op when no
    matching code is present. Raises ``IdempotencyConflictError`` or
    ``IdempotencyExpiredError`` populated with the full errors list.
    """
    from adcp.exceptions import (
        IDEMPOTENCY_ERROR_CODE_MAP,
        classify_task_error,
    )

    codes = collect_error_codes(data)
    if not any(code in IDEMPOTENCY_ERROR_CODE_MAP for code in codes):
        return
    errors = data.get("errors", []) if isinstance(data, dict) else []
    raise classify_task_error(tool_name, errors, agent_id=agent_id)


def raise_for_idempotency_text(
    tool_name: str,
    message: str | None,
    agent_id: str | None,
) -> None:
    """Raise a typed idempotency error if a text-only message carries the spec code.

    Fallback for MCP servers that return ``is_error=true`` with plain text
    content (e.g. FastMCP's default error shape) instead of a structured
    errors array. Scans for whole-word ``IDEMPOTENCY_CONFLICT`` /
    ``IDEMPOTENCY_EXPIRED`` tokens and raises the matching typed exception.
    No-op when the message is absent or doesn't contain a known code.
    """
    from adcp.exceptions import IDEMPOTENCY_ERROR_CODE_MAP, classify_task_error

    if not message:
        return
    match = _TEXT_CODE_PATTERN.search(message)
    if not match:
        return
    code = match.group(1)
    if code not in IDEMPOTENCY_ERROR_CODE_MAP:
        return
    synthetic_error = {"code": code, "message": message}
    raise classify_task_error(tool_name, [synthetic_error], agent_id=agent_id)


def annotate_result(
    result: TaskResult[Any],
    idempotency_key: str | None,
) -> TaskResult[Any]:
    """Surface the request's idempotency_key and the envelope ``replayed`` flag on a TaskResult.

    Populates first-class ``result.idempotency_key`` and ``result.replayed``
    attributes. Also mirrors into ``result.metadata`` under the legacy keys
    ``idempotency_key`` and ``idempotency_replayed`` for callers that were
    already reading from metadata; the mirror is a transitional convenience
    and may be removed in a future release.
    """
    replayed = extract_replayed(result.data)
    result.replayed = replayed
    if idempotency_key is not None:
        result.idempotency_key = idempotency_key
    if idempotency_key is None and not replayed:
        return result
    metadata = dict(result.metadata or {})
    if idempotency_key is not None:
        metadata["idempotency_key"] = idempotency_key
    metadata["idempotency_replayed"] = replayed
    result.metadata = metadata
    return result


def resolve_key(
    tool_name: str,
    params_key: str | None,
    client_token: str | None = None,
) -> str | None:
    """Resolve the effective idempotency_key for a tool call.

    Order of precedence:
    1. Explicit key already present in the params dict.
    2. Single-use key pinned via ``client.use_idempotency_key(...)`` on the
       SAME client. The scoped key is popped on first consume; concurrent
       gather() siblings falling into this branch get fresh UUIDs instead
       of duplicating the pinned key.
    3. Freshly generated UUID v4 when the tool is mutating.
    4. ``None`` for non-mutating tools — caller should not include the field.

    When a scoped key exists on a DIFFERENT client's slot (cross-client leak
    inside the same ``with`` block), this path falls through to fresh-key
    generation and emits a ``UserWarning`` — keys must be unique per
    (seller, request) pair (AdCP #2315).

    Raises ``ValueError`` when an explicit key fails format validation.
    """
    if params_key is not None:
        return validate_key(params_key)

    if client_token is not None:
        scoped = _scoped_keys.pop(client_token, None)
        if scoped is not None:
            return validate_key(scoped)

    if _scoped_keys:
        # Another client has a pinned key in scope but it's not this one.
        # Almost always a caller mistake — e.g. using client_b inside a
        # ``with client_a.use_idempotency_key(...)`` block.
        warnings.warn(
            "use_idempotency_key was set on a different client; the SDK is "
            "generating a fresh key for this call to prevent cross-seller "
            "correlation. Keys must be unique per (seller, request) pair "
            "(AdCP #2315).",
            UserWarning,
            stacklevel=2,
        )

    if is_mutating(tool_name):
        return generate_key()

    return None
