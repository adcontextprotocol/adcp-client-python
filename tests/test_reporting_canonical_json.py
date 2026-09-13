"""Cross-language golden vectors for ``canonical_json_utf8_v1``.

Every vector below was produced by the TypeScript reference implementation in
``@scope3/agentic-contracts`` (``canonicalJsonV1``) and is asserted here as
exact UTF-8 bytes plus SHA-256.  A Python producer whose manifest bytes differ
from a TypeScript producer's by one byte publishes a different manifest digest
for identical logical content, which breaks replay identity and every
fingerprint derived from it -- so this file is the contract, not a smoke test.

The first vector is copied verbatim from the shared conformance suite; the rest
extend it over the cases where a naive Python implementation silently diverges.
"""

from __future__ import annotations

import pytest

from adcp.reporting.canonical_json import (
    MAX_SAFE_INTEGER,
    CanonicalJsonError,
    canonical_json_sha256_v1,
    canonical_json_utf8_v1,
    reporting_fingerprint_v1,
)

# (name, value, expected hex bytes)
GOLDEN_VECTORS: list[tuple[str, object, str]] = [
    (
        # The published cross-language vector from the shared conformance test.
        "published_cross_language_vector",
        {"z": "line\n", "é": "é", "\U0001f600": "\U0001f600", "a": 1},
        "7b2261223a312c227a223a226c696e655c6e222c22c3a9223a22c3a9222c22f09f9880223a22f09f9880227d",
    ),
    (
        # The whole reason this module exists: UTF-16 code-unit ordering puts a
        # supplementary character (surrogate lead U+D83D) *before* BMP
        # characters in U+E000..U+FFFF.  Python's default code-point sort would
        # emit these three keys in the opposite order.
        "utf16_code_unit_key_order",
        {"": 1, "\U0001f600": 2, "�": 3},
        "7b22f09f9880223a322c22ee8080223a312c22efbfbd223a337d",
    ),
    (
        # C0 controls use the ECMAScript shortcuts where they exist and
        # ``\\u00xx`` otherwise; DEL (U+007F) is *not* escaped.
        "control_character_escaping",
        {"k": "\x00\x01\x08\t\n\x0c\r\x1f a\x7f"},
        "7b226b223a225c75303030305c75303030315c625c745c6e5c665c725c753030316620617f227d",
    ),
    (
        "nested_objects_and_arrays_retain_array_order",
        {"b": [1, {"d": None, "c": True}], "a": {"é": 0}},
        "7b2261223a7b22c3a9223a307d2c2262223a5b312c7b2263223a747275652c2264223a6e756c6c7d5d7d",
    ),
    (
        # U+2028/U+2029 are legal raw inside a JSON string and ``JSON.stringify``
        # emits them raw.  An encoder that "helpfully" escapes them diverges.
        "line_and_paragraph_separators_stay_raw",
        {"s": "  "},
        "7b2273223a22e280a8e280a9227d",
    ),
    (
        "safe_integer_bounds",
        {"n": -MAX_SAFE_INTEGER, "z": 0, "p": MAX_SAFE_INTEGER},
        "7b226e223a2d393030373139393235343734303939312c2270223a"
        "393030373139393235343734303939312c227a223a307d",
    ),
    (
        "quote_and_backslash_escaping_in_keys_and_values",
        {'q"k': 'back\\slash "quoted"'},
        "7b22715c226b223a226261636b5c5c736c617368205c2271756f7465645c22227d",
    ),
]

# SHA-256 of each vector's bytes, also produced by the TypeScript reference.
GOLDEN_DIGESTS = {
    "published_cross_language_vector": (
        "57d672581e912835bbb3b06dca5d2529559d186fa1981b7c36288f497b8806d5"
    ),
    "utf16_code_unit_key_order": (
        "3c52a84a91c6732bf05ce94e8e17b53674b1582a6e3f0aa7197e69291dedd4bf"
    ),
    "control_character_escaping": (
        "f1b2424fff75aaedfab04df35be8030ef5a01b44d578edbe2f5c24db6fb5f9db"
    ),
    "nested_objects_and_arrays_retain_array_order": (
        "177efdc3094fa4b46fa5bc25cff2b58729ce0994dd821131cfaef0d6367d8fe9"
    ),
    "line_and_paragraph_separators_stay_raw": (
        "37291683554efe5b232ef434c7f2ed8b3f39ce37f4a2d8db100eb4224e48edcd"
    ),
    "safe_integer_bounds": ("7896c9d28d3df71d7c4f8d8484a845d0c0281d57e09cda95b33910eb7de6d679"),
    "quote_and_backslash_escaping_in_keys_and_values": (
        "c2e234e6a78aecd021be90eb2f5e23b2b27e224cf3eb01c07b8614161668f66e"
    ),
}


@pytest.mark.parametrize(("name", "value", "expected_hex"), GOLDEN_VECTORS)
def test_golden_vector_bytes_match_typescript(name: str, value: object, expected_hex: str) -> None:
    assert canonical_json_utf8_v1(value).hex() == expected_hex


@pytest.mark.parametrize(("name", "value", "expected_hex"), GOLDEN_VECTORS)
def test_golden_vector_digests_match_typescript(
    name: str, value: object, expected_hex: str
) -> None:
    assert canonical_json_sha256_v1(value) == GOLDEN_DIGESTS[name]


def test_fingerprint_carries_the_algorithm_prefix() -> None:
    assert reporting_fingerprint_v1({"a": 1}) == f"sha256:{canonical_json_sha256_v1({'a': 1})}"


def test_scalars_encode_like_json_stringify() -> None:
    assert canonical_json_utf8_v1(None) == b"null"
    assert canonical_json_utf8_v1(True) == b"true"
    assert canonical_json_utf8_v1(False) == b"false"
    assert canonical_json_utf8_v1(0) == b"0"
    assert canonical_json_utf8_v1("") == b'""'
    assert canonical_json_utf8_v1([]) == b"[]"
    assert canonical_json_utf8_v1({}) == b"{}"


def test_booleans_are_not_encoded_as_integers() -> None:
    # ``bool`` subclasses ``int`` in Python; an encoder that checks ``int``
    # first emits ``{"a":1}`` here and silently changes the digest.
    assert canonical_json_utf8_v1({"a": True}) == b'{"a":true}'


def test_floats_are_rejected_rather_than_rounded() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical_json_utf8_v1({"spend": 1.25})


def test_unsafe_integers_are_rejected() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical_json_utf8_v1(MAX_SAFE_INTEGER + 1)
    with pytest.raises(CanonicalJsonError):
        canonical_json_utf8_v1(-MAX_SAFE_INTEGER - 1)


def test_unsupported_types_are_rejected() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical_json_utf8_v1({"when": object()})
    with pytest.raises(CanonicalJsonError):
        canonical_json_utf8_v1({1: "int key"})


def test_lone_surrogates_are_escaped_not_emitted() -> None:
    # A raw lone surrogate cannot be UTF-8 encoded at all, so the only
    # byte-stable answer is the ES2019 well-formed ``JSON.stringify`` escape.
    assert canonical_json_utf8_v1({"k": "\ud800"}) == b'{"k":"\\ud800"}'
