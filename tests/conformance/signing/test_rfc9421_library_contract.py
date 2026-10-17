"""The http-message-signatures and http-sf shapes adcp.signing._rfc9421 relies on.

The SDK takes RFC 9421 from ``http-message-signatures`` and RFC 8941 from
``http-sf``. It plugs in through public extension points (the
``component_resolver_class``, ``validate_created_and_expires``,
``signature_metadata_parameters``) and two protected subclass hooks
(``_build_signature_base``, ``_parse_covered_component_ids``). A library
release that moves any of them fails here, before it fails a profile test
with a confusing error code.
"""

from __future__ import annotations

import inspect

import http_sf
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from http_message_signatures import (
    HTTPMessageSigner,
    HTTPMessageVerifier,
    HTTPSignatureComponentResolver,
    InvalidSignature,
)
from http_message_signatures.signatures import HTTPSignatureHandler

from adcp.signing import _rfc9421


def test_protected_hooks_keep_their_signatures():
    build = inspect.signature(HTTPSignatureHandler._build_signature_base)
    assert list(build.parameters) == [
        "self",
        "message",
        "covered_component_ids",
        "signature_params",
    ]
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


def test_resolver_receives_nodes_with_value_and_params():
    seen = []

    class Recording(_rfc9421.ComponentResolver):
        def resolve(self, component_node):
            seen.append((component_node.value, dict(component_node.params)))
            return super().resolve(component_node)

    message = _rfc9421.Request(
        "GET", "https://example.com/", {"Example-Dict": "a=1, b=2;x=1;y=2, c=(a b c)"}
    )
    base = _rfc9421.build_base(
        message,
        covered_component_ids=['"example-dict";key="b"', '"@authority"'],
        params={"created": 1},
        resolver=Recording,
    )
    assert seen == [("example-dict", {"key": "b"}), ("@authority", {})]
    # RFC 9421 section 2.1.2: the member value with its parameters.
    assert base.decode().splitlines()[0] == '"example-dict";key="b": 2;x=1;y=2'


def test_sign_emits_the_parameter_order_signature_base_reproduces():
    key = ed25519.Ed25519PrivateKey.generate()
    covered = ['"@method"', '"@authority"']
    signed = _rfc9421.Request("POST", "https://example.com/p")
    signature_input, signature = _rfc9421.sign(
        signed,
        alg="ed25519",
        private_key=key,
        key_id="k",
        label="sig1",
        created=10,
        expires=20,
        nonce="n",
        tag="t",
        covered_component_ids=covered,
        resolver=_rfc9421.ComponentResolver,
    )
    assert signature_input == (
        'sig1=("@method" "@authority");created=10;keyid="k";alg="ed25519";'
        'expires=20;nonce="n";tag="t"'
    )
    base, member = _rfc9421.signature_base(
        _rfc9421.Request("POST", "https://example.com/p"),
        alg="ed25519",
        key_id="k",
        created=10,
        expires=20,
        nonce="n",
        tag="t",
        covered_component_ids=covered,
        resolver=_rfc9421.ComponentResolver,
    )
    assert signature_input == f"sig1={member}"
    assert _rfc9421.parse_dictionary(signature)["sig1"][0] == key.sign(base)


def test_verify_chains_the_component_error_and_the_crypto_error():
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
        resolver=_rfc9421.ComponentResolver,
    )
    fields = {"Signature-Input": signature_input, "Signature": signature}
    verifier = _rfc9421.ProfileVerifier(
        signature_algorithm=_rfc9421.ALGORITHMS["ed25519"],
        key_resolver=_rfc9421._Key(public_key=key.public_key()),
        component_resolver_class=_rfc9421.ComponentResolver,
    )
    with pytest.raises(InvalidSignature) as missing:
        verifier.verify(_rfc9421.Request("POST", "https://example.com/p", fields), expect_tag="t")
    assert isinstance(missing.value.__cause__, _rfc9421.ComponentError)
    with pytest.raises(InvalidSignature) as tampered:
        verifier.verify(
            _rfc9421.Request("POST", "https://example.com/p", {**fields, "x-a": "2"}),
            expect_tag="t",
        )
    from cryptography.exceptions import InvalidSignature as CryptoInvalidSignature

    assert isinstance(tampered.value.__cause__, CryptoInvalidSignature)
    # The library's own window check is off: the profile checks its window.
    verifier.verify(
        _rfc9421.Request("POST", "https://example.com/p", {**fields, "x-a": "1"}), expect_tag="t"
    )


def test_library_resolver_still_lacks_key_and_req():
    """pyauth/http-message-signatures#29: drop the key/req branches once fixed."""
    resolver = HTTPSignatureComponentResolver(
        _rfc9421.Request("GET", "https://example.com/", {"d": "a=1, b=2"})
    )
    node = HTTPMessageSigner._parse_covered_component_ids(None, ['"d";key="a"'])[0]
    if resolver.resolve(node) == "1":  # pragma: no cover - the upstream fix landed
        pytest.fail("the library now resolves ;key; remove the ComponentResolver branch")


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


def test_req_must_be_a_true_flag():
    response = _rfc9421.Response(200, _rfc9421.Request("GET", "https://example.com/"))
    for identifier in ('"@authority";req=?0', '"@authority";req=1'):
        with pytest.raises(ValueError, match="boolean flag"):
            _rfc9421.build_base(
                response,
                covered_component_ids=[identifier],
                params={},
                resolver=_rfc9421.ComponentResolver,
            )
    with pytest.raises(ValueError, match="only applies to a response"):
        _rfc9421.build_base(
            _rfc9421.Request("GET", "https://example.com/"),
            covered_component_ids=['"@authority";req'],
            params={},
            resolver=_rfc9421.ComponentResolver,
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
            resolver=_rfc9421.ComponentResolver,
            window_checked=False,
        )
