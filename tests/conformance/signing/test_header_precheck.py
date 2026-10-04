"""Step-1 strict rejection, graded in the form the vectors cannot express.

`negative/021`, `022`, `023` and `026` each ship their malformed shape as a
single comma-joined header value, because a JSON vector has nowhere to put two
lines with the same name. But the threat their own `$comment`s describe is a
proxy inserting a **second header line** — and every mapping view of headers
resolves that to one value before any check runs, so a gate written over the
mapping passes all four vectors while missing the attack entirely.

These tests drive the two-line form. Without them the conformance suite is
green and the threat is open.
"""

from __future__ import annotations

import pytest

from adcp.signing import (
    InMemoryReplayStore,
    SignatureVerificationError,
    VerifierCapability,
    VerifyOptions,
    verify_request_signature,
)
from adcp.signing.errors import REQUEST_SIGNATURE_HEADER_MALFORMED

_URL = "https://seller.example.com/adcp/create_media_buy"
_SIG_INPUT = (
    'sig1=("@method" "@target-uri" "@authority" "content-type");created=1776520800;'
    'expires=1776521100;nonce="KXYnfEfJ0PBRZXQyVXfVQA";keyid="test-ed25519-2026";'
    'alg="ed25519";tag="adcp/request-signing/v1"'
)
_SIG = "sig1=:" + "A" * 86 + ":"


def _options() -> VerifyOptions:
    return VerifyOptions(
        now=1776520800.0,
        capability=VerifierCapability(),
        operation="create_media_buy",
        jwks_resolver=lambda keyid: None,
        replay_store=InMemoryReplayStore(),
    )


def _verify(raw: list[tuple[bytes, bytes]] | None, headers: dict[str, str]) -> None:
    verify_request_signature(
        method="POST",
        url=_URL,
        headers=headers,
        body=b'{"plan_id":"plan_001"}',
        options=_options(),
        raw_headers=raw,
    )


def test_second_content_type_line_is_rejected_at_step_1() -> None:
    """The attack the vectors describe but cannot express.

    A proxy appends a second `Content-Type`. `dict(headers)` keeps exactly one
    of them — first or last depending on the framework — so the signed view and
    the parsed view can disagree while every conformance vector still passes.
    """
    raw = [
        (b"content-type", b"application/json"),
        (b"content-type", b"text/plain"),
        (b"signature-input", _SIG_INPUT.encode()),
        (b"signature", _SIG.encode()),
    ]
    # The mapping a framework would hand us has already lost the second line.
    collapsed = {
        "Content-Type": "application/json",
        "Signature-Input": _SIG_INPUT,
        "Signature": _SIG,
    }

    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(raw, collapsed)
    assert exc_info.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert exc_info.value.step == 1


def test_second_signature_input_line_is_rejected_at_step_1() -> None:
    """Two `Signature-Input` lines are the covered-component smuggling vector.

    Same shape as `negative/021`'s duplicate dictionary key, arriving by the
    other route: a second line whose component list is shorter than the one the
    producer signed.
    """
    weaker = 'sig1=("@method" "@target-uri");created=1776520800;expires=1776521100;nonce="AAAAAAAAAAAAAAAAAAAAAA";keyid="test-ed25519-2026";alg="ed25519";tag="adcp/request-signing/v1"'
    raw = [
        (b"content-type", b"application/json"),
        (b"signature-input", _SIG_INPUT.encode()),
        (b"signature-input", weaker.encode()),
        (b"signature", _SIG.encode()),
    ]
    collapsed = {
        "Content-Type": "application/json",
        "Signature-Input": _SIG_INPUT,
        "Signature": _SIG,
    }

    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(raw, collapsed)
    assert exc_info.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert exc_info.value.step == 1


def test_repeated_line_is_invisible_without_raw_headers() -> None:
    """The documented limit of the mapping-only path, pinned so it stays honest.

    This is not a bug being enshrined -- it is the reason `raw_headers` exists.
    If this ever starts raising, the mapping grew the ability to carry a
    repeated name and the docstring on `verify_request_signature` is stale.
    """
    collapsed = {
        "Content-Type": "application/json",
        "Signature-Input": _SIG_INPUT,
        "Signature": _SIG,
    }
    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(None, collapsed)
    # Fails later, on the unknown key -- NOT at step 1 as malformed.
    assert exc_info.value.code != REQUEST_SIGNATURE_HEADER_MALFORMED


def test_non_ascii_host_header_is_rejected_even_when_the_url_is_clean() -> None:
    """The real-traffic shape of `negative/026`, which no vector can carry.

    ASGI frameworks drop a non-ASCII Host when building `request.url`
    (Starlette's `URL` falls back to `scope["server"]` when the Host header
    fails its host regex), so in production the U-label survives only on the
    header. A gate that checked the URL alone would pass vector 026 -- which
    ships no Host header -- and miss every real request.
    """
    raw = [
        (b"host", "bücher.example.com".encode()),
        (b"content-type", b"application/json"),
        (b"signature-input", _SIG_INPUT.encode()),
        (b"signature", _SIG.encode()),
    ]
    collapsed = {
        "Host": "bücher.example.com",
        "Content-Type": "application/json",
        "Signature-Input": _SIG_INPUT,
        "Signature": _SIG,
    }
    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(raw, collapsed)
    assert exc_info.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert exc_info.value.step == 1


def test_duplicate_label_on_the_signature_header_is_rejected_at_step_1() -> None:
    """Checklist step 1 parses `Signature-Input` AND `Signature` per RFC 9421 §4.

    `negative/021` grades the duplicate dictionary key on `Signature-Input`;
    RFC 9421 §4.2 makes `Signature` a Dictionary by the same construction, and
    it is the field carrying the credential bytes. A parser that retains the
    last value reads a different signature than one that retains the first,
    over a `Signature-Input` both agree on -- the parser-differential RFC 8941
    §3.2 exists to close, on the one header where the disagreement is about the
    credential itself.
    """
    headers = {
        "Content-Type": "application/json",
        "Signature-Input": _SIG_INPUT,
        "Signature": f"sig1=:{'A' * 86}:, sig1=:{'B' * 86}:",
    }
    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(None, headers)
    assert exc_info.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert exc_info.value.step == 1


def test_content_digest_algorithms_differing_only_in_case_are_rejected_at_step_1() -> None:
    """`SHA-256` and `sha-256` are one algorithm, and `negative/023` is the rule.

    RFC 9530 §2 algorithm names are lowercase tokens and RFC 8941 §3.2's `key`
    production admits lowercase only, so an uppercase spelling is never a
    second key. Comparing the keys as written lets the same algorithm appear
    twice and reopens the parser-differential `negative/023` rejects behind a
    change of case.
    """
    digest = (
        "SHA-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:, "
        "sha-256=:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=:"
    )
    headers = {
        "Content-Type": "application/json",
        "Content-Digest": digest,
        "Signature-Input": _SIG_INPUT,
        "Signature": _SIG,
    }
    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(None, headers)
    assert exc_info.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert exc_info.value.step == 1


@pytest.mark.parametrize(
    "host",
    ["::1", "[::1", "[fe80::1%25eth0]", ":443", "user@"],
    ids=["bare-ipv6", "unclosed-bracket", "zone-id", "port-no-host", "userinfo-no-host"],
)
def test_malformed_authority_on_the_host_header_is_rejected_at_step_1(host: str) -> None:
    """The URL canonicalization algorithm's steps 2-3 MUST-reject shapes.

    Step 3 names the four malformed authority shapes and binds comparers to
    reject them; step 2 rejects IPv6 zone identifiers. The signing profile
    derives `@authority` from the as-received `Host` header and canonicalizes
    it by the same algorithm, so the rule governs the header and not only the
    URL -- and the header is the only place these shapes can arrive, since an
    ASGI framework building `request.url` does not carry them over.

    `malformed_authority_reason` is the rule's one definition and
    canonicalization already applies it to the URL. Applying it here is what
    makes the header side refuse the same shapes, at step 1, with step 1's code.
    """
    raw = [
        (b"host", host.encode("latin-1")),
        (b"content-type", b"application/json"),
        (b"signature-input", _SIG_INPUT.encode()),
        (b"signature", _SIG.encode()),
    ]
    collapsed = {
        "Host": host,
        "Content-Type": "application/json",
        "Signature-Input": _SIG_INPUT,
        "Signature": _SIG,
    }
    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(raw, collapsed)
    assert exc_info.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert exc_info.value.step == 1


@pytest.mark.parametrize(
    "host",
    ["seller.example.com", "seller.example.com:8443", "[2001:db8::1]", "[2001:db8::1]:8443"],
    ids=["host", "host-port", "ipv6", "ipv6-port"],
)
def test_well_formed_host_header_is_not_rejected(host: str) -> None:
    """False-positive guard for the rule above.

    A bracketed IPv6 literal and a non-default port are legal authorities that
    `positive/011` and `positive/012` sign. A gate that refused them would
    refuse traffic the profile requires accepting.
    """
    raw = [
        (b"host", host.encode("latin-1")),
        (b"content-type", b"application/json"),
        (b"signature-input", _SIG_INPUT.encode()),
        (b"signature", _SIG.encode()),
    ]
    collapsed = {
        "Host": host,
        "Content-Type": "application/json",
        "Signature-Input": _SIG_INPUT,
        "Signature": _SIG,
    }
    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(raw, collapsed)
    assert exc_info.value.code != REQUEST_SIGNATURE_HEADER_MALFORMED


def test_two_distinct_signature_labels_are_not_rejected() -> None:
    """False-positive guard: `positive/004` ships `sig1` and `sig2`.

    Only a REPEATED key is the defect. A gate that rejected every comma in the
    `Signature` header would refuse the multi-label request the profile
    requires accepting.
    """
    headers = {
        "Content-Type": "application/json",
        "Signature-Input": _SIG_INPUT,
        "Signature": f"sig1=:{'A' * 86}:, sig2=:{'B' * 86}:",
    }
    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(None, headers)
    assert exc_info.value.code != REQUEST_SIGNATURE_HEADER_MALFORMED


def test_multi_algorithm_content_digest_is_not_rejected() -> None:
    """False-positive guard: distinct algorithms are legal, duplicates are not.

    RFC 9530 permits a `Content-Digest` carrying several algorithms. Only a
    repeated *key* is the defect `negative/023` describes. A gate that rejected
    every comma would refuse traffic the spec requires accepting -- worse than
    the bug it closes.
    """
    digest = (
        "sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:, "
        "sha-512=:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=:"
    )
    headers = {
        "Content-Type": "application/json",
        "Content-Digest": digest,
        "Signature-Input": _SIG_INPUT,
        "Signature": _SIG,
    }
    with pytest.raises(SignatureVerificationError) as exc_info:
        _verify(None, headers)
    assert exc_info.value.code != REQUEST_SIGNATURE_HEADER_MALFORMED
