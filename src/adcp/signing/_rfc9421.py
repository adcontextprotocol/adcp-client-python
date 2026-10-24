"""RFC 9421 and RFC 8941 plumbing shared by every signing profile in the SDK.

The SDK writes no RFC 9421 or RFC 8941 grammar of its own:

* ``http-message-signatures`` builds every signature base, assembles
  ``Signature-Input``, and signs and verifies with its algorithm classes
  (``ed25519`` and ``ecdsa-p256-sha256``).
* ``http-sf`` parses and serializes Structured Fields, and reports a
  duplicate dictionary key or parameter through ``on_duplicate_key`` so the
  SDK can refuse it instead of keeping the last value.

What stays here is profile policy, expressed through the library's public
API:

* Every profile resolves components with the library's
  ``HTTPSignatureComponentResolver``, which implements the RFC 9421 ``;key``
  (section 2.1.2) and ``;req`` (section 2.4) component parameters. The AdCP
  RFC 9421 profile hands the library its canonical target URI
  (:func:`adcp.signing.canonical.canonicalize_target_uri`), so ``@target-uri``
  and ``@authority`` are the AdCP values; Web Bot Auth hands it the target
  URI as received.
* :class:`ProfileVerifier` turns off the library's own wall-clock window
  check, because every profile checks its window against an injected clock
  with its own error codes.
* :func:`prepare` and :func:`apply` are ``HTTPMessageSigner.prepare`` and
  ``apply``, for the async (:class:`~adcp.signing.SigningProvider`) path,
  where the signature is awaited.

:func:`build_base` reaches the library's protected ``_build_signature_base``,
because the library has no public call that builds a base for parameters the
caller supplies in their own order.
``tests/conformance/signing/test_rfc9421_library_contract.py`` pins the
library shapes this module relies on.
"""

from __future__ import annotations

import base64
import binascii
from collections import UserString
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import http_sf
from http_message_signatures import (
    HTTPMessageSigner,
    HTTPMessageVerifier,
    HTTPSignatureComponentResolver,
    HTTPSignatureKeyResolver,
    InvalidSignature,
)
from http_message_signatures.algorithms import (
    ECDSA_P256_SHA256,
    ED25519,
    HTTPSignatureAlgorithm,
)
from http_message_signatures.exceptions import HTTPMessageSignaturesException
from http_message_signatures.structures import CaseInsensitiveDict, PreparedSignature
from http_sf.bare_item import parse_bare_item
from http_sf.innerlist import ser_item_or_inner_list
from http_sf.item import parse_item, ser_item
from http_sf.state import ParserState
from http_sf.util import discard_http_ows, discard_ows, parse_key

#: RFC 9421 algorithm names mapped to the library's algorithm classes. Each
#: profile narrows this to its own allow-list before anything reaches it.
ALGORITHMS: dict[str, type[HTTPSignatureAlgorithm]] = {
    algorithm.algorithm_id: algorithm for algorithm in (ED25519, ECDSA_P256_SHA256)
}


# ---------------------------------------------------------------------------
# Structured Fields (RFC 8941 / RFC 9651) via http-sf
# ---------------------------------------------------------------------------


_EQUALS, _SEMICOLON, _COMMA = ord("="), ord(";"), ord(",")
_PAREN_OPEN, _PAREN_CLOSE = ord("("), ord(")")


class DuplicateFieldError(ValueError):
    """A Structured Field repeats a dictionary key or a parameter."""


def _has_data(state: ParserState) -> bool:
    """Whether the cursor has input left to read.

    http-sf 1.5.0 removed ``ParserState.has_data()``; this is the same check
    its own parsers now inline, so the walk below works on 1.4.x and 1.5.x.
    """
    return state.cursor < len(state.data)


def _refuse_duplicate(key: str, context: str) -> None:
    where = "dictionary member" if context == "dictionary" else "parameter"
    raise DuplicateFieldError(f"duplicate Structured Field {where} {key!r}")


def parse_dictionary(value: str) -> dict[str, Any]:
    """Parse an RFC 8941 Dictionary, refusing duplicate keys and parameters.

    RFC 8941 section 4.2.2 lets a parser keep the last of two duplicate
    keys. That is how a proxy-appended member downgrades what was signed, so
    every duplicate is an error here. Members come back in http-sf's shape:
    ``(value, params)`` for an item and ``([items], params)`` for an inner
    list. Raises ``ValueError`` for anything that does not parse, including
    an empty field.
    """
    if not value.isascii():
        raise ValueError("Structured Fields must be ASCII")
    try:
        parsed = http_sf.parse(
            value.encode("ascii"), tltype="dictionary", on_duplicate_key=_refuse_duplicate
        )
    except http_sf.StructuredFieldError as exc:
        raise ValueError(f"malformed Structured Field: {exc}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("empty Structured Field dictionary")
    return parsed


def serialize(value: Any) -> str:
    """Serialize a Structured Field value (an http-sf structure)."""
    return http_sf.ser(value)


def signature_input_field(label: str, member: str) -> str:
    """A ``Signature-Input`` value holding one member, serialized by http-sf.

    ``member`` is that member's own serialization, the ``@signature-params``
    value. Raises ``ValueError`` when ``label`` is not an RFC 8941 key.
    """
    try:
        parsed = http_sf.parse(member.encode("ascii"), tltype="list")
    except (http_sf.StructuredFieldError, ValueError) as exc:
        raise ValueError(f"malformed signature parameters: {exc}") from exc
    if not isinstance(parsed, list) or len(parsed) != 1:
        raise ValueError("signature parameters are not one member")
    return serialize({label: parsed[0]})


def component_identifier(item: Any) -> str:
    """The serialized component identifier, e.g. ``"signature-agent";key="sig1"``."""
    return ser_item(item)


def is_inner_list(member: Any) -> bool:
    return isinstance(member, tuple) and len(member) == 2 and isinstance(member[0], list)


def dictionary_has_parameter(value: str, name: str, expected: str) -> bool:
    """Whether any Dictionary member carries ``name=expected`` as a member parameter.

    Profile selection must see an explicit marker even where the strict
    parse would discard it: in a member a later duplicate overwrites, in a
    duplicated parameter, or before a syntax error. Every RFC 8941 parser
    keeps the last duplicate or stops at the first error, so this walks the
    Dictionary member by member with http-sf's own key, item and bare-item
    parsers and looks at each member parameter as it is read. It never
    verifies anything; a field it reads still has to pass
    :func:`parse_dictionary`. Component parameters inside an inner list and
    text inside strings are not member parameters and never match.
    """
    # Non-ASCII cannot be RFC 8941; "?" stands in for it so the walk stops there.
    state = ParserState(value.encode("ascii", "replace"))
    try:
        discard_http_ows(state)
        while _has_data(state):
            parse_key(state)
            if _has_data(state) and state.data[state.cursor] == _EQUALS:
                state.cursor += 1
                if _has_data(state) and state.data[state.cursor] == _PAREN_OPEN:
                    state.cursor += 1
                    while True:
                        discard_ows(state)
                        if state.data[state.cursor] == _PAREN_CLOSE:
                            state.cursor += 1
                            break
                        parse_item(state)
                else:
                    parse_bare_item(state)
            while _has_data(state) and state.data[state.cursor] == _SEMICOLON:
                state.cursor += 1
                discard_ows(state)
                key = parse_key(state)
                parameter: Any = True
                if _has_data(state) and state.data[state.cursor] == _EQUALS:
                    state.cursor += 1
                    parameter = parse_bare_item(state)
                if key == name and isinstance(parameter, (str, UserString)):
                    if parameter == expected:
                        return True
            discard_http_ows(state)
            if not _has_data(state) or state.data[state.cursor] != _COMMA:
                break
            state.cursor += 1
            discard_http_ows(state)
    except (http_sf.StructuredFieldError, IndexError):
        # A syntax error ends the walk; a marker already seen was returned.
        pass
    return False


# ---------------------------------------------------------------------------
# AdCP 3.0/3.1 legacy Base64URL sf-binary
# ---------------------------------------------------------------------------

_BASE64URL_ONLY = frozenset("-_")
_BASE64_ONLY = frozenset("+/")
_LEGACY_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=-_")


def _binary_spans(value: str) -> list[tuple[int, int, str | None]]:
    """``(start, end, owner)`` of every ``:...:`` token outside sf-strings.

    ``value[start:end]`` is the token between its colons. ``owner`` is the
    top-level dictionary key the token is the value of, or ``None`` for a
    token in a parameter.
    """
    spans: list[tuple[int, int, str | None]] = []
    index, length, key_start = 0, len(value), 0
    while index < length:
        char = value[index]
        if char == '"':
            index += 1
            while index < length and value[index] != '"':
                index += 2 if value[index] == "\\" else 1
        elif char == ":":
            end = value.find(":", index + 1)
            if end < 0:
                break
            owner = None
            if index > 0 and value[index - 1] == "=":
                candidate = value[key_start : index - 1].strip()
                owner = candidate if candidate and ";" not in candidate else None
            spans.append((index + 1, end, owner))
            index = end
        elif char == ",":
            key_start = index + 1
        index += 1
    return spans


def legacy_binary_tokens(value: str) -> list[tuple[str | None, str]]:
    """Every byte-sequence token outside sf-strings, with the member key it belongs to.

    AdCP 3.0 and 3.1 emitted ``Signature`` and ``Content-Digest`` values in
    unpadded Base64URL. No RFC 8941 parser accepts that alphabet (section
    3.3.5 is standard Base64), so the legacy profiles look at, and translate,
    those tokens before the strict http-sf parse.
    """
    return [(owner, value[start:end]) for start, end, owner in _binary_spans(value)]


def translate_legacy_binary(value: str) -> str:
    """Rewrite Base64URL ``:...:`` tokens as RFC 8941 standard Base64.

    Only a token made of Base64 characters that decodes is rewritten; a token
    already in standard Base64 is left alone, so a field in RFC 8941 form
    parses unchanged, and anything else is left for the strict parse to
    refuse. Only the legacy profiles call this.
    """
    out, cursor = [], 0
    for start, end, _owner in _binary_spans(value):
        token = value[start:end]
        if set(token) <= _LEGACY_ALPHABET and (_BASE64URL_ONLY & set(token) or len(token) % 4):
            standard = token.replace("-", "+").replace("_", "/").rstrip("=")
            try:
                raw = base64.b64decode(standard + "=" * (-len(standard) % 4), validate=True)
                token = base64.b64encode(raw).decode("ascii")
            except (binascii.Error, ValueError):
                # Not Base64 in either alphabet: the strict parse refuses it.
                pass
        out.append(value[cursor:start] + token)
        cursor = end
    out.append(value[cursor:])
    return "".join(out)


def mixes_base64_alphabets(token: str) -> bool:
    """Whether a byte-sequence token uses characters only one alphabet has from each."""
    characters = set(token)
    return bool(characters & _BASE64_ONLY) and bool(characters & _BASE64URL_ONLY)


# ---------------------------------------------------------------------------
# Messages and component resolution
# ---------------------------------------------------------------------------


@dataclass
class Request:
    """The request shape ``http-message-signatures`` reads: method, URL, headers.

    Header names are case-insensitive (the library's ``CaseInsensitiveDict``),
    so the library finds ``Signature-Input`` however the framework spelled it.
    """

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.headers = _first_wins(self.headers)


def _first_wins(headers: Mapping[str, str]) -> Any:
    """Case-insensitive headers where the first spelling of a name wins.

    The same rule as ``canonical._lookup``, which the profiles use for their
    own checks, so the library and the profile never read two different
    values for one name (``Content-Digest`` beside ``content-digest``).
    """
    folded: dict[str, str] = {}
    for name, value in headers.items():
        folded.setdefault(name.lower(), value)
    return CaseInsensitiveDict(folded)


@dataclass
class Response:
    """A response with the request it answers, for ``;req`` components."""

    status_code: int
    request: Request
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.headers = _first_wins(self.headers)


# ---------------------------------------------------------------------------
# Signing and verification on the library
# ---------------------------------------------------------------------------


class _Key(HTTPSignatureKeyResolver):
    """The one key a profile already selected under its own key policy."""

    def __init__(self, *, private_key: Any = None, public_key: Any = None) -> None:
        self._private_key = private_key
        self._public_key = public_key

    def resolve_private_key(self, key_id: str) -> Any:
        return self._private_key

    def resolve_public_key(self, key_id: str) -> Any:
        return self._public_key


def _signer(alg: str, private_key: Any = None) -> HTTPMessageSigner:
    return HTTPMessageSigner(
        signature_algorithm=ALGORITHMS[alg],
        key_resolver=_Key(private_key=private_key) if private_key is not None else None,
    )


def build_base(
    message: Request | Response,
    *,
    covered_component_ids: Sequence[str],
    params: Mapping[str, Any],
) -> bytes:
    """The RFC 9421 signature base for these components and parameters.

    ``params`` are serialized in the order given. A component that does not
    resolve raises ``ValueError``.
    """
    signer = _signer(ED25519.algorithm_id)
    try:
        base, _, _ = signer._build_signature_base(
            message,
            covered_component_ids=signer._parse_covered_component_ids(covered_component_ids),
            signature_params=dict(params),
        )
    except HTTPMessageSignaturesException as exc:
        raise ValueError(str(exc)) from exc
    return str(base).encode("utf-8")


def _check_signable(message: Request | Response) -> None:
    """Refuse a request the library can't resolve, and say how to fix its URL.

    The library parses the URL strictly (RFC 3986). A raw ``{``, ``}``, or
    ``|`` in a query is not a URI character, so the caller percent-encodes
    it, as ``httpx`` does for ``params=``.
    """
    try:
        HTTPSignatureComponentResolver(message)
    except HTTPMessageSignaturesException as exc:
        raise ValueError(
            f"cannot sign this request: {exc}. Percent-encode every character RFC 3986 "
            "doesn't allow in a URL, such as a raw '{', '}', or '|' in the query "
            "(httpx params= encodes them)"
        ) from exc


def _timestamp(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def sign(
    message: Request | Response,
    *,
    alg: str,
    private_key: Any,
    key_id: str,
    label: str,
    created: int,
    expires: int | None,
    nonce: str | None,
    tag: str,
    covered_component_ids: Sequence[str],
    append: bool = False,
) -> tuple[str, str]:
    """Sign ``message`` with ``HTTPMessageSigner.sign``; return the two fields.

    Returns ``(Signature-Input, Signature)``. ``Signature`` is RFC 8941
    standard Base64; a legacy profile re-encodes it. With ``append``, a
    signature already on ``message`` stays, and this one is added beside it.
    """
    _check_signable(message)
    try:
        _signer(alg, private_key).sign(
            message,
            key_id=key_id,
            created=_timestamp(created),
            expires=_timestamp(expires) if expires is not None else None,
            nonce=nonce,
            label=label,
            tag=tag,
            covered_component_ids=covered_component_ids,
            append_if_signature_exists=append,
        )
    except HTTPMessageSignaturesException as exc:
        raise ValueError(str(exc)) from exc
    return message.headers["Signature-Input"], message.headers["Signature"]


def prepare(
    message: Request | Response,
    *,
    alg: str,
    key_id: str,
    label: str,
    created: int,
    expires: int | None,
    nonce: str | None,
    tag: str,
    covered_component_ids: Sequence[str],
) -> PreparedSignature:
    """``HTTPMessageSigner.prepare``: what :func:`sign` would sign, unsigned.

    The signature over ``signature_base`` goes to :func:`apply`. Used where
    the signature itself is awaited.
    """
    _check_signable(message)
    try:
        return _signer(alg).prepare(
            message,
            key_id=key_id,
            created=_timestamp(created),
            expires=_timestamp(expires) if expires is not None else None,
            nonce=nonce,
            label=label,
            tag=tag,
            covered_component_ids=covered_component_ids,
        )
    except HTTPMessageSignaturesException as exc:
        raise ValueError(str(exc)) from exc


def apply(
    message: Request | Response, prepared: PreparedSignature, signature: bytes, *, alg: str
) -> tuple[str, str]:
    """``HTTPMessageSigner.apply``: put ``signature`` on ``message``; return the two fields."""
    try:
        _signer(alg).apply(message, prepared, signature)
    except HTTPMessageSignaturesException as exc:
        raise ValueError(str(exc)) from exc
    return message.headers["Signature-Input"], message.headers["Signature"]


class ProfileVerifier(HTTPMessageVerifier):
    """``HTTPMessageVerifier`` with the window left to the profile.

    The library compares ``created`` and ``expires`` with the real wall
    clock and a 5-second skew. Each profile has already checked its window
    against the verifier's injected clock, with its own skew and error codes,
    before this runs. Unknown signature parameters are the profile's call
    too, so the verifier is told which ones the signature carries.
    """

    def validate_created_and_expires(self, sig_input: Any, max_age: Any = None) -> None:
        return None


class SignatureBaseError(ValueError):
    """The signature base could not be built: a component does not resolve."""

    def __init__(self, reason: str, *, status: int = 403, cause: BaseException | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.cause = cause


class SignatureMismatchError(ValueError):
    """The signature does not verify over the base."""


def verify(
    message: Request | Response,
    *,
    label: str,
    tag: str,
    alg: str,
    public_key: Any,
    window_checked: bool,
    parameters: Sequence[str] = (),
) -> Mapping[str, str]:
    """Verify ``label`` with ``HTTPMessageVerifier``; return the covered components.

    ``message.headers`` must carry the ``Signature-Input`` and ``Signature``
    fields to verify. ``alg`` and ``public_key`` are the profile's choice;
    the library confirms the signature names the same ``alg``. Raises
    :class:`SignatureBaseError` when the signature base can't be built and
    :class:`SignatureMismatchError` when the signature does not verify.

    ``window_checked`` must be true: :class:`ProfileVerifier` does not check
    ``created``/``expires``, so the caller states it already has.
    """
    if window_checked is not True:
        raise ValueError("check the signature window before verifying")
    try:
        # A key that doesn't fit the algorithm can't verify this signature;
        # refusing it here leaves the library's errors to the base and the
        # signature.
        ALGORITHMS[alg](public_key=public_key)
    except HTTPMessageSignaturesException as exc:
        raise SignatureMismatchError(str(exc)) from exc
    verifier = ProfileVerifier(
        signature_algorithm=ALGORITHMS[alg],
        key_resolver=_Key(public_key=public_key),
    )
    # The profile has already decided which parameters it accepts.
    setattr(  # a per-verifier value of the library class variable
        verifier,
        "signature_metadata_parameters",
        HTTPMessageVerifier.signature_metadata_parameters | set(parameters),
    )
    try:
        (result,) = verifier.verify(message, expect_tag=tag, expect_label=label)
    except InvalidSignature as exc:
        cause = exc.__cause__
        from cryptography.exceptions import InvalidSignature as CryptoInvalidSignature

        if cause is not None and not isinstance(cause, CryptoInvalidSignature):
            # The library could not build the signature base.
            raise SignatureBaseError(str(cause), cause=cause) from exc
        raise SignatureMismatchError("signature did not verify") from exc
    return dict(result.covered_components)


__all__ = [
    "ALGORITHMS",
    "DuplicateFieldError",
    "ProfileVerifier",
    "Request",
    "Response",
    "SignatureBaseError",
    "SignatureMismatchError",
    "apply",
    "build_base",
    "component_identifier",
    "dictionary_has_parameter",
    "is_inner_list",
    "legacy_binary_tokens",
    "mixes_base64_alphabets",
    "parse_dictionary",
    "prepare",
    "ser_item_or_inner_list",
    "serialize",
    "sign",
    "translate_legacy_binary",
    "verify",
]
