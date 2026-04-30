"""Signer for the AdCP request-signing profile.

Produces the `Signature-Input`, `Signature`, and (optionally) `Content-Digest`
headers for a request, per the verifier checklist the other side will run.

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

from adcp.signing.canonical import (
    SignatureInputLabel,
    _lookup,
    build_signature_base,
)
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
    format_signature_header,
    sign_signature_base,
)
from adcp.signing.digest import compute_content_digest_sha256
from adcp.signing.provider import SigningProvider


@dataclass(frozen=True)
class SignedHeaders:
    """The headers a signer must add to the outgoing request."""

    signature_input: str
    signature: str
    content_digest: str | None = None

    def as_dict(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Signature-Input": self.signature_input,
            "Signature": self.signature,
        }
        if self.content_digest is not None:
            headers["Content-Digest"] = self.content_digest
        return headers


_SF_STRING_ALLOWED: frozenset[str] = frozenset(chr(c) for c in range(0x20, 0x7F))


def _escape_sf_string(value: str, *, field: str) -> str:
    """Escape ``value`` for embedding in an RFC 8941 §3.3.3 sf-string.

    The §3.3.3 grammar permits only printable ASCII 0x20-0x7E; ``"``
    and ``\\`` require escaping (and are the only escapes). Control
    bytes and non-ASCII have no representation at all and are rejected
    here — passing one through would emit a header line that conformant
    verifiers parse differently from this serializer (a parser-divergence
    bug class) and, in the CRLF case, can also turn into HTTP header
    injection at non-httpx integrators that don't sanitize embedded
    line terminators.
    """
    bad = next((c for c in value if c not in _SF_STRING_ALLOWED), None)
    if bad is not None:
        raise ValueError(
            f"{field} contains character {bad!r} (codepoint {ord(bad):#06x}) "
            "not allowed in RFC 8941 sf-string — only printable ASCII 0x20-0x7E "
            "may appear in keyid, nonce, or tag values"
        )
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class _PreparedSignature:
    """Result of canonicalizing a request — everything except the raw signature."""

    base: bytes
    raw_value: str
    label: str
    content_digest_value: str | None


def _prepare_signature(
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
) -> _PreparedSignature:
    """Canonicalize a request into a signable RFC 9421 base.

    Pure: takes only the request shape and the key metadata that lands in
    ``Signature-Input``. The actual key material (private key OR
    SigningProvider) is applied separately so the sync and async signers
    produce byte-identical bases.
    """
    if alg not in ALLOWED_ALGS:
        raise ValueError(f"alg must be one of {sorted(ALLOWED_ALGS)}, got {alg!r}")
    if expires_in_seconds <= 0 or expires_in_seconds > MAX_WINDOW_SECONDS:
        raise ValueError(
            f"expires_in_seconds must be in (0, {MAX_WINDOW_SECONDS}], got {expires_in_seconds}"
        )
    if not key_id:
        raise ValueError("key_id must be a non-empty string")

    if created is None:
        created = int(time.time())
    expires = created + expires_in_seconds
    if nonce is None:
        nonce = b64url_encode(secrets.token_bytes(NONCE_BYTES))

    components = ["@method", "@target-uri", "@authority"]
    outgoing_headers: dict[str, str] = dict(headers)
    content_digest_value: str | None = None
    if _lookup(headers, "content-type") is not None:
        components.append("content-type")
    if cover_content_digest:
        content_digest_value = compute_content_digest_sha256(body)
        outgoing_headers["Content-Digest"] = content_digest_value
        components.append("content-digest")

    comp_serialized = "(" + " ".join(f'"{c}"' for c in components) + ")"
    nonce_escaped = _escape_sf_string(nonce, field="nonce")
    key_id_escaped = _escape_sf_string(key_id, field="key_id")
    tag_escaped = _escape_sf_string(tag, field="tag")
    params_serialized = (
        f';created={created};expires={expires};nonce="{nonce_escaped}"'
        f';keyid="{key_id_escaped}";alg="{alg}";tag="{tag_escaped}"'
    )
    raw_value = comp_serialized + params_serialized

    parsed = SignatureInputLabel(
        label=label,
        components=tuple(components),
        params={
            "created": created,
            "expires": expires,
            "nonce": nonce,
            "keyid": key_id,
            "alg": alg,
            "tag": tag,
        },
        raw_value=raw_value,
    )
    base = build_signature_base(
        method=method, url=url, headers=outgoing_headers, parsed=parsed
    ).encode("utf-8")
    return _PreparedSignature(
        base=base,
        raw_value=raw_value,
        label=label,
        content_digest_value=content_digest_value,
    )


def _assemble_headers(prepared: _PreparedSignature, sig_bytes: bytes) -> SignedHeaders:
    return SignedHeaders(
        signature_input=f"{prepared.label}={prepared.raw_value}",
        signature=format_signature_header(sig_bytes, label=prepared.label),
        content_digest=prepared.content_digest_value,
    )


def sign_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    private_key: PrivateKey,
    key_id: str,
    alg: str,
    cover_content_digest: bool = False,
    created: int | None = None,
    expires_in_seconds: int = DEFAULT_EXPIRES_IN_SECONDS,
    nonce: str | None = None,
    tag: str = DEFAULT_TAG,
    label: str = SIG_LABEL_DEFAULT,
) -> SignedHeaders:
    """Sign a request and return the headers to add to it.

    The caller is responsible for attaching `SignedHeaders.as_dict()` to the
    outgoing HTTP request before sending.
    """
    prepared = _prepare_signature(
        method=method,
        url=url,
        headers=headers,
        body=body,
        key_id=key_id,
        alg=alg,
        cover_content_digest=cover_content_digest,
        created=created,
        expires_in_seconds=expires_in_seconds,
        nonce=nonce,
        tag=tag,
        label=label,
    )
    sig_bytes = sign_signature_base(alg=alg, private_key=private_key, signature_base=prepared.base)
    return _assemble_headers(prepared, sig_bytes)


async def async_sign_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    provider: SigningProvider,
    cover_content_digest: bool = False,
    created: int | None = None,
    expires_in_seconds: int = DEFAULT_EXPIRES_IN_SECONDS,
    nonce: str | None = None,
    tag: str = DEFAULT_TAG,
    label: str = SIG_LABEL_DEFAULT,
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
    prepared = _prepare_signature(
        method=method,
        url=url,
        headers=headers,
        body=body,
        key_id=provider.key_id(),
        alg=provider.algorithm(),
        cover_content_digest=cover_content_digest,
        created=created,
        expires_in_seconds=expires_in_seconds,
        nonce=nonce,
        tag=tag,
        label=label,
    )
    sig_bytes = await provider.sign(prepared.base)
    return _assemble_headers(prepared, sig_bytes)


__all__ = [
    "ALG_ED25519",
    "ALG_ES256",
    "DEFAULT_EXPIRES_IN_SECONDS",
    "DEFAULT_TAG",
    "SignedHeaders",
    "async_sign_request",
    "sign_request",
]
