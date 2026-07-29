"""RFC 9421 checklist step 1: strict structured-field rejection.

The verifier's later stages are permissive by design -- they resolve ambiguity
so that well-formed-but-unusual input still verifies. That is wrong at step 1.
Input that is *ambiguous* must be refused before anything downstream picks an
interpretation, because "pick one" is exactly what an attacker inserting a
second header line is counting on.

The rules are a predicate table rather than an if-chain: the same predicate can
be reused at another call site that raises a different code at a different step.
`host_has_raw_non_ascii` is shared with `canonical` for precisely that reason --
canonicalization rejects a malformed authority as `request_target_uri_malformed`
at step 6, while a U-label arriving on the wire is `request_signature_header_malformed`
at step 1. Same rule, two codes, two steps.

## What "raw headers" buys, and where it does not

The single-value rules below are only as strong as the header view they run
over. Every dict view of headers resolves a repeated name to ONE value -- first
or last depending on the framework -- so a proxy-inserted second line vanishes
before any check runs. Passing `raw_headers` preserves the repetition and lets
rule `repeated-line` see it.

This works on ASGI/Starlette, where `request.headers.raw` is the wire order.
It does NOT work on WSGI/Flask, and that is not fixable here: PEP 3333 folds
repeated `HTTP_*` headers into a comma-joined string and writes `CONTENT_TYPE`
and `CONTENT_LENGTH` to bare environ keys with last-wins and no join. The
repetition is destroyed one layer above this SDK. A Flask `raw_headers` list
therefore carries the same information as the mapping, and the `repeated-line`
rule is inert there -- the comma-joined rules below still apply.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from adcp.signing.canonical import (
    TargetUriMalformedError,
    _split_or_reject,
    host_has_raw_non_ascii,
    split_structured_field,
)

#: Headers that must arrive exactly once. Each is either a singleton by RFC
#: (`Content-Type`, RFC 9110 §8.3; `Host`, §7.2) or is a signed component whose
#: value the signature is computed over -- where a second line would split the
#: signed view from the parsed view even when RFC 9110 §5.3 would permit
#: combining. Rejecting a legally-split `Signature-Input` or `Content-Digest` is
#: a deliberate profile choice: the ambiguity it removes is worth more than the
#: flexibility it costs, and no known producer splits them.
_SINGLE_LINE_SIGNED_HEADERS = frozenset(
    {"signature", "signature-input", "content-type", "content-digest", "host"}
)

#: Headers whose value must be a single entry, i.e. carry no top-level comma.
#: `Content-Type` is a singleton by RFC 9110 §8.3, so a comma means two values
#: were folded together regardless of whether the signature covers it.
_SINGLE_VALUE_HEADERS = frozenset({"content-type"})


def strict_header_precheck(
    *,
    headers: Mapping[str, str],
    raw_headers: Sequence[tuple[bytes, bytes]] | None,
    url: str,
) -> str | None:
    """Why this request's headers are malformed at step 1, or `None`.

    Returns a reason string rather than raising so the caller owns the error
    type and step number -- the same table is reusable at a call site that
    raises a different code.
    """
    pairs = _header_pairs(headers=headers, raw_headers=raw_headers)

    if raw_headers is not None:
        seen: set[str] = set()
        for name, _ in pairs:
            if name in _SINGLE_LINE_SIGNED_HEADERS and name in seen:
                return (
                    f"{name!r} arrived on more than one header line; a repeated "
                    "signed field is ambiguous and MUST be rejected rather than folded"
                )
            seen.add(name)

    for name, value in pairs:
        if name in _SINGLE_VALUE_HEADERS and len(split_structured_field(value, ",")) > 1:
            return (
                f"{name!r} carries more than one value; it is a singleton field "
                "and a comma-joined value means two were folded together"
            )
        if name == "signature-input":
            reason = _duplicate_dictionary_key_reason(value, "Signature-Input")
            if reason is not None:
                return reason
        if name == "content-digest":
            reason = _duplicate_dictionary_key_reason(value, "Content-Digest")
            if reason is not None:
                return reason

    return _non_ascii_authority_reason(pairs=pairs, url=url)


def _header_pairs(
    *,
    headers: Mapping[str, str],
    raw_headers: Sequence[tuple[bytes, bytes]] | None,
) -> list[tuple[str, str]]:
    """Lower-cased (name, value) pairs, preferring the raw list when supplied.

    The raw list is the only view that preserves a repeated header name; the
    mapping has already resolved it. Undecodable bytes are not an error to
    diagnose here -- they cannot match any rule, so they are skipped and left to
    the framework.
    """
    if raw_headers is None:
        return [(k.lower(), v) for k, v in headers.items()]
    pairs: list[tuple[str, str]] = []
    for raw_name, raw_value in raw_headers:
        try:
            pairs.append((raw_name.decode("latin-1").lower(), raw_value.decode("latin-1")))
        except (UnicodeDecodeError, AttributeError):
            continue
    return pairs


def _duplicate_dictionary_key_reason(value: str, header_name: str) -> str | None:
    """RFC 8941 §3.2: a duplicate dictionary key MUST be rejected, not resolved.

    "Retaining only the last value" is the other option the RFC allows, and it
    is the one a parser reaches for by default -- `d[key] = v` in a loop. That
    is the smuggling vector: a proxy appends a second entry with the same label
    and a weaker covered-component set, the parser keeps the last, and the
    signature verifies over fewer components than the producer signed.
    """
    keys: set[str] = set()
    for entry in split_structured_field(value, ","):
        entry = entry.strip()
        if not entry:
            continue
        key = entry.split("=", 1)[0].strip()
        if not key:
            continue
        if key in keys:
            return (
                f"{header_name} carries duplicate dictionary key {key!r}; RFC 8941 §3.2 "
                "requires rejecting rather than resolving, since resolving lets a "
                "repeated key downgrade what was signed"
            )
        keys.add(key)
    return None


def _non_ascii_authority_reason(*, pairs: Sequence[tuple[str, str]], url: str) -> str | None:
    """A host carrying raw non-ASCII bytes MUST be rejected, never re-normalized.

    Checked against BOTH the `Host` header and the URL's authority, because
    neither alone is sufficient: ASGI frameworks drop a non-ASCII Host when
    building `request.url` (Starlette's `URL` falls back to `scope["server"]`
    when the Host header fails its host regex), so the U-label survives only on
    the header -- while the conformance vectors carry no Host header at all and
    express the case through the URL.

    Re-normalizing instead of rejecting would pick one of several legitimate
    UTS-46 outcomes and risk disagreeing with whoever signed. Converting is the
    producer's job.
    """
    for name, value in pairs:
        if name == "host" and host_has_raw_non_ascii(value):
            return (
                "the Host header carries raw non-ASCII bytes; the producer must send an "
                "A-label, and a comparer that re-normalized could disagree with the signer"
            )
    try:
        netloc = _split_or_reject(url).netloc
    except TargetUriMalformedError:
        # A malformed authority is canonicalization's rejection to make, with
        # its own code at its own step. Not this gate's business -- but it must
        # not escape as an uncaught ValueError either, which is why it is caught
        # here and handed on rather than left to propagate from a bare urlsplit.
        return None
    if host_has_raw_non_ascii(netloc):
        return (
            "the request URL's authority carries raw non-ASCII bytes; the producer must "
            "send an A-label, and a comparer that re-normalized could disagree with the signer"
        )
    return None
