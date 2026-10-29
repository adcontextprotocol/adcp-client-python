"""Key generation helpers for AdCP request signing.

Two equivalent entry points — pick whichever fits your calling context:

- :func:`generate_signing_keypair` — programmatic API returning
  ``(pem_bytes, public_jwk)``. Use from tests, provisioning scripts, and
  any non-shell code where spawning a subprocess is the wrong shape.
- ``adcp-keygen`` CLI (:func:`main`) — wraps the same helper plus
  file-writing and stdout printing, so CI / shell pipelines stay
  ergonomic.

Both paths produce the same PEM and JWK. Publish the JWK (public half)
at your agent's ``jwks_uri`` — the ``adcp_use`` claim pins the key to one
signing purpose (``request-signing``, ``response-signing``,
``governance-signing``, the deprecated ``webhook-signing``, or any other
lowercase hyphenated token); verifiers reject keys used in the wrong surface.

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
import re
import secrets
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from joserfc.jwk import ECKey, OKPKey

from adcp.signing.constants import (
    ADCP_USE_GOVERNANCE,
    ADCP_USE_REQUEST,
    ADCP_USE_RESPONSE,
    ADCP_USE_WEBHOOK,
)
from adcp.signing.crypto import ALG_ED25519, ALG_ES256, load_private_key_pem

# Key generation, PKCS#8 PEM export and RFC 7517 / 8037 public JWK export are
# delegated to joserfc. What stays here is AdCP policy: the curve allow-list,
# the JWS ``alg`` each curve is published under, and the AdCP-specific JWK
# members (``use``, ``key_ops``, ``adcp_use``, ``kid``).

#: Purposes the spec names today, for help text. The accepted vocabulary is
#: open: any token matching ``_ADCP_USE_PATTERN`` is a valid ``adcp_use``.
_KNOWN_ADCP_USES = (ADCP_USE_REQUEST, ADCP_USE_RESPONSE, ADCP_USE_GOVERNANCE, ADCP_USE_WEBHOOK)
_ADCP_USE_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ADCP_USE_MAX_LENGTH = 64

# JWS ``alg`` (RFC 7518 / RFC 8037) published for each AdCP-allowed curve. Not
# the RFC 9421 ``alg`` used in ``Signature-Input``.
_JWS_ALG_BY_CURVE = {"Ed25519": "EdDSA", "P-256": "ES256"}


def _adcp_public_jwk(key: OKPKey | ECKey, *, kid: str, adcp_use: str) -> dict[str, Any]:
    """Public JWK for ``key`` with the AdCP members added.

    joserfc exports the public members only (``as_dict(private=False)``
    drops ``d``); member order is kept stable for the CLI's printed JWKS.
    """
    public: dict[str, Any] = dict(key.as_dict(private=False))
    crv = public.pop("crv")
    return {
        "kty": public.pop("kty"),
        "crv": crv,
        "alg": _JWS_ALG_BY_CURVE[crv],
        "use": "sig",
        "key_ops": ["verify"],
        "adcp_use": adcp_use,
        "kid": kid,
        **public,
    }


def _check_purpose(purpose: object) -> str:
    """``purpose`` if it is a lowercase hyphenated token, else ``ValueError``.

    Validation is syntactic, not by membership: an AdCP verifier accepts a
    key only when ``adcp_use`` equals the purpose of the surface it verifies
    (fail-closed), so a purpose no verifier implements authorizes nothing.
    """
    if (
        not isinstance(purpose, str)
        or len(purpose) > _ADCP_USE_MAX_LENGTH
        or _ADCP_USE_PATTERN.fullmatch(purpose) is None
    ):
        raise ValueError(
            "purpose must be a lowercase hyphenated token of at most "
            f"{_ADCP_USE_MAX_LENGTH} characters (e.g. {ADCP_USE_REQUEST!r}), got {purpose!r}"
        )
    return purpose


def _private_pem(key: OKPKey | ECKey, passphrase: bytes | None) -> bytes:
    # joserfc serializes as PKCS#8, using BestAvailableEncryption when a
    # password is given. Its ``password`` is annotated ``str``, but the value
    # goes through ``joserfc.util.to_bytes``, which returns ``bytes`` unchanged,
    # so a non-UTF-8 passphrase stays byte-exact.
    return key.as_pem(private=True, password=cast("str | None", passphrase))


def generate_ed25519(
    kid: str, passphrase: bytes | None = None, adcp_use: str = "request-signing"
) -> tuple[bytes, dict[str, Any]]:
    key = OKPKey.generate_key("Ed25519", private=True)
    return _private_pem(key, passphrase), _adcp_public_jwk(key, kid=kid, adcp_use=adcp_use)


def generate_es256(
    kid: str, passphrase: bytes | None = None, adcp_use: str = "request-signing"
) -> tuple[bytes, dict[str, Any]]:
    key = ECKey.generate_key("P-256", private=True)
    return _private_pem(key, passphrase), _adcp_public_jwk(key, kid=kid, adcp_use=adcp_use)


def pem_to_adcp_jwk(
    pem: bytes,
    *,
    kid: str,
    purpose: str,
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
      lacking it. MUST match the signing surface (``"request-signing"``,
      ``"response-signing"``, ``"governance-signing"``, ...).
    * ``key_ops`` — MUST be ``["verify"]`` (the public half cannot
      sign).

    :param pem: PEM-encoded private key (PKCS#8). Pass the PEM only
        when the private half is at hand — for KMS deployments where
        the private material never leaves the managed store, pass an
        SPKI public-key PEM (``-----BEGIN PUBLIC KEY-----``) instead;
        the loader handles both forms.
    :param kid: JWK ``kid`` to embed. MUST match the value the signer
        will advertise via :meth:`SigningProvider.key_id`.
    :param purpose: Which AdCP signing purpose this key is for. Sets
        ``adcp_use``. Any lowercase hyphenated token is accepted; see
        :func:`generate_signing_keypair` for the vocabulary and why an
        open one is safe. Generate distinct keys per purpose.
    :param password: Passphrase if ``pem`` is an encrypted private
        key.

    :returns: Public JWK ready to publish in the agent's ``jwks_uri``.
        The private scalar (``d``) is NEVER included in the output.

    :raises ValueError: ``purpose`` is not a lowercase hyphenated token;
        the PEM is not Ed25519 or ECDSA-P-256; the EC curve is not P-256.
    """
    purpose = _check_purpose(purpose)
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
        return _adcp_public_jwk(OKPKey.import_key(public_key), kid=kid, adcp_use=purpose)
    return _adcp_public_jwk(ECKey.import_key(public_key), kid=kid, adcp_use=purpose)


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
    purpose: str = ADCP_USE_REQUEST,
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
    :param purpose: Which AdCP signing purpose this key is for. Sets the
        JWK ``adcp_use`` claim. The spec names ``"request-signing"`` (RFC
        9421 requests, and webhooks from new signers),
        ``"response-signing"`` (designated-task response JWS),
        ``"governance-signing"`` and the deprecated ``"webhook-signing"``;
        the constants are in :mod:`adcp.signing.constants`
        (``ADCP_USE_REQUEST``, ...). Any other lowercase hyphenated token
        is accepted for a surface the vocabulary does not name yet.
        An open vocabulary is safe because purpose matching is
        fail-closed: a verifier accepts a key only when ``adcp_use``
        equals the purpose of the surface it verifies (webhook verifiers
        accept ``request-signing`` or ``webhook-signing``), so a key
        published under any other purpose verifies nothing there.
        Generate separate keys per purpose.
    :param passphrase: When provided, the PEM is encrypted with
        ``BestAvailableEncryption``. Typical only for dev-laptop keys;
        automated deployments usually leave the PEM unencrypted and
        rely on filesystem perms (the CLI writes mode 0600).

        **Passphrase lifecycle.** CPython cannot zero ``bytes``. Once
        passed here, the buffer is handed to joserfc (which encrypts via
        ``cryptography``) and then released to GC; there's no ``zeroize`` step. Callers
        handling long-lived credentials should source the passphrase
        from a secret manager per call rather than hold a Python
        literal in process memory.

    :returns: ``(pem_bytes, public_jwk)``. The PEM is PKCS#8
        (optionally encrypted); the JWK is the public half, ready to
        publish at your ``jwks_uri``. The private scalar is NOT in the
        JWK — only in the PEM.

    :raises ValueError: ``alg`` is unsupported, or ``purpose`` is not a
        lowercase hyphenated token.

    Example:
        >>> pem, jwk = generate_signing_keypair(
        ...     alg="ed25519", purpose="webhook-signing"
        ... )
        >>> jwk["adcp_use"]
        'webhook-signing'
    """
    if alg not in ("ed25519", "es256"):
        raise ValueError(f"alg must be 'ed25519' or 'es256', got {alg!r}")
    purpose = _check_purpose(purpose)
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


def _purpose_arg(value: str) -> str:
    try:
        return _check_purpose(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adcp-keygen",
        description="Generate an AdCP signing keypair (PEM private key + public JWK).",
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
        type=_purpose_arg,
        default=ADCP_USE_REQUEST,
        metavar="PURPOSE",
        help=(
            "Which AdCP signing purpose this key is for (sets the JWK `adcp_use` "
            f"claim): {', '.join(_KNOWN_ADCP_USES)}, or another lowercase "
            "hyphenated token. `request-signing` (default) signs outbound tool "
            "calls and, for new signers, webhooks; `response-signing` signs "
            "designated-task response envelopes; `webhook-signing` is deprecated. "
            "Verifiers enforce the claim, so a key under the wrong purpose fails "
            "at first use."
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
