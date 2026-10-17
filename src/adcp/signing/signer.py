"""Signer for the AdCP request-signing profile.

Produces the `Signature-Input`, `Signature`, and (optionally) `Content-Digest`
headers for a request, per the verifier checklist the other side will run.
``http-message-signatures`` builds the signature base and serializes
``Signature-Input``; this module applies the profile's choices (covered
components, window, nonce, tag, and the 3.0/3.1 Base64URL ``Signature``).

Two entry points share the same canonicalization spine:

* :func:`sign_request` — synchronous, takes a loaded ``PrivateKey`` and
  signs in process. Use when the key lives in process memory.
* :func:`async_sign_request` — asynchronous, takes a
  :class:`adcp.signing.SigningProvider`. Use when the key lives in a
  managed key store (KMS / HSM / Vault) or anywhere ``sign`` may
  involve I/O.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from cryptography.hazmat.primitives.asymmetric import ed25519
from http_message_signatures.exceptions import HTTPMessageSignaturesException

from adcp.signing import _rfc9421
from adcp.signing.canonical import _lookup
from adcp.signing.constants import (
    DEFAULT_EXPIRES_IN_SECONDS,
    DEFAULT_TAG,
    MAX_WINDOW_SECONDS,
    NONCE_BYTES,
    SIG_LABEL_DEFAULT,
)
from adcp.signing.crypto import (
    ALG_ED25519,
    ALG_ES256,
    ALLOWED_ALGS,
    PrivateKey,
    b64url_encode,
    extract_signature_bytes,
    format_signature_header,
)
from adcp.signing.digest import compute_content_digest_sha256
from adcp.signing.provider import SigningProvider


@dataclass(frozen=True)
class SignedHeaders:
    """The headers a signer must add to the outgoing request."""

    signature_input: str
    signature: str
    content_digest: str | None = None
    signature_agent: str | None = None

    def as_dict(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Signature-Input": self.signature_input,
            "Signature": self.signature,
        }
        if self.content_digest is not None:
            headers["Content-Digest"] = self.content_digest
        if self.signature_agent is not None:
            headers["Signature-Agent"] = self.signature_agent
        return headers


@dataclass(frozen=True)
class _PreparedRequest:
    """Everything about a request to sign except the signature itself."""

    message: _rfc9421.Request
    covered_component_ids: tuple[str, ...]
    created: int
    expires: int
    nonce: str
    content_digest_value: str | None


def _prepare_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    key_id: str,
    alg: str,
    cover_content_digest: bool,
    created: int | None,
    expires_in_seconds: int,
    nonce: str | None,
    tag: str,
    label: str,
) -> _PreparedRequest:
    """Apply the AdCP profile's signer rules; the library does the RFC 9421 work.

    Pure: takes only the request shape and the key metadata that lands in
    ``Signature-Input``. The key material (private key or SigningProvider) is
    applied separately, and the sync and async signers produce byte-identical
    headers. Values that RFC 8941 cannot carry (a control character in
    ``key_id``, a label outside the key grammar) are refused by the
    ``http-sf`` and ``http-message-signatures`` serializers.
    """
    if alg not in ALLOWED_ALGS:
        raise ValueError(f"alg must be one of {sorted(ALLOWED_ALGS)}, got {alg!r}")
    if expires_in_seconds <= 0 or expires_in_seconds > MAX_WINDOW_SECONDS:
        raise ValueError(
            f"expires_in_seconds must be in (0, {MAX_WINDOW_SECONDS}], got {expires_in_seconds}"
        )
    for name, value in (("key_id", key_id), ("label", label), ("tag", tag)):
        if not value:
            raise ValueError(f"{name} must be a non-empty string")
    if nonce is not None and not nonce:
        raise ValueError("nonce must be a non-empty string")
    try:
        # A label is an RFC 8941 key in both fields; refuse a bad one before
        # anything is signed.
        _rfc9421.serialize({label: (True, {})})
    except ValueError as exc:
        raise ValueError(f"label {label!r} is not a valid RFC 8941 key: {exc}") from exc

    if created is None:
        created = int(time.time())
    if nonce is None:
        nonce = b64url_encode(secrets.token_bytes(NONCE_BYTES))

    components = ["@method", "@target-uri", "@authority"]
    outgoing_headers: dict[str, str] = dict(headers)
    content_digest_value: str | None = None
    if _lookup(headers, "content-type") is not None:
        components.append("content-type")
    if cover_content_digest:
        content_digest_value = compute_content_digest_sha256(body)
        outgoing_headers = {
            k: v for k, v in outgoing_headers.items() if k.lower() != "content-digest"
        }
        outgoing_headers["Content-Digest"] = content_digest_value
        components.append("content-digest")
    return _PreparedRequest(
        message=_rfc9421.Request(method=method, url=url, headers=outgoing_headers),
        covered_component_ids=tuple(
            _rfc9421.component_identifier((name, {})) for name in components
        ),
        created=created,
        expires=created + expires_in_seconds,
        nonce=nonce,
        content_digest_value=content_digest_value,
    )


def _signed_headers(
    prepared: _PreparedRequest,
    *,
    signature_input: str,
    signature: bytes,
    label: str,
    signing_profile_version: Literal["3.0", "3.1", "3.2"],
) -> SignedHeaders:
    return SignedHeaders(
        signature_input=signature_input,
        signature=format_signature_header(
            signature,
            label=label,
            use_legacy_base64url=signing_profile_version != "3.2",
        ),
        content_digest=prepared.content_digest_value,
    )


def _resolve_content_digest_coverage(
    *,
    signing_profile_version: str,
    body: bytes,
    cover_content_digest: bool | None,
) -> bool:
    if signing_profile_version not in {"3.0", "3.1", "3.2"}:
        raise ValueError("signing_profile_version must be one of '3.0', '3.1', or '3.2'")
    if cover_content_digest is None:
        return signing_profile_version == "3.2" and bool(body)
    if signing_profile_version == "3.2" and body and not cover_content_digest:
        raise ValueError(
            "AdCP 3.2 signatures over non-empty request bodies must cover content-digest"
        )
    return cover_content_digest


def sign_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    private_key: PrivateKey,
    key_id: str,
    alg: str,
    cover_content_digest: bool | None = None,
    created: int | None = None,
    expires_in_seconds: int = DEFAULT_EXPIRES_IN_SECONDS,
    nonce: str | None = None,
    tag: str = DEFAULT_TAG,
    label: str = SIG_LABEL_DEFAULT,
    signing_profile_version: Literal["3.0", "3.1", "3.2"],
    agent_origin: str | None = None,
) -> SignedHeaders:
    """Sign a request and return the headers to add to it.

    The caller is responsible for attaching `SignedHeaders.as_dict()` to the
    outgoing HTTP request before sending. ``signing_profile_version`` is
    intentionally required: this low-level primitive has no negotiated AdCP
    version from which it could safely infer the wire encoding.
    """
    if agent_origin is not None:
        from adcp.signing.wba import public_directory_key, sign_wba_request

        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise ValueError(
                "Web Bot Auth here takes an Ed25519 key (sign RSA keys with "
                "sign_wba_request); ES256 is an AdCP RFC 9421 profile algorithm only"
            )
        if key_id != public_directory_key(private_key)["kid"]:
            raise ValueError("Web Bot Auth key_id must be the public key's RFC 7638 thumbprint")
        result = sign_wba_request(
            method=method,
            url=url,
            headers=headers,
            body=body,
            private_key=private_key,
            agent_origin=agent_origin,
            created=created,
            expires_in_seconds=expires_in_seconds,
            nonce=nonce,
            label=label,
        )
        return SignedHeaders(
            signature_input=result["Signature-Input"],
            signature=result["Signature"],
            content_digest=result.get("Content-Digest"),
            signature_agent=result["Signature-Agent"],
        )
    resolved_cover_content_digest = _resolve_content_digest_coverage(
        signing_profile_version=signing_profile_version,
        body=body,
        cover_content_digest=cover_content_digest,
    )
    prepared = _prepare_request(
        method=method,
        url=url,
        headers=headers,
        body=body,
        key_id=key_id,
        alg=alg,
        cover_content_digest=resolved_cover_content_digest,
        created=created,
        expires_in_seconds=expires_in_seconds,
        nonce=nonce,
        tag=tag,
        label=label,
    )
    try:
        # Fails before signing when the key does not fit ``alg``.
        _rfc9421.ALGORITHMS[alg](private_key=private_key)
    except HTTPMessageSignaturesException as exc:
        expected = "Ed25519 key" if alg == ALG_ED25519 else "EC P-256 key"
        raise ValueError(f"alg/private_key mismatch: expected {expected}") from exc
    signature_input, signature = _rfc9421.sign(
        prepared.message,
        alg=alg,
        private_key=private_key,
        key_id=key_id,
        label=label,
        created=prepared.created,
        expires=prepared.expires,
        nonce=prepared.nonce,
        tag=tag,
        covered_component_ids=prepared.covered_component_ids,
        resolver=_rfc9421.AdcpComponentResolver,
    )
    return _signed_headers(
        prepared,
        signature_input=signature_input,
        signature=extract_signature_bytes(signature, label, allow_legacy_base64url=False),
        label=label,
        signing_profile_version=signing_profile_version,
    )


async def async_sign_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    provider: SigningProvider,
    cover_content_digest: bool | None = None,
    created: int | None = None,
    expires_in_seconds: int = DEFAULT_EXPIRES_IN_SECONDS,
    nonce: str | None = None,
    tag: str = DEFAULT_TAG,
    label: str = SIG_LABEL_DEFAULT,
    signing_profile_version: Literal["3.0", "3.1", "3.2"],
) -> SignedHeaders:
    """Sign a request via a :class:`SigningProvider` and return its headers.

    Async counterpart to :func:`sign_request`. Use this entry point when
    the key lives in a managed key store (KMS / HSM / Vault) — the
    provider's :meth:`SigningProvider.sign` may involve network I/O.

    The provider's :meth:`SigningProvider.key_id` and
    :meth:`SigningProvider.algorithm` are read once and embedded in the
    ``Signature-Input`` header. Calling them MUST NOT trigger any
    expensive work in the adapter — those are constant-like accessors.
    The KMS round-trip belongs in :meth:`SigningProvider.sign`.

    The signature base passed to :meth:`SigningProvider.sign` is the
    raw RFC 9421 base — NOT a pre-hashed digest. See the
    :class:`SigningProvider` docstring for the ECDSA double-hash caveat.
    """
    resolved_cover_content_digest = _resolve_content_digest_coverage(
        signing_profile_version=signing_profile_version,
        body=body,
        cover_content_digest=cover_content_digest,
    )
    key_id = provider.key_id()
    alg = provider.algorithm()
    prepared = _prepare_request(
        method=method,
        url=url,
        headers=headers,
        body=body,
        key_id=key_id,
        alg=alg,
        cover_content_digest=resolved_cover_content_digest,
        created=created,
        expires_in_seconds=expires_in_seconds,
        nonce=nonce,
        tag=tag,
        label=label,
    )
    # The same base and parameters HTTPMessageSigner.sign produces, so the
    # headers match sign_request byte for byte.
    base, member = _rfc9421.signature_base(
        prepared.message,
        alg=alg,
        key_id=key_id,
        created=prepared.created,
        expires=prepared.expires,
        nonce=prepared.nonce,
        tag=tag,
        covered_component_ids=prepared.covered_component_ids,
        resolver=_rfc9421.AdcpComponentResolver,
    )
    signature = await provider.sign(base)
    return _signed_headers(
        prepared,
        signature_input=f"{label}={member}",
        signature=signature,
        label=label,
        signing_profile_version=signing_profile_version,
    )


__all__ = [
    "ALG_ED25519",
    "ALG_ES256",
    "DEFAULT_EXPIRES_IN_SECONDS",
    "DEFAULT_TAG",
    "SignedHeaders",
    "async_sign_request",
    "sign_request",
]
