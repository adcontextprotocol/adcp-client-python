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

from pydantic import BaseModel

from adcp._version import ADCP_MAJOR_VERSION, normalize_to_release_precision
from adcp.validation._response_envelope import uses_task_status_envelope
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


class SchemaRuleError(ValueError):
    """A conditional invariant the generated model cannot express, refused with
    the vocabulary term that names the rule and a pointer to the offending field.

    ``core/error.json`` (AdCP 3.2.1) makes ``keyword`` required on every
    ``issues[]`` row and describes it as "Schema keyword that rejected the
    payload, drawn from the JSON Schema vocabulary ... Implementers SHOULD use
    the validator's native keyword name; do not invent custom values here". Its
    stated purpose is that an agent can "pattern-match on rejection class without
    parsing message text". A hand-written refusal therefore carries its own term,
    because one constant over a heterogeneous set of rules conveys no class at
    all — it says only that something refused, which ``code`` already says.

    ``pointer`` is carried for the same reason: an agent that cannot locate the
    rejection is back to parsing the message.

    A SITE THAT ENFORCES TWO RULES CARRIES TWO ISSUES. ``issues`` is an array, so
    a refusal that is both a missing-required and a forbidden-present keeps the
    class of each half instead of collapsing to whichever term fits worse.

    ``str()`` joins the messages, so a caller that only logs the exception still
    reads the same text it did before.
    """

    def __init__(self, *issues: ValidationIssue) -> None:
        if not issues:
            raise TypeError("a SchemaRuleError names at least one violated rule")
        self.issues: tuple[ValidationIssue, ...] = issues
        super().__init__("; ".join(issue.message for issue in issues))


def rule(keyword: str, pointer: str, message: str) -> ValidationIssue:
    """One violated rule, for :class:`SchemaRuleError`.

    ``schema_path`` is empty: these rules live in the schema's ``if``/``then``
    and negative intersections, which the generated model does not retain a path
    into, and an invented path would be worse than an absent one.
    """
    return ValidationIssue(pointer=pointer, message=message, keyword=keyword, schema_path="")


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
        code: ``"VERSION_UNSUPPORTED"`` when no explicit-version schema
            exists; otherwise ``"VALIDATION_ERROR"``.
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
        if any(issue.keyword == "schema_unavailable" for issue in issues):
            from adcp.validation.envelope import SUPPORTED_WIRE_VERSIONS

            self.code = "VERSION_UNSUPPORTED"
            self.details["supported_versions"] = list(SUPPORTED_WIRE_VERSIONS)
        if message is None:
            first = issues[0] if issues else None
            if first is not None:
                message = (
                    f"{tool} {side} failed schema validation at {first.pointer}: {first.message}"
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
                pointer="/adcp_version",
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


#: A segment JSONPath-lite can write after a ``.`` unambiguously. Schema-declared
#: AdCP names are all of this shape; a segment that is not comes from an open map
#: in the payload (``ext``, ``details``, a targeting key) where a peer chose it.
_BARE_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INDEX_SEGMENT = re.compile(r"^(?:0|[1-9][0-9]*)$")
_JSONPATH_TOKEN = re.compile(
    r"""
      \[ (?P<index>\d+) \]                      # [0]
    | \[ ' (?P<quoted>(?:[^'\\]|\\.)*) ' \]     # ['a.b']
    | \.? (?P<bare>[A-Za-z_][A-Za-z0-9_]*)      # name  or  .name
    """,
    re.VERBOSE,
)


def pointer_to_field(pointer: str) -> str:
    """An RFC 6901 pointer as the JSONPath-lite ``error.field`` uses.

    ``/packages/0/targeting`` -> ``packages[0].targeting``. Required by
    ``core/error.json``: when ``issues[]`` is present, ``field`` is
    ``issues[0].pointer`` translated.

    The target format is the lossy one, and the pin says so where it defines
    ``issues[].pointer`` — RFC 6901 is "standardized and unambiguous on keys
    containing ``/`` or ``~``", "differs from the legacy top-level ``field``
    which uses JSONPath-lite", and sellers "MUST translate the format"
    regardless. So dot notation cannot express three pointer shapes:

    * a segment that is not a bare identifier is bracket-quoted
      (``/a.b`` -> ``['a.b']``), which covers keys containing ``.`` and the
      ``~1``/``~0`` escapes for ``/`` and ``~``. Standard JSONPath member
      access, beyond the examples the spec gives;
    * an all-digit segment becomes an index (``[0]``), so a digit-string
      object key is indistinguishable from one — the one lossy case;
    * root (``"/"``, this repo's spelling for the empty path) becomes ``""``.
    """
    if pointer in ("", "/"):
        return ""
    out: list[str] = []
    for raw in pointer.lstrip("/").split("/"):
        segment = raw.replace("~1", "/").replace("~0", "~")
        if _INDEX_SEGMENT.match(segment):
            out.append(f"[{segment}]")
        elif _BARE_SEGMENT.match(segment):
            out.append(segment if not out else f".{segment}")
        else:
            escaped = segment.replace("\\", "\\\\").replace("'", "\\'")
            out.append(f"['{escaped}']")
    return "".join(out)


def field_to_pointer(field: str) -> str:
    """The RFC 6901 pointer a JSONPath-lite ``error.field`` came from.

    The inverse of :func:`pointer_to_field`; the two are asserted inverses in
    ``tests/test_error_field_jsonpath_lite.py``, which is what covers the
    escape handling in both directions.
    """
    if not field:
        return "/"
    segments: list[str] = []
    position = 0
    while position < len(field):
        match = _JSONPATH_TOKEN.match(field, position)
        if match is None:
            raise ValueError(f"not JSONPath-lite at offset {position}: {field!r}")
        position = match.end()
        if (index := match.group("index")) is not None:
            segments.append(index)
        elif (quoted := match.group("quoted")) is not None:
            segments.append(re.sub(r"\\(.)", r"\1", quoted))
        else:
            segments.append(match.group("bare"))
    return _path_to_pointer(segments)


_BOUNDED_KEYWORDS = frozenset(
    {
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
    }
)


#: The four JSON Schema keywords whose failure is a composition rather than a
#: single constraint. Named once: ``safe_message_for`` renders them together and
#: both lanes ask the same question of a keyword before deriving field names.
_COMPOSITION_KEYWORDS = frozenset({"oneOf", "anyOf", "allOf", "not"})


class _Unset:
    """Sentinel: a fact the caller's error shape does not carry.

    Distinct from ``None``, which a schema can legitimately supply as a
    bound — so the two render differently.
    """


UNSET = _Unset()


def _render_fields(fields: Any) -> str:
    """The field names a refusal cites, as one phrase.

    Accepts the comma-joined string the compiled validators put in their error
    context and any iterable of names, so each lane hands over whatever shape it
    already has rather than converting at the call site.
    """
    if isinstance(fields, str):
        names = [part.strip() for part in fields.split(",")]
    else:
        names = [str(name).strip() for name in fields]
    return ", ".join(sorted({name for name in names if name}))


def safe_message_for(
    keyword: str,
    *,
    required_message: str | None = None,
    bound: Any = UNSET,
    enum_size: int | None = None,
    format_name: Any = UNSET,
    fields: Any = UNSET,
) -> str:
    """Value-free wording for a rejection, given only structural facts.

    The one owner of how a refusal is PHRASED to a peer. Takes facts
    rather than a validator's error object so both projections share it:
    :func:`_safe_message` extracts them from a ``jsonschema`` error, and
    :mod:`adcp.validation.pydantic_errors` extracts them from a pydantic
    one. Neither passes a payload value in, so neither can leak one out.
    """
    if keyword == "required":
        # A schema-declared key name, not user data. Pattern:
        # "'X' is a required property".
        return required_message or "required constraint failed"
    if keyword == "type":
        if bound is UNSET:
            return "value is the wrong type"
        return f"expected type {bound!r}"
    if keyword == "enum":
        if enum_size is None:
            return "value not in allowed enum"
        return f"value not in allowed enum ({enum_size} option{'s' if enum_size != 1 else ''})"
    if keyword == "const":
        return "value does not match the required constant"
    if keyword in _BOUNDED_KEYWORDS:
        if bound is UNSET:
            return f"{keyword} constraint failed"
        return f"{keyword} constraint failed (bound={bound!r})"
    if keyword == "pattern":
        return "string does not match required pattern"
    if keyword == "format":
        if format_name is UNSET:
            return "value does not match required format"
        return f"value does not match required format ({format_name!r})"
    if keyword == "additionalProperties":
        return "unexpected property"
    if keyword in _COMPOSITION_KEYWORDS:
        # The names a composition constrains are declared by the SCHEMA, so
        # citing them is the same move as passing a ``required`` failure's key
        # name through above — and it is what turns "allOf composition failed"
        # from a rule identifier into something a buyer can act on. Nothing from
        # the instance reaches here: each lane derives the names from a schema
        # node or from the context the generator composed out of one.
        if rendered := _render_fields(fields) if fields is not UNSET else "":
            return f"{keyword} composition failed (fields: {rendered})"
        return f"{keyword} composition failed"
    return f"{keyword} constraint failed"


def _declared_field_names(node: Any) -> tuple[str, ...]:
    """Every property name a schema node declares, sorted.

    Schema-derived and instance-free by construction: the only thing read is
    the node, so a value the peer sent cannot reach the result. Mirrors
    ``scripts/compile_conditions.named_fields``, which does this over the
    generator's own condition tree -- "``agent-notification-config.json``'s
    conditional reads ``event_types`` and ``all_authorized_accounts``, and
    naming them is the difference between a diagnostic and a citation".

    Recurses through the composition and conditional keywords, because that is
    where the names live for the failure this serves: an ``allOf`` node's own
    ``properties`` is usually empty and its arms carry everything.
    """
    found: set[str] = set()
    seen: set[int] = set()

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            if id(current) in seen:
                return
            seen.add(id(current))
            properties = current.get("properties")
            if isinstance(properties, dict):
                found.update(str(name) for name in properties)
            required = current.get("required")
            if isinstance(required, list):
                found.update(str(name) for name in required)
            for keyword in ("allOf", "anyOf", "oneOf", "not", "if", "then", "else", "items"):
                walk(current.get(keyword))
            dependencies = current.get("dependencies") or current.get("dependentRequired")
            if isinstance(dependencies, dict):
                found.update(str(name) for name in dependencies)
        elif isinstance(current, list):
            if id(current) in seen:
                return
            seen.add(id(current))
            for item in current:
                walk(item)

    walk(node)
    return tuple(sorted(found))


def _safe_message(err: Any, keyword: str) -> str:
    """Value-free description of a ``jsonschema`` failure.

    Never echoes the offending payload value — that would leak bearer
    tokens, PII, or prompt-injection strings through the error envelope.
    Keeps only the structural facts the caller needs to locate the
    offending field and understand why the schema rejected it.
    """
    enum_size: int | None = None
    if keyword == "enum":
        try:
            enum_size = len(err.validator_value)
        except TypeError:
            enum_size = 0
    # A composition's names come off the SCHEMA, never the instance, so the
    # oracle cites the same thing the compiled lane's context carries.
    #
    # The ARMS, not the enclosing node. ``err.schema`` for a root ``anyOf`` is
    # the whole request schema, so walking it names all 31 of its properties --
    # noise, and the inverse of a citation. ``err.validator_value`` is the
    # composition's own operand list, which is what the generator's
    # ``named_fields`` walks too: for the create-request root it yields the six
    # names that distinguish the arms.
    fields: Any = UNSET
    if keyword in _COMPOSITION_KEYWORDS:
        fields = _declared_field_names(getattr(err, "validator_value", None)) or UNSET
    return safe_message_for(
        keyword,
        required_message=str(err.message) if keyword == "required" else None,
        bound=err.validator_value,
        enum_size=enum_size,
        format_name=err.validator_value,
        fields=fields,
    )


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


def _as_wire_payload(payload: Any) -> Any:
    """Serialize a Pydantic application model exactly as SDK calls do."""
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json", by_alias=True, exclude_none=True)
    return payload


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
    payload = _as_wire_payload(payload)
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

    Most beta 3 responses require the protocol ``status`` envelope. Compact
    ``list_products`` and synchronous ``decline_proposals`` retain their native
    envelopes instead. ``cache_scope`` is deliberately not inferred here:
    response-only validation lacks the request account context needed to
    distinguish public wholesale feeds from account overlays.
    """
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    if uses_task_status_envelope(tool_name):
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
    payload = _normalize_response_for_validation(tool_name, _as_wire_payload(payload))
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
