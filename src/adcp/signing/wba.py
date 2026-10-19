"""Experimental AdCP 3.3 Web Bot Auth transport profile.

Verification establishes an origin identity, never commercial authority. It
needs only the signer's key directory and a replay store
(:func:`verify_wba_request`), so it can run in front of the AdCP server.
Revocation (:func:`check_wba_revocation`), bindings and grants are the
authorization layer that runs after it. Nothing on the signature path reads a
trust record or a revocation list, and the signature path answers only with
Web Bot Auth statuses and headers (DR-0023): 400, 403 with or without
``Accept-Signature``, and 429.

The profile is adcontextprotocol/adcp#7894 (``wba-profile.mdx``): one
``web-bot-auth`` signature and one ``Signature-Agent`` member per request,
``ed25519`` keys only, a 64-byte nonce on every request, and key
directories signed per WBA Appendix B.1. RFC 9421 itself comes from
``http-message-signatures`` (signature bases, signing, verification) and
``http-sf`` (Structured Fields); this module keeps the profile rules.

JWS artifacts (governance contexts, designated-task responses, rights
attestations) from an issuer that a trust record lists are verified with that
origin's key directory under ``specs/agent-identity-3-3.md`` R4
(:mod:`adcp.signing.jws_artifacts`). :func:`verify_wba_governance_issuer`
implements the earlier #7894 draft's governance check, which predates R4.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519
from joserfc.jwk import OKPKey

from adcp.signing import _rfc9421
from adcp.signing._bounded_http import async_read_limited_bytes
from adcp.signing._header_precheck import strict_header_precheck
from adcp.signing._strict_json import StrictJsonError, parse_strict_json
from adcp.signing.canonical import _lookup, canonical_origin
from adcp.signing.crypto import (
    _PRIVATE_JWK_MEMBERS,
    b64url_decode,
    b64url_encode,
    import_jwk,
    public_key_from_jwk,
)
from adcp.signing.digest import compute_content_digest_sha256, content_digest_matches
from adcp.signing.jwks import SSRFValidationError, validate_uri_static
from adcp.signing.publisher_pins import jwk_thumbprint
from adcp.signing.replay import ReplayStore
from adcp.signing.signer_keys import ResolvedSignerKey, SignerKeyResolver, WbaSignerKeyResolver
from adcp.signing.verifier import VerifiedSigner, _claim_replay_nonce

WBA_TAG = "web-bot-auth"
DIRECTORY_TAG = "http-message-signatures-directory"
DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
DIRECTORY_MEDIA_TYPE = "application/http-message-signatures-directory+json"
ALG_ED25519 = "ed25519"
#: The algorithms the profile allows (``wba-profile.mdx``, Profile rules).
#: ES256 is an AdCP RFC 9421 profile algorithm only.
WBA_ALGORITHMS: frozenset[str] = frozenset({ALG_ED25519})

#: Every signed request carries a nonce of exactly 64 random bytes
#: (``wba-profile.mdx``, Profile rules; the architecture draft §4.2.2).
WBA_NONCE_BYTES = 64
#: Seconds :func:`sign_key_directory` backdates ``created`` by, so a
#: verifier whose clock is slightly behind still accepts a fresh directory.
DIRECTORY_SKEW_ALLOWANCE_SECONDS = 300
#: How long a cached directory keeps verifying while its refreshes fail
#: (WBA §6.10), counted from the last successful fetch.
DEFAULT_DIRECTORY_MAX_STALE_SECONDS = 86400

_DIRECTORY_MAX_BYTES = 256 * 1024
_DIRECTORY_MAX_KEYS = 100
_DIRECTORY_CACHE_SECONDS = 300
_DISCOVERY_NEGATIVE_SECONDS = 60
_DIRECTORY_CREATED_SKEW_SECONDS = 60
_REQUEST_SKEW_SECONDS = 60
_REQUEST_COMPONENTS = ("@method", "@target-uri", "@authority")
_BODY_COMPONENTS = ("content-type", "content-digest")

#: The one body every signature-path failure carries, whatever the cause, so
#: the response is no oracle. The specific reason is for server logs only.
GENERIC_AUTHENTICATION_ERROR: dict[str, Any] = {
    "errors": [
        {
            "code": "AUTH_INVALID",
            "message": "Request authentication failed",
            "recovery": "correctable",
        }
    ]
}

WbaRevocationStatus = Literal["current", "revoked", "no_list", "stale", "unavailable"]
_REVOCATION_STATUSES: frozenset[str] = frozenset(
    {"current", "revoked", "no_list", "stale", "unavailable"}
)


def _new_nonce() -> str:
    return b64url_encode(secrets.token_bytes(WBA_NONCE_BYTES))


def is_64_byte_nonce(nonce: object) -> bool:
    """Whether ``nonce`` encodes exactly 64 bytes, canonically.

    The profile's spelling is unpadded base64url, which this SDK's signer
    sends. Padded standard base64 of 64 bytes is accepted too: Cloudflare's
    ``web-bot-auth`` signer and the protocol draft's Appendix E vectors emit
    it, and the bytes are what make the nonce.
    """
    if type(nonce) is not str or not nonce.isascii():
        return False
    try:
        # Unpadded base64url, decoded strictly (one spelling per value).
        return len(b64url_decode(nonce)) == WBA_NONCE_BYTES
    except ValueError:
        # Not base64url; the standard-base64 form is tried next.
        pass
    try:
        raw = base64.b64decode(nonce, validate=True)
    except ValueError:
        return False
    return len(raw) == WBA_NONCE_BYTES and base64.b64encode(raw).decode("ascii") == nonce


def valid_wba_nonce(nonce: object) -> bool:
    """Whether a received nonce is acceptable: exactly 64 bytes (see :func:`is_64_byte_nonce`).

    The profile requires a 64-byte nonce on every signed request, reads
    included (``wba-profile.mdx``, Profile rules and Replay protection). A
    shorter nonce is a profile failure: 403 with ``Accept-Signature``.
    """
    return is_64_byte_nonce(nonce)


class WbaVerificationError(ValueError):
    """A Web Bot Auth signature-path failure: an HTTP status and headers.

    ``status`` is 400 (a field does not parse), 403 (a signature, profile
    check, or key discovery fails; with ``Accept-Signature`` unless the key is
    revoked) or 429 (a reused nonce, with ``Accept-Signature`` carrying a
    fresh nonce; or local capacity, without it). Answer it with
    :data:`GENERIC_AUTHENTICATION_ERROR`; ``reason`` is for logs only and must
    never be sent to the caller.
    """

    discovery_failure: bool = False
    revoked: bool = False

    def __init__(
        self,
        reason: str,
        *,
        status: int = 403,
        label: str = "sig1",
        challenge: bool = True,
        fresh_nonce: bool = False,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.label = label
        self.challenge = challenge and status in {403, 429}
        self._nonce = _new_nonce() if fresh_nonce else None

    @property
    def headers(self) -> dict[str, str]:
        """``Accept-Signature`` for a 403 or a replay 429, else no headers."""
        if not self.challenge:
            return {}
        return {"Accept-Signature": accept_signature_value(self.label, nonce=self._nonce)}


def accept_signature_value(label: str = "sig1", *, nonce: str | None = None) -> str:
    """``Accept-Signature`` naming what the profile requires (RFC 9421 §5.1).

    The components and parameters of ``wba-profile.mdx``, Error responses:
    parameters name requirements, not values to sign. A replay answer passes
    ``nonce``, the fresh value the signer uses on its next attempt.
    """
    components: list[tuple[str, dict[str, Any]]] = [
        (name, {}) for name in (*_REQUEST_COMPONENTS, *_BODY_COMPONENTS)
    ]
    components.append(("signature-agent", {"key": label}))
    params: dict[str, Any] = {
        "created": True,
        "expires": True,
        "nonce": nonce if nonce is not None else True,
        "keyid": True,
        "alg": True,
        "tag": WBA_TAG,
    }
    return _rfc9421.serialize({label: (components, params)})


class WbaDiscoveryError(WbaVerificationError):
    """The signer's key directory could not be resolved (WBA §6.10).

    A network or TLS error, a timeout, any status other than 200 (404, 410,
    429 and 5xx included), or a response that is not a valid directory, with
    no usable cached copy holding the key. The request is unverified, which
    says nothing about the signer: by default it is answered 403 with
    ``Accept-Signature``; a verifier may instead serve it as anonymous.
    """

    discovery_failure = True

    def __init__(self, reason: str, *, label: str = "sig1") -> None:
        super().__init__(reason, status=403, label=label)


class WbaRevokedKeyError(WbaVerificationError):
    """The signer's origin revoked the key: 403 without ``Accept-Signature``.

    The signer must stop using the key, so a hint to sign again would be
    wrong. The body is the generic authentication error.
    """

    revoked = True

    def __init__(self, reason: str = "signing key revoked", *, label: str = "sig1") -> None:
        super().__init__(reason, status=403, label=label, challenge=False)


#: Deprecated alias of :class:`WbaRevokedKeyError`.
WbaAuthorizationError = WbaRevokedKeyError


class WbaUnavailableError(RuntimeError):
    """An infrastructure outage (replay or revocation store), not a signature outcome.

    The server answers it at the application layer with the retryable
    ``SERVICE_UNAVAILABLE`` AdCP error, never with a Web Bot Auth status.
    """


def _dictionary(value: str | None, *, missing_status: int = 400) -> dict[str, Any]:
    """Parse a signature field with http-sf; a field that does not parse is 400."""
    if value is None:
        raise WbaVerificationError("missing signature field", status=missing_status)
    if len(value) > 65536:
        raise WbaVerificationError("oversized signature field", status=400)
    try:
        return _rfc9421.parse_dictionary(value)
    except ValueError as exc:
        raise WbaVerificationError(f"malformed signature field: {exc}", status=400) from exc


def _check_signature_members(inputs: Mapping[str, Any], signatures: Mapping[str, Any]) -> None:
    """RFC 9421 sections 4.1 and 4.2: the shape of every member, read or not.

    Each ``Signature-Input`` member is an inner list, and each ``Signature``
    member a Byte Sequence with no parameters. A member another party added
    is not read, but a malformed one makes the field malformed: 400.
    """
    if not all(_rfc9421.is_inner_list(member) for member in inputs.values()) or any(
        type(value) is not bytes or params for value, params in signatures.values()
    ):
        raise WbaVerificationError("malformed signature field member", status=400)


def uses_web_bot_auth(headers: Mapping[str, str]) -> bool:
    """Select WBA from either marker; never fall back after a WBA failure."""
    if _lookup(headers, "signature-agent") is not None:
        return True
    raw = _lookup(headers, "signature-input")
    if raw is None:
        return False
    # Observe every explicit tag, including one an ambiguous duplicate would
    # overwrite. Text inside nonce/component strings cannot select a profile.
    return _rfc9421.dictionary_has_parameter(raw, "tag", WBA_TAG)


def _https_origin(url: str) -> str:
    if any(ord(c) < 0x21 for c in url):
        raise ValueError("origin URL contains whitespace or controls")
    parts = urlsplit(url)
    origin = canonical_origin(url)
    if (
        parts.username is not None
        or parts.password is not None
        or not origin.startswith("https://")
    ):
        raise ValueError("Web Bot Auth origins require HTTPS without userinfo")
    try:
        validate_uri_static(origin)
    except SSRFValidationError as exc:
        raise ValueError("invalid Web Bot Auth origin") from exc
    return origin


def thumbprint_key_id(jwk: Mapping[str, Any]) -> str:
    """Return the RFC 7638 thumbprint (``joserfc``) for supported public key material."""
    thumbprint = jwk_thumbprint(jwk)
    if thumbprint is None:
        raise ValueError("incomplete public JWK")
    return thumbprint


WbaPrivateKey = ed25519.Ed25519PrivateKey


def public_directory_key(private_key: WbaPrivateKey) -> dict[str, Any]:
    """Export a WBA public JWK, with a thumbprint kid and no purpose claim.

    The profile's one algorithm is ``ed25519``, so only Ed25519 keys export.
    """
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise ValueError("Web Bot Auth signs with Ed25519 keys only")
    key = OKPKey.import_key(private_key)
    return {**key.as_dict(private=False), "kid": key.thumbprint(), "use": "sig"}


def _algorithm(jwk: Mapping[str, Any]) -> str:
    """Derive the signature algorithm and check the key may verify.

    The profile's one algorithm qualifies: an Ed25519 key signs ``ed25519``.
    Any other key (P-256 and RSA included) is not a Web Bot Auth key.
    """
    shape = (str(jwk.get("kty")), str(jwk.get("crv")))
    if (
        shape != ("OKP", "Ed25519")
        or jwk.get("use", "sig") != "sig"
        or _PRIVATE_JWK_MEMBERS & set(jwk)
    ):
        raise WbaVerificationError("unsupported directory key")
    # One key, one thumbprint: joserfc refuses a non-canonical x.
    try:
        import_jwk({name: jwk.get(name) for name in ("kty", "crv", "x")})
    except ValueError as exc:
        raise WbaVerificationError("invalid directory key member") from exc
    if jwk.get("alg") not in {None, "EdDSA"}:
        raise WbaVerificationError("directory key algorithm mismatch")
    if "key_ops" in jwk and (
        not isinstance(jwk["key_ops"], list) or "verify" not in jwk["key_ops"]
    ):
        raise WbaVerificationError("directory key cannot verify")
    return ALG_ED25519


def _received_authority(url: str) -> str:
    """``@authority`` per RFC 9421 §2.2.3: the target URI's authority, normalized.

    Lowercase, with userinfo and a default port removed (HTTP §4.2.3). This
    is not the AdCP origin canonicalization, which identifies the signer only.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    authority = parts.netloc.rsplit("@", 1)[-1].lower()
    host, sep, port = authority.rpartition(":")
    default_port = {"https": "443", "http": "80"}.get(scheme)
    if sep and "]" not in port and port in {"", default_port}:
        authority = host
    if not authority:
        raise WbaVerificationError("request URL has no authority")
    return authority


def _signature_base(
    label: str, method: str, url: str, headers: Mapping[str, str], *, response: bool = False
) -> bytes:
    """The signature base for ``label`` in ``headers["Signature-Input"]``.

    For diagnostics and conformance tests: verification builds the same base
    inside ``http-message-signatures``.
    """
    member = _dictionary(_lookup(headers, "signature-input"))[label]
    items, params = member
    request = _rfc9421.Request(method=method, url=url, headers=dict(headers))
    message: _rfc9421.Request | _rfc9421.Response = (
        _rfc9421.Response(
            status_code=200, request=_rfc9421.Request("GET", url), headers=dict(headers)
        )
        if response
        else request
    )
    return _rfc9421.build_base(
        message,
        covered_component_ids=[_rfc9421.component_identifier(item) for item in items],
        params=params,
    )


def _window(
    params: Mapping[str, Any], *, now: float, directory: bool = False, label: str = "sig1"
) -> None:
    if type(params.get("tag")) is not str:
        raise WbaVerificationError("signature tag must be an sf-string", label=label)
    for name in ("created", "expires"):
        if type(params.get(name)) is not int:
            raise WbaVerificationError("missing integer time parameter", label=label)
    created, expires = params["created"], params["expires"]
    if expires <= created or (not directory and expires - created > 300):
        raise WbaVerificationError("invalid signature lifetime", label=label)
    if directory:
        # Directory signatures tolerate a publisher clock slightly ahead, but
        # an expired directory signature never verifies.
        if created > now + _DIRECTORY_CREATED_SKEW_SECONDS or expires <= now:
            raise WbaVerificationError("signature outside its validity window", label=label)
    elif created > now + _REQUEST_SKEW_SECONDS or expires < now - _REQUEST_SKEW_SECONDS:
        raise WbaVerificationError("signature outside its validity window", label=label)
    if type(params.get("keyid")) is not str or len(params["keyid"]) != 43:
        raise WbaVerificationError("missing thumbprint keyid", label=label)


def _verify_crypto(
    message: _rfc9421.Request | _rfc9421.Response,
    *,
    label: str,
    params: Mapping[str, Any],
    jwk: Mapping[str, Any],
) -> str:
    """Verify ``label`` with ``http-message-signatures``; return the algorithm.

    The algorithm comes from the key (:func:`_algorithm`), and the signature's
    ``alg`` must name the same one. The caller checked the fields' shape
    (:func:`_check_signature_members`).
    """
    alg = _algorithm(jwk)
    # Every signature names its algorithm (Profile rules); none is inferred.
    if params.get("alg") != alg:
        raise WbaVerificationError("signature algorithm is absent or does not match key")
    try:
        public_key = public_key_from_jwk(dict(jwk))
    except (ValueError, KeyError, TypeError) as exc:
        raise WbaVerificationError("invalid directory key") from exc
    try:
        _rfc9421.verify(
            message,
            label=label,
            tag=str(params["tag"]),
            alg=alg,
            public_key=public_key,
            window_checked=True,  # every caller ran _window first
            parameters=tuple(params),
        )
    except _rfc9421.SignatureBaseError as exc:
        raise WbaVerificationError(exc.reason, status=exc.status) from exc
    except _rfc9421.SignatureMismatchError as exc:
        raise WbaVerificationError("signature did not verify") from exc
    return alg


def sign_wba_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    private_key: WbaPrivateKey,
    agent_origin: str,
    created: int | None = None,
    expires_in_seconds: int = 300,
    nonce: str | None = None,
    label: str = "sig1",
) -> dict[str, str]:
    """Sign with the opt-in WBA profile; infer alg and kid from the public key.

    ``url`` is the target URI exactly as the request will send it:
    ``@target-uri`` covers it unchanged (RFC 9421 §2.2.2), percent-encoding
    included. The nonce defaults to 64 random bytes, and an explicit one must
    be 64 bytes too. A request that already carries signature fields is
    refused rather than stripped: another party's signature is never
    silently dropped. ``http-message-signatures`` builds the base, signs,
    and serializes ``Signature-Input`` and ``Signature``.
    """
    origin = _https_origin(agent_origin)
    if not 0 < expires_in_seconds <= 300:
        raise ValueError("expires_in_seconds must be in (0, 300]")
    if any(
        _lookup(headers, name) is not None
        for name in ("signature", "signature-input", "signature-agent")
    ):
        raise ValueError(
            "request already carries signature fields; refusing to replace another "
            "party's signature"
        )
    if not label:
        raise ValueError("label must be a non-empty RFC 8941 key")
    jwk = public_directory_key(private_key)
    alg = _algorithm(jwk)
    created = int(time.time()) if created is None else created
    nonce = _new_nonce() if nonce is None else nonce
    if not valid_wba_nonce(nonce):
        raise ValueError("nonce must encode exactly 64 bytes")
    try:
        agent = _rfc9421.serialize({label: (origin, {})})
    except ValueError as exc:
        raise ValueError(f"label {label!r} is not a valid RFC 8941 key: {exc}") from exc
    outgoing = {"Signature-Agent": agent}
    components = list(_REQUEST_COMPONENTS)
    if body:
        if _lookup(headers, "content-type") is None:
            raise ValueError("body-bearing WBA requests require Content-Type")
        outgoing["Content-Digest"] = compute_content_digest_sha256(body)
        components.extend(_BODY_COMPONENTS)
    covered = [_rfc9421.component_identifier((name, {})) for name in components]
    covered.append(_rfc9421.component_identifier(("signature-agent", {"key": label})))
    message = _rfc9421.Request(
        method=method,
        url=url,
        headers={
            **{k.lower(): v for k, v in headers.items()},
            **{k.lower(): v for k, v in outgoing.items()},
        },
    )
    signature_input, signature = _rfc9421.sign(
        message,
        alg=alg,
        private_key=private_key,
        key_id=jwk["kid"],
        label=label,
        created=created,
        expires=created + expires_in_seconds,
        nonce=nonce,
        tag=WBA_TAG,
        covered_component_ids=covered,
    )
    outgoing["Signature-Input"] = signature_input
    outgoing["Signature"] = signature
    return outgoing


@dataclass(frozen=True)
class DirectoryResponse:
    """Directory bytes and headers, retained as the record of discovery."""

    body: bytes
    headers: Mapping[str, str]
    status: int = 200


@dataclass(frozen=True)
class _VerifiedDirectory:
    keys: dict[str, dict[str, Any]]
    #: Earliest signature ``expires`` across the directory's signatures.
    signatures_expire: float


def _parse_directory_keys(body: bytes) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Index the usable keys by thumbprint; also return the unusable keys' thumbprints."""
    try:
        document = parse_strict_json(body)
    except StrictJsonError as exc:
        raise WbaVerificationError("invalid directory JSON") from exc
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not 1 <= len(keys) <= _DIRECTORY_MAX_KEYS:
        raise WbaVerificationError("invalid directory key count")
    index: dict[str, dict[str, Any]] = {}
    ignored: set[str] = set()
    for jwk in keys:
        if not isinstance(jwk, dict):
            raise WbaVerificationError("directory entry is not an object")
        if _PRIVATE_JWK_MEMBERS & {str(name).lower() for name in jwk}:
            raise WbaVerificationError("directory entry carries private key material")
        if "key_ops" in jwk and (
            not isinstance(jwk["key_ops"], list)
            or any(not isinstance(op, str) for op in jwk["key_ops"])
        ):
            raise WbaVerificationError("directory key_ops is not a string array")
        try:
            _algorithm(jwk)
            thumbprint = thumbprint_key_id(jwk)
        except (WbaVerificationError, ValueError):
            # A directory may hold keys for other uses: skip what the profile
            # cannot verify with (RFC 7517 section 5), and remember it so its
            # signature is skipped too.
            try:
                ignored.add(thumbprint_key_id(jwk))
            except ValueError:
                pass  # No thumbprint, so no signature can name it.
            continue
        if thumbprint in index:
            raise WbaVerificationError("directory repeats a key")
        # A key is found by its thumbprint; a kid, when present, does not
        # affect lookup (draft-02 §5.5: kid MAY be present).
        index[thumbprint] = {**jwk, "kid": thumbprint}
    if not index:
        raise WbaVerificationError("directory holds no usable key")
    return index, ignored


def _verify_directory(
    origin: str, response: DirectoryResponse, *, now: float
) -> _VerifiedDirectory:
    origin = _https_origin(origin)
    media_type = (_lookup(response.headers, "content-type") or "").split(";", 1)[0]
    # WBA section 5.5.1: a directory is served as the directory media type.
    if (
        response.status != 200
        or media_type.strip().lower() != DIRECTORY_MEDIA_TYPE
        or len(response.body) > _DIRECTORY_MAX_BYTES
    ):
        raise WbaVerificationError("invalid key directory response")
    raw_inputs = _lookup(response.headers, "signature-input")
    raw_signatures = _lookup(response.headers, "signature")
    if raw_inputs is None or raw_signatures is None:
        # The profile requires signed directory responses (Appendix B.1).
        raise WbaVerificationError("key directory is not signed")
    digest = _lookup(response.headers, "content-digest")
    # The digest covers the exact bytes served, a UTF-8 BOM included.
    if digest is None or not content_digest_matches(digest, response.body):
        raise WbaVerificationError("directory digest mismatch")
    keys, ignored = _parse_directory_keys(response.body)
    inputs = _dictionary(raw_inputs)
    signatures = _dictionary(raw_signatures)
    if set(signatures) != set(inputs):
        raise WbaVerificationError("directory signature labels differ")
    _check_signature_members(inputs, signatures)
    message = _rfc9421.Response(
        status_code=response.status,
        request=_rfc9421.Request(method="GET", url=origin + DIRECTORY_PATH),
        headers=dict(response.headers),
    )
    signed: dict[str, dict[str, Any]] = {}
    earliest = float("inf")
    for label, member in inputs.items():
        if not _rfc9421.is_inner_list(member):
            raise WbaVerificationError("invalid directory signature profile")
        items, params = member
        if params.get("keyid") in ignored:
            # The signature of a key the profile cannot use is not read.
            continue
        if params.get("tag") != DIRECTORY_TAG:
            raise WbaVerificationError("invalid directory signature profile")
        _window(params, now=now, directory=True, label=label)
        covered = {_rfc9421.component_identifier(item) for item in items}
        # Exact identifiers: a "content-digest" member or a bare @authority
        # does not bind the response's digest and request authority.
        if not {'"@authority";req', '"content-digest"'} <= covered:
            raise WbaVerificationError("directory signature does not bind authority and digest")
        keyid = params["keyid"]
        if keyid not in keys or keyid in signed:
            raise WbaVerificationError("directory signature key is absent or repeated")
        _verify_crypto(message, label=label, params=params, jwk=keys[keyid])
        signed[keyid] = keys[keyid]
        earliest = min(earliest, float(params["expires"]))
    # One signature per key (Appendix B.1): every usable key proves possession.
    if signed.keys() != keys.keys():
        raise WbaVerificationError("a usable directory key did not sign")
    return _VerifiedDirectory(signed, earliest)


def verify_directory_response(
    origin: str, response: DirectoryResponse, *, now: float
) -> dict[str, dict[str, Any]]:
    """Validate a key directory served at ``origin``; return keys by thumbprint.

    The response must be served as :data:`DIRECTORY_MEDIA_TYPE` and signed
    per WBA Appendix B.1, as the profile requires: every key the profile can
    use signs it, and each of those signatures verifies and covers
    ``"@authority";req`` and ``"content-digest"``. A key the profile cannot
    use (P-256 or RSA, for example) is ignored, and so is its signature. An
    unsigned directory is refused. Every key is indexed by its RFC 7638
    thumbprint (a ``kid`` is optional).
    """
    return _verify_directory(origin, response, now=now).keys


def sign_key_directory(
    *,
    agent_origin: str,
    private_keys: Sequence[WbaPrivateKey],
    created: int | None = None,
    expires_in_seconds: int = 86400,
) -> DirectoryResponse:
    """Generate a signed directory response; republish before its expires time.

    One signature per key (WBA Appendix B.1), each from
    ``HTTPMessageSigner.sign``. With no explicit ``created``, the signatures
    are backdated by :data:`DIRECTORY_SKEW_ALLOWANCE_SECONDS` (verifiers
    allow little clock skew on directory signatures) and still expire
    ``expires_in_seconds`` from now.
    """
    origin = _https_origin(agent_origin)
    if not 1 <= len(private_keys) <= _DIRECTORY_MAX_KEYS or expires_in_seconds <= 0:
        raise ValueError("directory requires 1–100 keys and a positive lifetime")
    keys = [public_directory_key(key) for key in private_keys]
    if len({key["kid"] for key in keys}) != len(keys):
        raise ValueError("directory keys must be distinct")
    body = json.dumps({"keys": keys}, separators=(",", ":")).encode()
    headers = {
        "Content-Type": DIRECTORY_MEDIA_TYPE,
        "Content-Digest": compute_content_digest_sha256(body),
        "Cache-Control": "max-age=300",
    }
    if created is None:
        now = int(time.time())
        created, expires = now - DIRECTORY_SKEW_ALLOWANCE_SECONDS, now + expires_in_seconds
    else:
        expires = created + expires_in_seconds
    message = _rfc9421.Response(
        status_code=200,
        request=_rfc9421.Request(method="GET", url=origin + DIRECTORY_PATH),
        headers=dict(headers),
    )
    for number, (key, jwk) in enumerate(zip(private_keys, keys), 1):
        # Each key adds its own member to the same two fields.
        signature_input, signature = _rfc9421.sign(
            message,
            alg=_algorithm(jwk),
            private_key=key,
            key_id=jwk["kid"],
            label=f"key{number}",
            created=created,
            expires=expires,
            nonce=None,
            tag=DIRECTORY_TAG,
            covered_component_ids=['"@authority";req', '"content-digest"'],
            append=True,
        )
    headers.update({"Signature-Input": signature_input, "Signature": signature})
    return DirectoryResponse(body=body, headers=headers)


class DirectoryFetcher(Protocol):
    async def __call__(self, url: str, *, allow_private: bool = False) -> DirectoryResponse: ...


async def fetch_key_directory(url: str, *, allow_private: bool = False) -> DirectoryResponse:
    """Fetch over TLS with IP pinning, no redirects/proxies, and bounded bytes/time."""
    from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport

    _https_origin(url)

    async def fetch() -> DirectoryResponse:
        transport = await asyncio.to_thread(
            build_async_ip_pinned_transport, url, allow_private=allow_private
        )
        fetch_timeout = httpx.Timeout(10, connect=5)
        async with httpx.AsyncClient(
            transport=transport,
            timeout=fetch_timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                url,
                # The only media type _verify_directory accepts (WBA section 5.5.1).
                headers={"Accept": DIRECTORY_MEDIA_TYPE, "Accept-Encoding": "identity"},
            ) as response:
                return DirectoryResponse(
                    status=response.status_code,
                    headers=dict(response.headers),
                    body=(
                        await async_read_limited_bytes(response, limit=_DIRECTORY_MAX_BYTES)
                        if response.status_code == 200
                        else b""
                    ),
                )

    return await asyncio.wait_for(fetch(), timeout=10)


@dataclass
class _DirectoryEntry:
    response: DirectoryResponse
    keys: dict[str, dict[str, Any]]
    refresh_at: float
    expires: float


def _operator_bucket(origin: str) -> str:
    """Quota group for an origin: PSL PRIVATE tenants share their provider's group."""
    from adcp.signing.etld import quota_key

    return quota_key(origin) or urlsplit(origin).hostname or origin


class DiscoveredSignerKeys:
    """Discover many WBA signers by (covered origin, thumbprint), with bounded caching.

    Caching follows WBA §6.10. A directory is refreshed after at most five
    minutes (sooner if its signatures expire). A key absent from a directory
    that resolves is absent: the new directory replaces the cached one. A
    directory that fails to resolve (a network or TLS error, a timeout, any
    status other than 200, or an invalid directory) is not evidence: it
    never evicts the cached entry, which keeps verifying for up to
    ``max_stale_seconds`` from the last successful fetch (never past the
    directory signatures' expiry), and the failure is retried after a
    60-second cooldown. With no usable cached entry the lookup raises
    :class:`WbaDiscoveryError`. A key missing from a stale cached copy is a
    discovery failure too, since a key rotated in during the outage cannot be
    seen.

    Successful directories are a bounded LRU cache of ``max_origins``
    origins; eviction only forces a refetch. Concurrent discoveries are
    bounded globally and per operator (a registrable domain, with every
    tenant of a PSL PRIVATE hosting suffix counted together). A discovery
    the resolver's own limits refuse is local load shedding: a 429, not a
    re-sign challenge, and a cached copy keeps verifying for one more cache
    period only.

    A fetcher is a trusted test/transport seam and must enforce fetch policy.
    This resolver never reads capabilities, brand.json, or trust.json.
    """

    def __init__(
        self,
        *,
        fetcher: DirectoryFetcher = fetch_key_directory,
        allow_private: bool = False,
        max_origins: int = 256,
        max_origins_per_operator: int = 8,
        max_stale_seconds: float = DEFAULT_DIRECTORY_MAX_STALE_SECONDS,
        clock: Callable[[], float] = time.time,
        legacy_resolver: SignerKeyResolver | None = None,
    ) -> None:
        if any(
            type(limit) is not int or limit <= 0
            for limit in (
                max_origins,
                max_origins_per_operator,
            )
        ):
            raise ValueError("origin admission limits must be positive")
        if not max_stale_seconds > 0:
            raise ValueError("max_stale_seconds must be positive")
        self._fetcher, self._allow_private, self._max_origins, self._clock = (
            fetcher,
            allow_private,
            max_origins,
            clock,
        )
        self._max_origins_per_operator = max_origins_per_operator
        self._max_stale = float(max_stale_seconds)
        self._cache: OrderedDict[str, _DirectoryEntry] = OrderedDict()
        self._failures: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()
        self._legacy_resolver = legacy_resolver
        self._pending: dict[str, asyncio.Task[_DirectoryEntry]] = {}

    def evidence(self, origin: str) -> DirectoryResponse | None:
        """The last directory response that resolved for ``origin``, if cached."""
        try:
            entry = self._cache.get(_https_origin(origin))
        except ValueError:
            return None
        return entry.response if entry is not None else None

    @staticmethod
    def _lookup_key(
        origin: str, entry: _DirectoryEntry, keyid: str, *, stale: bool
    ) -> ResolvedSignerKey | None:
        key = entry.keys.get(keyid)
        if key is not None:
            return ResolvedSignerKey(origin, dict(key))
        if stale:
            raise WbaDiscoveryError("key absent from a stale cached directory")
        return None

    async def __call__(
        self, keyid: str, *, signer_origin: str | None = None
    ) -> ResolvedSignerKey | None:
        if signer_origin is None:
            # Coexistence is explicit and never falls back from the WBA branch.
            return await self._legacy_resolver(keyid) if self._legacy_resolver is not None else None
        try:
            origin = _https_origin(signer_origin)
        except ValueError as exc:
            raise WbaVerificationError("invalid signer origin") from exc
        async with self._lock:
            now = self._clock()
            entry = self._cache.get(origin)
            if entry is not None and now < entry.refresh_at:
                self._cache.move_to_end(origin)
                return self._lookup_key(origin, entry, keyid, stale=False)
            stale = entry if entry is not None and now < entry.expires else None
            if self._failures.get(origin, 0) > now:
                if stale is not None:
                    return self._lookup_key(origin, stale, keyid, stale=True)
                raise WbaDiscoveryError("key directory discovery is cooling down")
            task = self._pending.get(origin)
            if task is None:
                refused = None
                # Refreshing a cached origin does not compete with first
                # discoveries for the global slots, so a flood of new origins
                # cannot hold a known signer's refresh off.
                if entry is None and len(self._pending) >= self._max_origins:
                    refused = "key directory discovery capacity exceeded"
                else:
                    bucket = _operator_bucket(origin)
                    if (
                        sum(_operator_bucket(o) == bucket for o in self._pending)
                        >= self._max_origins_per_operator
                    ):
                        refused = "operator key directory discovery capacity exceeded"
                if refused is not None:
                    # A local refusal says nothing about the origin: a cached
                    # copy keeps verifying for one more cache period only, so
                    # verifier load cannot keep a removed key alive.
                    if entry is not None and now < min(
                        entry.expires, entry.refresh_at + _DIRECTORY_CACHE_SECONDS
                    ):
                        return self._lookup_key(origin, entry, keyid, stale=True)
                    raise WbaVerificationError(refused, status=429, challenge=False)
                task = asyncio.create_task(self._refresh(origin))
                # A cancelled HTTP request must not cancel discovery for other
                # waiters or leave an unobserved exception in a background task.
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
                self._pending[origin] = task
        try:
            fresh = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock:
                entry = self._cache.get(origin)
                stale = entry if entry is not None and self._clock() < entry.expires else None
            if stale is not None:
                return self._lookup_key(origin, stale, keyid, stale=True)
            if isinstance(exc, WbaDiscoveryError):
                raise WbaDiscoveryError(exc.reason) from exc
            raise WbaDiscoveryError("key directory could not be resolved") from exc
        return self._lookup_key(origin, fresh, keyid, stale=False)

    async def _refresh(self, origin: str) -> _DirectoryEntry:
        try:
            try:
                # Calling the fetcher inside this guard also contains a
                # fetcher that raises synchronously.
                response = await self._fetcher(
                    origin + DIRECTORY_PATH, allow_private=self._allow_private
                )
                if not isinstance(response, DirectoryResponse) or response.status != 200:
                    # Any status other than 200 is a discovery failure (§5.5).
                    raise WbaVerificationError("key directory fetch did not return 200")
                verified = _verify_directory(origin, response, now=self._clock())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._lock:
                    now = self._clock()
                    # A failure never evicts the cached directory.
                    self._failures[origin] = now + _DISCOVERY_NEGATIVE_SECONDS
                    self._failures.move_to_end(origin)
                    for failed, until in list(self._failures.items()):
                        if until <= now:
                            del self._failures[failed]
                    while len(self._failures) > self._max_origins:
                        self._failures.popitem(last=False)
                raise WbaDiscoveryError("key directory could not be resolved") from exc
            now = self._clock()
            expires = min(now + self._max_stale, verified.signatures_expire)
            entry = _DirectoryEntry(
                response, verified.keys, min(now + _DIRECTORY_CACHE_SECONDS, expires), expires
            )
            async with self._lock:
                self._failures.pop(origin, None)
                self._cache[origin] = entry
                self._cache.move_to_end(origin)
                while len(self._cache) > self._max_origins:
                    self._cache.popitem(last=False)
            return entry
        finally:
            self._pending.pop(origin, None)


async def verify_wba_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    signer_keys: WbaSignerKeyResolver,
    replay_store: ReplayStore | None,
    revocation_checker: Any = None,
    now: float | None = None,
    raw_headers: Sequence[tuple[bytes, bytes]] | None = None,
    authorities: Collection[str] | None = None,
) -> tuple[VerifiedSigner, ...]:
    """Verify the request's one WBA signature; return the established origin.

    The verification procedure of ``wba-profile.mdx``: the request carries
    exactly one ``web-bot-auth`` signature and exactly one ``Signature-Agent``
    member, keyed to that signature's label. The result is a one-element
    tuple holding that signer.

    ``url`` is the target URI as received (RFC 9421 §2.2.2): build it from
    the request line, not a decoded path. Verification needs only the key
    directory (``signer_keys``) and the replay store, reads no trust record
    and grants no authority.

    Failures raise :class:`WbaVerificationError` (or its
    :class:`WbaDiscoveryError` subclass for an unresolvable directory), whose
    status and headers are the whole answer: 400 when a field does not parse,
    403 with ``Accept-Signature`` for any other signature failure, and 429
    with ``Accept-Signature`` carrying a fresh nonce for a reused nonce. A
    replay-store failure raises :class:`WbaUnavailableError`, an
    application-layer outage. ``authorities`` lists the ``host[:port]``
    values this endpoint serves; a request whose ``@authority`` is not among
    them is refused before key discovery.

    ``revocation_checker`` is optional and belongs to the authorization
    layer. When given, :func:`check_wba_revocation` runs after verification:
    a revoked key raises :class:`WbaRevokedKeyError`, and the signer records
    its revocation state.
    """
    if replay_store is None:
        raise ValueError("Web Bot Auth requires a replay store")
    now = time.time() if now is None else now
    if strict_header_precheck(headers=headers, raw_headers=raw_headers, url=url) is not None:
        raise WbaVerificationError("ambiguous signature fields", status=400)
    if raw_headers is not None:
        raw_agents = [v for k, v in raw_headers if k.lower() == b"signature-agent"]
        if len(raw_agents) > 1:
            raise WbaVerificationError("repeated Signature-Agent field", status=400)
    # A missing signature is a 403 challenge; a present field that does not
    # parse is a 400.
    inputs = _dictionary(_lookup(headers, "signature-input"), missing_status=403)
    signatures = _dictionary(_lookup(headers, "signature"), missing_status=403)
    # A malformed member is a malformed field, before any member is selected.
    _check_signature_members(inputs, signatures)
    raw_agent = _lookup(headers, "signature-agent")
    # Selection by tag alone leaves the Signature-Agent member absent, which
    # fails the signature with 403 below.
    agents = _dictionary(raw_agent) if raw_agent is not None else {}
    wba = [(label, member) for label, member in inputs.items() if member[1].get("tag") == WBA_TAG]
    if not wba:
        raise WbaVerificationError("Signature-Agent requires the Web Bot Auth profile")
    if set(signatures) != set(inputs):
        raise WbaVerificationError("signature labels do not match", status=400)
    label, (items, params) = wba[0]
    if len(wba) > 1 or len(agents) > 1:
        # One signature, one Signature-Agent member (Profile rules, "One
        # signature"). An intermediary that signs is the signer; acting for
        # another party is an authorization question, not a second signature.
        raise WbaVerificationError(
            "the profile takes one web-bot-auth signature and one Signature-Agent member",
            label=label,
        )
    if authorities is not None:
        # A signature captured at another seller cannot be replayed to this
        # endpoint under that seller's Host.
        allowed = {_received_authority("https://" + a) for a in authorities}
        if _received_authority(url) not in allowed:
            raise WbaVerificationError(
                "request authority is not served by this verifier", label=label
            )
    _window(params, now=now, label=label)
    nonce = params.get("nonce")
    if not valid_wba_nonce(nonce):
        raise WbaVerificationError("missing or invalid nonce", label=label)
    nonce = cast(str, nonce)
    agent = agents.get(label)
    if (
        agent is None
        or _rfc9421.is_inner_list(agent)
        or type(agent[0]) is not str
        or agent[1].get("type", "directory") != "directory"
    ):
        raise WbaVerificationError("missing supported Signature-Agent member", label=label)
    try:
        origin = _https_origin(agent[0])
    except ValueError as exc:
        raise WbaVerificationError("invalid signer origin", label=label) from exc
    # The value is the serialized origin itself, compared as a string: a
    # spelling that only normalizes to an origin names no origin.
    if agent[0] != origin:
        raise WbaVerificationError("signer origin is not in canonical form", label=label)
    covered = [_rfc9421.component_identifier(item) for item in items]
    required = {_rfc9421.component_identifier((name, {})) for name in _REQUEST_COMPONENTS}
    required.add(_rfc9421.component_identifier(("signature-agent", {"key": label})))
    if body:
        required.update(_rfc9421.component_identifier((name, {})) for name in _BODY_COMPONENTS)
    if not required <= set(covered) or len(set(covered)) != len(covered):
        raise WbaVerificationError("missing or repeated covered component", label=label)
    keyid = params["keyid"]
    try:
        key = await signer_keys(keyid, signer_origin=origin)
    except WbaVerificationError as exc:
        exc.label = label
        raise
    except Exception as exc:
        # A resolver that cannot answer leaves the request unverified.
        raise WbaDiscoveryError("signer key resolution failed", label=label) from exc
    try:
        matches = (
            key is not None
            and canonical_origin(key.agent_url) == origin
            and thumbprint_key_id(key.jwk) == keyid
        )
    except (ValueError, TypeError, AttributeError):
        matches = False
    if not matches:
        raise WbaVerificationError("key absent from the named origin", label=label)
    assert key is not None
    message = _rfc9421.Request(method=method, url=url, headers=dict(headers))
    try:
        alg = _verify_crypto(message, label=label, params=params, jwk=key.jwk)
    except WbaVerificationError as exc:
        exc.label = label
        raise
    if body:
        digest = _lookup(headers, "content-digest")
        if digest is None or not content_digest_matches(digest, body):
            raise WbaVerificationError("request digest mismatch", label=label)
    # One extra second keeps the nonce through the last second of the
    # accepted window, including a replay at expires + 60.
    ttl = max(params["expires"] - now + _REQUEST_SKEW_SECONDS + 1, 1)
    try:
        claim = await asyncio.to_thread(_claim_replay_nonce, replay_store, origin, nonce, ttl)
    except Exception as exc:
        raise WbaUnavailableError("replay store unavailable") from exc
    if claim == "replayed":
        # A reused nonce is 429 with Accept-Signature carrying a fresh nonce
        # (Error responses; WBA section 5.3).
        raise WbaVerificationError("replayed nonce", status=429, label=label, fresh_nonce=True)
    if claim != "claimed":
        raise WbaVerificationError(
            "replay store capacity exceeded", status=429, label=label, challenge=False
        )
    try:
        parsed = parse_strict_json(body) if body.strip() else None
    except StrictJsonError as exc:
        raise WbaVerificationError("malformed signed JSON body", status=400) from exc
    signers: tuple[VerifiedSigner, ...] = (
        VerifiedSigner(
            key_id=keyid,
            alg=alg,
            label=label,
            verified_at=now,
            agent_url=origin,
            body_authenticated=bool(body),
            profile=WBA_TAG,
            parsed_body=parsed,
        ),
    )
    if revocation_checker is not None:
        signers = await check_wba_revocation(revocation_checker, signers)
    return signers


async def wba_revocation_status(checker: Any, origin: str, keyid: str) -> WbaRevocationStatus:
    """A key's state in its origin's revocation list, for the authorization layer.

    ``checker`` is a :class:`WbaRevocationStatusChecker` (``status(origin,
    keyid)``), or a legacy async callable ``(origin, keyid) -> bool`` whose
    failure means the list cannot be read (``"stale"``).
    """
    status_method = getattr(checker, "status", None)
    if callable(status_method):
        try:
            status = await status_method(origin, keyid)
        except Exception:
            return "unavailable"
        return cast(WbaRevocationStatus, status) if status in _REVOCATION_STATUSES else "stale"
    try:
        revoked = await checker(origin, keyid)
    except Exception:
        return "stale"
    return "revoked" if revoked else "current"


async def check_wba_revocation(
    checker: Any, signers: Sequence[VerifiedSigner]
) -> tuple[VerifiedSigner, ...]:
    """Record each verified signer's revocation state; refuse a revoked key.

    Runs in the authorization layer, after verification. A revoked key
    raises :class:`WbaRevokedKeyError` (403, no ``Accept-Signature``). Every
    other state is recorded in :attr:`VerifiedSigner.revocation` for the
    binding and grant gates.
    """
    checked: list[VerifiedSigner] = []
    for signer in signers:
        status = await wba_revocation_status(checker, signer.agent_url or "", signer.key_id)
        if status == "revoked":
            raise WbaRevokedKeyError(label=signer.label)
        checked.append(replace(signer, revocation=status))
    return tuple(checked)


WBA_GOVERNANCE_TYP = "adcp-gov+jws"
#: JWS ``alg`` values a governance token may carry under the profile, by the
#: Web Bot Auth algorithm of the issuer's key. ``Ed25519`` is RFC 9864's
#: fully specified name for ``EdDSA`` over Ed25519.
_GOVERNANCE_JWS_ALGS: dict[str, frozenset[str]] = {
    ALG_ED25519: frozenset({"EdDSA", "Ed25519"}),
}


class WbaGovernanceError(ValueError):
    """A governance token failed a Web Bot Auth governance check.

    ``code`` is the governance error code from ``security.mdx`` (Verification
    error taxonomy); ``reason`` is for logs only.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


async def verify_wba_governance_issuer(
    token: object,
    *,
    signer_keys: WbaSignerKeyResolver,
    governance_agents: Collection[str],
    now: float | None = None,
    clock_skew_seconds: float = 60,
) -> str:
    """Check a governance token's key and issuer; return the issuer origin.

    The two checks of ``wba-profile.mdx``, One agent per purpose:

    1. the token's ``kid`` is in the key directory of the ``iss`` origin
       (resolved through ``signer_keys``, keyed on origin and key), and
       ``jku`` is that directory's URL (``governance_key_unknown``);
    2. the ``iss`` origin is the buyer's governance agent: one of
       ``governance_agents``, the governance-typed ``agents[]`` entry in the
       buyer's brand.json or the ``adcp:governance`` origin in its trust.json
       (``governance_issuer_not_authorized``).

    In checklist order around them: the compact JWS and its ``alg`` and
    ``typ`` (``governance_token_invalid``), the signature, verified by
    ``joserfc`` (``governance_token_invalid``), and ``exp`` and ``iat`` with
    ``clock_skew_seconds`` of skew (``governance_token_expired`` /
    ``governance_token_not_yet_valid``). An unresolvable directory is
    ``governance_jwks_unavailable``. Failures raise
    :class:`WbaGovernanceError`.

    The rest of the governance checklist (``aud``, ``phase``, ``sub``, the
    action binding, revocation, replay) is the caller's.

    This is the earlier #7894 draft's check, graded by the vendored
    ``wba-governance`` vectors (``alg`` ``Ed25519`` accepted, ``jku``
    required). The R4 erratum (adcontextprotocol/adcp#8299) selects the key
    directory from the trust record and requires ``alg`` ``EdDSA``: use
    ``verify_governance_authorization(web_bot_auth=...)``.
    """
    from joserfc import jws as jose_jws
    from joserfc.errors import BadSignatureError, JoseError
    from joserfc.jwk import OKPKey

    from adcp.governance import (
        _decode_json_segment,
        _governance_jws_registry,
        critical_claims_failure,
    )
    from adcp.signing.jws import JwsMalformedError, parse_compact_jws

    def fail(code: str, reason: str) -> WbaGovernanceError:
        return WbaGovernanceError(code, reason)

    now = time.time() if now is None else now
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise fail("governance_token_invalid", "governance token is missing or too large")
    # The splitter and segment parser of the AdCP 3.2 governance verifier.
    try:
        b64_header, b64_payload, _signature = parse_compact_jws(token)
        header = _decode_json_segment(b64_header)
        claims = _decode_json_segment(b64_payload)
    except (JwsMalformedError, ValueError) as exc:
        raise fail("governance_token_invalid", "governance token is not a compact JWS") from exc
    alg = header.get("alg")
    if type(alg) is not str or alg not in _GOVERNANCE_JWS_ALGS[ALG_ED25519]:
        raise fail("governance_token_invalid", "governance token alg not allowed")
    if header.get("typ") != WBA_GOVERNANCE_TYP:
        raise fail("governance_token_invalid", "governance token typ mismatch")
    critical_failure = critical_claims_failure(header, claims)
    if critical_failure is not None:
        raise fail("governance_token_invalid", critical_failure)
    try:
        issuer = _https_origin(str(claims.get("iss", "")))
    except ValueError as exc:
        raise fail("governance_token_invalid", "governance token iss is not an origin") from exc
    if issuer != claims.get("iss") or header.get("jku") != issuer + DIRECTORY_PATH:
        raise fail("governance_key_unknown", "jku is not the iss origin's key directory")
    try:
        authorized = {_https_origin(origin) for origin in governance_agents}
    except ValueError as exc:
        raise ValueError("governance_agents must be HTTPS origins") from exc
    # Checked before key discovery, so a token cannot make the verifier fetch
    # a directory from an origin that is not the buyer's governance agent.
    if issuer not in authorized:
        raise fail("governance_issuer_not_authorized", "iss is not the buyer's governance agent")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise fail("governance_key_unknown", "governance token kid is missing")
    try:
        key = await signer_keys(kid, signer_origin=issuer)
    except WbaVerificationError as exc:
        raise fail("governance_jwks_unavailable", "issuer key directory unavailable") from exc
    try:
        matches = (
            key is not None
            and canonical_origin(key.agent_url) == issuer
            and thumbprint_key_id(key.jwk) == kid
        )
        key_alg = _algorithm(key.jwk) if key is not None and matches else None
    except (ValueError, TypeError, AttributeError, WbaVerificationError):
        matches, key_alg = False, None
    if not matches or key is None or key_alg is None:
        raise fail("governance_key_unknown", "kid is not in the iss origin's key directory")
    if alg not in _GOVERNANCE_JWS_ALGS[key_alg]:
        raise fail("governance_token_invalid", "governance token alg does not match the key")
    public = {name: value for name, value in key.jwk.items() if name not in {"kid", "alg"}}
    try:
        jose_key = OKPKey.import_key(public)
        jose_jws.deserialize_compact(token, jose_key, registry=_governance_jws_registry(str(alg)))
    except BadSignatureError as exc:
        raise fail("governance_token_invalid", "governance token signature invalid") from exc
    except JoseError as exc:
        raise fail("governance_token_invalid", f"governance token rejected: {exc}") from exc
    expires, issued = claims.get("exp"), claims.get("iat")
    if type(expires) is not int or type(issued) is not int:
        raise fail("governance_token_invalid", "governance token exp and iat must be integers")
    if expires <= now - clock_skew_seconds:
        raise fail("governance_token_expired", "governance token expired")
    if issued > now + clock_skew_seconds:
        raise fail("governance_token_not_yet_valid", "governance token issued in the future")
    return issuer


class WbaRevocationStatusChecker(Protocol):
    """Report a WBA key's state in its origin's signed revocation list."""

    async def status(self, origin: str, keyid: str) -> WbaRevocationStatus:
        raise NotImplementedError("report the key's revocation state")


__all__ = [
    "ALG_ED25519",
    "DEFAULT_DIRECTORY_MAX_STALE_SECONDS",
    "DIRECTORY_MEDIA_TYPE",
    "DIRECTORY_SKEW_ALLOWANCE_SECONDS",
    "DirectoryResponse",
    "DiscoveredSignerKeys",
    "GENERIC_AUTHENTICATION_ERROR",
    "OriginRevocationChecker",
    "WBA_ALGORITHMS",
    "WBA_GOVERNANCE_TYP",
    "WBA_NONCE_BYTES",
    "WbaAuthorizationError",
    "WbaDiscoveryError",
    "WbaGovernanceError",
    "WbaRevocationStatus",
    "WbaRevocationStatusChecker",
    "WbaRevokedKeyError",
    "WbaUnavailableError",
    "WbaVerificationError",
    "accept_signature_value",
    "check_wba_revocation",
    "public_directory_key",
    "sign_key_directory",
    "sign_wba_request",
    "thumbprint_key_id",
    "uses_web_bot_auth",
    "is_64_byte_nonce",
    "valid_wba_nonce",
    "verify_directory_response",
    "verify_wba_governance_issuer",
    "verify_wba_request",
    "wba_revocation_status",
]


class _NoRevocationListError(Exception):
    """The origin answered 404/410: it serves no revocation list."""


class OriginRevocationChecker:
    """Check WBA keys against each origin's signed revocation list.

    The list lives at ``/.well-known/governance-revocations.json`` on the
    origin (``adcp-gov-revocation+jws``), signed by a key from the origin's
    directory. :meth:`status` reports, per key:

    * ``"revoked"``: the last verified list revokes it, however stale;
    * ``"current"``: a list within ``next_update`` plus grace does not;
    * ``"no_list"``: the origin answers 404/410 and no list was ever verified.
      The identity is established but cannot be bound for spend;
    * ``"stale"``: the list cannot be read or verified, is past grace, or
      its revocations exceed this checker's fact limits. Spend-committing
      calls fail closed in the authorization layer; other calls proceed;
    * ``"unavailable"``: too many discoveries are in flight to check now.
      Every bound call fails closed with ``SERVICE_UNAVAILABLE``.

    Revoked facts learned from authenticated lists are retained for the
    checker's lifetime and never weakened or evicted; fact overflow refuses
    the offending origin (it stays ``"stale"``) rather than evicting another
    origin's revocations. Per-origin list state is a least-recently-used
    cache of ``max_origins`` origins and ``max_origins_per_operator`` per
    operator (a registrable domain; every tenant of a PSL PRIVATE hosting
    suffix counts together), so a flood of origins cannot lock new origins
    out. This state is in memory: revocations learned by one process are
    lost on restart and are not shared across replicas.
    """

    def __init__(
        self,
        signer_keys: WbaSignerKeyResolver,
        *,
        fetcher: Any = None,
        max_origins: int = 256,
        max_origins_per_operator: int = 8,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_facts_per_origin: int = 2048,
        max_facts_per_operator: int = 8192,
        max_history_facts: int = 65536,
    ) -> None:
        if any(
            type(limit) is not int or limit <= 0
            for limit in (
                max_origins,
                max_origins_per_operator,
                max_facts_per_origin,
                max_facts_per_operator,
                max_history_facts,
            )
        ):
            raise ValueError("origin admission limits must be positive")
        self._signer_keys = signer_keys
        self._fetcher = fetcher
        self._max_origins = max_origins
        self._max_origins_per_operator = max_origins_per_operator
        self._clock = clock
        self._wall_clock = wall_clock
        self._max_facts_per_origin = max_facts_per_origin
        self._max_facts_per_operator = max_facts_per_operator
        self._max_history_facts = max_history_facts
        self._revoked_facts: dict[str, set[tuple[str, str]]] = {}
        self._operator_fact_counts: dict[str, int] = {}
        self._history_fact_count = 0
        self._checkers: OrderedDict[str, Any] = OrderedDict()
        self._pending: dict[str, asyncio.Task[Any]] = {}
        # origin -> (retry_after, served no list)
        self._failures: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def __call__(self, origin: str, keyid: str) -> bool:
        """Compatibility check: True when revoked; raises when undecidable.

        Prefer :meth:`status`, which distinguishes an origin with no list
        from one whose list is stale.
        """
        status = await self.status(origin, keyid)
        if status in {"revoked", "current"}:
            return status == "revoked"
        raise WbaUnavailableError(f"origin revocation status is {status}")

    def _retained(self, origin: str, keyid: str) -> bool:
        return ("kid", keyid) in self._revoked_facts.get(origin, set())

    async def status(self, origin: str, keyid: str) -> WbaRevocationStatus:
        """The key's revocation state; see the class docstring."""
        try:
            origin = _https_origin(origin)
        except ValueError:
            return "stale"
        if self._retained(origin, keyid):
            return "revoked"
        async with self._lock:
            checker = self._checkers.get(origin)
            task = None
            if checker is not None:
                self._checkers.move_to_end(origin)
            else:
                failure = self._failures.get(origin)
                if failure is not None and failure[0] > self._clock():
                    self._failures.move_to_end(origin)
                    return "no_list" if failure[1] else "stale"
                task = self._pending.get(origin)
                if task is None:
                    if not self._make_room(origin):
                        # Local backpressure (concurrent discoveries): no
                        # bound call may act until the list can be read.
                        return "unavailable"
                    task = asyncio.create_task(self._discover(origin))
                    task.add_done_callback(
                        lambda done: done.exception() if not done.cancelled() else None
                    )
                    self._pending[origin] = task
        if task is not None:
            try:
                checker = await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except _NoRevocationListError:
                return "no_list"
            except Exception:
                return "revoked" if self._retained(origin, keyid) else "stale"
        assert checker is not None
        try:
            return "revoked" if await checker(keyid) else "current"
        except asyncio.CancelledError:
            raise
        except Exception:
            # A stale list still revokes everything it revoked.
            if checker.retains_revoked_key(keyid) or self._retained(origin, keyid):
                return "revoked"
            return "stale"

    def _make_room(self, origin: str) -> bool:
        """Admit ``origin``, evicting least-recently-used checker state if needed.

        Called with the lock held. Checker state is a cache: evicting it never
        forgets a revocation, because revoked facts are kept separately and
        are checked first. Only concurrent discoveries can refuse admission,
        so a flood of origins cannot lock new origins out.
        """
        operator = self._operator(origin)

        def group(origins: Any) -> list[str]:
            return [other for other in origins if self._operator(other) == operator]

        while len(group(self._checkers)) + len(group(self._pending)) >= (
            self._max_origins_per_operator
        ):
            evictable = group(self._checkers)
            if not evictable:
                return False
            del self._checkers[evictable[0]]
        while len(self._checkers) + len(self._pending) >= self._max_origins:
            if not self._checkers:
                return False
            self._checkers.popitem(last=False)
        return True

    @staticmethod
    def _operator(origin: str) -> str:
        # Origins without a registrable domain cannot bind through trust.json,
        # but still share a hostname bucket for anonymous identity discovery.
        return _operator_bucket(origin)

    def _admit_revocations(self, record: Any) -> bool:
        # Called synchronously after authentication and schema validation.
        # No await separates checking shared counters from committing them.
        origin = record.issuer
        operator = self._operator(origin)
        known = self._revoked_facts.get(origin, set())
        facts = {("kid", value) for value in record.revoked_kids} | {
            ("jti", value) for value in record.revoked_jtis
        }
        added = len(facts - known)
        if (
            len(known | facts) > self._max_facts_per_origin
            or self._operator_fact_counts.get(operator, 0) + added > self._max_facts_per_operator
            or self._history_fact_count + added > self._max_history_facts
        ):
            return False
        self._revoked_facts[origin] = known | facts
        self._operator_fact_counts[operator] = self._operator_fact_counts.get(operator, 0) + added
        self._history_fact_count += added
        return True

    async def _discover(self, origin: str) -> Any:
        from adcp.signing.revocation_fetcher import (
            AsyncCachingRevocationChecker,
            RevocationListFetchError,
        )

        checker = None
        try:

            async def resolve(keyid: str) -> dict[str, Any] | None:
                resolved = await self._signer_keys(keyid, signer_origin=origin)
                return dict(resolved.jwk) if resolved is not None else None

            checker = AsyncCachingRevocationChecker.from_issuer_origin(
                origin,
                jwks_resolver=resolve,
                fetcher=self._fetcher,
                grace_multiplier=4,
                clock=self._clock,
                wall_clock=self._wall_clock,
                history_admission=self._admit_revocations,
                # The profile's revocation-list JWS is EdDSA only.
                allowed_algs=frozenset({"EdDSA"}),
            )
            # Authenticated revocations are permanent history. A 404 or
            # transport failure with no signed evidence consumes no slot.
            await checker.aprime()
            async with self._lock:
                self._checkers[origin] = checker
                self._failures.pop(origin, None)
            return checker
        except Exception as exc:
            no_list = (
                isinstance(exc, RevocationListFetchError)
                and exc.status_code in {404, 410}
                and (checker is None or not checker.has_verified_list)
            )
            async with self._lock:
                if checker is not None and checker.has_verified_list:
                    # Freshness failure denies spend but cannot erase
                    # revocations learned from the authenticated document.
                    self._checkers[origin] = checker
                self._failures[origin] = (self._clock() + 60, no_list)
                self._failures.move_to_end(origin)
                while len(self._failures) > self._max_origins:
                    self._failures.popitem(last=False)
            if no_list:
                raise _NoRevocationListError from exc
            raise
        finally:
            async with self._lock:
                self._pending.pop(origin, None)
