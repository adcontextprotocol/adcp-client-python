"""Bounded exact JSON values at the new verification boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import cast

from adcp.reporting.canonical_json import MAX_SAFE_INTEGER, JsonValue, canonical_json_utf8_v1
from adcp.reporting.materializer.contracts import failure


@dataclass(frozen=True, slots=True)
class ReportingVerificationLimits:
    max_depth: int = 32
    max_value_bytes: int = 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_items: int = 1_000_000
    max_rows: int = 100_000
    max_pages: int = 10_000
    max_objects: int = 10_000
    max_chunks: int = 1_000_000

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, f.name)) is not int or getattr(self, f.name) < 1
            for f in fields(self)
        ):
            raise ValueError("verification limits require positive integer bounds")
        if self.max_depth > 128:
            raise ValueError("verification depth cannot exceed 128")


DEFAULT_LIMITS = ReportingVerificationLimits()


def strict_reporting_json(
    value: object, limits: ReportingVerificationLimits = DEFAULT_LIMITS
) -> bytes:
    """Encode exact JSON types, without coercion, subclass hooks or Unicode normalization.

    Integers must be JS-safe; exact decimals are strings. Floats, tuples,
    subclasses and lone surrogates are deliberately outside this SDK profile.
    """
    stack = [(value, 0)]
    count, encoded_bytes = 0, 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > limits.max_depth or count > limits.max_items:
            raise failure("LIMIT_EXCEEDED")
        kind = type(item)
        if kind is str:
            text = cast(str, item)
            if len(text) > limits.max_value_bytes:
                raise failure("LIMIT_EXCEEDED")
            encoded_bytes += 2  # Opening/closing quotes, including escaped UTF-8.
            for char in text:
                code = ord(char)
                if 0xD800 <= code <= 0xDFFF:
                    raise failure("SOURCE_INVALID")
                if char in '\\"\b\t\n\f\r':
                    encoded_bytes += 2
                elif code < 0x20:
                    encoded_bytes += 6
                else:
                    encoded_bytes += (
                        1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4
                    )
                if encoded_bytes > limits.max_value_bytes:
                    raise failure("LIMIT_EXCEEDED")
        elif kind is int:
            if abs(cast(int, item)) > MAX_SAFE_INTEGER:
                raise failure("SOURCE_INVALID")
            encoded_bytes += len(str(item))
        elif item is None or kind is bool:
            encoded_bytes += 5 if item is False else 4
        elif kind is list:
            values = cast(list[object], item)
            if len(values) + count + len(stack) > limits.max_items:
                raise failure("LIMIT_EXCEEDED")
            encoded_bytes += 2 + max(0, len(values) - 1)
            stack.extend((v, depth + 1) for v in values)
        elif kind is dict:
            mapping = cast(dict[object, object], item)
            if len(mapping) * 2 + count + len(stack) > limits.max_items:
                raise failure("LIMIT_EXCEEDED")
            encoded_bytes += 2 + max(0, 2 * len(mapping) - 1)
            for key, child in mapping.items():
                if type(key) is not str:
                    raise failure("SOURCE_INVALID")
                stack.extend(((key, depth + 1), (child, depth + 1)))
        else:
            raise failure("SOURCE_INVALID")
        if encoded_bytes > limits.max_value_bytes:
            raise failure("LIMIT_EXCEEDED")
    result = canonical_json_utf8_v1(value)
    if len(result) > limits.max_value_bytes:
        raise failure("LIMIT_EXCEEDED")
    return result


def parse_reporting_json(
    payload: bytes, limits: ReportingVerificationLimits = DEFAULT_LIMITS
) -> JsonValue:
    """Reject ambiguous JSON before it can be normalized into a Python mapping."""
    if type(payload) is not bytes:
        raise failure("SOURCE_INVALID")
    if len(payload) > limits.max_value_bytes:
        raise failure("LIMIT_EXCEEDED")
    # Bound parser recursion before json.loads allocates a nested tree.
    depth, quoted, escaped = 0, False, False
    for byte in payload:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > limits.max_depth:
                raise failure("LIMIT_EXCEEDED")
        elif byte in (93, 125):
            depth -= 1

    def pairs(values: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject(value: str) -> JsonValue:
        raise ValueError

    invalid = False
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject,
            parse_float=reject,
        )
    except (ValueError, UnicodeError, RecursionError):
        invalid = True
    if invalid:
        raise failure("SOURCE_INVALID")
    strict_reporting_json(value, limits)
    return cast(JsonValue, value)
