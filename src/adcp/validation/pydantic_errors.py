"""Project a pydantic ``ValidationError`` onto the AdCP ``issues[]`` shape.

``str(ValidationError)`` renders pydantic's full diagnostic, which carries
``input_value=`` — the offending payload value — and ``type=``, pydantic's
internal error-type name. Neither belongs in a protocol error: the value is
the peer's own data reflected back through a wire envelope that logs and
intermediaries see, and the type name is a producer implementation detail
that no AdCP consumer can act on.

What a peer IS owed is in ``core/error.json`` (AdCP 3.2) under ``issues[]``:
the RFC 6901 ``pointer`` to the offending field, a ``message`` saying why it
was rejected, and a ``keyword`` "drawn from the JSON Schema vocabulary …
Implementers SHOULD use the validator's native keyword name; do not invent
custom values here." Pydantic is not a JSON Schema validator, so its native
names (``model_type``, ``missing``) are not in that vocabulary and are mapped
here to the keyword the equivalent JSON Schema rule would have reported.

The result feeds :func:`adcp.validation.schema_errors.build_adcp_validation_error_payload`
— the same builder the jsonschema oracle path uses — so both routes to the
``VALIDATION_ERROR`` envelope produce one shape.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft7Validator
from pydantic import ValidationError

from adcp.validation.schema_validator import (
    _COMPOSITION_KEYWORDS,
    UNSET,
    ValidationIssue,
    _path_to_pointer,
    safe_message_for,
)

#: The keyword vocabulary core/error.json points ``issues[].keyword`` at, taken
#: from the validator that implements it rather than restated here — a hand-kept
#: copy is a second declaration of someone else's list.
_JSON_SCHEMA_KEYWORDS = frozenset(Draft7Validator.VALIDATORS)

# Pydantic error ``type`` -> the JSON Schema keyword the same rejection would
# carry. Only the structural rejections appear here; see ``_keyword`` for the
# families handled by suffix and for the deliberate fallback.
_KEYWORD_BY_TYPE: dict[str, str] = {
    # Presence
    "missing": "required",
    "missing_argument": "required",
    "missing_keyword_only_argument": "required",
    "missing_positional_only_argument": "required",
    "extra_forbidden": "additionalProperties",
    # Closed sets
    "literal_error": "enum",
    "enum": "enum",
    # Strings
    "string_pattern_mismatch": "pattern",
    "string_too_short": "minLength",
    "string_too_long": "maxLength",
    # Collections
    "too_short": "minItems",
    "too_long": "maxItems",
    # Numbers
    "greater_than": "exclusiveMinimum",
    "greater_than_equal": "minimum",
    "less_than": "exclusiveMaximum",
    "less_than_equal": "maximum",
    "multiple_of": "multipleOf",
    # Discriminated unions
    "union_tag_invalid": "oneOf",
    "union_tag_not_found": "oneOf",
}

# Pydantic ``type`` -> the JSON type name a schema would have declared. Drives
# the ``expected type 'x'`` wording without naming the pydantic model class
# that pydantic's own message would have named.
_JSON_TYPE_BY_TYPE: dict[str, str] = {
    "model_type": "object",
    "model_attributes_type": "object",
    "dataclass_type": "object",
    "dict_type": "object",
    "list_type": "array",
    "tuple_type": "array",
    "set_type": "array",
    "frozen_set_type": "array",
    "string_type": "string",
    "int_type": "integer",
    "int_parsing": "integer",
    "int_parsing_size": "integer",
    "int_from_float": "integer",
    "float_type": "number",
    "float_parsing": "number",
    "decimal_type": "number",
    "decimal_parsing": "number",
    "bool_type": "boolean",
    "bool_parsing": "boolean",
    "none_required": "null",
}

# Pydantic ``type`` values whose rejection is a string FORMAT failure.
_FORMAT_TYPES = frozenset(
    {
        "url_parsing",
        "url_syntax_violation",
        "url_type",
        "url_too_long",
        "url_scheme",
        "uuid_parsing",
        "uuid_type",
        "uuid_version",
        "date_parsing",
        "date_type",
        "date_from_datetime_parsing",
        "datetime_parsing",
        "datetime_type",
        "datetime_from_date_parsing",
        "time_parsing",
        "time_type",
        "timedelta_parsing",
        "timedelta_type",
    }
)

# The ctx key each bounded keyword stores its SCHEMA-SIDE bound under. Only
# these are read: ``ctx`` also carries ``actual_length``, which is a fact
# about the peer's payload and so is never surfaced.
_BOUND_CTX_KEY: dict[str, str] = {
    "minLength": "min_length",
    "maxLength": "max_length",
    "minItems": "min_length",
    "maxItems": "max_length",
    "exclusiveMinimum": "gt",
    "minimum": "ge",
    "exclusiveMaximum": "lt",
    "maximum": "le",
    "multipleOf": "multiple_of",
}

# Pydantic reports a rule raised by a model's own validator as ``value_error``
# (or ``assertion_error``), and the message is ``str()`` of whatever that
# validator raised. Those texts interpolate caller values in this SDK --
# ``types/core.py:96`` raises ``f"timeout must be positive, got: {v}"`` -- so
# the message cannot be passed through. CEILING: the peer learns the pointer
# and that a declared rule rejected the field, but not WHICH rule. Lifting it
# means giving those validators structured errors (a keyword plus value-free
# facts) instead of an interpolated sentence; until then this is the only
# treatment that is provably value-free. Tracked as salesagent-8qela.19.
_OPAQUE_TYPES = frozenset({"value_error", "assertion_error"})


class _Unresolved:
    """Sentinel: no document supplied, so ``loc`` is used as given."""


_UNRESOLVED = _Unresolved()


def _keyword(error_type: str) -> str:
    """The JSON Schema keyword standing in for a pydantic error type.

    A type that is ALREADY a keyword passes through. The generated models
    raise ``PydanticCustomError('allOf', ...)`` for every schema conditional
    — 592 sites across ``allOf``, ``anyOf`` and ``oneOf`` — so mapping those
    to a fallback discards the one thing ``issues[].keyword`` is for.
    """
    if error_type in _JSON_SCHEMA_KEYWORDS:
        return error_type
    if error_type in _KEYWORD_BY_TYPE:
        return _KEYWORD_BY_TYPE[error_type]
    if error_type in _JSON_TYPE_BY_TYPE:
        return "type"
    if error_type in _FORMAT_TYPES:
        return "format"
    # ``_format_error`` in schema_validator uses this same spelling when a
    # jsonschema error carries no keyword. Keeping one word for "no keyword
    # applies" means a consumer matching on it sees both lanes alike.
    return "validation"


def _message(entry: dict[str, Any], keyword: str) -> str:
    """Value-free wording for one pydantic error entry."""
    error_type = str(entry.get("type", ""))
    if error_type in _OPAQUE_TYPES:
        return "a declared constraint on this field rejected the value"
    if keyword == "required":
        name = entry["loc"][-1] if entry.get("loc") else None
        if name is None:
            return safe_message_for(keyword)
        return safe_message_for(keyword, required_message=f"{str(name)!r} is a required property")
    if keyword == "type":
        return safe_message_for(keyword, bound=_JSON_TYPE_BY_TYPE[error_type])
    ctx = entry.get("ctx") or {}
    if keyword in _COMPOSITION_KEYWORDS:
        # The compiled validators compose the field names from the schema node
        # and put them in the context -- ``name_the_combinator`` carries the
        # branch groups' fields, ``absent`` the forbidden names. Dropping the
        # context left the buyer "anyOf composition failed", which names the
        # rule and not the place. A conjunct whose context is EMPTY renders as
        # before: the generator not supplying them is a separate defect
        # (salesagent-8qela.49) and inventing names here would be worse.
        return safe_message_for(keyword, fields=ctx.get("fields", UNSET))
    if keyword in _BOUND_CTX_KEY:
        ctx_key = _BOUND_CTX_KEY[keyword]
        if ctx_key in ctx:
            return safe_message_for(keyword, bound=ctx[ctx_key])
        return safe_message_for(keyword)
    # ``literal_error`` ctx carries ``expected`` as a rendered sentence
    # ("'p' or 'q'"), not a list, so there is no reliable option count to
    # report; ``safe_message_for`` drops the count rather than guess one.
    return safe_message_for(keyword)


def _document_path(loc: tuple[Any, ...], payload: Any) -> list[Any]:
    """The elements of ``loc`` that address ``payload``.

    pydantic's ``loc`` is not a document path. It carries synthetic elements
    that name the validator or the selected class — ``function-after[...]`` for
    a wrapping validator, and the branch's class name for any union, which
    pydantic emits with no wrapper involved. Those address nothing in the
    submitted document, so an RFC 6901 pointer built from them does not
    resolve against what the peer sent.

    Resolution rather than pattern-matching is deliberate: a union branch name
    is a plain string indistinguishable from a key, so no list of spellings can
    be complete. An element is kept when the document actually has it. The last
    element is kept regardless, since a ``missing`` failure names a key that is
    absent by definition.
    """
    kept: list[Any] = []
    node = payload
    for index, element in enumerate(loc):
        is_last = index == len(loc) - 1
        if isinstance(node, dict) and element in node:
            kept.append(element)
            node = node[element]
        elif (
            isinstance(node, list)
            and isinstance(element, int)
            and -len(node) <= element < len(node)
        ):
            kept.append(element)
            node = node[element]
        elif is_last and isinstance(element, str):
            # A key the document does not have — the whole point of `missing`.
            kept.append(element)
    return kept


def issues_from_validation_error(
    error: ValidationError, payload: Any = _UNRESOLVED
) -> list[ValidationIssue]:
    """Every refusal in ``error``, as value-free :class:`ValidationIssue` rows.

    The ``pointer`` is RFC 6901 over pydantic's ``loc``; the ``keyword`` is
    the JSON Schema keyword standing in for pydantic's internal error type;
    the ``message`` is built from schema-side facts only. No field carries a
    payload value or a pydantic type name.

    Pass ``payload`` — the document that was validated — to resolve ``loc``
    against it, which drops the synthetic elements pydantic adds for a union
    branch or a wrapping validator. Without it the pointer is whatever ``loc``
    said, which may name a class rather than a field.
    """
    issues: list[ValidationIssue] = []
    for entry in error.errors():
        keyword = _keyword(str(entry.get("type", "")))
        loc = tuple(entry.get("loc") or ())
        path = list(loc) if payload is _UNRESOLVED else _document_path(loc, payload)
        issues.append(
            ValidationIssue(
                pointer=_path_to_pointer(path),
                message=_message(dict(entry), keyword),
                keyword=keyword,
                # A pydantic model is not a schema document, so there is no
                # schema tree path to report. ``issues[].schemaPath`` is
                # optional in core/error.json; empty is how this lane says
                # "no schema path", rather than inventing one.
                schema_path="",
            )
        )
    return issues
