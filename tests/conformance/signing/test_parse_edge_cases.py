"""Edge cases for Signature-Input parsing and canonicalization.

Covers RFC 8941 sf-string escape handling, unquoted-value rejection,
duplicate-component detection, and target-URI fragment stripping.
"""

from __future__ import annotations

import pytest

from adcp.signing.canonical import (
    canonicalize_target_uri,
    parse_signature_input_header,
)


def test_sfv_splitter_handles_escaped_backslash_before_closing_quote() -> None:
    # Raw nonce value is `abc\\` — encoded as sf-string `"abc\\\\"` (four
    # backslashes to escape the pair). The closing `"` must be recognized as
    # the span terminator even though the preceding char is a literal `\`.
    header = 'sig1=("@method");nonce="abc\\\\";alg="ed25519";keyid="kid1"'
    labels = parse_signature_input_header(header)
    assert "sig1" in labels
    assert labels["sig1"].params["nonce"] == "abc\\"
    assert labels["sig1"].params["alg"] == "ed25519"
    assert labels["sig1"].params["keyid"] == "kid1"


def test_escaped_quote_inside_string_does_not_terminate_the_value() -> None:
    # Escaped double-quote inside a quoted value must not terminate the span.
    labels = parse_signature_input_header('sig1=("@method");nonce="quoted \\"inner\\" text";b=1')
    assert labels["sig1"].params == {"nonce": 'quoted "inner" text', "b": 1}


def test_parse_rejects_unterminated_quoted_string() -> None:
    with pytest.raises(ValueError):
        parse_signature_input_header('sig1=("@method");nonce="abc')


def test_parse_rejects_empty_param_value() -> None:
    with pytest.raises(ValueError):
        parse_signature_input_header('sig1=("@method");tag=')


def test_target_uri_drops_fragment() -> None:
    # Fragment is client-local per RFC 7230 §5.5, never sent on the wire —
    # @target-uri must reflect the effective request URI, excluding fragment.
    assert canonicalize_target_uri("https://example.com/a#section") == "https://example.com/a"
    assert canonicalize_target_uri("https://example.com/a?q=1#top") == "https://example.com/a?q=1"
