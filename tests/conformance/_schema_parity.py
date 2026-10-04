"""Support for the schema/model parity suite.

The SDK validates the same payload twice: against the bundled JSON Schema
(``adcp.validation``) and against the generated Pydantic model for the same
tool (``adcp.types``). The two must accept the same documents. This module
builds the corpus that decides whether they do:

* :func:`synthesize` produces a schema-valid request payload for a tool by
  instantiating its bundled schema, exploring ``anyOf``/``oneOf`` branches with
  the compiled validator as the oracle;
* :data:`MUTATIONS` replaces one leaf at a time with a value of a different
  JSON type, which is where a type-coercion difference shows up;
* :func:`classify` names the reason a divergence exists, so the suite can
  distinguish a known, documented difference from a new one.

Nothing here is specific to a tool or a field: the suite enumerates the bundle,
so a schema added by a version bump is covered without an edit.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from adcp.validation.schema_loader import _ensure_state, _make_ref_resolver
from adcp.validation.schema_validator import validate_request

if sys.version_info >= (3, 11):
    import re._constants as sre
    import re._parser as sre_parse
else:  # pragma: no cover - Python 3.10 spells the same modules at top level
    import sre_constants as sre
    import sre_parse

# ---------------------------------------------------------------------------
# A string matching a regex, so a ``pattern`` field can be instantiated.
# ---------------------------------------------------------------------------

# Atomic groups arrived with Python 3.11's regex engine; on 3.10 the constant
# does not exist, and reaching for it made every anchored pattern fail to
# synthesize (the ``^`` node matched no branch and the lookup raised).
_ATOMIC_GROUP = getattr(sre, "ATOMIC_GROUP", None)

_CATEGORY_SAMPLE = {
    sre.CATEGORY_DIGIT: "5",
    sre.CATEGORY_WORD: "a",
    sre.CATEGORY_SPACE: " ",
    sre.CATEGORY_NOT_DIGIT: "a",
    sre.CATEGORY_NOT_WORD: "-",
    sre.CATEGORY_NOT_SPACE: "a",
}


def _sample_from_set(items: Any) -> str:
    for op, arg in items:
        if op is sre.LITERAL:
            return chr(arg)
        if op is sre.RANGE:
            return chr(arg[0])
        if op is sre.CATEGORY:
            return _CATEGORY_SAMPLE.get(arg, "a")
    return "a"


def _sample_from_nodes(nodes: Any) -> str:
    out: list[str] = []
    for op, arg in nodes:
        if op is sre.LITERAL:
            out.append(chr(arg))
        elif op is sre.NOT_LITERAL:
            out.append("a" if chr(arg) != "a" else "b")
        elif op is sre.IN:
            out.append("a" if arg and arg[0][0] is sre.NEGATE else _sample_from_set(arg))
        elif op is sre.ANY:
            out.append("a")
        elif op in (sre.MAX_REPEAT, sre.MIN_REPEAT):
            low, high, sub = arg
            repeat = low if low > 0 else (1 if high and high >= 1 else 0)
            out.append(_sample_from_nodes(sub) * repeat)
        elif op is sre.SUBPATTERN:
            out.append(_sample_from_nodes(arg[3]))
        elif op is sre.BRANCH:
            out.append(_sample_from_nodes(arg[1][0]))
        elif op is _ATOMIC_GROUP:
            out.append(_sample_from_nodes(arg))
    return "".join(out)


def sample_matching(pattern: str) -> str | None:
    """Return a string matching ``pattern``, or ``None`` when none is derived."""
    try:
        candidate = _sample_from_nodes(sre_parse.parse(pattern))
    except Exception:
        return None
    return candidate if re.search(pattern, candidate) else None


# ---------------------------------------------------------------------------
# Instantiating a schema.
# ---------------------------------------------------------------------------

#: One value per format the bundle uses, chosen to satisfy both the bundled
#: validator's format checker and the generated model's parser.
FORMAT_SAMPLES = {
    "date-time": "2026-01-01T00:00:00Z",
    "date": "2026-01-01",
    "time": "00:00:00Z",
    "duration": "PT1H",
    "email": "a@example.com",
    "idn-email": "a@example.com",
    "hostname": "example.com",
    "idn-hostname": "example.com",
    "ipv4": "203.0.113.1",
    "ipv6": "2001:db8::1",
    "uri": "https://example.com/a",
    "uri-reference": "https://example.com/a",
    "uri-template": "https://example.com/{id}",
    "iri": "https://example.com/a",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "regex": "^a$",
    "json-pointer": "/a",
}

_MAX_DEPTH = 12


class UnsynthesizableError(Exception):
    """No value could be derived for a schema node."""


def _merge(left: Any, right: Any) -> Any:
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = _merge(merged[key], value) if key in merged else value
        return merged
    return right


def _inherit_properties(branch: Any, base: Any) -> Any:
    """Give a ``required``-only composition branch the base's property schemas."""
    if not isinstance(branch, dict) or "$ref" in branch or "properties" in branch:
        return branch
    if not isinstance(base, dict) or not base.get("properties"):
        return branch
    return {**branch, "properties": base["properties"]}


def instances(node: Any, resolver: Any, depth: int = 0, budget: int = 6) -> list[Any]:
    """Return up to ``budget`` candidate instances of a schema node.

    More than one is returned where the node offers a choice, so the caller can
    ask the compiled validator which choice is the valid one instead of
    guessing from the schema's structure.
    """
    if depth > _MAX_DEPTH:
        raise UnsynthesizableError("nesting depth")
    if not isinstance(node, dict):
        return [{}]

    if "$ref" in node:
        with resolver.resolving(node["$ref"]) as target:
            return instances(target, resolver, depth + 1, budget)

    if "allOf" in node:
        base = {key: value for key, value in node.items() if key != "allOf"}
        parts = [instances(base, resolver, depth, budget)] if base else [[{}]]
        parts += [
            instances(_inherit_properties(member, base), resolver, depth, budget)
            for member in node["allOf"]
        ]
        width = min(budget, max(len(part) for part in parts))
        merged = []
        for index in range(width):
            value: Any = {}
            for part in parts:
                value = _merge(value, part[min(index, len(part) - 1)])
            merged.append(value)
        return merged

    for keyword in ("oneOf", "anyOf"):
        if keyword in node:
            base = {k: v for k, v in node.items() if k not in ("oneOf", "anyOf")}
            if base.get("properties") or base.get("required"):
                try:
                    base_values = instances(base, resolver, depth, 2)
                except UnsynthesizableError:
                    base_values = [{}]
            else:
                base_values = [None]
            per_branch: list[list[Any]] = []
            for branch in node[keyword]:
                try:
                    branch_values = instances(
                        _inherit_properties(branch, base), resolver, depth + 1, 2
                    )
                except UnsynthesizableError:
                    continue
                per_branch.append(
                    [
                        _merge(base_value, value) if isinstance(base_value, dict) else value
                        for base_value in base_values
                        for value in branch_values
                    ]
                )
            if not per_branch:
                raise UnsynthesizableError(f"no satisfiable {keyword} branch")
            # Interleave, so every branch contributes before the budget runs out.
            out = [
                part[index]
                for index in range(max(len(part) for part in per_branch))
                for part in per_branch
                if index < len(part)
            ]
            return out[:budget]

    if "const" in node:
        return [node["const"]]
    if "enum" in node:
        return [value for value in node["enum"] if value is not None][:budget] or [None]

    declared = node.get("type")
    if isinstance(declared, list):
        declared = next((entry for entry in declared if entry != "null"), None)
    if declared is None and "properties" in node:
        declared = "object"
    if declared is None and "items" in node:
        declared = "array"
    if declared is None and "default" in node:
        return [node["default"]]

    if declared == "object":
        return _object_instances(node, resolver, depth, budget)
    if declared == "array":
        return _array_instances(node, resolver, depth, budget)
    if declared == "string":
        return _string_instances(node)
    if declared == "integer":
        return [_integer_instance(node)]
    if declared == "number":
        return [_number_instance(node)]
    if declared == "boolean":
        return [True]
    if declared == "null":
        return [None]
    return [{}]


def _object_instances(node: Any, resolver: Any, depth: int, budget: int) -> list[Any]:
    properties = node.get("properties") or {}
    required = node.get("required") or []
    out: list[Any] = [{}]
    for name in required:
        schema = properties.get(name)
        if schema is None:
            extra = node.get("additionalProperties")
            schema = extra if isinstance(extra, dict) else {"type": "string"}
        values = instances(schema, resolver, depth + 1, budget)
        grown = [
            _merge(existing, {name: values[min(index, len(values) - 1)]})
            for index, existing in enumerate(out)
        ]
        grown += [{**out[0], name: value} for value in values[1:budget]]
        out = grown[:budget]
    if not required and node.get("minProperties"):
        extra = node.get("additionalProperties")
        schema = extra if isinstance(extra, dict) else {"type": "string"}
        out = [{"key": value} for value in instances(schema, resolver, depth + 1, budget)]
    return out or [{}]


def _array_instances(node: Any, resolver: Any, depth: int, budget: int) -> list[Any]:
    minimum = node.get("minItems")
    if not minimum:
        return [[]]
    items = node.get("items")
    if isinstance(items, list):
        return [[instances(entry, resolver, depth + 1, budget)[0] for entry in items]]
    values = instances(items or {"type": "string"}, resolver, depth + 1, budget)
    return [[value] * max(1, int(minimum)) for value in values[:budget]]


def _string_instances(node: Any) -> list[Any]:
    fmt = node.get("format")
    pattern = node.get("pattern")
    out: list[Any] = []
    # A field carrying both keywords must satisfy both. The format-shaped value
    # comes first: a string derived from the pattern often satisfies the pattern
    # and violates the format (``^https://`` admits ``"https://"``).
    if fmt in FORMAT_SAMPLES and (pattern is None or re.search(pattern, FORMAT_SAMPLES[fmt])):
        out.append(FORMAT_SAMPLES[fmt])
    if pattern is not None:
        derived = sample_matching(pattern)
        if derived is not None and len(derived) >= node.get("minLength", 0):
            out.append(derived)
        if not out:
            raise UnsynthesizableError(f"pattern {pattern}")
        return out
    if fmt is not None:
        if not out:
            raise UnsynthesizableError(f"format {fmt}")
        return out
    return ["a" * max(1, int(node.get("minLength") or 1))]


def _integer_instance(node: Any) -> int:
    low = node.get("minimum", node.get("exclusiveMinimum"))
    value = 1 if low is None else int(low) + (1 if "exclusiveMinimum" in node else 0)
    high = node.get("maximum", node.get("exclusiveMaximum"))
    if high is not None and value > int(high):
        value = int(high) - (1 if "exclusiveMaximum" in node else 0)
    multiple = node.get("multipleOf")
    if multiple:
        value = max(int(multiple), value - value % int(multiple))
    return value


def _number_instance(node: Any) -> float:
    low = node.get("minimum", node.get("exclusiveMinimum"))
    value = 1.0 if low is None else float(low) + (0.5 if "exclusiveMinimum" in node else 0.0)
    high = node.get("maximum", node.get("exclusiveMaximum"))
    if high is not None and value > float(high):
        value = float(high) - (0.5 if "exclusiveMaximum" in node else 0.0)
    return value


def request_tools() -> list[str]:
    """Every tool in the pinned bundle that ships a request schema."""
    state = _ensure_state(None)
    return sorted(tool for (tool, direction) in state.file_index if direction == "request")


def synthesize(tool: str) -> dict[str, Any]:
    """Return a schema-valid request payload for ``tool``.

    Starts from the required fields and grows the payload with every optional
    property that keeps it valid, so the mutation corpus reaches optional
    fields too. Raises :class:`UnsynthesizableError` when no valid payload is
    derived from the schema.
    """
    state = _ensure_state(None)
    file = state.file_index[(tool, "request")]
    schema = json.loads(file.read_text())
    resolver = _make_ref_resolver(state, file, schema)

    last = ""
    for candidate in instances(schema, resolver, 0, 64):
        outcome = validate_request(tool, candidate)
        if outcome.valid:
            return _grow(tool, candidate, schema, resolver)
        last = "; ".join(f"{issue.pointer}:{issue.message}" for issue in outcome.issues[:2])
    raise UnsynthesizableError(last or "no valid candidate")


def _grow(tool: str, payload: dict[str, Any], schema: Any, resolver: Any) -> dict[str, Any]:
    def walk(node: Any, obj: Any, depth: int = 0) -> None:
        if depth > 5 or not isinstance(obj, dict) or not isinstance(node, dict):
            return
        if "$ref" in node:
            with resolver.resolving(node["$ref"]) as target:
                walk(target, obj, depth)
            return
        for keyword in ("allOf", "oneOf", "anyOf"):
            for member in node.get(keyword) or []:
                walk(member, obj, depth)
        properties = node.get("properties") or {}
        for name, sub in properties.items():
            if name in obj:
                continue
            try:
                value = instances(sub, resolver, depth + 1, 1)[0]
            except UnsynthesizableError:
                continue
            obj[name] = value
            if not validate_request(tool, payload).valid:
                del obj[name]
        for name, sub in properties.items():
            if isinstance(obj.get(name), dict):
                walk(sub, obj[name], depth + 1)

    walk(schema, payload)
    return payload


# ---------------------------------------------------------------------------
# The mutation corpus.
# ---------------------------------------------------------------------------

#: Values substituted for one leaf at a time. Each is of a JSON type that the
#: leaf's schema most likely forbids, which is what makes a coercion
#: difference between the two validators observable. ``None`` probes
#: nullability; the numeric strings probe string-to-number coercion; ``1`` and
#: ``0`` probe boolean coercion; ``2.0`` probes the integral float, which JSON
#: Schema counts as an integer and a strict Python ``int`` does not.
MUTATIONS: tuple[Any, ...] = (
    "yes",
    "true",
    "1",
    "0",
    "",
    "A" * 300,
    "!!NOT-VALID!!",
    1,
    0,
    -1,
    2,
    2.0,
    1.5,
    10**12,
    True,
    False,
    None,
    [],
    {},
)


def leaves(obj: Any, pointer: str = "") -> Any:
    """Yield ``(json_pointer, value)`` for every scalar in ``obj``."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from leaves(value, f"{pointer}/{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from leaves(value, f"{pointer}/{index}")
    else:
        yield pointer, obj


def replace_at(obj: Any, pointer: str, value: Any) -> None:
    """Set the value at an RFC 6901 ``pointer`` in place."""
    parts = [part for part in pointer.split("/") if part != ""]
    cursor = obj
    for part in parts[:-1]:
        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
    last = parts[-1]
    if isinstance(cursor, list):
        cursor[int(last)] = value
    else:
        cursor[last] = value


# ---------------------------------------------------------------------------
# Naming a divergence.
# ---------------------------------------------------------------------------

SCHEMA_ACCEPTS = "the bundled schema accepts a document the model refuses"
MODEL_ACCEPTS = "the model accepts a document the bundled schema refuses"


@dataclass(frozen=True)
class Divergence:
    """One document the two validators grade differently."""

    tool: str
    pointer: str
    #: The value substituted at ``pointer``, as the validators saw it.
    mutation: Any
    direction: str
    #: jsonschema keyword that refused, when the schema is the one refusing.
    keyword: str
    #: Sanitized schema message, when the schema is the one refusing.
    message: str
    #: Path inside the schema that refused, when the schema is the one refusing.
    schema_path: str
    #: Pydantic error types, when the model is the one refusing.
    error_types: frozenset[str]

    def describe(self) -> str:
        reason = self.message or ", ".join(sorted(self.error_types))
        where = self.pointer or "/"
        return f"{self.tool}{where} = {self.mutation!r}: {self.direction} ({reason})"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_numeric_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        float(value)
    except ValueError:
        return False
    return True


#: The differences this SDK knows about, each with the reason it is still here.
#: A divergence matching none of these predicates is new, and the suite fails
#: on it. A predicate that matches nothing has been fixed, and the suite says
#: to delete it — this set only shrinks.
DECLARED: dict[str, Callable[[Divergence], bool]] = {
    # The generator renders an optional property as ``X | None``, which
    # conflates "may be absent" with "may be null". JSON Schema separates
    # them: a property outside ``required`` may be absent, and ``null`` is
    # valid only when the declared type admits it. Closing this needs a
    # decision about whether AdCP intends optional properties to accept an
    # explicit null; if it does, the schemas declare ``["string", "null"]``.
    "optional_field_accepts_explicit_null": lambda d: (
        d.direction == MODEL_ACCEPTS and d.mutation is None
    ),
    # ``format: date-time`` and ``format: date`` become ``AwareDatetime`` and
    # ``date``, whose parsers read a number as a Unix timestamp and a numeric
    # string as one too. The schema requires an RFC 3339 string.
    "datetime_field_parses_a_number": lambda d: (
        d.direction == MODEL_ACCEPTS
        and (d.keyword == "format" or "'string'" in d.message)
        and (_is_number(d.mutation) or _is_numeric_string(d.mutation))
    ),
    # The schema states the constraint with ``if``/``then``/``else``, which
    # code generation cannot express as a field annotation, so the model never
    # carries it. :class:`adcp.types.base.AdCPBaseModel` says as much: complete
    # conformance is defined by the bundled schema, and a conditional is one of
    # the rules only the schema can enforce.
    "conditional_subschema_is_not_generated": lambda d: (
        d.direction == MODEL_ACCEPTS
        and any(f"/{keyword}/" in d.schema_path for keyword in ("if", "then", "else"))
    ),
    # A field whose schema pins a single boolean (``const: true``) becomes
    # ``Literal[True]``, and pydantic compares a literal by equality, where
    # ``1 == True``.
    "boolean_literal_equals_one": lambda d: (
        d.direction == MODEL_ACCEPTS
        and "'boolean'" in d.message
        and isinstance(d.mutation, int)
        and not isinstance(d.mutation, bool)
        and d.mutation in (0, 1)
    ),
    # The model renders a ``oneOf``/``anyOf`` as a plain union whose arms
    # allow extra properties, so a value that fits no arm's declared type is
    # kept as an extra field on whichever arm validates. The schema evaluates
    # the composition and refuses.
    "union_arm_absorbs_a_type_error": lambda d: (
        d.direction == MODEL_ACCEPTS and d.keyword in ("oneOf", "anyOf")
    ),
    # ``CanonicalBoundaryModel`` refuses any payload carrying a legacy
    # creative identity, while ``core/format-id.json`` is marked deprecated
    # and retained for 3.x compatibility, so the bundled schema accepts it.
    # Closing this needs a decision: either the canonical model accepts and
    # projects the legacy shape the way ``LegacyFormatId`` does, or the
    # schemas stop retaining it.
    "canonical_model_refuses_legacy_creative_identity": lambda d: (
        d.direction == SCHEMA_ACCEPTS and d.error_types == frozenset({"value_error"})
    ),
    # The model marks a field required that the schema leaves optional.
    "model_requires_a_field_the_schema_does_not": lambda d: (
        d.direction == SCHEMA_ACCEPTS and d.error_types == frozenset({"missing"})
    ),
}


def classify(divergence: Divergence) -> str | None:
    """Return the name of the declared class covering ``divergence``, if any."""
    for name, predicate in DECLARED.items():
        if predicate(divergence):
            return name
    return None
