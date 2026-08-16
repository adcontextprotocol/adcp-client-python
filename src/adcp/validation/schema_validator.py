"""Schema-driven validation for AdCP tool requests and responses.

The client uses this pre-send and post-receive; the opt-in server
middleware uses the same core to reject drift at the dispatcher.

Issues carry an RFC 6901 JSON Pointer to the offending field so callers
can index every failure programmatically instead of parsing free text.

Issue messages are sanitized: jsonschema's built-in ``ValidationError.message``
embeds the offending value verbatim (``"'Bearer sk-...' is not of type
'integer'"``). That string flows to the wire envelope and to logs, so we
never let raw payload values escape. The sanitized form keeps the
structural facts (``keyword``, ``expected type``, ``enum size``,
constraint bound) and drops the value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

from adcp._version import ADCP_MAJOR_VERSION, normalize_to_release_precision
from adcp.validation.oneof_hints import compute_oneof_hint
from adcp.validation.schema_loader import Direction, ResponseVariant, get_validator

# Cap the number of issues returned. A hostile peer sending a deeply-
# nested payload can otherwise force jsonschema to walk and sort the
# entire error tree before we format. The cap is well above what a
# human caller would ever debug against.
_MAX_ISSUES = 50

# Cap input size before validation. Protects against a peer posting a
# multi-megabyte object designed to chew CPU in ``iter_errors``. Transport
# layers typically enforce their own limits; this is defense-in-depth.
_MAX_PAYLOAD_NODES = 10_000

_REQUIRED_MSG = re.compile(r"^'(?P<name>.+)' is a required property$")


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation failure.

    Attributes:
        pointer: RFC 6901 JSON Pointer to the offending field.
        message: Sanitized, value-free description of the failure.
            Safe to return over the wire; does not echo input data.
        keyword: jsonschema keyword that rejected the payload
            (``required``, ``type``, ``enum``, etc.).
        schema_path: Path inside the schema that rejected the payload.
        hint: Optional near-miss diagnostic naming the closest matching
            ``oneOf`` variant and the wrong discriminator key. Only
            populated when the heuristic in :mod:`adcp.validation.oneof_hints`
            picks a clear winner; ``None`` otherwise. Additive — clients
            that ignore the field behave as before.
    """

    pointer: str
    message: str
    keyword: str
    schema_path: str
    hint: str | None = None


@dataclass(frozen=True)
class ValidationOutcome:
    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    variant: str = "skipped"


class SchemaValidationError(Exception):
    """Raised by strict-mode client hooks when a payload fails schema.

    Carries the full issue list via :attr:`issues` so callers can inspect
    every JSON Pointer, not just the first. Mirrors the shape of the AdCP
    L3 ``VALIDATION_ERROR`` error envelope.

    Attributes:
        tool: AdCP tool name that was being validated.
        side: ``"request"`` or ``"response"``.
        issues: Every failure, each with a sanitized message.
        code: Always ``"VALIDATION_ERROR"``.
        details: Structured payload mirroring the wire error envelope's
            ``details`` shape — tool/side/issues, ready for programmatic
            inspection by callers that don't want to parse the exception
            message.
    """

    tool: str
    side: str
    issues: list[ValidationIssue]
    code: str
    details: dict[str, Any]

    def __init__(
        self,
        tool: str,
        side: str,
        issues: list[ValidationIssue],
        message: str | None = None,
    ) -> None:
        self.tool = tool
        self.side = side
        self.issues = issues
        self.code = "VALIDATION_ERROR"
        self.details = {
            "tool": tool,
            "side": side,
            "issues": [_issue_to_wire(i) for i in issues],
        }
        if message is None:
            first = issues[0] if issues else None
            if first is not None:
                message = (
                    f"{tool} {side} failed schema validation at "
                    f"{first.pointer}: {first.message}"
                )
            else:
                message = f"{tool} {side} failed schema validation"
        super().__init__(message)


_OK_SKIPPED = ValidationOutcome(valid=True, issues=[], variant="skipped")


def _missing_explicit_schema_outcome(
    tool_name: str,
    direction: Direction,
    version: str | None,
) -> ValidationOutcome | None:
    """Fail closed for native tools when an explicit bundle is unavailable.

    ``skipped`` remains the extension-tool contract: if the SDK has no schema
    for a tool at its own pin, it cannot know whether an adopter-defined tool
    should be validated. A native tool is different. Once its current schema
    proves that the name belongs to the SDK, an explicit version with no
    matching validator must never be reported as valid.
    """

    if version is None:
        return None
    try:
        requested_major = int(normalize_to_release_precision(version).split(".", 1)[0])
    except (TypeError, ValueError):
        return None
    # Cross-major validation is outside this SDK's native schema contract.
    # Preserve the extension/custom-tool ``skipped`` behavior there without
    # probing (and warning about) the SDK-pinned bundle.
    if requested_major != ADCP_MAJOR_VERSION:
        return None
    native_direction: Direction = "request" if direction == "request" else "sync"
    if get_validator(tool_name, native_direction) is None:
        return None
    return ValidationOutcome(
        valid=False,
        issues=[
            ValidationIssue(
                pointer="/",
                message=(
                    f"no bundled {direction} validator is available for the explicitly "
                    f"requested AdCP version {version!r}"
                ),
                keyword="schema_unavailable",
                schema_path="",
            )
        ],
        variant=direction,
    )


def _issue_to_wire(issue: ValidationIssue) -> dict[str, Any]:
    """Serialize a :class:`ValidationIssue` for the wire envelope.

    ``hint`` is only included when populated — clients ignoring the
    field see exactly the pre-hint envelope shape.
    """
    payload: dict[str, Any] = {
        "pointer": issue.pointer,
        "message": issue.message,
        "keyword": issue.keyword,
        "schema_path": issue.schema_path,
    }
    if issue.hint is not None:
        payload["hint"] = issue.hint
    return payload


def _path_to_pointer(path: Any) -> str:
    """Convert a jsonschema ``deque(['packages', 0, 'targeting'])`` to
    ``/packages/0/targeting``. Empty path maps to ``/`` per RFC 6901
    convention used in the TS SDK (AJV's ``instancePath='' -> '/'``)."""
    if not path:
        return "/"

    def escape(seg: Any) -> str:
        s = str(seg)
        return s.replace("~", "~0").replace("/", "~1")

    return "/" + "/".join(escape(seg) for seg in path)


def _safe_message(err: Any, keyword: str) -> str:
    """Value-free description of the failure.

    Never echoes the offending payload value — that would leak bearer
    tokens, PII, or prompt-injection strings through the error envelope.
    Keeps only the structural facts the caller needs to locate the
    offending field and understand why the schema rejected it.
    """
    if keyword == "required":
        # ``err.message`` contains a schema-declared key name, not user
        # data. Pattern: "'X' is a required property". Safe to pass through.
        return str(err.message)
    if keyword == "type":
        expected = err.validator_value
        return f"expected type {expected!r}"
    if keyword == "enum":
        try:
            size = len(err.validator_value)
        except TypeError:
            size = 0
        return f"value not in allowed enum ({size} option{'s' if size != 1 else ''})"
    if keyword == "const":
        return "value does not match the required constant"
    if keyword in {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "multipleOf",
    }:
        return f"{keyword} constraint failed (bound={err.validator_value!r})"
    if keyword == "pattern":
        return "string does not match required pattern"
    if keyword == "format":
        return f"value does not match required format ({err.validator_value!r})"
    if keyword == "additionalProperties":
        return "unexpected property"
    if keyword in {"oneOf", "anyOf", "allOf", "not"}:
        return f"{keyword} composition failed"
    return f"{keyword} constraint failed"


def _missing_required_key(err: Any) -> str | None:
    """Extract the missing property name from a ``required`` failure.

    Prefer the regex-parsed name; fall back to diffing the schema's
    required list against the payload's keys when parsing fails.
    Returns ``None`` if both paths fail — the caller keeps the pointer
    at the parent object.
    """
    match = _REQUIRED_MSG.match(err.message or "")
    if match:
        return match.group("name")
    required = err.validator_value or []
    instance = err.instance if isinstance(err.instance, dict) else None
    if instance is not None:
        for key in required:
            if key not in instance:
                return str(key)
    return None


def _format_error(
    err: Any,
    root_schema: Any | None = None,
    payload: Any = None,
) -> ValidationIssue:
    """Turn a ``jsonschema.exceptions.ValidationError`` into a ``ValidationIssue``.

    When ``root_schema`` and ``payload`` are supplied and the failing
    keyword is ``oneOf``, attaches a near-miss ``hint`` naming the closest
    matching variant and the wrong discriminator key (see
    :mod:`adcp.validation.oneof_hints`).
    """
    pointer = _path_to_pointer(list(err.absolute_path))
    keyword = str(err.validator or "validation")

    if keyword == "required":
        name = _missing_required_key(err)
        if name is not None:
            pointer = pointer.rstrip("/") + "/" + name if pointer != "/" else "/" + name

    schema_path = "#/" + "/".join(str(seg) for seg in err.absolute_schema_path)

    hint: str | None = None
    if keyword == "oneOf" and root_schema is not None:
        hint = compute_oneof_hint(
            root_schema,
            list(err.absolute_schema_path),
            list(err.absolute_path),
            payload,
        )

    return ValidationIssue(
        pointer=pointer,
        message=_safe_message(err, keyword),
        keyword=keyword,
        schema_path=schema_path,
        hint=hint,
    )


def _count_nodes(payload: Any, limit: int) -> int:
    """Count payload nodes up to ``limit``; stops early once the cap is hit.

    Works as a cheap proxy for "how much work will jsonschema do on this
    tree" — good enough to reject obviously-hostile payloads before
    ``iter_errors`` walks them.
    """
    stack: list[Any] = [payload]
    n = 0
    while stack:
        node = stack.pop()
        n += 1
        if n >= limit:
            return n
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return n


def _iter_errors_bounded(validator: Any, payload: Any) -> list[Any]:
    """Collect up to ``_MAX_ISSUES`` errors, sorted by path depth first,
    then lexically — callers see the shallowest failure first."""
    errors = list(islice(validator.iter_errors(payload), _MAX_ISSUES))
    errors.sort(key=lambda e: (len(e.absolute_path), list(e.absolute_path)))
    return errors


def validate_request(
    tool_name: str,
    payload: Any,
    *,
    version: str | None = None,
) -> ValidationOutcome:
    """Validate an outgoing request against ``{tool}-request.json``.

    ``version=None`` validates against the SDK's compile-time-pinned
    schema. Pass an explicit wire version (e.g. ``"2.5"``, ``"3.0.7"``)
    to validate against a non-current bundle — the dispatcher uses this
    when the buyer claims a legacy ``adcp_major_version`` so the request
    is checked against the schema the buyer actually targets.
    """
    validator = get_validator(tool_name, "request", version=version)
    if validator is None:
        missing = _missing_explicit_schema_outcome(tool_name, "request", version)
        if missing is not None:
            return missing
        return _OK_SKIPPED
    if _count_nodes(payload, _MAX_PAYLOAD_NODES) >= _MAX_PAYLOAD_NODES:
        return ValidationOutcome(
            valid=False,
            issues=[
                ValidationIssue(
                    pointer="/",
                    message=f"payload exceeds validator size limit ({_MAX_PAYLOAD_NODES} nodes)",
                    keyword="payload_size",
                    schema_path="",
                )
            ],
            variant="request",
        )
    errors = _iter_errors_bounded(validator, payload)
    if not errors:
        return ValidationOutcome(valid=True, issues=[], variant="request")
    root_schema = getattr(validator, "schema", None)
    return ValidationOutcome(
        valid=False,
        issues=[_format_error(e, root_schema, payload) for e in errors],
        variant="request",
    )


def _select_response_variant(payload: Any) -> ResponseVariant:
    """Pick the response variant by payload shape per AdCP 3.0 async contract.

    Per issue #688: choose by ``status`` field, not just tool name.
    ``submitted`` / ``working`` / ``input-required`` are the three
    async variants; everything else (``completed``, no status, terminal
    errors) routes to the sync schema. A2A adapters pre-extract the
    artifact data before validation, which normally doesn't carry a
    top-level ``status`` — sync-fallback is load-bearing in that path.
    """
    if isinstance(payload, dict):
        status = payload.get("status")
        if status == "submitted":
            return "submitted"
        if status == "working":
            return "working"
        if status == "input-required":
            return "input-required"
    return "sync"


def _normalize_response_for_validation(tool_name: str, payload: Any) -> Any:
    """Apply SDK compatibility defaults before strict beta 3 validation.

    Beta 3 made the protocol envelope ``status`` required on every response.
    ``cache_scope`` is deliberately not inferred here: response-only validation
    lacks the request account context needed to distinguish public wholesale
    feeds from account overlays.
    """
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    normalized.setdefault("status", "completed")
    return normalized


def validate_response(
    tool_name: str,
    payload: Any,
    *,
    version: str | None = None,
) -> ValidationOutcome:
    """Validate an incoming response, selecting the variant by payload shape.

    ``version`` semantics match :func:`validate_request` — defaults to the
    SDK pin; pass a wire version to validate against a legacy schema.
    """
    payload = _normalize_response_for_validation(tool_name, payload)
    variant: ResponseVariant = _select_response_variant(payload)
    validator = get_validator(tool_name, variant, version=version)
    used_variant: Direction = variant
    if validator is None and variant != "sync":
        validator = get_validator(tool_name, "sync", version=version)
        used_variant = "sync"
    if validator is None:
        missing = _missing_explicit_schema_outcome(tool_name, used_variant, version)
        if missing is not None:
            return missing
        return _OK_SKIPPED
    if _count_nodes(payload, _MAX_PAYLOAD_NODES) >= _MAX_PAYLOAD_NODES:
        return ValidationOutcome(
            valid=False,
            issues=[
                ValidationIssue(
                    pointer="/",
                    message=f"payload exceeds validator size limit ({_MAX_PAYLOAD_NODES} nodes)",
                    keyword="payload_size",
                    schema_path="",
                )
            ],
            variant=used_variant,
        )
    errors = _iter_errors_bounded(validator, payload)
    if not errors:
        return ValidationOutcome(valid=True, issues=[], variant=used_variant)
    root_schema = getattr(validator, "schema", None)
    return ValidationOutcome(
        valid=False,
        issues=[_format_error(e, root_schema, payload) for e in errors],
        variant=used_variant,
    )


def format_issues(issues: list[ValidationIssue], limit: int = 3) -> str:
    """Render a compact one-line summary of failures — useful for logs.

    Issues already carry sanitized messages (see :func:`_safe_message`),
    so this output is safe to emit to stdlib loggers and debug buffers.
    """
    head = "; ".join(f"{i.pointer} {i.message}" for i in issues[:limit])
    rest = len(issues) - limit
    return f"{head} (+{rest} more)" if rest > 0 else head
