"""The http-message-signatures and http-sf shapes adcp.signing._rfc9421 relies on.

The SDK takes RFC 9421 from ``http-message-signatures`` and RFC 8941 from
``http-sf``. It uses the library's resolver as it ships (``;key`` and
``;req`` included), its public extension points
(``validate_created_and_expires``, ``signature_metadata_parameters``,
``prepare`` and ``apply``), and, for the diagnostic base builder, two
protected hooks (``_build_signature_base``, ``_parse_covered_component_ids``).
A library release that moves any of them fails here, before it fails a
profile test with a confusing error code.
"""

from __future__ import annotations

import inspect

import http_sf
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from http_message_signatures import (
    HTTPMessageSigner,
    HTTPMessageVerifier,
)
from http_message_signatures.signatures import HTTPSignatureHandler

from adcp.signing import _rfc9421


def test_protected_hooks_keep_their_signatures():
    build = inspect.signature(HTTPSignatureHandler._build_signature_base)
    # ``build_base`` passes exactly these; a release may add optional
    # parameters after them (2.2.0 added ``component_resolver=None``).
    assert list(build.parameters)[:4] == [
        "self",
        "message",
        "covered_component_ids",
        "signature_params",
    ]
    assert all(
        parameter.default is not inspect.Parameter.empty
        for parameter in list(build.parameters.values())[4:]
    )
    parse = inspect.signature(HTTPMessageSigner._parse_covered_component_ids)
    assert list(parse.parameters) == ["self", "covered_component_ids"]
    init = inspect.signature(HTTPSignatureHandler.__init__)
    assert "component_resolver_class" in init.parameters
    assert callable(HTTPMessageVerifier.validate_created_and_expires)
    assert HTTPSignatureHandler.signature_metadata_parameters == {
        "alg",
        "created",
        "expires",
        "keyid",
        "nonce",
        "tag",
    }


def test_library_resolves_key_and_req():
    """RFC 9421 sections 2.1.2 and 2.4, which the profiles cover, come from the library."""
    message = _rfc9421.Request(
        "GET", "https://example.com/", {"Example-Dict": "a=1, b=2;x=1;y=2, c=(a b c)"}
    )
    base = _rfc9421.build_base(
        message, covered_component_ids=['"example-dict";key="b"'], params={"created": 1}
    )
    # The member value with its parameters.
    assert base.decode().splitlines()[0] == '"example-dict";key="b": 2;x=1;y=2'
    response = _rfc9421.Response(200, _rfc9421.Request("GET", "https://Example.com/p"))
    base = _rfc9421.build_base(
        response, covered_component_ids=['"@authority";req'], params={"created": 1}
    )
    assert base.decode().splitlines()[0] == '"@authority";req: example.com'
    for identifier in ('"@authority";req=?0', '"@authority"'):
        with pytest.raises(ValueError):
            _rfc9421.build_base(response, covered_component_ids=[identifier], params={})


def test_verify_tells_a_base_failure_from_a_bad_signature():
    key = ed25519.Ed25519PrivateKey.generate()
    message = _rfc9421.Request("POST", "https://example.com/p", {"x-a": "1"})
    signature_input, signature = _rfc9421.sign(
        message,
        alg="ed25519",
        private_key=key,
        key_id="k",
        label="sig1",
        created=10,
        expires=20,
        nonce="n",
        tag="t",
        covered_component_ids=['"x-a"'],
    )
    fields = {"Signature-Input": signature_input, "Signature": signature}

    def verify(headers):
        return _rfc9421.verify(
            _rfc9421.Request("POST", "https://example.com/p", headers),
            label="sig1",
            tag="t",
            alg="ed25519",
            public_key=key.public_key(),
            window_checked=True,
            parameters=("created", "expires", "keyid", "alg", "nonce", "tag"),
        )

    with pytest.raises(_rfc9421.SignatureBaseError):
        verify(fields)
    with pytest.raises(_rfc9421.SignatureMismatchError):
        verify({**fields, "x-a": "2"})
    # The library's own window check is off: the profile checks its window.
    assert verify({**fields, "x-a": "1"})['"x-a"'] == "1"


def test_http_sf_reports_every_duplicate():
    calls = []
    http_sf.parse(
        b'a=1, a=2, b=("x";p=1;p=2);q=1;q=2',
        tltype="dictionary",
        on_duplicate_key=lambda key, context: calls.append((key, context)),
    )
    assert calls == [("a", "dictionary"), ("p", "parameter"), ("q", "parameter")]


def test_http_sf_primitives_the_profile_selection_walk_uses():
    """``dictionary_has_parameter`` walks members with these http-sf functions."""
    from http_sf.bare_item import parse_bare_item
    from http_sf.item import parse_item
    from http_sf.state import ParserState
    from http_sf.util import discard_http_ows, discard_ows, parse_key

    state = ParserState(b'  sig1=("a";k=1);tag="x"')
    discard_ows(state)
    discard_http_ows(state)
    assert parse_key(state) == "sig1"
    state.cursor += 2  # "=("
    assert parse_item(state) == ("a", {"k": 1})
    state.cursor += 2  # ");"
    assert parse_key(state) == "tag"
    state.cursor += 1
    assert parse_bare_item(state) == "x"
    assert _rfc9421.dictionary_has_parameter(
        '\tsig1=("a");tag="web-bot-auth";nonce="x', "tag", "web-bot-auth"
    )


def test_first_spelling_of_a_header_wins_as_in_lookup():
    from adcp.signing.canonical import _lookup

    headers = {"Content-Digest": "first", "content-digest": "second"}
    assert _rfc9421.Request("GET", "https://example.com/", headers).headers[
        "content-digest"
    ] == _lookup(headers, "content-digest")


def test_verify_requires_the_caller_to_have_checked_the_window():
    with pytest.raises(ValueError, match="window"):
        _rfc9421.verify(
            _rfc9421.Request("GET", "https://example.com/"),
            label="sig1",
            tag="t",
            alg="ed25519",
            public_key=None,
            window_checked=False,
        )


@pytest.mark.parametrize("query", ["a={b}", "a=b|c"])
def test_a_url_with_a_raw_non_uri_character_is_refused_with_the_fix(query):
    """``http-message-signatures`` parses the URL per RFC 3986 and refuses ``{``, ``}``, ``|``.

    Both signers say to percent-encode the URL, as ``httpx`` does for ``params=``.
    """
    from adcp.signing import sign_request, sign_wba_request

    key = ed25519.Ed25519PrivateKey.generate()
    url = f"https://seller.example.com/mcp?{query}"
    with pytest.raises(ValueError, match="Percent-encode"):
        sign_request(
            method="POST",
            url=url,
            headers={"Content-Type": "application/json"},
            body=b"{}",
            private_key=key,
            key_id="k",
            alg="ed25519",
            signing_profile_version="3.2",
        )
    with pytest.raises(ValueError, match="Percent-encode"):
        sign_wba_request(
            method="GET",
            url=url,
            headers={},
            body=b"",
            private_key=key,
            agent_origin="https://buyer.example.com",
        )
