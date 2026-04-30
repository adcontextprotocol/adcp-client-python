"""SigningProvider abstraction for external key management (KMS / HSM / Vault).

The pure :func:`adcp.signing.sign_request` primitive accepts a private
key already loaded into process memory. That is the right shape for
tests, the ``adcp-keygen`` PEM path, and small deployments — but it
forces operators who store key material in AWS KMS, GCP KMS, Azure Key
Vault, or Vault Transit to either fork the signer or pull the private
half out of the managed key store at boot, which defeats the point of
using one.

This module defines a Protocol that decouples the request-signing
profile from how the key is held. The default :class:`InMemorySigningProvider`
matches the existing in-memory path; KMS adapters implement the same
Protocol by calling the provider's signing API in :meth:`SigningProvider.sign`.

Companion API: :func:`adcp.signing.async_sign_request` accepts a
``SigningProvider`` and is the entry point for KMS-backed signing. The
sync :func:`adcp.signing.sign_request` continues to take the
in-memory ``private_key`` directly — it does not call into a network
KMS.

Adapter contract
================

1. **Lazy init, not eager.** Do not warm the KMS client (fetch the
   public key, ping the API, etc.) before the calling process has
   bound its listener. gRPC retries inside KMS clients can block
   indefinitely; the process never opens its port; an infra
   health-check times out without the underlying KMS error surfacing.
   Defer any KMS contact to the first :meth:`SigningProvider.sign`
   call. Cache success aggressively, but never cache the error result
   of a failed warm-up — retry on the next call.

2. **Type-check the public key against** :meth:`SigningProvider.algorithm`.
   Adapters that fetch their public key (for JWKS publication or
   rotation tripwires) MUST verify the returned key type matches the
   algorithm they advertise. A KMS cryptoKeyVersion that lands on a
   P-256 curve when the adapter advertises ``ed25519`` will produce
   signatures every verifier rejects.

3. **Tripwire on rotation.** Managed key stores can silently rotate
   the underlying material (the cryptoKeyVersion changes; the
   resource name does not). Commit the expected SPKI bytes alongside
   code; assert at process start that the provider returns the same
   bytes. Mismatch should fail loudly rather than emit signatures no
   verifier will accept.

4. **Distinct key material per** ``adcp_use``. The AdCP signing
   profile requires distinct keys per signing purpose —
   request-signing, webhook-signing — not just RFC 9421 ``tag``
   isolation. Verifiers enforce the JWK ``adcp_use`` claim per
   AdCP #2423. A ``SigningProvider`` instance is bound to ONE purpose;
   sharing one provider across request-signing and webhook-signing
   emission silently fails at first delivery against a strict-mode
   verifier.

5. **Fingerprint logging caveat.** Cloud-KMS resource names typically
   embed the project / account ID. If your adapter exposes a
   fingerprint for cache disambiguation, document that it MUST be
   redacted before logging, or expose a separate accessor that returns
   a redacted form. Raw resource names in observability pipelines leak
   the project ID.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from adcp.signing.crypto import (
    ALG_ED25519,
    ALLOWED_ALGS,
    PrivateKey,
    sign_signature_base,
)

#: The algorithm identifiers an AdCP-conformant SigningProvider may
#: advertise. Casing matches :data:`adcp.signing.ALLOWED_ALGS` and the
#: ``alg`` parameter that ends up in the ``Signature-Input`` header.
SigningAlgorithm = Literal["ed25519", "ecdsa-p256-sha256"]


@runtime_checkable
class SigningProvider(Protocol):
    """Decoupled signing surface for the AdCP request-signing profile.

    A ``SigningProvider`` produces a raw signature over a payload that
    is the RFC 9421 *signature base* (NOT a pre-hashed digest). For
    Ed25519 this is the message Ed25519 will sign directly; for
    ECDSA-P-256 / ES256 the provider MUST hash the input with SHA-256
    internally — passing an already-hashed digest to a KMS that expects
    the raw message is the classic ECDSA double-hash pitfall.

    Concretely: KMS adapters using GCP KMS / AWS KMS for ECDSA-P-256
    MUST use ``DigestSign`` rather than the pre-hashed ``Sign`` variant,
    and pass the SHA-256 digest of the supplied payload as the digest
    argument. They MUST NOT hash twice.

    Lifecycle (see module docstring for full rationale):

    * Lazy init on first :meth:`sign` — no eager warm-up.
    * Cache success, never errors.
    * Dedup in-flight requests if the underlying client doesn't already.
    * One provider instance is bound to one ``adcp_use`` (request-signing
      OR webhook-signing — never both).

    Implementations MUST be safe to call :meth:`sign` from multiple
    coroutines concurrently. The default :class:`InMemorySigningProvider`
    is trivially safe because the underlying ``cryptography`` private-key
    objects are immutable.
    """

    async def sign(self, signature_base: bytes) -> bytes:
        """Sign ``signature_base`` and return the signature bytes.

        ``signature_base`` is the RFC 9421 signature base — the
        canonicalized component list joined by LF, with
        ``"@signature-params": ...`` as the final line. The provider
        MUST treat it as the raw message, NOT as a pre-hashed digest.

        For ``ecdsa-p256-sha256``: hash with SHA-256 internally, then
        produce an IEEE P1363-encoded signature (``r || s``, 64 bytes
        total — NOT DER). KMS adapters typically use the platform's
        ``DigestSign`` operation with the SHA-256 digest of
        ``signature_base``; return the raw concatenated ``r || s``
        (most KMS APIs return DER and require conversion).

        For ``ed25519``: sign the raw input directly. The result is
        always 64 bytes.

        :param signature_base: The RFC 9421 signature base bytes to sign.
        :returns: The signature bytes (64 bytes for both supported algorithms).
        :raises Exception: Any error surfaced by the underlying signer.
            The caller (:func:`async_sign_request`) does not retry — that
            is the provider's responsibility.
        """
        ...

    def key_id(self) -> str:
        """Return the JWK ``kid`` advertised at the buyer's ``jwks_uri``.

        Embedded in the ``keyid="..."`` parameter of the
        ``Signature-Input`` header. The verifier will look this up in
        the seller-side JWKS cache. The signer escapes the value per
        RFC 8941 §3.3.3 before writing the header, so a ``kid``
        containing ``"`` or ``\\`` will not break header parsing — but
        adapter authors should still prefer opaque, well-formed kids.
        """
        ...

    def algorithm(self) -> SigningAlgorithm:
        """Return the RFC 9421 alg this provider produces.

        MUST match the algorithm of the published JWK and (for KMS
        adapters that fetch their public key at init) MUST be type-checked
        against the returned key. Mismatch produces signatures every
        conformant verifier rejects.
        """
        ...


class InMemorySigningProvider:
    """Default :class:`SigningProvider` backed by an in-process private key.

    Wraps the same in-memory signing path as the original
    :func:`sign_request`. Suitable for tests, local dev, and small
    deployments where managed-key-store integration is overkill.

    Treat the key as sensitive: prefer constructing one of these in a
    short-lived scope (e.g. inside a ``with`` block that loads the PEM
    just before use) rather than holding the instance for the lifetime
    of the process when the host runs untrusted code.

    :param private_key: Loaded ``cryptography`` private key. Use
        :func:`adcp.signing.load_private_key_pem` to load from a PEM.
    :param key_id: The JWK ``kid`` matching the public half published at
        ``jwks_uri``.
    :param algorithm: One of ``"ed25519"`` or ``"ecdsa-p256-sha256"``.
        Defaults to ``"ed25519"``. MUST be the algorithm of
        ``private_key`` — passing an Ed25519 key with
        ``algorithm="ecdsa-p256-sha256"`` raises :class:`ValueError`
        on :meth:`sign`, which fails on the first signed request rather
        than producing valid-looking but verifier-rejected output.
    """

    def __init__(
        self,
        *,
        private_key: PrivateKey,
        key_id: str,
        algorithm: SigningAlgorithm = "ed25519",
    ) -> None:
        if algorithm not in ALLOWED_ALGS:
            raise ValueError(f"algorithm must be one of {sorted(ALLOWED_ALGS)}, got {algorithm!r}")
        if not key_id:
            raise ValueError("key_id must be a non-empty string")
        # Bind private_key type and curve to algorithm at construction. The
        # underlying sign_signature_base catches Ed25519/EC mismatch but does
        # not check the EC curve — a P-384 or secp256k1 key with
        # algorithm="ecdsa-p256-sha256" produces an opaque OverflowError at
        # the r||s encoding step. Fail loudly here instead.
        if algorithm == ALG_ED25519:
            if not isinstance(private_key, ed25519.Ed25519PrivateKey):
                raise ValueError(
                    f"algorithm={algorithm!r} requires an Ed25519 private key, "
                    f"got {type(private_key).__name__}"
                )
        else:  # ALG_ES256
            if not isinstance(private_key, ec.EllipticCurvePrivateKey):
                raise ValueError(
                    f"algorithm={algorithm!r} requires an EC private key, "
                    f"got {type(private_key).__name__}"
                )
            if not isinstance(private_key.curve, ec.SECP256R1):
                raise ValueError(
                    f"algorithm={algorithm!r} requires SECP256R1 (P-256), "
                    f"got curve {private_key.curve.name}"
                )
        self._private_key = private_key
        self._key_id = key_id
        self._algorithm: SigningAlgorithm = algorithm

    async def sign(self, signature_base: bytes) -> bytes:
        return sign_signature_base(
            alg=self._algorithm,
            private_key=self._private_key,
            signature_base=signature_base,
        )

    def key_id(self) -> str:
        return self._key_id

    def algorithm(self) -> SigningAlgorithm:
        return self._algorithm

    def __repr__(self) -> str:
        return (
            f"InMemorySigningProvider(key_id={self._key_id!r}, "
            f"algorithm={self._algorithm!r}, private_key=<redacted>)"
        )


__all__ = [
    "InMemorySigningProvider",
    "SigningAlgorithm",
    "SigningProvider",
]
