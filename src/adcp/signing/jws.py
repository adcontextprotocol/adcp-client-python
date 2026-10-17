"""JWS verification for AdCP revocation lists, on top of ``joserfc``.

The AdCP governance profile uses JSON Web Signature (RFC 7515) to sign
revocation-list documents published at
``{origin}/.well-known/governance-revocations.json``. The list MAY be
serialized in compact form (``header.payload.signature``) or JWS general
JSON serialization (an object with ``payload`` and ``signatures[]``).

``joserfc`` does the JOSE work: base64url and header decoding, header
parameter validation, the JWS signing input, JWK import, key ``use`` /
``key_ops`` checks, ES256 raw ``r || s`` handling, and the signature check.
This module keeps only the AdCP profile on top of it:

* ``alg`` is ``EdDSA`` (Ed25519 only) or ``ES256``; checked before any key
  lookup, never left to library defaults (security.mdx checklist step 2);
* ``typ`` byte-equals the caller's expected value (step 3);
* ``kid`` is a non-empty protected-header string; the key comes from the
  caller's resolver;
* ``crit`` lists no extensions: the revocation-list profile defines none;
* the general JSON form carries exactly one signature, and only its
  protected header is honored;
* the protected header and payload are strict JSON (see
  :func:`_decode_json_object`).
"""

from __future__ import annotations

import sys
from typing import Any

from joserfc import jws
from joserfc.errors import (
    BadSignatureError,
    InvalidKeyCurveError,
    JoseError,
    KeyParameterError,
    UnsupportedKeyAlgorithmError,
)
from joserfc.jwa import EdDSAAlgorithm, ESAlgorithm, JWSAlgModel
from joserfc.jwk import ECKey, OKPKey
from joserfc.jws import JWSRegistry
from joserfc.util import urlsafe_b64decode, urlsafe_b64encode

from adcp.signing._strict_json import StrictJsonError, parse_strict_json
from adcp.signing.crypto import ALG_ED25519, ALG_ES256
from adcp.signing.jwks import AsyncJwksResolver, JwksResolver

# JWS (RFC 7518) alg names mapped to the RFC 9421 names the rest of the SDK
# uses. Kept as public API; its keys are the profile's allowed algs.
JWS_ALG_TO_INTERNAL: dict[str, str] = {
    "EdDSA": ALG_ED25519,
    "ES256": ALG_ES256,
}
ALLOWED_JWS_ALGS: frozenset[str] = frozenset(JWS_ALG_TO_INTERNAL.keys())

# RFC 7518 section 6.2.1.2: a P-256 coordinate is exactly 32 bytes.
_P256_COORDINATE_BYTES = 32


class JwsError(Exception):
    """Base class for JWS parse/verify failures."""


class JwsMalformedError(JwsError):
    """JWS document is syntactically invalid or uses a disallowed shape."""


class JwsUnknownKeyError(JwsError):
    """The JWS header ``kid`` is not present in the configured JWKS."""


class JwsSignatureInvalidError(JwsError):
    """The JWS signature did not verify against the resolved key."""


class _Ed25519EdDSA(EdDSAAlgorithm):
    """RFC 8037 ``EdDSA``, pinned to Ed25519.

    joserfc's ``EdDSA`` also accepts Ed448 keys, which the AdCP profile does
    not. It also warns that RFC 9864 deprecates the name, but AdCP mandates
    ``EdDSA`` on the wire, so the warning is dropped.
    """

    # joserfc types this ``str`` on EdDSAAlgorithm; its base model allows None.
    security_warning = None  # type: ignore[assignment]

    def check_key(self, key: Any) -> None:
        key.check_use("sig")
        if not isinstance(key, OKPKey) or key.curve_name != "Ed25519":
            raise InvalidKeyCurveError("AdCP EdDSA requires an Ed25519 OKP key")
        # A JWK may advertise RFC 9864's fully-specified "Ed25519" for the same
        # key. The verifier accepted that before joserfc, so it still does.
        designed_alg = key.get("alg")
        if designed_alg not in (None, "EdDSA", "Ed25519"):
            raise UnsupportedKeyAlgorithmError(
                f"This key is designed for algorithm '{designed_alg}'"
            )


class _AdcpJwsRegistry(JWSRegistry):
    """joserfc registry that knows only the AdCP profile algorithms.

    The size caps are lifted. The fetch layer already bounds the document
    (``max_body_bytes``, 1 MiB by default and configurable). joserfc's
    smaller default caps (512-byte header, 128 kB payload) would reject
    revocation lists that the configured limit admits.
    """

    algorithms: dict[str, JWSAlgModel] = {
        "EdDSA": _Ed25519EdDSA(),
        "ES256": ESAlgorithm("ES256", "P-256", 256),
    }
    recommended: list[str] = []
    max_header_length = sys.maxsize
    max_payload_length = sys.maxsize
    max_signature_length = sys.maxsize


def _b64url_decode(segment: str) -> bytes:
    """RFC 7515 base64url: URL-safe alphabet, no padding, canonical tail bits."""
    return urlsafe_b64decode(segment.encode("ascii"))


def _decode_json_object(raw: bytes, what: str) -> dict[str, Any]:
    """Parse ``raw`` as a strict JSON object.

    joserfc parses JSON with ``json.loads``. That keeps the last of two
    duplicate member names and accepts ``NaN`` and lone surrogates. The bytes
    are signed, not their reading, so two verifiers could disagree about what
    the same signed list says. For example, a second ``revoked_kids`` could
    erase a revocation for one of them. RFC 7515 section 5.2 lets a recipient
    reject duplicate header names, so both header and payload are re-parsed
    with the SDK's strict parser.
    """
    try:
        value = parse_strict_json(raw)
    except StrictJsonError as exc:
        raise JwsMalformedError(f"{what} is not valid JSON: {exc.reason}") from exc
    if not isinstance(value, dict):
        raise JwsMalformedError(f"{what} is not a JSON object")
    return value


def _split_registry() -> _AdcpJwsRegistry:
    # Splitting decodes the protected header and checks segment sizes only;
    # the profile's alg allow-list is applied by _validate_header afterwards.
    return _AdcpJwsRegistry(strict_check_header=False)


def parse_compact_jws(token: str) -> tuple[str, str, bytes]:
    """Split a compact JWS into ``(b64_header, b64_payload, signature bytes)``.

    ``joserfc.jws.extract_compact`` does the split. The header and payload
    come back as the ORIGINAL base64url substrings, not decoded and
    re-encoded, so the verifier checks the exact bytes the signer signed.
    """
    if not isinstance(token, str):
        raise JwsMalformedError("compact JWS must be a string")
    parts = token.split(".")
    if len(parts) != 3:
        raise JwsMalformedError(
            f"compact JWS must have exactly 3 dot-separated segments, got {len(parts)}"
        )
    if "" in parts:
        raise JwsMalformedError("compact JWS has an empty segment")
    try:
        compact = jws.extract_compact(token.encode("ascii"), registry=_split_registry())
    except (JoseError, UnicodeError, ValueError) as exc:
        raise JwsMalformedError(
            f"compact JWS protected header is not valid base64url JSON: {exc}"
        ) from exc
    try:
        signature = _b64url_decode(compact.segments["signature"].decode("ascii"))
    except ValueError as exc:
        raise JwsMalformedError(f"compact JWS signature is not valid base64url: {exc}") from exc
    return (
        compact.segments["header"].decode("ascii"),
        compact.segments["payload"].decode("ascii"),
        signature,
    )


def parse_general_json_jws(doc: dict[str, Any]) -> tuple[str, str, bytes]:
    """Extract the single signature from a JWS general JSON serialization.

    ``joserfc.jws.extract_compact`` does the parse (see below). Returns
    ``(b64_header, b64_payload, signature_bytes)``. A single operator key
    signs an AdCP revocation list, so this profile treats a list with more
    than one signature as malformed. An unprotected ``header`` member is
    ignored: every parameter the profile reads must be integrity-protected.
    """
    if not isinstance(doc, dict):
        raise JwsMalformedError("JWS general JSON document must be an object")
    if "payload" not in doc or "signatures" not in doc:
        raise JwsMalformedError("JWS general JSON document must have 'payload' and 'signatures'")
    signatures = doc["signatures"]
    if not isinstance(signatures, list) or len(signatures) == 0:
        raise JwsMalformedError("JWS general JSON 'signatures' must be a non-empty array")
    if len(signatures) > 1:
        raise JwsMalformedError(
            "JWS general JSON 'signatures' with multiple entries is not supported for "
            "this profile"
        )
    entry = signatures[0]
    if not isinstance(entry, dict):
        raise JwsMalformedError("JWS signature entry must be an object")
    if not isinstance(entry.get("protected"), str) or not isinstance(entry.get("signature"), str):
        raise JwsMalformedError("JWS signature entry missing 'protected' or 'signature'")
    if not isinstance(doc["payload"], str):
        raise JwsMalformedError("JWS 'payload' must be a base64url string")
    # One signature with a protected header is the compact serialization in
    # another shape (RFC 7515 section 7.2.1), so joserfc's compact extractor
    # splits it with the same checks.
    return parse_compact_jws(f"{entry['protected']}.{doc['payload']}.{entry['signature']}")


def _validate_header(
    b64_protected: str,
    *,
    expected_typ: str,
    allowed_algs: frozenset[str],
) -> tuple[str, str]:
    """Apply the AdCP header profile; return ``(jws_alg, kid)``.

    Runs before key resolution, so a token outside the profile never causes a
    JWKS lookup. Shared by the sync and async paths. Everything here is pure
    CPU.
    """
    try:
        raw = _b64url_decode(b64_protected)
    except ValueError as exc:
        raise JwsMalformedError(f"protected header is not valid base64url: {exc}") from exc
    header = _decode_json_object(raw, "protected header")

    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in allowed_algs or alg not in JWS_ALG_TO_INTERNAL:
        raise JwsMalformedError(
            f"JWS alg {alg!r} not allowed; permitted values: {sorted(allowed_algs)}"
        )

    typ = header.get("typ")
    if typ != expected_typ:
        raise JwsMalformedError(f"JWS typ {typ!r} does not match expected {expected_typ!r}")

    crit = header.get("crit")
    if crit is not None and (not isinstance(crit, list) or len(crit) > 0):
        raise JwsMalformedError("JWS 'crit' header is not supported for this profile")

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise JwsMalformedError("JWS protected header must include a non-empty 'kid'")

    return alg, kid


def _import_jwk(jwk: dict[str, Any]) -> OKPKey | ECKey:
    """Import the resolved JWK as an Ed25519 or P-256 public key.

    An unusable JWK raises ``ValueError``, not a :class:`JwsError`. That is the
    exception the verifier raised before joserfc, so callers see no change.
    """
    kty = jwk.get("kty")
    crv = jwk.get("crv")
    try:
        if kty == "OKP" and crv == "Ed25519":
            return OKPKey.import_key(jwk)
        if kty == "EC" and crv == "P-256":
            # joserfc accepts a P-256 coordinate with leading zero bytes.
            # RFC 7518 section 6.2.1.2 fixes the length, so check it here.
            for name in ("x", "y"):
                coordinate = jwk.get(name)
                if (
                    not isinstance(coordinate, str)
                    or len(_b64url_decode(coordinate)) != _P256_COORDINATE_BYTES
                ):
                    raise ValueError(f"P-256 JWK {name} must be {_P256_COORDINATE_BYTES} bytes")
            return ECKey.import_key(jwk)
    except (JoseError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"unusable {kty}/{crv} JWK: {exc}") from exc
    raise ValueError(f"unsupported JWK kty/crv: {kty}/{crv}")


def _verify_signature_and_decode_payload(
    *,
    b64_protected: str,
    b64_payload: str,
    signature: bytes,
    jwk: dict[str, Any],
    alg: str,
    kid: str,
    allowed_algs: frozenset[str],
) -> dict[str, Any]:
    """Verify with joserfc, then decode the payload as a strict JSON object.

    The header and payload segments go to joserfc exactly as received, so the
    signing input is the wire bytes.
    """
    key = _import_jwk(jwk)
    b64_signature = urlsafe_b64encode(signature).decode("ascii")
    token = f"{b64_protected}.{b64_payload}.{b64_signature}"
    registry = _AdcpJwsRegistry(algorithms=allowed_algs, strict_check_header=False)
    try:
        verified = jws.deserialize_compact(token, key, registry=registry)
    except BadSignatureError as exc:
        raise JwsSignatureInvalidError(f"signature did not verify for kid {kid!r}") from exc
    except (KeyParameterError, InvalidKeyCurveError) as exc:
        # The key cannot produce this alg: wrong type or curve, or its use,
        # key_ops, or alg forbids verifying it. The signature cannot verify.
        raise JwsSignatureInvalidError(
            f"key for kid {kid!r} cannot verify a {alg} signature: {exc}"
        ) from exc
    except JoseError as exc:
        raise JwsMalformedError(f"JWS is malformed: {exc}") from exc
    return _decode_json_object(verified.payload, "JWS payload")


def verify_detached_jws(
    *,
    b64_protected: str,
    b64_payload: str,
    signature: bytes,
    jwks_resolver: JwksResolver,
    expected_typ: str,
    allowed_algs: frozenset[str] = ALLOWED_JWS_ALGS,
) -> dict[str, Any]:
    """Verify a parsed JWS and return the decoded payload JSON.

    Performs, in order:
    1. Decode + parse the protected header.
    2. Reject if ``alg`` is absent, is ``none``, or not in ``allowed_algs``.
    3. Reject if ``typ`` is not exactly ``expected_typ`` (byte equality, no
       normalization — matches the AdCP spec).
    4. Resolve ``kid`` via ``jwks_resolver``; reject unknown.
    5. Verify the signature against the original b64 strings.
    6. Decode the payload as JSON and return it.

    Any JWS failure raises a :class:`JwsError` subclass. The caller maps these
    to transport-error codes (e.g. ``request_signature_revocation_stale``).
    """
    alg, kid = _validate_header(b64_protected, expected_typ=expected_typ, allowed_algs=allowed_algs)
    jwk = jwks_resolver(kid)
    if jwk is None:
        raise JwsUnknownKeyError(f"no JWK for kid {kid!r}")
    return _verify_signature_and_decode_payload(
        b64_protected=b64_protected,
        b64_payload=b64_payload,
        signature=signature,
        jwk=jwk,
        alg=alg,
        kid=kid,
        allowed_algs=allowed_algs,
    )


async def averify_detached_jws(
    *,
    b64_protected: str,
    b64_payload: str,
    signature: bytes,
    jwks_resolver: AsyncJwksResolver,
    expected_typ: str,
    allowed_algs: frozenset[str] = ALLOWED_JWS_ALGS,
) -> dict[str, Any]:
    """Async variant of :func:`verify_detached_jws`.

    Awaits an :class:`AsyncJwksResolver` on the ``kid`` lookup; all
    other work is pure CPU and runs inline.
    """
    alg, kid = _validate_header(b64_protected, expected_typ=expected_typ, allowed_algs=allowed_algs)
    jwk = await jwks_resolver(kid)
    if jwk is None:
        raise JwsUnknownKeyError(f"no JWK for kid {kid!r}")
    return _verify_signature_and_decode_payload(
        b64_protected=b64_protected,
        b64_payload=b64_payload,
        signature=signature,
        jwk=jwk,
        alg=alg,
        kid=kid,
        allowed_algs=allowed_algs,
    )


def _parse_document(doc: str | dict[str, Any]) -> tuple[str, str, bytes]:
    if isinstance(doc, str):
        return parse_compact_jws(doc)
    if isinstance(doc, dict):
        return parse_general_json_jws(doc)
    raise JwsMalformedError(
        "JWS document must be a compact string or JSON general-serialization object"
    )


def verify_jws_document(
    doc: str | dict[str, Any],
    *,
    jwks_resolver: JwksResolver,
    expected_typ: str,
    allowed_algs: frozenset[str] = ALLOWED_JWS_ALGS,
) -> dict[str, Any]:
    """Parse a JWS (compact string or general-JSON dict) and verify in one call.

    Dispatches to the right parser based on the input shape, then calls
    :func:`verify_detached_jws`. Returns the verified payload dict.
    """
    b64_header, b64_payload, signature = _parse_document(doc)
    return verify_detached_jws(
        b64_protected=b64_header,
        b64_payload=b64_payload,
        signature=signature,
        jwks_resolver=jwks_resolver,
        expected_typ=expected_typ,
        allowed_algs=allowed_algs,
    )


async def averify_jws_document(
    doc: str | dict[str, Any],
    *,
    jwks_resolver: AsyncJwksResolver,
    expected_typ: str,
    allowed_algs: frozenset[str] = ALLOWED_JWS_ALGS,
) -> dict[str, Any]:
    """Async variant of :func:`verify_jws_document`.

    Accepts an :class:`AsyncJwksResolver`; parsing and signature verify
    are pure CPU and run inline.
    """
    b64_header, b64_payload, signature = _parse_document(doc)
    return await averify_detached_jws(
        b64_protected=b64_header,
        b64_payload=b64_payload,
        signature=signature,
        jwks_resolver=jwks_resolver,
        expected_typ=expected_typ,
        allowed_algs=allowed_algs,
    )


__all__ = [
    "ALLOWED_JWS_ALGS",
    "AsyncJwksResolver",
    "JWS_ALG_TO_INTERNAL",
    "JwksResolver",
    "JwsError",
    "JwsMalformedError",
    "JwsSignatureInvalidError",
    "JwsUnknownKeyError",
    "averify_detached_jws",
    "averify_jws_document",
    "parse_compact_jws",
    "parse_general_json_jws",
    "verify_detached_jws",
    "verify_jws_document",
]
