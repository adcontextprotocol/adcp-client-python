"""Experimental AdCP 3.3 Web Bot Auth transport profile.

Key discovery establishes an origin identity, never commercial authority.
Governance JWS verification continues to use the RFC 9421 discovery chain.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from http_sfv.dictionary import Dictionary
from http_sfv.item import InnerList, Item

from adcp.signing._bounded_http import async_read_limited_bytes
from adcp.signing._header_precheck import strict_header_precheck
from adcp.signing._strict_json import StrictJsonError, parse_strict_json
from adcp.signing._structured_fields import dictionary_has_parameter, reject_dictionary_duplicates
from adcp.signing.canonical import (
    _lookup,
    canonical_origin,
    canonicalize_authority,
    canonicalize_target_uri,
)
from adcp.signing.crypto import (
    PrivateKey,
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


class WbaVerificationError(ValueError):
    """A WBA failure, expressed as HTTP status and response headers."""

    def __init__(self, reason: str, *, status: int = 403, label: str = "sig1") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.headers: dict[str, str] = {}
        if status == 403:
            # RFC 9421 §5.1: parameters indicate requirements, not values to sign.
            member = InnerList(
                [
                    Item(c)
                    for c in (
                        "@method",
                        "@target-uri",
                        "@authority",
                        "content-type",
                        "content-digest",
                    )
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
                    "nonce": b64url_encode(secrets.token_bytes(32)),
                    "tag": WBA_TAG,
                }
            )
            self.headers["Accept-Signature"] = str(Dictionary({label: member}))


class WbaAuthorizationError(WbaVerificationError):
    """A key-policy rejection uses AdCP authentication semantics without a re-sign challenge.

    A revoked key establishes no identity. Principal and grant authorization
    after establishment uses the normal AdCP transport error binding.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.headers = {}


class WbaUnavailableError(WbaVerificationError):
    """Identity cannot be checked during an outage; retry without serving the request."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, status=503)


def _dictionary(value: str | None) -> Dictionary:
    if value is None or len(value) > 65536:
        raise WbaVerificationError("missing or oversized signature field", status=400)
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


def public_directory_key(private_key: PrivateKey) -> dict[str, Any]:
    """Export a WBA public JWK, with a thumbprint kid and no purpose claim."""
    public = private_key.public_key()
    if isinstance(public, ed25519.Ed25519PublicKey):
        from cryptography.hazmat.primitives import serialization

        jwk: dict[str, Any] = {
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
    else:
        raise ValueError("Web Bot Auth supports Ed25519 and P-256 keys")
    jwk.update({"kid": thumbprint_key_id(jwk), "use": "sig"})
    return jwk


def _algorithm(jwk: Mapping[str, Any]) -> str:
    shape = (str(jwk.get("kty")), str(jwk.get("crv")))
    alg = {("OKP", "Ed25519"): "ed25519", ("EC", "P-256"): "ecdsa-p256-sha256"}.get(shape)
    if alg is None or jwk.get("use", "sig") != "sig" or "d" in jwk:
        raise WbaVerificationError("unsupported directory key")
    if jwk.get("alg") not in {None, "EdDSA" if alg == "ed25519" else "ES256"}:
        raise WbaVerificationError("directory key algorithm mismatch")
    if "key_ops" in jwk and (
        not isinstance(jwk["key_ops"], list) or "verify" not in jwk["key_ops"]
    ):
        raise WbaVerificationError("directory key cannot verify")
    return alg


def _base(
    method: str,
    url: str,
    headers: Mapping[str, str],
    member: InnerList,
    *,
    directory_response: bool = False,
) -> bytes:
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
                value = canonicalize_target_uri(url)
            elif name == "@authority":
                value = canonicalize_authority(url)
            else:
                raise WbaVerificationError("unsupported derived component")
        else:
            raw = _lookup(headers, name)
            if raw is None:
                raise WbaVerificationError("missing covered field")
            if params:
                if set(params) != {"key"} or type(params["key"]) is not str:
                    raise WbaVerificationError("unsupported field component parameters")
                dictionary = _dictionary(raw)
                if params["key"] not in dictionary:
                    raise WbaVerificationError("missing covered dictionary member")
                value = str(dictionary[params["key"]])
            else:
                value = raw.strip()
        lines.append(f"{component}: {value}")
    lines.append(f'"@signature-params": {member}')
    return "\n".join(lines).encode("utf-8")


def _window(member: InnerList, *, now: float, directory: bool = False) -> None:
    params = member.params
    if type(params.get("tag")) is not str:
        raise WbaVerificationError("signature tag must be an sf-string")
    for name in ("created", "expires"):
        if type(params.get(name)) is not int:
            raise WbaVerificationError("missing integer time parameter")
    created, expires = params["created"], params["expires"]
    if expires <= created or (not directory and expires - created > 300):
        raise WbaVerificationError("invalid signature lifetime")
    if created > now + (0 if directory else 60) or expires < now - (0 if directory else 60):
        raise WbaVerificationError("signature outside its validity window")
    if type(params.get("keyid")) is not str or len(params["keyid"]) != 43:
        raise WbaVerificationError("missing thumbprint keyid")


def _verify_crypto(member: InnerList, signature: Any, jwk: Mapping[str, Any], base: bytes) -> str:
    alg = _algorithm(jwk)
    if member.params.get("alg", alg) != alg or type(member.params.get("alg", alg)) is not str:
        raise WbaVerificationError("signature algorithm does not match key")
    if not isinstance(signature, Item) or not isinstance(signature.value, bytes):
        raise WbaVerificationError("signature must be sf-binary", status=400)
    try:
        valid = verify_signature(
            alg=alg,
            public_key=public_key_from_jwk(dict(jwk)),
            signature_base=base,
            signature=signature.value,
        )
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
    private_key: PrivateKey,
    agent_origin: str,
    created: int | None = None,
    expires_in_seconds: int = 300,
    nonce: str | None = None,
    label: str = "sig1",
) -> dict[str, str]:
    """Sign with the opt-in WBA profile; infer alg and kid from the public key."""
    origin = _https_origin(agent_origin)
    if not 0 < expires_in_seconds <= 300:
        raise ValueError("expires_in_seconds must be in (0, 300]")
    if any(
        _lookup(headers, name) is not None
        for name in ("signature", "signature-input", "signature-agent")
    ):
        raise ValueError("use a relay signer to append to an already signed request")
    from adcp.signing.signer import _validate_sf_key

    _validate_sf_key(label, field="label")
    jwk = public_directory_key(private_key)
    created = int(time.time()) if created is None else created
    nonce = b64url_encode(secrets.token_bytes(32)) if nonce is None else nonce
    if not 22 <= len(nonce) <= 256:
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
    member = InnerList([c for c in components])
    member.params.update(
        {
            "created": created,
            "expires": created + expires_in_seconds,
            "nonce": nonce,
            "keyid": jwk["kid"],
            "alg": _algorithm(jwk),
            "tag": WBA_TAG,
        }
    )
    signature = sign_signature_base(
        alg=_algorithm(jwk),
        private_key=private_key,
        signature_base=_base(
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
    """Signed directory bytes and headers, retained as evidence of discovery."""

    body: bytes
    headers: Mapping[str, str]
    status: int = 200


def verify_directory_response(
    origin: str, response: DirectoryResponse, *, now: float
) -> dict[str, dict[str, Any]]:
    """Validate every directory key's response signature (WBA Appendix B.1)."""
    origin = _https_origin(origin)
    media_type = (_lookup(response.headers, "content-type") or "").split(";", 1)[0].strip()
    if (
        response.status != 200
        or media_type != DIRECTORY_MEDIA_TYPE
        or len(response.body) > 256 * 1024
    ):
        raise WbaVerificationError("invalid key directory response")
    try:
        document = parse_strict_json(response.body)
    except StrictJsonError as exc:
        raise WbaVerificationError("invalid directory JSON") from exc
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
        raise WbaVerificationError("invalid directory key count")
    index: dict[str, dict[str, Any]] = {}
    for jwk in keys:
        if (
            not isinstance(jwk, dict)
            or jwk.get("kid") != thumbprint_key_id(jwk)
            or jwk["kid"] in index
        ):
            raise WbaVerificationError("invalid or duplicate directory thumbprint")
        _algorithm(jwk)
        index[jwk["kid"]] = jwk
    digest = _lookup(response.headers, "content-digest")
    if digest is None or not content_digest_matches(digest, response.body):
        raise WbaVerificationError("directory digest mismatch")
    inputs = _dictionary(_lookup(response.headers, "signature-input"))
    signatures = _dictionary(_lookup(response.headers, "signature"))
    verified: set[str] = set()
    for label, member in inputs.items():
        if not isinstance(member, InnerList) or member.params.get("tag") != DIRECTORY_TAG:
            raise WbaVerificationError("invalid directory signature profile")
        _window(member, now=now, directory=True)
        covered = {str(c) for c in member}
        if not {'"@authority";req', '"content-digest"'} <= covered:
            raise WbaVerificationError("directory signature does not bind authority and digest")
        keyid = member.params["keyid"]
        if keyid not in index or keyid in verified:
            raise WbaVerificationError("directory signature key is absent or repeated")
        _verify_crypto(
            member,
            signatures.get(label),
            index[keyid],
            _base(
                "GET", origin + DIRECTORY_PATH, response.headers, member, directory_response=True
            ),
        )
        verified.add(keyid)
    if verified != set(index) or set(signatures) != set(inputs):
        raise WbaVerificationError("directory requires one signature per key")
    return index


def sign_key_directory(
    *,
    agent_origin: str,
    private_keys: Sequence[PrivateKey],
    created: int | None = None,
    expires_in_seconds: int = 86400,
) -> DirectoryResponse:
    """Generate a signed directory response; republish before its expires time."""
    origin = _https_origin(agent_origin)
    if not 1 <= len(private_keys) <= 100 or expires_in_seconds <= 0:
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
    created = int(time.time()) if created is None else created
    inputs, signatures = Dictionary(), Dictionary()
    for number, (key, jwk) in enumerate(zip(private_keys, keys), 1):
        authority = Item("@authority")
        authority.params["req"] = True
        member = InnerList([authority, Item("content-digest")])
        member.params.update(
            {
                "created": created,
                "expires": created + expires_in_seconds,
                "keyid": jwk["kid"],
                "alg": _algorithm(jwk),
                "tag": DIRECTORY_TAG,
            }
        )
        label = f"key{number}"
        inputs[label] = member
        signatures[label] = Item(
            sign_signature_base(
                alg=_algorithm(jwk),
                private_key=key,
                signature_base=_base(
                    "GET", origin + DIRECTORY_PATH, headers, member, directory_response=True
                ),
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
                "GET", url, headers={"Accept": DIRECTORY_MEDIA_TYPE, "Accept-Encoding": "identity"}
            ) as response:
                return DirectoryResponse(
                    status=response.status_code,
                    headers=dict(response.headers),
                    body=(
                        await async_read_limited_bytes(response, limit=256 * 1024)
                        if response.status_code == 200
                        else b""
                    ),
                )

    return await asyncio.wait_for(fetch(), timeout=10)


@dataclass(frozen=True)
class _DirectoryFailure:
    reason: str
    status: int
    unavailable: bool

    def exception(self) -> WbaVerificationError:
        return (
            WbaUnavailableError(self.reason)
            if self.unavailable
            else WbaVerificationError(self.reason, status=self.status)
        )


@dataclass
class _DirectoryCache:
    response: DirectoryResponse | None
    keys: dict[str, dict[str, Any]]
    expires: float
    retry_after: float = 0
    failure: _DirectoryFailure | None = None


class DiscoveredSignerKeys:
    """Discover many WBA signers using (covered origin, thumbprint), with bounded caching.

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
        self._fetcher, self._allow_private, self._max_origins, self._clock = (
            fetcher,
            allow_private,
            max_origins,
            clock,
        )
        self._cache: OrderedDict[str, _DirectoryCache] = OrderedDict()
        self._max_origins_per_operator = max_origins_per_operator
        self._failed_discoveries: OrderedDict[str, _DirectoryCache] = OrderedDict()
        self._lock = asyncio.Lock()
        self._legacy_resolver = legacy_resolver
        self._pending: dict[str, asyncio.Task[_DirectoryCache]] = {}

    async def __call__(
        self, keyid: str, *, signer_origin: str | None = None
    ) -> ResolvedSignerKey | None:
        if signer_origin is None:
            # Coexistence is explicit and never falls back from the WBA branch.
            return await self._legacy_resolver(keyid) if self._legacy_resolver is not None else None
        origin = _https_origin(signer_origin)
        async with self._lock:
            now = self._clock()
            cached = self._cache.get(origin) or self._failed_discoveries.get(origin)
            if cached is not None and cached.expires > now and cached.response is not None:
                self._cache.move_to_end(origin)
                key = cached.keys.get(keyid)
                return ResolvedSignerKey(origin, dict(key)) if key is not None else None
            if cached is not None and cached.retry_after > now:
                if cached.failure is not None:
                    raise cached.failure.exception()
                raise WbaUnavailableError("key directory discovery is cooling down")
            task = self._pending.get(origin)
            if task is None:
                admitted = self._cache.keys() | self._pending.keys()
                if origin not in admitted and len(admitted) >= self._max_origins:
                    raise WbaUnavailableError("directory discovery capacity exceeded")
                operator = OriginRevocationChecker._operator(origin)
                if (
                    origin not in admitted
                    and sum(
                        OriginRevocationChecker._operator(value) == operator for value in admitted
                    )
                    >= self._max_origins_per_operator
                ):
                    raise WbaUnavailableError("operator directory discovery capacity exceeded")
                task = asyncio.create_task(self._refresh(origin, cached))
                # A cancelled HTTP request must not cancel discovery for other
                # waiters or leave an unobserved exception in a background task.
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
                self._pending[origin] = task
        cached = await asyncio.shield(task)
        key = cached.keys.get(keyid)
        return ResolvedSignerKey(origin, dict(key)) if key is not None else None

    async def _refresh(self, origin: str, previous: _DirectoryCache | None) -> _DirectoryCache:
        try:
            response = await self._fetcher(
                origin + DIRECTORY_PATH, allow_private=self._allow_private
            )
            now = self._clock()
            if response.status not in (200, 404, 410):
                raise WbaUnavailableError("key directory temporarily unavailable")
            keys = verify_directory_response(origin, response, now=now)
            inputs = _dictionary(_lookup(response.headers, "signature-input"))
            expires = min(now + 300, *(float(m.params["expires"]) for m in inputs.values()))
            fresh = _DirectoryCache(response, keys, expires)
            async with self._lock:
                self._cache[origin] = fresh
                self._cache.move_to_end(origin)
                self._failed_discoveries.pop(origin, None)
            return fresh
        except Exception as exc:
            failure = (
                exc
                if isinstance(exc, WbaVerificationError)
                else (
                    WbaVerificationError("invalid key directory response")
                    if isinstance(exc, ValueError)
                    else WbaUnavailableError("key directory discovery failed")
                )
            )
            # Keep the previous signed snapshot as evidence. Expired keys never
            # authenticate requests, and a fetch failure never proves removal.
            async with self._lock:
                failed = previous or _DirectoryCache(None, {}, 0)
                failed.retry_after = self._clock() + 60
                failed.failure = _DirectoryFailure(
                    failure.reason, failure.status, isinstance(failure, WbaUnavailableError)
                )
                if failed.response is not None:
                    self._cache[origin] = failed
                else:
                    self._failed_discoveries[origin] = failed
                    self._failed_discoveries.move_to_end(origin)
                    while len(self._failed_discoveries) > self._max_origins:
                        self._failed_discoveries.popitem(last=False)
            if failure is exc:
                raise
            raise failure from exc
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
    revocation_checker: Any,
    now: float | None = None,
    raw_headers: Sequence[tuple[bytes, bytes]] | None = None,
) -> tuple[VerifiedSigner, ...]:
    """Verify all WBA signatures and relay coverage; return established origins only."""
    if replay_store is None:
        raise ValueError("Web Bot Auth requires a replay store")
    if revocation_checker is None:
        raise ValueError("Web Bot Auth requires an origin revocation checker")
    now = time.time() if now is None else now
    if strict_header_precheck(headers=headers, raw_headers=raw_headers, url=url) is not None:
        raise WbaVerificationError("ambiguous signature fields", status=400)
    if raw_headers is not None:
        raw_agents = [v for k, v in raw_headers if k.lower() == b"signature-agent"]
        if len(raw_agents) > 1:
            raise WbaVerificationError("repeated Signature-Agent field", status=400)
    inputs = _dictionary(_lookup(headers, "signature-input"))
    signatures = _dictionary(_lookup(headers, "signature"))
    agents = _dictionary(_lookup(headers, "signature-agent"))
    wba = [
        (label, member)
        for label, member in inputs.items()
        if isinstance(member, InnerList) and member.params.get("tag") == WBA_TAG
    ]
    if not wba:
        raise WbaVerificationError("Signature-Agent requires the Web Bot Auth profile")
    if set(signatures) != set(inputs):
        raise WbaVerificationError("signature labels do not match", status=400)
    verified: list[tuple[VerifiedSigner, str, float]] = []
    earlier: list[str] = []
    for label, member in wba:
        _window(member, now=now)
        nonce = member.params.get("nonce")
        if type(nonce) is not str or not 22 <= len(nonce) <= 256:
            raise WbaVerificationError("missing or invalid nonce", label=label)
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
        except WbaVerificationError:
            raise
        except Exception as exc:
            raise WbaUnavailableError("signer key resolution unavailable") from exc
        if (
            key is None
            or canonical_origin(key.agent_url) != origin
            or thumbprint_key_id(key.jwk) != keyid
        ):
            raise WbaVerificationError("key absent from the named origin", label=label)
        alg = _verify_crypto(
            member, signatures.get(label), key.jwk, _base(method, url, headers, member)
        )
        if body:
            digest = _lookup(headers, "content-digest")
            if digest is None or not content_digest_matches(digest, body):
                raise WbaVerificationError("request digest mismatch", label=label)
        try:
            revoked = await revocation_checker(origin, keyid)
        except WbaVerificationError:
            raise
        except Exception as exc:
            raise WbaUnavailableError("origin revocation status unavailable") from exc
        if revoked:
            raise WbaAuthorizationError("signing key revoked")
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
                member.params["expires"] - now + 60,
            )
        )
        earlier.append(label)
    for signer, nonce, ttl in verified:
        claim = await asyncio.to_thread(
            _claim_replay_nonce, replay_store, signer.agent_url or "", nonce, max(ttl, 0)
        )
        if claim != "claimed":
            raise WbaVerificationError(
                "replayed nonce" if claim == "replayed" else "replay store capacity exceeded",
                status=403 if claim == "replayed" else 429,
                label=signer.label,
            )
    try:
        parsed = parse_strict_json(body) if body.strip() else None
    except StrictJsonError as exc:
        raise WbaVerificationError("malformed signed JSON body", status=400) from exc
    from dataclasses import replace

    return tuple(replace(signer, parsed_body=parsed) for signer, _, _ in verified)


__all__ = [
    "OriginRevocationChecker",
    "WbaAuthorizationError",
    "WbaUnavailableError",
    "DIRECTORY_MEDIA_TYPE",
    "DirectoryResponse",
    "DiscoveredSignerKeys",
    "WbaVerificationError",
    "public_directory_key",
    "sign_key_directory",
    "sign_wba_request",
    "thumbprint_key_id",
    "uses_web_bot_auth",
    "verify_directory_response",
    "verify_wba_request",
]


class OriginRevocationChecker:
    """Verify origin revocation lists with keys from that origin's signed directory.

    Retains successful checker state so revoked keys never become usable again.
    Failed first discoveries are cooled down in an evictable cache. Admission
    is bounded globally and per operator; larger deployments should supply a
    durable checker with their own principal admission policy.
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
        self._checkers: dict[str, Any] = {}
        self._pending: dict[str, asyncio.Task[Any]] = {}
        self._failures: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def __call__(self, origin: str, keyid: str) -> bool:
        origin = _https_origin(origin)
        async with self._lock:
            checker = self._checkers.get(origin)
            task = None
            if checker is None:
                if self._failures.get(origin, 0) > self._clock():
                    self._failures.move_to_end(origin)
                    raise WbaUnavailableError("origin revocation discovery is cooling down")
                task = self._pending.get(origin)
                if task is None:
                    admitted_origins = self._checkers.keys() | self._pending.keys()
                    if len(admitted_origins) >= self._max_origins:
                        raise WbaUnavailableError("origin revocation capacity exceeded")
                    operator = self._operator(origin)
                    admitted = sum(
                        self._operator(admitted_origin) == operator
                        for admitted_origin in admitted_origins
                    )
                    if admitted >= self._max_origins_per_operator:
                        raise WbaUnavailableError("operator revocation capacity exceeded")
                    task = asyncio.create_task(self._discover(origin))
                    task.add_done_callback(
                        lambda done: done.exception() if not done.cancelled() else None
                    )
                    self._pending[origin] = task
        if task is not None:
            checker = await asyncio.shield(task)
        assert checker is not None
        try:
            return bool(await checker(keyid))
        except Exception as exc:
            raise WbaUnavailableError("origin revocation status unavailable") from exc

    @staticmethod
    def _operator(origin: str) -> str:
        from adcp.signing.etld import registrable_domain

        # Origins without a registrable domain cannot bind through trust.json,
        # but still share a hostname bucket for anonymous identity discovery.
        return registrable_domain(origin) or urlsplit(origin).hostname or origin

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
        from adcp.signing.revocation_fetcher import AsyncCachingRevocationChecker

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
            async with self._lock:
                if checker is not None and checker.has_verified_list:
                    # Freshness failure denies this request but cannot erase
                    # revocations learned from the authenticated document.
                    self._checkers[origin] = checker
                self._failures[origin] = self._clock() + 60
                self._failures.move_to_end(origin)
                while len(self._failures) > self._max_origins:
                    self._failures.popitem(last=False)
            raise WbaUnavailableError("origin revocation status unavailable") from exc
        finally:
            async with self._lock:
                self._pending.pop(origin, None)
