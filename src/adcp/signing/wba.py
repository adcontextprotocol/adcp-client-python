"""Experimental AdCP 3.3 Web Bot Auth transport profile.

Verification establishes an origin identity, never commercial authority. It
needs only the signer's key directory and a replay store
(:func:`verify_wba_request`), so it can run in front of the AdCP server.
Revocation (:func:`check_wba_revocation`), bindings and grants are the
authorization layer that runs after it. Nothing on the signature path reads a
trust record or a revocation list, and the signature path answers only with
Web Bot Auth statuses and headers (DR-0023): 400, 403 with or without
``Accept-Signature``, and 429.

Governance JWS verification continues to use the RFC 9421 discovery chain.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
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
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from http_sfv.dictionary import Dictionary
from http_sfv.item import InnerList, Item

from adcp.signing._bounded_http import async_read_limited_bytes
from adcp.signing._header_precheck import strict_header_precheck
from adcp.signing._strict_json import StrictJsonError, parse_strict_json
from adcp.signing._structured_fields import dictionary_has_parameter, reject_dictionary_duplicates
from adcp.signing.canonical import _lookup, canonical_origin
from adcp.signing.crypto import (
    b64url_decode,
    b64url_encode,
    public_key_from_jwk,
    sign_signature_base,
    verify_signature,
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
ALG_RSA_PSS_SHA512 = "rsa-pss-sha512"

#: The signer's nonce is 64 random bytes (RECOMMENDED by the architecture
#: draft §4.2.2, and required by Cloudflare's ``web-bot-auth`` verifier). The
#: verifier accepts any 22-256 character nonce (see :func:`valid_wba_nonce`).
WBA_NONCE_BYTES = 64
#: Seconds :func:`sign_key_directory` backdates ``created`` by, so a
#: verifier whose clock is slightly behind still accepts a fresh directory.
DIRECTORY_SKEW_ALLOWANCE_SECONDS = 300
#: How long a cached directory keeps verifying while its refreshes fail
#: (WBA §6.10), counted from the last successful fetch.
DEFAULT_DIRECTORY_MAX_STALE_SECONDS = 86400

_DIRECTORY_MEDIA_TYPES = frozenset(
    {DIRECTORY_MEDIA_TYPE, "application/jwk-set+json", "application/json"}
)
_DIRECTORY_MAX_BYTES = 256 * 1024
_DIRECTORY_MAX_KEYS = 100
_DIRECTORY_CACHE_SECONDS = 300
_DISCOVERY_NEGATIVE_SECONDS = 60
_DIRECTORY_CREATED_SKEW_SECONDS = 60
_REQUEST_SKEW_SECONDS = 60
_RSA_MIN_BITS, _RSA_MAX_BITS = 2048, 8192
_PRIVATE_JWK_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "k", "oth"})

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

    The architecture draft (§4.2.2) RECOMMENDS 64 random bytes, as unpadded
    base64url or padded standard base64, and Cloudflare's ``web-bot-auth``
    verifier requires it. This SDK's signer always sends one.
    """
    if type(nonce) is not str or not nonce.isascii():
        return False
    candidates: list[tuple[bytes, str]] = []
    try:
        raw = base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4))
        candidates.append((raw, b64url_encode(raw)))
    except (binascii.Error, ValueError):
        # Not base64url; the standard-base64 form is tried next.
        pass
    try:
        raw = base64.b64decode(nonce, validate=True)
        candidates.append((raw, base64.b64encode(raw).decode("ascii")))
    except (binascii.Error, ValueError):
        # Not standard base64 either; only a decoded candidate can match.
        pass
    return any(len(raw) == WBA_NONCE_BYTES and encoded == nonce for raw, encoded in candidates)


def valid_wba_nonce(nonce: object) -> bool:
    """Whether a received nonce is acceptable: 22 to 256 printable characters.

    64 bytes is RECOMMENDED for signers (architecture draft §4.2.2), and
    httpsig-02 adds no nonce requirement, so a compliant signer may send a
    shorter nonce. The 22-character floor keeps at least 128 bits of entropy
    in a base64 encoding.
    """
    return (
        type(nonce) is str
        and 22 <= len(nonce) <= 256
        and all(0x21 <= ord(c) <= 0x7E for c in nonce)
    )


class WbaVerificationError(ValueError):
    """A Web Bot Auth signature-path failure: an HTTP status and headers.

    ``status`` is 400 (a field does not parse), 403 (a signature, profile
    check, or key discovery fails; with ``Accept-Signature`` unless the key is
    revoked) or 429 (capacity). Answer it with
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
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.label = label
        self.headers: dict[str, str] = {}
        if status == 403 and challenge:
            self.headers["Accept-Signature"] = accept_signature_value(label)


def accept_signature_value(label: str = "sig1") -> str:
    """``Accept-Signature`` naming what the profile requires (RFC 9421 §5.1).

    Parameters indicate requirements, not values to sign; the nonce is a
    fresh 64-byte value the signer may use on its next attempt.
    """
    member = InnerList(
        [
            Item(c)
            for c in ("@method", "@target-uri", "@authority", "content-type", "content-digest")
        ]
    )
    agent = Item("signature-agent")
    agent.params["key"] = label
    member.append(agent)
    member.params.update(
        {
            "created": True,
            "expires": True,
            "keyid": True,
            "nonce": _new_nonce(),
            "tag": WBA_TAG,
        }
    )
    return str(Dictionary({label: member}))


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


def _dictionary(value: str | None, *, missing_status: int = 400) -> Dictionary:
    if value is None:
        raise WbaVerificationError("missing signature field", status=missing_status)
    if len(value) > 65536:
        raise WbaVerificationError("oversized signature field", status=400)
    try:
        reject_dictionary_duplicates(value)
        result = Dictionary()
        result.parse(value.encode("ascii"))
        if not result:
            raise ValueError("empty dictionary")
        return result
    except (ValueError, UnicodeError) as exc:
        raise WbaVerificationError(f"malformed signature field: {exc}", status=400) from exc


def uses_web_bot_auth(headers: Mapping[str, str]) -> bool:
    """Select WBA from either marker; never fall back after a WBA failure."""
    if _lookup(headers, "signature-agent") is not None:
        return True
    raw = _lookup(headers, "signature-input")
    if raw is None:
        return False
    # Observe every explicit tag, including one an ambiguous duplicate would
    # overwrite. Text inside nonce/component strings cannot select a profile.
    return dictionary_has_parameter(raw, "tag", WBA_TAG)


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
    """Return an RFC 7638 thumbprint for supported public key material."""
    digest = jwk_thumbprint(jwk)
    if digest is None:
        raise ValueError("incomplete public JWK")
    return b64url_encode(digest)


def _int_member(value: int) -> str:
    return b64url_encode(value.to_bytes((value.bit_length() + 7) // 8, "big"))


WbaPrivateKey = ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey


def public_directory_key(private_key: WbaPrivateKey) -> dict[str, Any]:
    """Export a WBA public JWK, with a thumbprint kid and no purpose claim.

    Ed25519, P-256 and RSA (2048 to 8192 bits, used with ``rsa-pss-sha512``)
    keys are supported.
    """
    public = private_key.public_key()
    jwk: dict[str, Any]
    if isinstance(public, ed25519.Ed25519PublicKey):
        jwk = {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": b64url_encode(
                public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            ),
        }
    elif isinstance(public, ec.EllipticCurvePublicKey) and isinstance(public.curve, ec.SECP256R1):
        numbers = public.public_numbers()
        jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": b64url_encode(numbers.x.to_bytes(32, "big")),
            "y": b64url_encode(numbers.y.to_bytes(32, "big")),
        }
    elif isinstance(public, rsa.RSAPublicKey):
        rsa_numbers = public.public_numbers()
        if not _RSA_MIN_BITS <= public.key_size <= _RSA_MAX_BITS:
            raise ValueError("Web Bot Auth RSA keys must be 2048 to 8192 bits")
        jwk = {"kty": "RSA", "n": _int_member(rsa_numbers.n), "e": _int_member(rsa_numbers.e)}
    else:
        raise ValueError("Web Bot Auth supports Ed25519, P-256 and RSA keys")
    jwk.update({"kid": thumbprint_key_id(jwk), "use": "sig"})
    return jwk


def _canonical_b64url(value: object) -> bool:
    if type(value) is not str or not value or not value.isascii():
        return False
    try:
        return b64url_encode(b64url_decode(value)) == value
    except (binascii.Error, ValueError):
        return False


def _rsa_public_key(jwk: Mapping[str, Any]) -> rsa.RSAPublicKey:
    modulus = b64url_decode(str(jwk.get("n", "")))
    exponent = b64url_decode(str(jwk.get("e", "")))
    if not modulus or modulus[0] == 0 or not exponent or len(exponent) > 4 or exponent[0] == 0:
        raise ValueError("invalid RSA key members")
    n = int.from_bytes(modulus, "big")
    e = int.from_bytes(exponent, "big")
    if not _RSA_MIN_BITS <= n.bit_length() <= _RSA_MAX_BITS:
        raise ValueError("RSA modulus size not allowed")
    if e < 3 or e > 2**31 - 1 or e % 2 == 0:
        raise ValueError("RSA exponent not allowed")
    return rsa.RSAPublicNumbers(e, n).public_key()


def _algorithm(jwk: Mapping[str, Any]) -> str:
    """Derive the signature algorithm and check the key may verify."""
    shape = (str(jwk.get("kty")), str(jwk.get("crv")))
    alg = {
        ("OKP", "Ed25519"): "ed25519",
        ("EC", "P-256"): "ecdsa-p256-sha256",
        ("RSA", "None"): ALG_RSA_PSS_SHA512,
    }.get(shape)
    if alg is None or jwk.get("use", "sig") != "sig" or _PRIVATE_JWK_MEMBERS & set(jwk):
        raise WbaVerificationError("unsupported directory key")
    # One key, one thumbprint: key members must be canonical base64url.
    for member in ("x", "y", "n", "e"):
        if member in jwk and not _canonical_b64url(jwk[member]):
            raise WbaVerificationError("directory key member is not canonical base64url")
    if alg == ALG_RSA_PSS_SHA512:
        try:
            _rsa_public_key(jwk)
        except (ValueError, TypeError) as exc:
            raise WbaVerificationError("unsupported RSA directory key") from exc
    jws_alg = {"ed25519": "EdDSA", "ecdsa-p256-sha256": "ES256", ALG_RSA_PSS_SHA512: "PS512"}[alg]
    if jwk.get("alg") not in {None, jws_alg}:
        raise WbaVerificationError("directory key algorithm mismatch")
    if "key_ops" in jwk and (
        not isinstance(jwk["key_ops"], list) or "verify" not in jwk["key_ops"]
    ):
        raise WbaVerificationError("directory key cannot verify")
    return alg


def _sign(alg: str, private_key: WbaPrivateKey, base: bytes) -> bytes:
    if alg == ALG_RSA_PSS_SHA512:
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("alg/private_key mismatch: expected an RSA key")
        return private_key.sign(
            base,
            padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64),
            hashes.SHA512(),
        )
    if isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("alg/private_key mismatch: RSA keys sign with rsa-pss-sha512")
    return sign_signature_base(alg=alg, private_key=private_key, signature_base=base)


def _verify(alg: str, jwk: Mapping[str, Any], base: bytes, signature: bytes) -> bool:
    if alg == ALG_RSA_PSS_SHA512:
        try:
            _rsa_public_key(jwk).verify(
                signature,
                base,
                padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64),
                hashes.SHA512(),
            )
            return True
        except InvalidSignature:
            return False
    return verify_signature(
        alg=alg,
        public_key=public_key_from_jwk(dict(jwk)),
        signature_base=base,
        signature=signature,
    )


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


def _target_uri(url: str) -> str:
    """``@target-uri`` per RFC 9421 §2.2.2: the target URI as received, unchanged."""
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url) or not url.isascii():
        raise WbaVerificationError("request URL is not a valid target URI", status=400)
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or parts.fragment:
        raise WbaVerificationError("request URL is not an absolute target URI")
    return url


def _base(
    method: str,
    url: str,
    headers: Mapping[str, str],
    member: InnerList,
    *,
    directory_response: bool = False,
) -> bytes:
    """Build the RFC 9421 §2.5 signature base for ``member``.

    ``url`` is the target URI as sent or received. ``@target-uri`` is that
    string unchanged; ``@authority`` is its normalized authority. The
    canonical origin applies only to the ``Signature-Agent`` identity.
    """
    lines: list[str] = []
    for component in member:
        name = component.value
        if type(name) is not str:
            raise WbaVerificationError("component must be a string", status=400)
        params = component.params
        if name.startswith("@"):
            expected = {"req": True} if directory_response and name == "@authority" else {}
            if dict(params) != expected:
                raise WbaVerificationError("unsupported derived component parameters")
            if name == "@method":
                value = method.upper()
            elif name == "@target-uri":
                value = _target_uri(url)
            elif name == "@authority":
                value = _received_authority(url)
            else:
                raise WbaVerificationError("unsupported derived component")
        else:
            if name != name.lower():
                raise WbaVerificationError("covered field names must be lowercase")
            raw = _lookup(headers, name)
            if raw is None:
                raise WbaVerificationError("missing covered field")
            if any(c in raw for c in "\r\n\x00"):
                raise WbaVerificationError("covered field carries a control character", status=400)
            if params:
                if set(params) != {"key"} or type(params["key"]) is not str:
                    raise WbaVerificationError("unsupported field component parameters")
                dictionary = _dictionary(raw)
                if params["key"] not in dictionary:
                    raise WbaVerificationError("missing covered dictionary member")
                value = str(dictionary[params["key"]])
            else:
                value = raw.strip(" \t")
        lines.append(f"{component}: {value}")
    lines.append(f'"@signature-params": {member}')
    return "\n".join(lines).encode("utf-8")


def _window(member: InnerList, *, now: float, directory: bool = False, label: str = "sig1") -> None:
    params = member.params
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


def _verify_crypto(member: InnerList, signature: Any, jwk: Mapping[str, Any], base: bytes) -> str:
    alg = _algorithm(jwk)
    declared = member.params.get("alg", alg)
    if type(declared) is not str or declared != alg:
        raise WbaVerificationError("signature algorithm does not match key")
    if not isinstance(signature, Item) or not isinstance(signature.value, bytes):
        raise WbaVerificationError("signature must be sf-binary", status=400)
    try:
        valid = _verify(alg, jwk, base, signature.value)
    except (ValueError, KeyError, TypeError) as exc:
        raise WbaVerificationError("invalid directory key") from exc
    if not valid:
        raise WbaVerificationError("signature did not verify")
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
    included. The nonce defaults to 64 random bytes. A request that already
    carries signature fields is refused rather than stripped: another
    party's signature is never silently dropped.
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
    from adcp.signing.signer import _validate_sf_key

    _validate_sf_key(label, field="label")
    jwk = public_directory_key(private_key)
    alg = _algorithm(jwk)
    created = int(time.time()) if created is None else created
    nonce = _new_nonce() if nonce is None else nonce
    if not valid_wba_nonce(nonce):
        raise ValueError(
            "nonce must carry at least 128 bits of entropy and be at most 256 characters"
        )
    outgoing = {"Signature-Agent": str(Dictionary({label: Item(origin)}))}
    components = [Item(c) for c in ("@method", "@target-uri", "@authority")]
    if body:
        if _lookup(headers, "content-type") is None:
            raise ValueError("body-bearing WBA requests require Content-Type")
        outgoing["Content-Digest"] = compute_content_digest_sha256(body)
        components.extend(Item(c) for c in ("content-type", "content-digest"))
    agent = Item("signature-agent")
    agent.params["key"] = label
    components.append(agent)
    member = InnerList(list(components))
    member.params.update(
        {
            "created": created,
            "expires": created + expires_in_seconds,
            "nonce": nonce,
            "keyid": jwk["kid"],
            "alg": alg,
            "tag": WBA_TAG,
        }
    )
    signature = _sign(
        alg,
        private_key,
        _base(
            method,
            url,
            {
                **{k.lower(): v for k, v in headers.items()},
                **{k.lower(): v for k, v in outgoing.items()},
            },
            member,
        ),
    )
    outgoing["Signature-Input"] = str(Dictionary({label: member}))
    outgoing["Signature"] = str(Dictionary({label: Item(signature)}))
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
    #: Earliest signature ``expires`` for a signed directory; ``None`` if unsigned.
    signatures_expire: float | None


def _parse_directory_keys(body: bytes) -> dict[str, dict[str, Any]]:
    try:
        document = parse_strict_json(body)
    except StrictJsonError as exc:
        raise WbaVerificationError("invalid directory JSON") from exc
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not 1 <= len(keys) <= _DIRECTORY_MAX_KEYS:
        raise WbaVerificationError("invalid directory key count")
    index: dict[str, dict[str, Any]] = {}
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
            # A plain JWK Set may hold keys for other uses: skip what the
            # profile cannot verify with.
            continue
        if thumbprint in index:
            raise WbaVerificationError("directory repeats a key")
        # A key is found by its thumbprint; a kid, when present, does not
        # affect lookup (draft-02 §5.5: kid MAY be present).
        index[thumbprint] = {**jwk, "kid": thumbprint}
    if not index:
        raise WbaVerificationError("directory holds no usable key")
    return index


def _verify_directory(
    origin: str, response: DirectoryResponse, *, now: float, require_signed: bool
) -> _VerifiedDirectory:
    origin = _https_origin(origin)
    media_type = (_lookup(response.headers, "content-type") or "").split(";", 1)[0]
    if (
        response.status != 200
        or media_type.strip().lower() not in _DIRECTORY_MEDIA_TYPES
        or len(response.body) > _DIRECTORY_MAX_BYTES
    ):
        raise WbaVerificationError("invalid key directory response")
    raw_inputs = _lookup(response.headers, "signature-input")
    raw_signatures = _lookup(response.headers, "signature")
    if raw_inputs is None and raw_signatures is None:
        # Appendix B.1 signing is RECOMMENDED, not required.
        if require_signed:
            raise WbaVerificationError("key directory is not signed")
        return _VerifiedDirectory(_parse_directory_keys(response.body), None)
    digest = _lookup(response.headers, "content-digest")
    # The digest covers the exact bytes served, a UTF-8 BOM included.
    if digest is None or not content_digest_matches(digest, response.body):
        raise WbaVerificationError("directory digest mismatch")
    keys = _parse_directory_keys(response.body)
    inputs = _dictionary(raw_inputs)
    signatures = _dictionary(raw_signatures)
    if set(signatures) != set(inputs):
        raise WbaVerificationError("directory signature labels differ")
    signed: dict[str, dict[str, Any]] = {}
    earliest = float("inf")
    for label, member in inputs.items():
        if not isinstance(member, InnerList) or member.params.get("tag") != DIRECTORY_TAG:
            raise WbaVerificationError("invalid directory signature profile")
        _window(member, now=now, directory=True, label=label)
        covered = {str(c) for c in member}
        # Exact identifiers: a "content-digest" member or a bare @authority
        # does not bind the response's digest and request authority.
        if not {'"@authority";req', '"content-digest"'} <= covered:
            raise WbaVerificationError("directory signature does not bind authority and digest")
        keyid = member.params["keyid"]
        if keyid not in keys or keyid in signed:
            raise WbaVerificationError("directory signature key is absent or repeated")
        _verify_crypto(
            member,
            signatures.get(label),
            keys[keyid],
            _base(
                "GET", origin + DIRECTORY_PATH, response.headers, member, directory_response=True
            ),
        )
        signed[keyid] = keys[keyid]
        earliest = min(earliest, float(member.params["expires"]))
    # In a signed directory only keys carrying a verified possession proof
    # are usable; an unsigned key alongside them is ignored.
    return _VerifiedDirectory(signed, earliest)


def verify_directory_response(
    origin: str, response: DirectoryResponse, *, now: float, require_signed: bool = False
) -> dict[str, dict[str, Any]]:
    """Validate a key directory served at ``origin``; return keys by thumbprint.

    A plain, unsigned JWK Set is accepted unless ``require_signed``; every key
    is indexed by its RFC 7638 thumbprint (a ``kid`` is optional). When the
    response carries signatures (WBA Appendix B.1), every one must verify,
    cover exactly ``"@authority";req`` and ``"content-digest"``, and only the
    keys they prove are returned.
    """
    return _verify_directory(origin, response, now=now, require_signed=require_signed).keys


def sign_key_directory(
    *,
    agent_origin: str,
    private_keys: Sequence[WbaPrivateKey],
    created: int | None = None,
    expires_in_seconds: int = 86400,
) -> DirectoryResponse:
    """Generate a signed directory response; republish before its expires time.

    With no explicit ``created``, the signatures are backdated by
    :data:`DIRECTORY_SKEW_ALLOWANCE_SECONDS` (verifiers allow little clock
    skew on directory signatures) and still expire ``expires_in_seconds``
    from now.
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
    inputs, signatures = Dictionary(), Dictionary()
    for number, (key, jwk) in enumerate(zip(private_keys, keys), 1):
        authority = Item("@authority")
        authority.params["req"] = True
        member = InnerList([authority, Item("content-digest")])
        alg = _algorithm(jwk)
        member.params.update(
            {
                "created": created,
                "expires": expires,
                "keyid": jwk["kid"],
                "alg": alg,
                "tag": DIRECTORY_TAG,
            }
        )
        label = f"key{number}"
        inputs[label] = member
        signatures[label] = Item(
            _sign(
                alg,
                key,
                _base("GET", origin + DIRECTORY_PATH, headers, member, directory_response=True),
            )
        )
    headers.update({"Signature-Input": str(inputs), "Signature": str(signatures)})
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
                headers={
                    "Accept": (
                        f"{DIRECTORY_MEDIA_TYPE}, application/jwk-set+json;q=0.9, "
                        "application/json;q=0.8"
                    ),
                    "Accept-Encoding": "identity",
                },
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
        require_signed_directories: bool = False,
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
        self._require_signed = require_signed_directories
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
                    raise WbaVerificationError(refused, status=429)
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
                verified = _verify_directory(
                    origin, response, now=self._clock(), require_signed=self._require_signed
                )
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
            expires = now + self._max_stale
            if verified.signatures_expire is not None:
                expires = min(expires, verified.signatures_expire)
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
    replay_status: Literal[403, 429] = 429,
    authorities: Collection[str] | None = None,
) -> tuple[VerifiedSigner, ...]:
    """Verify every WBA signature and relay coverage; return established origins.

    ``url`` is the target URI as received (RFC 9421 §2.2.2): build it from
    the request line, not a decoded path. Verification needs only the key
    directory (``signer_keys``) and the replay store, reads no trust record
    and grants no authority. The first signer is the originating signer and
    the AdCP caller; later ones are relays that covered it.

    Failures raise :class:`WbaVerificationError` (or its
    :class:`WbaDiscoveryError` subclass for an unresolvable directory), whose
    status and headers are the whole answer. A replay-store failure raises
    :class:`WbaUnavailableError`, an application-layer outage.

    A replayed nonce is 429 by default; ``replay_status=403`` is a
    compatibility switch for #7894's older replay vector. ``authorities``
    lists the ``host[:port]`` values this endpoint serves; a request whose
    ``@authority`` is not among them is refused before key discovery.

    ``revocation_checker`` is optional and belongs to the authorization
    layer. When given, :func:`check_wba_revocation` runs after verification:
    a revoked key raises :class:`WbaRevokedKeyError`, and every signer
    records its revocation state.
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
    raw_agent = _lookup(headers, "signature-agent")
    # Selection by tag alone leaves every Signature-Agent member absent, which
    # fails each signature with 403 below.
    agents = _dictionary(raw_agent) if raw_agent is not None else Dictionary()
    wba = [
        (label, member)
        for label, member in inputs.items()
        if isinstance(member, InnerList) and member.params.get("tag") == WBA_TAG
    ]
    if not wba:
        raise WbaVerificationError("Signature-Agent requires the Web Bot Auth profile")
    if set(signatures) != set(inputs):
        raise WbaVerificationError("signature labels do not match", status=400)
    if authorities is not None:
        # A signature captured at another seller cannot be replayed to this
        # endpoint under that seller's Host.
        allowed = {_received_authority("https://" + a) for a in authorities}
        if _received_authority(url) not in allowed:
            raise WbaVerificationError(
                "request authority is not served by this verifier", label=wba[0][0]
            )
    verified: list[tuple[VerifiedSigner, str, float]] = []
    earlier: list[str] = []
    for label, member in wba:
        _window(member, now=now, label=label)
        nonce = member.params.get("nonce")
        if not valid_wba_nonce(nonce):
            raise WbaVerificationError("missing or invalid nonce", label=label)
        nonce = cast(str, nonce)
        agent = agents.get(label)
        if (
            not isinstance(agent, Item)
            or type(agent.value) is not str
            or agent.params.get("type", "directory") != "directory"
        ):
            raise WbaVerificationError("missing supported Signature-Agent member", label=label)
        try:
            origin = _https_origin(agent.value)
        except ValueError as exc:
            raise WbaVerificationError("invalid signer origin", label=label) from exc
        covered = {str(c) for c in member}
        required = {
            '"@method"',
            '"@target-uri"',
            '"@authority"',
            f'"signature-agent";key="{label}"',
        }
        if body:
            required.update({'"content-type"', '"content-digest"'})
        for previous in earlier:
            required.update(
                f'"{field}";key="{previous}"'
                for field in ("signature-agent", "signature-input", "signature")
            )
            # RFC 9421 §2.2.8 also requires all components of the signature
            # being covered, including application-specific header fields.
            required.update(str(component) for component in inputs[previous])
        if not required <= covered or len(covered) != len(member):
            raise WbaVerificationError("missing or repeated covered component", label=label)
        keyid = member.params["keyid"]
        try:
            key = await signer_keys(keyid, signer_origin=origin)
        except WbaVerificationError as exc:
            exc.label = label
            if exc.status == 403 and "Accept-Signature" in exc.headers:
                exc.headers["Accept-Signature"] = accept_signature_value(label)
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
        try:
            alg = _verify_crypto(
                member, signatures.get(label), key.jwk, _base(method, url, headers, member)
            )
        except WbaVerificationError as exc:
            exc.label = label
            raise
        if body:
            digest = _lookup(headers, "content-digest")
            if digest is None or not content_digest_matches(digest, body):
                raise WbaVerificationError("request digest mismatch", label=label)
        verified.append(
            (
                VerifiedSigner(
                    key_id=keyid,
                    alg=alg,
                    label=label,
                    verified_at=now,
                    agent_url=origin,
                    body_authenticated=bool(body),
                    profile=WBA_TAG,
                ),
                nonce,
                # One extra second keeps the nonce through the last second of
                # the accepted window, including a replay at expires + 60.
                max(member.params["expires"] - now + _REQUEST_SKEW_SECONDS + 1, 1),
            )
        )
        earlier.append(label)
    # Claim relays before the originator, so a relay replay does not burn
    # the originator's nonce. No nonce is claimed until every signature
    # verified.
    for signer, nonce, ttl in reversed(verified):
        try:
            claim = await asyncio.to_thread(
                _claim_replay_nonce, replay_store, signer.agent_url or "", nonce, ttl
            )
        except Exception as exc:
            raise WbaUnavailableError("replay store unavailable") from exc
        if claim == "replayed":
            # A reused nonce is 429 (security.mdx, Web Bot Auth identities).
            # replay_status=403 reproduces #7894's older negative/002 vector.
            raise WbaVerificationError("replayed nonce", status=replay_status, label=signer.label)
        if claim != "claimed":
            raise WbaVerificationError(
                "replay store capacity exceeded", status=429, label=signer.label
            )
    try:
        parsed = parse_strict_json(body) if body.strip() else None
    except StrictJsonError as exc:
        raise WbaVerificationError("malformed signed JSON body", status=400) from exc
    signers = tuple(replace(signer, parsed_body=parsed) for signer, _, _ in verified)
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

    Runs in the authorization layer, after verification. A revoked key, the
    originator's or a relay's, raises :class:`WbaRevokedKeyError` (403, no
    ``Accept-Signature``). Every other state is recorded in
    :attr:`VerifiedSigner.revocation` for the binding and grant gates.
    """
    checked: list[VerifiedSigner] = []
    for signer in signers:
        status = await wba_revocation_status(checker, signer.agent_url or "", signer.key_id)
        if status == "revoked":
            raise WbaRevokedKeyError(label=signer.label)
        checked.append(replace(signer, revocation=status))
    return tuple(checked)


class WbaRevocationStatusChecker(Protocol):
    """Report a WBA key's state in its origin's signed revocation list."""

    async def status(self, origin: str, keyid: str) -> WbaRevocationStatus:
        raise NotImplementedError("report the key's revocation state")


__all__ = [
    "ALG_RSA_PSS_SHA512",
    "DEFAULT_DIRECTORY_MAX_STALE_SECONDS",
    "DIRECTORY_MEDIA_TYPE",
    "DIRECTORY_SKEW_ALLOWANCE_SECONDS",
    "DirectoryResponse",
    "DiscoveredSignerKeys",
    "GENERIC_AUTHENTICATION_ERROR",
    "OriginRevocationChecker",
    "WBA_NONCE_BYTES",
    "WbaAuthorizationError",
    "WbaDiscoveryError",
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
