"""Key handling and signature primitives for the AdCP request-signing profile.

Supported algorithms per RFC 9421 profile:
- `ed25519` (EdDSA over Curve25519)
- `ecdsa-p256-sha256` (ES256)

Signing and verification are ``http-message-signatures``' algorithm classes,
which also convert ES256 to and from the IEEE P1363 ``r || s`` form RFC 9421
§3.3.2 requires. The ``Signature`` field is parsed and serialized by
``http-sf``. What stays here is AdCP key policy: the JWK shapes the profile
accepts and the PEM loader.

Encrypted PEM support (passphrase-protected private keys) is tracked in #191.
"""

from __future__ import annotations

from typing import Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from http_message_signatures.exceptions import HTTPMessageSignaturesException
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, OKPKey
from joserfc.util import urlsafe_b64decode, urlsafe_b64encode

from adcp.signing._rfc9421 import ALGORITHMS, parse_dictionary, serialize, translate_legacy_binary

PublicKey = ed25519.Ed25519PublicKey | ec.EllipticCurvePublicKey
PrivateKey = ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey

ALG_ED25519 = "ed25519"
ALG_ES256 = "ecdsa-p256-sha256"
ALLOWED_ALGS: frozenset[str] = frozenset({ALG_ED25519, ALG_ES256})

# RFC 7518 section 6.2.1.2: a P-256 coordinate (and section 6.2.2.1, the
# private scalar) is exactly 32 bytes. joserfc accepts a shorter spelling of
# the same number, so the length is checked here.
_P256_MEMBER_BYTES = 32

# RFC 7518 sections 6.2.2, 6.3.2 and 6.4.1: the members only a private or
# symmetric key carries.
_PRIVATE_JWK_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})


def b64url_decode(s: str) -> bytes:
    """Decode RFC 7515 base64url with ``joserfc``: no padding, one spelling.

    Characters outside the URL-safe alphabet, padding, and non-zero trailing
    bits raise ``ValueError``.
    """
    return urlsafe_b64decode(s.encode("ascii"))


def b64url_encode(b: bytes) -> str:
    """Encode bytes as base64url without padding (``joserfc``)."""
    return urlsafe_b64encode(b).decode("ascii")


def load_private_key_pem(pem: bytes, *, password: bytes | None = None) -> PrivateKey:
    """Load an Ed25519 or P-256 private key from PKCS8 PEM bytes.

    Closes the loop between ``adcp-keygen`` (which writes a PEM) and
    :func:`sign_request` (which takes a ``PrivateKey`` object), so
    integrators don't need a direct ``cryptography`` import just to
    rehydrate the key.

    Parameters
    ----------
    pem:
        PEM-encoded PKCS8 private key as bytes. Read via
        ``pathlib.Path(...).read_bytes()``.
    password:
        Passphrase if the PEM is encrypted (``adcp-keygen --encrypt``).
        Passed through to the cryptography loader as bytes.

    Returns
    -------
    PrivateKey
        An :class:`Ed25519PrivateKey` or
        :class:`EllipticCurvePrivateKey` ready to pass into
        :func:`sign_request`.

    Raises
    ------
    ValueError
        The PEM is not Ed25519 or ES256 (P-256). These are the only
        algorithms the AdCP request-signing profile allows.
    """
    key = serialization.load_pem_private_key(pem, password=password)
    if not isinstance(key, (ed25519.Ed25519PrivateKey, ec.EllipticCurvePrivateKey)):
        raise ValueError(
            f"unsupported private key type {type(key).__name__} — "
            f"AdCP signing accepts Ed25519 or ECDSA-P-256 only"
        )
    if isinstance(key, ec.EllipticCurvePrivateKey) and not isinstance(key.curve, ec.SECP256R1):
        raise ValueError(
            f"EC key curve {key.curve.name} is not supported — only P-256 (SECP256R1) is allowed"
        )
    return key


def import_jwk(jwk: dict[str, Any], *, private: bool = False) -> OKPKey | ECKey:
    """Import an Ed25519 or P-256 JWK with ``joserfc``.

    ``joserfc`` decodes the members strictly and checks the key. An unusable
    JWK raises ``ValueError``. Unless ``private``, the private members are
    dropped first: ``joserfc`` derives the public key from a ``d`` it is
    given, and a ``kid`` or pin names the key by a thumbprint over ``x``.
    """
    if not private:
        jwk = {name: value for name, value in jwk.items() if name not in _PRIVATE_JWK_MEMBERS}
    kty = jwk.get("kty")
    crv = jwk.get("crv")
    key_type: type[OKPKey] | type[ECKey]
    if kty == "OKP" and crv == "Ed25519":
        key_type = OKPKey
    elif kty == "EC" and crv == "P-256":
        key_type = ECKey
    else:
        raise ValueError(f"unsupported JWK kty/crv: {kty}/{crv}")
    try:
        if key_type is ECKey:
            for name in ("x", "y", "d") if "d" in jwk else ("x", "y"):
                member = jwk.get(name)
                if not isinstance(member, str) or len(b64url_decode(member)) != _P256_MEMBER_BYTES:
                    raise ValueError(f"P-256 JWK {name} must be {_P256_MEMBER_BYTES} bytes")
        # joserfc 1.8 uses Python 3.11 Self; retain the SDK's Python 3.10
        # static contract at this library boundary.
        imported: OKPKey | ECKey = key_type.import_key(jwk)
        return imported
    except (JoseError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"unusable {kty}/{crv} JWK: {exc}") from exc


def public_key_from_jwk(jwk: dict[str, Any]) -> PublicKey:
    """Reconstruct a public key from its JWK (see :func:`import_jwk`)."""
    return cast(PublicKey, import_jwk(jwk).public_key)


def private_key_from_jwk(jwk: dict[str, Any], *, d_field: str = "d") -> PrivateKey:
    """Reconstruct an Ed25519 or P-256 private key from its JWK's ``d``.

    `d_field` lets callers read from a non-standard scalar field — the AdCP test
    vectors store the private half under `_private_d_for_test_only` to signal
    that the keys are not production-grade.

    The public members are optional. When ``x`` (or ``y``) is present, it must
    be the public key of ``d``, so a JWK that pairs ``d`` with another key's
    ``x`` raises ``ValueError`` here rather than failing at the receiver.
    """
    d = jwk.get(d_field)
    if not isinstance(d, str):
        raise ValueError(f"JWK has no private member {d_field!r}")
    kty, crv = jwk.get("kty"), jwk.get("crv")
    scalar = b64url_decode(d)
    if len(scalar) != 32:
        raise ValueError(f"{kty}/{crv} JWK {d_field} must be 32 bytes")
    key: PrivateKey
    if kty == "OKP" and crv == "Ed25519":
        key = ed25519.Ed25519PrivateKey.from_private_bytes(scalar)
        public = OKPKey.import_key(key).as_dict(private=False)
    elif kty == "EC" and crv == "P-256":
        key = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
        public = ECKey.import_key(key).as_dict(private=False)
    else:
        raise ValueError(f"unsupported JWK kty/crv: {kty}/{crv}")
    if any(name in jwk and jwk[name] != public[name] for name in ("x", "y") if name in public):
        raise ValueError(f"JWK public members are not the public key of {d_field}")
    return key


def alg_for_jwk(jwk: dict[str, Any]) -> str:
    """Map a JWK to its RFC 9421 alg name for the AdCP profile."""
    kty = jwk.get("kty")
    crv = jwk.get("crv")
    if kty == "OKP" and crv == "Ed25519":
        return ALG_ED25519
    if kty == "EC" and crv == "P-256":
        return ALG_ES256
    raise ValueError(f"unsupported JWK kty/crv: {kty}/{crv}")


def verify_signature(
    *,
    alg: str,
    public_key: PublicKey,
    signature_base: bytes,
    signature: bytes,
) -> bool:
    """Return True iff `signature` is valid over `signature_base` for `alg`.

    The check is ``http-message-signatures``' algorithm class for ``alg``
    (ES256 takes the RFC 9421 §3.3.2 ``r || s`` form). A key of the wrong
    type for ``alg`` cannot verify, so it returns False.
    """
    if alg not in ALLOWED_ALGS:
        raise ValueError(f"unsupported alg: {alg}")
    try:
        ALGORITHMS[alg](public_key=public_key).verify(signature=signature, message=signature_base)
    except (InvalidSignature, HTTPMessageSignaturesException):
        return False
    return True


def sign_signature_base(
    *,
    alg: str,
    private_key: PrivateKey,
    signature_base: bytes,
) -> bytes:
    """Produce a signature over the base. ES256 returns r||s (64 bytes)."""
    if alg not in ALLOWED_ALGS:
        raise ValueError(f"unsupported alg: {alg}")
    try:
        signer = ALGORITHMS[alg](private_key=private_key)
    except HTTPMessageSignaturesException as exc:
        expected = "Ed25519 key" if alg == ALG_ED25519 else "EC P-256 key"
        raise ValueError(f"alg/private_key mismatch: expected {expected}") from exc
    signature: bytes = signer.sign(signature_base)
    return signature


def extract_signature_bytes(
    signature_header: str,
    label: str = "sig1",
    *,
    allow_legacy_base64url: bool = True,
) -> bytes:
    """Parse a Signature header value and return the raw signature bytes for `label`.

    The value is an RFC 8941 Dictionary parsed by ``http-sf``, with duplicate
    labels and parameters refused. RFC 8941 byte sequences are standard
    padded Base64. AdCP 3.0/3.1 compatibility permits the historical
    Base64URL spelling, which is translated before the strict parse; 3.2
    callers disable that so a malformed field fails at verifier step 1.
    """
    try:
        members = parse_dictionary(signature_header)
    except ValueError:
        if not allow_legacy_base64url:
            raise
        members = parse_dictionary(translate_legacy_binary(signature_header))
    if label not in members:
        raise KeyError(f"label not found in Signature header: {label}")
    value, params = members[label]
    if not isinstance(value, bytes):
        raise ValueError(f"Signature value for {label!r} is not an sf-binary")
    if params:
        # RFC 9421 §4.2 defines no Signature member parameters; unsigned
        # extras are refused rather than ignored.
        raise ValueError(f"Signature value for {label!r} carries parameters")
    return value


def format_signature_header(
    signature: bytes,
    label: str = "sig1",
    *,
    use_legacy_base64url: bool = False,
) -> str:
    """Produce a Signature header value for a single-label signature.

    RFC 8941 serialization comes from ``http-sf``. The AdCP 3.0/3.1 legacy
    spelling (unpadded Base64URL) is not RFC 8941 and is formatted here.
    """
    if use_legacy_base64url:
        serialize({label: (True, {})})  # validates the label as an RFC 8941 key
        return f"{label}=:{b64url_encode(signature)}:"
    return serialize({label: (signature, {})})
