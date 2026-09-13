"""``canonical_json_utf8_v1`` — the byte-exact reporting evidence encoding.

Every immutable reporting artifact (manifest, fingerprint, publication id) is
bound to *bytes*, not to a Python object.  Two producers in two languages must
agree on those bytes or the SHA-256 that binds them is worthless, so this
module is a deliberately restricted RFC 8785 / JCS profile rather than "JSON
with ``sort_keys=True``":

* Object keys are ordered by raw **UTF-16 code units** — the ECMAScript /
  JCS ordering, never locale collation and (importantly) never Python's
  default code-*point* ordering.  The two disagree exactly when a
  supplementary character (U+10000 and above, encoded as a surrogate pair
  starting at U+D800) is compared against a BMP character in U+E000..U+FFFF.
* Strings use ECMAScript ``JSON.stringify`` escaping with no Unicode
  normalization.  Lone surrogates are escaped rather than emitted, matching
  well-formed ``JSON.stringify`` (ES2019).
* Arrays retain order.
* Numbers are base-10 JSON **safe integers only**.  Exact decimals belong in
  the wire as JSON strings; a float here is a bug, not a rounding question.
* ``None`` is ``null``; there is no representation for "undefined", so callers
  drop absent fields instead of emitting them.
* No insignificant whitespace anywhere.

The golden vectors in ``tests/test_reporting_canonical_json.py`` are the
cross-language contract.  They are copied byte-for-byte from the TypeScript
conformance suite; if you change anything in this module and those vectors
still pass, the change is safe.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Union

__all__ = [
    "MAX_SAFE_INTEGER",
    "CanonicalJsonError",
    "JsonValue",
    "canonical_json_sha256_v1",
    "canonical_json_utf8_v1",
    "reporting_fingerprint_v1",
    "strip_absent",
]

#: ECMAScript ``Number.MAX_SAFE_INTEGER``.  The contract permits only integers
#: that survive a round trip through a JavaScript producer unchanged.
MAX_SAFE_INTEGER = 9007199254740991

JsonValue = Union[
    None,
    bool,
    int,
    str,
    "list[JsonValue]",
    "dict[str, JsonValue]",
]


class CanonicalJsonError(TypeError, ValueError):
    """A value cannot be represented in ``canonical_json_utf8_v1``.

    Deliberately both a :class:`TypeError` (it *is* a type problem) and a
    :class:`ValueError` (so a Pydantic validator that encodes a field surfaces
    it as a normal validation error rather than a 500).
    """


def _encode_string(value: str) -> str:
    """ECMAScript ``JSON.stringify`` escaping, with lone surrogates escaped.

    ``json.dumps(ensure_ascii=False)`` already matches ``JSON.stringify`` for
    every well-formed string: the same ``\\b \\t \\n \\f \\r \\" \\\\``
    shortcuts, ``\\u00xx`` for the remaining C0 controls, and raw UTF-8 for
    everything else (including U+2028/U+2029, which JSON.stringify also emits
    raw).  Only unpaired surrogates diverge -- Python would emit them raw and
    then fail to UTF-8 encode, so they are escaped here the way well-formed
    ``JSON.stringify`` does.
    """
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        return _encode_string_with_lone_surrogates(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _encode_string_with_lone_surrogates(value: str) -> str:
    pieces: list[str] = ['"']
    index = 0
    length = len(value)
    while index < length:
        character = value[index]
        code_point = ord(character)
        if 0xD800 <= code_point <= 0xDBFF and index + 1 < length:
            trailing = ord(value[index + 1])
            if 0xDC00 <= trailing <= 0xDFFF:
                # A well-formed pair; Python stores it as one character
                # already, so this branch only fires for explicitly split
                # surrogates. Emit both as escapes to stay byte-stable.
                pieces.append(f"\\u{code_point:04x}\\u{trailing:04x}")
                index += 2
                continue
        if 0xD800 <= code_point <= 0xDFFF:
            pieces.append(f"\\u{code_point:04x}")
        else:
            pieces.append(json.dumps(character, ensure_ascii=False)[1:-1])
        index += 1
    pieces.append('"')
    return "".join(pieces)


def _utf16_key(key: str) -> bytes:
    """Sort key reproducing ECMAScript string ordering.

    Comparing UTF-16 big-endian bytes lexicographically is exactly comparing
    the code-unit sequences numerically, which is what ``Array.prototype.sort``
    does for strings.
    """
    return key.encode("utf-16-be", errors="surrogatepass")


def _encode(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, int):
        # ``bool`` is handled above; every remaining int must be safe.
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalJsonError(
                f"canonical_json_utf8_v1 permits safe integers only, got {value!r}"
            )
        return str(value)
    if isinstance(value, float):
        raise CanonicalJsonError(
            "canonical_json_utf8_v1 does not permit floating-point numbers; "
            "encode exact decimals as JSON strings"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalJsonError(
                    f"canonical_json_utf8_v1 permits string keys only, got {key!r}"
                )
        items = [
            f"{_encode_string(key)}:{_encode(value[key])}" for key in sorted(value, key=_utf16_key)
        ]
        return "{" + ",".join(items) + "}"
    raise CanonicalJsonError(f"canonical_json_utf8_v1 does not permit {type(value).__name__}")


def canonical_json_utf8_v1(value: object) -> bytes:
    """Encode ``value`` as ``canonical_json_utf8_v1`` bytes."""
    return _encode(value).encode("utf-8")


def canonical_json_sha256_v1(value: object) -> str:
    """Lowercase hex SHA-256 of the canonical encoding of ``value``."""
    return hashlib.sha256(canonical_json_utf8_v1(value)).hexdigest()


def reporting_fingerprint_v1(value: object) -> str:
    """``sha256:<hex>`` fingerprint of the canonical encoding of ``value``.

    The ``sha256:`` prefix distinguishes a fingerprint (which binds a *logical
    structure*) from a bare content digest (which binds *exact bytes*) at every
    boundary that carries both.
    """
    return f"sha256:{canonical_json_sha256_v1(value)}"


def strip_absent(value: Any) -> Any:
    """Recursively drop ``None`` members so two encodings of one document agree.

    ``canonical_json_utf8_v1`` has no "undefined", so an absent optional field
    and a field explicitly set to ``null`` are different documents with
    different digests.  Pydantic's ``exclude_none=True`` does this for models;
    this does it for the plain mappings that reach a fingerprint helper by
    another route, so both paths hash the same bytes.
    """
    if isinstance(value, dict):
        return {key: strip_absent(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [strip_absent(item) for item in value]
    return value
