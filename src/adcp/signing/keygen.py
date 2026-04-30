"""Key generation helpers for AdCP request signing.

Two equivalent entry points — pick whichever fits your calling context:

- :func:`generate_signing_keypair` — programmatic API returning
  ``(pem_bytes, public_jwk)``. Use from tests, provisioning scripts, and
  any non-shell code where spawning a subprocess is the wrong shape.
- ``adcp-keygen`` CLI (:func:`main`) — wraps the same helper plus
  file-writing and stdout printing, so CI / shell pipelines stay
  ergonomic.

Both paths produce the same PEM and JWK. Publish the JWK (public half)
at your agent's ``jwks_uri`` — the ``adcp_use`` claim (``request-signing``
or ``webhook-signing``) pins the key to one signing profile; verifiers
reject keys used in the wrong surface.

Usage (CLI):
    adcp-keygen --alg ed25519 --out private-key.pem
    adcp-keygen --alg es256 --out private-key.pem
    adcp-keygen --alg ed25519 --encrypt --out private-key.pem

Usage (programmatic):
    from adcp.signing import generate_signing_keypair

    pem_bytes, public_jwk = generate_signing_keypair(
        alg="ed25519", purpose="webhook-signing"
    )

    # Write mode-0600 atomically. DON'T use Path.write_bytes — it
    # inherits the process umask (typically 0644 = world-readable).
    import os
    fd = os.open("key.pem", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, pem_bytes)
    finally:
        os.close(fd)
    publish_to_jwks_uri(public_jwk)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from adcp.signing.crypto import ALG_ED25519, ALG_ES256, b64url_encode, load_private_key_pem


def _encryption_algorithm(
    passphrase: bytes | None,
) -> serialization.KeySerializationEncryption:
    if passphrase is None:
        return serialization.NoEncryption()
    return serialization.BestAvailableEncryption(passphrase)


_ADCP_USE_VALUES = ("request-signing", "webhook-signing")


def _public_jwk_ed25519(
    public_key: ed25519.Ed25519PublicKey, *, kid: str, adcp_use: str
) -> dict[str, Any]:
    x = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "key_ops": ["verify"],
        "adcp_use": adcp_use,
        "kid": kid,
        "x": b64url_encode(x),
    }


def _public_jwk_es256(
    public_key: ec.EllipticCurvePublicKey, *, kid: str, adcp_use: str
) -> dict[str, Any]:
    numbers = public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "use": "sig",
        "key_ops": ["verify"],
        "adcp_use": adcp_use,
        "kid": kid,
        "x": b64url_encode(numbers.x.to_bytes(32, "big")),
        "y": b64url_encode(numbers.y.to_bytes(32, "big")),
    }


def generate_ed25519(
    kid: str, passphrase: bytes | None = None, adcp_use: str = "request-signing"
) -> tuple[bytes, dict[str, Any]]:
    private = ed25519.Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=_encryption_algorithm(passphrase),
    )
    jwk = _public_jwk_ed25519(private.public_key(), kid=kid, adcp_use=adcp_use)
    return pem, jwk


def generate_es256(
    kid: str, passphrase: bytes | None = None, adcp_use: str = "request-signing"
) -> tuple[bytes, dict[str, Any]]:
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=_encryption_algorithm(passphrase),
    )
    jwk = _public_jwk_es256(private.public_key(), kid=kid, adcp_use=adcp_use)
    return pem, jwk


def pem_to_adcp_jwk(
    pem: bytes,
    *,
    kid: str,
    purpose: Literal["request-signing", "webhook-signing"],
    password: bytes | None = None,
) -> dict[str, Any]:
    """Derive the public JWK for an existing AdCP signing PEM.

    Companion to :func:`generate_signing_keypair` for the case where
    the key was minted elsewhere — typically in a managed key store
    (KMS / HSM / Vault) that exports the public half as a PEM. The
    output JWK is byte-shape-identical to what
    :func:`generate_signing_keypair` would have produced for the same
    key material, so it is safe to publish at the agent's
    ``jwks_uri`` directly.

    Why a helper at all? Three fields in the JWK are easy to mis-emit
    by hand and every wrong value yields a verifier rejection at the
    first signed request:

    * ``alg`` — MUST be ``"EdDSA"`` for Ed25519, ``"ES256"`` for P-256
      (NOT the RFC 9421 ``alg`` casing used in ``Signature-Input``).
    * ``adcp_use`` — required by AdCP #2423; verifiers reject keys
      lacking it. MUST match the signing surface (``"request-signing"``
      vs. ``"webhook-signing"``).
    * ``key_ops`` — MUST be ``["verify"]`` (the public half cannot
      sign).

    :param pem: PEM-encoded private key (PKCS#8). Pass the PEM only
        when the private half is at hand — for KMS deployments where
        the private material never leaves the managed store, pass an
        SPKI public-key PEM (``-----BEGIN PUBLIC KEY-----``) instead;
        the loader handles both forms.
    :param kid: JWK ``kid`` to embed. MUST match the value the signer
        will advertise via :meth:`SigningProvider.key_id`.
    :param purpose: Which AdCP signing profile this key is for. Sets
        ``adcp_use``. Generate distinct keys per purpose — sharing
        material across request-signing and webhook-signing is a spec
        violation, not just a convention.
    :param password: Passphrase if ``pem`` is an encrypted private
        key.

    :returns: Public JWK ready to publish in the agent's ``jwks_uri``.
        The private scalar (``d``) is NEVER included in the output.

    :raises ValueError: ``purpose`` is not in
        ``("request-signing", "webhook-signing")``; the PEM is not
        Ed25519 or ECDSA-P-256; the EC curve is not P-256.
    """
    if purpose not in _ADCP_USE_VALUES:
        raise ValueError(f"purpose must be one of {_ADCP_USE_VALUES}, got {purpose!r}")
    if not kid:
        raise ValueError("kid must be a non-empty string")

    # SPKI public-key PEMs use the exact header `-----BEGIN PUBLIC KEY-----`;
    # private-key PEMs use a different header. Match the full sentinel rather
    # than a substring so a future PEM type whose header contains the words
    # "PUBLIC" + "KEY" (e.g., a hypothetical encrypted-public-key form)
    # doesn't silently dispatch to the wrong loader.
    if b"-----BEGIN PUBLIC KEY-----" in pem[:128]:
        loaded = serialization.load_pem_public_key(pem)
        if not isinstance(loaded, (ed25519.Ed25519PublicKey, ec.EllipticCurvePublicKey)):
            raise ValueError(
                f"unsupported public key type {type(loaded).__name__} — "
                f"AdCP signing accepts Ed25519 or ECDSA-P-256 only"
            )
        if isinstance(loaded, ec.EllipticCurvePublicKey) and not isinstance(
            loaded.curve, ec.SECP256R1
        ):
            raise ValueError(
                f"EC public key curve {loaded.curve.name} is not supported — only "
                f"P-256 (SECP256R1) is allowed"
            )
        public_key: ed25519.Ed25519PublicKey | ec.EllipticCurvePublicKey = loaded
    else:
        public_key = load_private_key_pem(pem, password=password).public_key()

    if isinstance(public_key, ed25519.Ed25519PublicKey):
        return _public_jwk_ed25519(public_key, kid=kid, adcp_use=purpose)
    return _public_jwk_es256(public_key, kid=kid, adcp_use=purpose)


def _default_kid(alg: str) -> str:
    """Default ``kid`` — opaque, collision-resistant.

    Combines alg + UTC date + 4 random hex chars so two calls in the
    same UTC day produce distinct kids. Format is an implementation
    detail; downstream tooling MUST NOT parse it. Callers managing
    rotation SHOULD pass an explicit ``kid`` they control.

    Same-day collisions without the random suffix silently break
    verification: two JWKs advertised under the same kid, verifiers
    cache the first one and reject signatures made with the second as
    ``REQUEST_SIGNATURE_INVALID``. The suffix prevents that at no
    readability cost.
    """
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"adcp-{alg}-{date}-{secrets.token_hex(2)}"


def generate_signing_keypair(
    *,
    alg: Literal["ed25519", "es256"] = "ed25519",
    kid: str | None = None,
    purpose: Literal["request-signing", "webhook-signing"] = "request-signing",
    passphrase: bytes | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Generate an AdCP signing keypair.

    Programmatic companion to the ``adcp-keygen`` CLI — call this from
    tests, provisioning scripts, or any non-shell context where spawning
    a subprocess is the wrong shape.

    :param alg: Signature algorithm. ``"ed25519"`` (default; tiny keys,
        recommended) or ``"es256"`` (ECDSA over P-256, broader ecosystem
        support).
    :param kid: Key ID to embed in the JWK. When omitted, the SDK mints
        an opaque default combining alg + UTC date + 4 random hex chars
        — suitable for first-time provisioning only. **Callers managing
        rotation MUST supply their own ``kid``.** The default is
        collision-resistant within a single process but does not
        guarantee uniqueness across processes; rotation tooling needs
        its own identifier scheme to track retirement / revocation.
    :param purpose: Which AdCP signing profile this key is for. Sets the
        JWK ``adcp_use`` claim. **Request-signing and webhook-signing
        keys MUST be distinct** — a signature from one surface cannot
        replay as the other, and every conformant verifier enforces the
        claim. Generate separate keys per purpose.
    :param passphrase: When provided, the PEM is encrypted with
        ``BestAvailableEncryption``. Typical only for dev-laptop keys;
        automated deployments usually leave the PEM unencrypted and
        rely on filesystem perms (the CLI writes mode 0600).

        **Passphrase lifecycle.** CPython cannot zero ``bytes``. Once
        passed here, the buffer is consumed by ``cryptography`` and
        then released to GC; there's no ``zeroize`` step. Callers
        handling long-lived credentials should source the passphrase
        from a secret manager per call rather than hold a Python
        literal in process memory.

    :returns: ``(pem_bytes, public_jwk)``. The PEM is PKCS#8
        (optionally encrypted); the JWK is the public half, ready to
        publish at your ``jwks_uri``. The private scalar is NOT in the
        JWK — only in the PEM.

    :raises ValueError: ``alg`` or ``purpose`` is unsupported.

    Example:
        >>> pem, jwk = generate_signing_keypair(
        ...     alg="ed25519", purpose="webhook-signing"
        ... )
        >>> jwk["adcp_use"]
        'webhook-signing'
    """
    if alg not in ("ed25519", "es256"):
        raise ValueError(f"alg must be 'ed25519' or 'es256', got {alg!r}")
    if purpose not in _ADCP_USE_VALUES:
        raise ValueError(f"purpose must be one of {_ADCP_USE_VALUES}, got {purpose!r}")
    resolved_kid = kid or _default_kid(alg)
    if alg == "ed25519":
        return generate_ed25519(resolved_kid, passphrase=passphrase, adcp_use=purpose)
    return generate_es256(resolved_kid, passphrase=passphrase, adcp_use=purpose)


def _prompt_passphrase_bytes() -> bytes:
    first = getpass.getpass("Passphrase: ")
    if not first:
        print("error: passphrase cannot be empty", file=sys.stderr)
        raise SystemExit(2)
    second = getpass.getpass("Confirm passphrase: ")
    if first != second:
        print("error: passphrases do not match", file=sys.stderr)
        raise SystemExit(2)
    # NFC-normalize so a passphrase typed on macOS (which may emit NFD for some
    # composed characters) round-trips against the same passphrase typed on
    # Linux/Windows. CPython can't truly zero the plaintext string, but encoding
    # to bytes here lets the caller drop the string reference promptly.
    return unicodedata.normalize("NFC", first).encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adcp-keygen",
        description="Generate a signing keypair for the AdCP request-signing profile.",
    )
    parser.add_argument(
        "--alg",
        choices=["ed25519", "es256"],
        default="ed25519",
        help="Signature algorithm (default: ed25519)",
    )
    parser.add_argument(
        "--kid",
        default=None,
        help="Key ID to embed in the JWK (default: generated from alg + timestamp)",
    )
    parser.add_argument(
        "--out",
        default="adcp-signing-key.pem",
        help="Path to write the PEM private key (default: adcp-signing-key.pem)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output path if it exists",
    )
    parser.add_argument(
        "--encrypt",
        action="store_true",
        help=(
            "Prompt for a passphrase and encrypt the PEM with BestAvailableEncryption. "
            "Suitable for dev laptops and CI test keys; for automated deployments the "
            "default unencrypted PKCS8 (protected by mode 0600) is usually what you want."
        ),
    )
    parser.add_argument(
        "--purpose",
        choices=list(_ADCP_USE_VALUES),
        default="request-signing",
        help=(
            "Which AdCP signing profile this key is for (sets the JWK `adcp_use` "
            "claim). `request-signing` for outbound tool calls; `webhook-signing` "
            "for keys a sender uses to sign outbound webhooks to buyers. Verifiers "
            "enforce the claim, so mixing the two silently fails at first delivery."
        ),
    )
    args = parser.parse_args(argv)

    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        print(
            f"refusing to overwrite {out_path} — pass --force to replace",
            file=sys.stderr,
        )
        return 2

    passphrase = _prompt_passphrase_bytes() if args.encrypt else None

    pem, jwk = generate_signing_keypair(
        alg=args.alg,
        kid=args.kid,
        purpose=args.purpose,
        passphrase=passphrase,
    )
    alg_rfc = ALG_ED25519 if args.alg == "ed25519" else ALG_ES256

    # `--force` clobbers in two steps (non-atomic on overwrite), but the
    # happy-path create is atomic via O_EXCL | mode=0o600 so there is no window
    # where the file exists with permissive perms.
    if args.force and out_path.exists():
        out_path.unlink()
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)

    encryption_note = ", encrypted" if passphrase is not None else ""
    print(
        f"wrote PEM private key to {out_path} (mode 600{encryption_note})",
        file=sys.stderr,
    )
    print(
        f"rfc 9421 alg: {alg_rfc}   (use this for `alg` in Signature-Input)",
        file=sys.stderr,
    )
    print("publish this JWK (public half) at your agent's jwks_uri:", file=sys.stderr)
    print(json.dumps({"keys": [jwk]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
