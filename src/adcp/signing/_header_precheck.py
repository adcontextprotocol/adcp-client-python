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

from adcp.signing._rfc9421 import DuplicateFieldError, parse_dictionary, translate_legacy_binary
from adcp.signing.canonical import (
    TargetUriMalformedError,
    _split_or_reject,
    host_has_raw_non_ascii,
    malformed_authority_reason,
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

#: Headers whose value is an RFC 8941 Dictionary, mapped to the spelling a
#: rejection names. A table rather than an if-chain per header: the rule is one
#: rule, and a per-header branch is a per-header chance to omit one.
#:
#: `Signature` is here because checklist step 1 parses `Signature-Input` **and
#: `Signature`** per RFC 9421 §4, and §4.2 makes `Signature` a Dictionary as
#: §4.1 does `Signature-Input`. Omitting it left the field carrying the
#: credential bytes as the one Dictionary whose duplicate key nothing refused.
_DICTIONARY_HEADERS = {
    "signature": "Signature",
    "signature-input": "Signature-Input",
    "signature-agent": "Signature-Agent",
    "content-digest": "Content-Digest",
}


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

    names = [name for name, _ in _header_pairs(headers=headers, raw_headers=None)]
    for name in _SINGLE_LINE_SIGNED_HEADERS | {"signature-agent"}:
        if names.count(name) > 1:
            # A mapping can hold two spellings of one name (``Content-Digest``
            # and ``content-digest``); which one a reader takes is arbitrary.
            return (
                f"{name!r} appears under more than one spelling; a repeated signed "
                "field is ambiguous and MUST be rejected rather than folded"
            )

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
        if name in _SINGLE_VALUE_HEADERS and _has_top_level_comma(value):
            return (
                f"{name!r} carries more than one value; it is a singleton field "
                "and a comma-joined value means two were folded together"
            )
        header_name = _DICTIONARY_HEADERS.get(name)
        if header_name is not None:
            reason = _dictionary_reason(value, header_name)
            if reason is not None:
                return reason

    return _authority_reason(pairs=pairs, url=url)


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


def _has_top_level_comma(value: str) -> bool:
    """Whether a media type carries a comma outside an RFC 9110 quoted-string.

    ``Content-Type`` is not a Structured Field, so this is HTTP field syntax,
    not RFC 8941: a ``boundary="a,b"`` parameter is one value, a bare comma is
    two values folded together.
    """
    quoted = escaped = False
    for character in value:
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == "," and not quoted:
            return True
    return False


def _dictionary_reason(value: str, header_name: str) -> str | None:
    """RFC 8941 §3.2: a duplicate dictionary key MUST be rejected, not resolved.

    "Retaining only the last value" is the other option the RFC allows, and it
    is the one a parser reaches for by default -- `d[key] = v` in a loop. That
    is the smuggling vector: a proxy appends a second entry with the same label
    and a weaker covered-component set, the parser keeps the last, and the
    signature verifies over fewer components than the producer signed.
    ``http-sf`` reports every duplicate key and parameter through its
    ``on_duplicate_key`` hook, which :func:`parse_dictionary` turns into an
    error.

    Keys differing only in case (``SHA-256`` beside ``sha-256``) never reach
    the duplicate check: RFC 8941 §3.2's ``key`` production is lowercase only,
    so the uppercase spelling fails the parse and the field is malformed.
    """
    try:
        try:
            parse_dictionary(value)
        except DuplicateFieldError:
            raise
        except ValueError:
            # The AdCP 3.0/3.1 Base64URL byte-sequence spelling is the
            # decoder's call (3.2 refuses it there, still at step 1), not an
            # ambiguity: judge the field with those tokens translated.
            parse_dictionary(translate_legacy_binary(value))
    except ValueError as exc:
        if isinstance(exc, DuplicateFieldError):
            return (
                f"{header_name} carries a {exc}; RFC 8941 §3.2 requires rejecting rather "
                "than resolving, since resolving lets a repeated key downgrade what was signed"
            )
        return f"{header_name} is malformed: {exc}"
    return None


def _authority_reason(*, pairs: Sequence[tuple[str, str]], url: str) -> str | None:
    """Why the authority this request carries is malformed at step 1, or `None`.

    Two rules, both of which the URL canonicalization algorithm states as a
    comparer MUST:

    * a host carrying raw non-ASCII bytes is refused, never re-normalized
      (step 2). Re-normalizing would pick one of several legitimate UTS-46
      outcomes and risk disagreeing with whoever signed; converting is the
      producer's job.
    * the authority shapes of steps 2-3 -- a bare IPv6 address outside
      brackets, a bracketed host missing its closing bracket, an IPv6 zone
      identifier, userinfo or a port with no host -- are refused as written.
      `malformed_authority_reason` is the one definition of that rule and is
      shared with canonicalization, which raises `request_target_uri_malformed`
      at step 6 over the URL. Same rule, two codes, two steps.

    The non-ASCII rule runs against BOTH the `Host` header and the URL's
    authority, because neither alone is sufficient: ASGI frameworks drop a
    non-ASCII Host when building `request.url` (Starlette's `URL` falls back to
    `scope["server"]` when the Host header fails its host regex), so the U-label
    survives only on the header -- while the conformance vectors carry no Host
    header at all and express the case through the URL. The shape rules run
    against the header for the same reason the non-ASCII rule does, and over the
    URL they are canonicalization's rejection to make with its own code.
    """
    for name, value in pairs:
        if name != "host":
            continue
        if host_has_raw_non_ascii(value):
            return (
                "the Host header carries raw non-ASCII bytes; the producer must send an "
                "A-label, and a comparer that re-normalized could disagree with the signer"
            )
        shape = malformed_authority_reason(value)
        if shape is not None:
            return f"the Host header carries a malformed authority: {shape}"
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
