"""Strict JSON parsing for signed request bodies (checklist step 14).

A valid Content-Digest authenticates bytes, not their interpretation. RFC 8259
section 4 leaves duplicate-name behavior unspecified, and Python's
``json.loads`` silently keeps the last duplicate, so two components parsing the
same signed body can disagree about what it says (a parser-differential, cf.
CVE-2017-12635). Step 14a requires a parser that surfaces duplicates rather
than resolving them; this module is that parser.

Beyond duplicate keys at any depth, the parse rejects:

- bytes that are not valid UTF-8 (RFC 8259 section 8.1). ``json.loads(bytes)``
  would otherwise sniff UTF-16/UTF-32 and decode a body other parsers read
  differently;
- anything that is not RFC 8259 JSON, including the ``NaN`` / ``Infinity`` /
  ``-Infinity`` literals Python's decoder accepts by default and a leading
  byte-order mark;
- numbers that overflow to a non-finite float (``1e999``), which Python
  would otherwise turn into ``inf``;
- unpaired UTF-16 surrogate escapes (``"\\ud800"``). The grammar allows them,
  but they are not Unicode scalar values (I-JSON, RFC 7493 section 2.1), and
  parsers disagree on them (replace with U+FFFD, reject, or pass through), so
  two keys that differ only in a lone surrogate can collide downstream;
- inputs the decoder cannot finish (nesting deep enough to exhaust recursion,
  integers past the interpreter's digit limit).

Failures carry a coarse ``reason`` only. Neither the offending key name nor any
body bytes are placed in the exception, so a rejected frame cannot push
attacker-chosen bytes into logs (step 14b).
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

StrictJsonReason = Literal["invalid_utf8", "invalid_json", "duplicate_key"]

# RFC 8259 section 2 insignificant whitespace. A body made only of these bytes
# carries no JSON value and is treated as absent, matching the TypeScript SDK.
_JSON_WHITESPACE = b" \t\n\r"

# The body is decoded as strict UTF-8 first, which cannot yield a surrogate
# code point. So any surrogate in the parsed result came from an escape that
# the decoder did not combine into a pair: an unpaired surrogate.
# Raw pattern: `re` expands the escapes, so no surrogate lives in the source.
_SURROGATE = re.compile(r"[\ud800-\udfff]")


class StrictJsonError(ValueError):
    """The body is not strict, unambiguous JSON."""

    def __init__(self, reason: StrictJsonReason) -> None:
        super().__init__(reason)
        self.reason: StrictJsonReason = reason


class _DuplicateKeyError(Exception):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite number literal is not JSON")


def _finite_float(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):
        raise ValueError("number overflows to a non-finite float")
    return value


def _has_unpaired_surrogate(value: Any) -> bool:
    # Iterative: the decoder already bounded nesting, but recursing here would
    # re-introduce a second, lower recursion limit.
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if _SURROGATE.search(current):
                return True
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def body_is_empty(body: bytes) -> bool:
    """True when the body has no JSON value to parse (empty or whitespace only)."""
    return not body.strip(_JSON_WHITESPACE)


def parse_strict_json(body: bytes) -> Any:
    """Parse ``body`` as strict UTF-8 JSON, or raise :class:`StrictJsonError`."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise StrictJsonError("invalid_utf8") from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except _DuplicateKeyError:
        raise StrictJsonError("duplicate_key") from None
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError, the non-finite hooks, and the
        # int-digit limit; RecursionError covers pathological nesting.
        raise StrictJsonError("invalid_json") from None
    if _has_unpaired_surrogate(value):
        raise StrictJsonError("invalid_json")
    return value


__all__ = ["StrictJsonError", "StrictJsonReason", "body_is_empty", "parse_strict_json"]
