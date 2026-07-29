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
