"""Framework helpers for running the AdCP request-signing verifier.

These are thin wrappers around `verify_request_signature`. The spec requires
rejection with `401` and `WWW-Authenticate: Signature error="<code>"` (no
realm) — `unauthorized_response_headers` gives you that header exactly.

Both wrappers return the verifier's `VerifiedSigner`, whose `parsed_body` is
the body as parsed by the strict step-14 parser. Route and dispatch on that
value; do not re-parse the raw bytes with `request.json()` / `get_json()`,
which keep the last of two duplicate keys and so can read a different
operation than the one the verifier accepted.

`parsed_body` is guaranteed well-formed, but it is signer-attested only when
`VerifiedSigner.body_authenticated` is true (the signature covered a matching
`content-digest`). With `covers_content_digest="either"` a signature may omit
the digest, which leaves the body unauthenticated. Require
`body_authenticated` (or advertise `"required"`, mandatory under AdCP 3.2)
before trusting the body's contents.
"""

from __future__ import annotations

from typing import Any

from adcp.signing.errors import SignatureVerificationError, signature_challenge
from adcp.signing.verifier import (
    VerifiedSigner,
    VerifyOptions,
    verify_request_signature,
)

#: ASGI scope key under which a verified :class:`VerifiedSigner` is recorded.
#: :func:`verify_starlette_request` sets it so framework verification
#: (``serve(request_signature_verification=...)``) reuses the result rather
#: than verifying again and rejecting the already-claimed nonce as replayed.
VERIFIED_SIGNER_SCOPE_KEY = "adcp.signing.verified_signer"


def unauthorized_response_headers(exc: SignatureVerificationError) -> dict[str, str]:
    """Headers for the 401 response. Realm is intentionally omitted per spec."""
    return {"WWW-Authenticate": signature_challenge(exc.code)}


def _wsgi_raw_headers(request: Any) -> list[tuple[bytes, bytes]] | None:
    """Best-effort raw header list from a WSGI request.

    Deliberately weaker than the ASGI equivalent, and it cannot be otherwise:
    PEP 3333 folds repeated `HTTP_*` headers into one comma-joined environ value
    and writes `CONTENT_TYPE` / `CONTENT_LENGTH` to bare environ keys with
    last-wins and no join. A second `Content-Type` line is therefore already
    gone before any WSGI app runs. This list carries the same information as
    `dict(request.headers)`; it exists so the comma-joined rules run over the
    same shape on both frameworks, not because it recovers the repetition.

    Returns `None` rather than raising if the headers cannot be encoded — a
    header outside latin-1 is not something to turn a 401 into a 500 over.
    """
    try:
        return [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in request.headers.items()
        ]
    except (AttributeError, UnicodeEncodeError):
        return None


def verify_flask_request(request: Any, *, options: VerifyOptions) -> VerifiedSigner:
    """Verify a Flask `request` object against the AdCP profile.

    Returns a `VerifiedSigner` whose `parsed_body` is the strictly parsed
    JSON body (`None` for a bodyless request). Use it instead of
    `request.get_json()`, which resolves duplicate keys last-wins. The body
    is signed only when `body_authenticated` is true.
    """
    return verify_request_signature(
        method=request.method,
        url=request.url,
        headers=dict(request.headers),
        body=request.get_data(),
        options=options,
        raw_headers=_wsgi_raw_headers(request),
    )


async def verify_starlette_request(request: Any, *, options: VerifyOptions) -> VerifiedSigner:
    """Verify a Starlette / FastAPI ``Request`` object against the AdCP profile.

    Consumes ``await request.body()`` once — Starlette caches the result
    internally, so downstream handlers calling ``request.body()`` again get
    the same bytes. If your handler needs the parsed body AFTER this
    verifier succeeds, use :attr:`VerifiedSigner.parsed_body` — the body as
    parsed by the strict step-14 parser. Don't call ``request.json()``:
    it resolves duplicate keys last-wins, which is the parser differential
    step 14 closes. The body is signer-attested only when
    :attr:`VerifiedSigner.body_authenticated` is true.

    On success the signer is also recorded at
    ``request.scope[VERIFIED_SIGNER_SCOPE_KEY]``, so framework verification
    downstream reuses it instead of claiming the nonce a second time.

    Returns
    -------
    VerifiedSigner
        On success — carries the verified ``key_id``, metadata, and
        ``parsed_body`` (``None`` for a bodyless request).

    Raises
    ------
    SignatureVerificationError
        On any failure of the AdCP verifier checklist. The ``.code``
        attribute holds the spec's error code string (e.g.
        ``request_signature_replayed``) and ``.step`` points at the
        failed checklist step. Frameworks typically map this to a 401
        with :func:`unauthorized_response_headers`.
    """
    body = await request.body()
    signer = verify_request_signature(
        method=request.method,
        url=str(request.url),
        headers=dict(request.headers),
        body=body,
        options=options,
        # The as-received list, wire order intact. This is the arm where the
        # step-1 repeated-line rule actually bites: `dict(request.headers)`
        # resolves a repeated name to one value, so a proxy-inserted second
        # `Content-Type` is invisible without it. It also carries a raw
        # non-ASCII Host, which `str(request.url)` drops -- Starlette's `URL`
        # falls back to `scope["server"]` when the Host header fails its host
        # regex, so the U-label survives only here.
        raw_headers=getattr(request.headers, "raw", None),
    )
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        scope[VERIFIED_SIGNER_SCOPE_KEY] = signer
    return signer


__all__ = [
    "VERIFIED_SIGNER_SCOPE_KEY",
    "unauthorized_response_headers",
    "verify_flask_request",
    "verify_starlette_request",
]
