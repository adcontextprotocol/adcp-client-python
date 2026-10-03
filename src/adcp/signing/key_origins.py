"""``identity.key_origins`` consistency check (ADCP #3690).

Per ADCP request-signing spec, an agent advertising signing posture
declares an ``identity.key_origins`` map on its ``get_adcp_capabilities``
response — keyed by purpose (``request_signing``, ``webhook_signing``,
``governance_signing``, ``tmp_signing``) and valued with the origin URI
that hosts the JWKS for that purpose.

After resolving an agent's keys via the brand.json chain, the verifier
MUST confirm the resolved ``jwks_uri`` host equals the declared origin
for the purpose under check. The check defends against the
shared-tenancy spoof where an attacker stands up a brand.json that
lists a counterparty's legitimate ``jwks_uri`` while the counterparty's
own capabilities advertise a different origin: the agent claims one
trust root via brand.json and a different one via capabilities, and
without the consistency check the verifier silently honors the
brand.json side.

Reject codes:

* ``request_signature_key_origin_mismatch`` — declared origin differs
  from resolved ``jwks_uri`` host (after canonicalization).
* ``request_signature_key_origin_missing`` — signing posture asserted
  but no ``identity.key_origins.{purpose}`` declaration.

Publisher ``adagents.json signing_keys`` pins narrow the operator JWKS and
never bypass this check.

The webhook profile reuses this check; pass ``code_family="webhook"`` to
raise the webhook-family code instead. That code comes from
``REQUEST_TO_WEBHOOK_CODE``, which maps both key-origin codes to
``webhook_signature_key_unknown`` — the webhook profile declares no per-hop
code for the key-discovery chain.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

import idna

from adcp.signing._idna_canonicalize import canonicalize_host
from adcp.signing.errors import (
    REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISSING,
    REQUEST_TO_WEBHOOK_CODE,
    SignatureVerificationError,
)

CodeFamily = Literal["request", "webhook"]


def _code(request_code: str, code_family: CodeFamily) -> str:
    """Translate a request-family code into ``code_family``.

    Per spec #3690 §"Discovering an agent's signing keys via brand_json_url"
    step 7. Operator JWKS origins remain authoritative with publisher pins.
    ``REQUEST_TO_WEBHOOK_CODE`` is the one table that decides the webhook
    profile's code for a request-family failure, so the webhook family is read
    from it rather than restated here.
    """
    if code_family == "request":
        return request_code
    return REQUEST_TO_WEBHOOK_CODE[request_code]


def check_key_origin_consistency(
    *,
    jwks_uri: str,
    key_origins: Mapping[str, str] | None,
    purpose: str,
    posture: str | None = None,
    code_family: CodeFamily = "request",
) -> None:
    """Verify that the resolved ``jwks_uri`` host matches the declared
    ``identity.key_origins.{purpose}``.

    Apply this check even when a publisher pin narrows the operator keys.
    Only the 3.x discovery fallback without brand_json_url skips it.

    Parameters
    ----------
    jwks_uri:
        The JWKS URI the verifier resolved via the brand.json chain.
        Only the host portion is consulted.
    key_origins:
        The ``identity.key_origins`` map from the agent's
        ``get_adcp_capabilities`` response. ``None`` is equivalent to
        an empty map.
    purpose:
        The purpose under check — typically one of ``request_signing``,
        ``webhook_signing``, ``governance_signing``, ``tmp_signing``.
        Free-form string so a future purpose can be checked without
        changes here.
    posture:
        Optional context attached to ``key_origin_missing`` rejection
        for adopter diagnostics (e.g. ``"required"``, ``"supported"``).
        Surfaced as ``detail['posture']`` and in the message.
    code_family:
        ``"request"`` (default) or ``"webhook"``. Picks the
        corresponding spec error code family.

    Raises
    ------
    SignatureVerificationError
        With ``code = *_key_origin_missing`` when ``purpose`` is absent
        from ``key_origins`` — ``detail`` carries ``{purpose, posture}``.

        With ``code = *_key_origin_mismatch`` when the purpose's declared
        origin differs from the resolved ``jwks_uri`` host (after IDNA
        A-label canonicalization). ``detail`` carries
        ``{purpose, expected_origin, actual_origin}`` per the spec's
        rejection-code shape — middleware adapters surface these as
        structured fields on the 401 / in a DLQ.
    """
    declared = (key_origins or {}).get(purpose)
    if declared is None:
        missing_detail: dict[str, str] = {"purpose": purpose}
        if posture:
            missing_detail["posture"] = posture
        raise SignatureVerificationError(
            _code(REQUEST_SIGNATURE_KEY_ORIGIN_MISSING, code_family),
            step=7,
            message=(
                f"identity.key_origins.{purpose} declaration missing"
                + (f" (posture={posture})" if posture else "")
            ),
            detail=missing_detail,
        )

    actual_host = _origin_host(jwks_uri)
    declared_host = _origin_host(declared)
    if actual_host is None or declared_host is None or actual_host != declared_host:
        raise SignatureVerificationError(
            _code(REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH, code_family),
            step=7,
            message=(
                f"identity.key_origins.{purpose} declares {declared_host!r} "
                f"but resolved jwks_uri host is {actual_host!r}"
            ),
            detail={
                "purpose": purpose,
                # Use the canonicalized values when available; fall back
                # to a best-effort host extraction (NOT the raw URL — the
                # field name promises a host, and surfacing a full URL on
                # canonicalization failure was inconsistent with the
                # success path's host-only shape). Spec wording is
                # ``expected_origin`` / ``actual_origin`` verbatim.
                "expected_origin": _diagnostic_host(declared_host, declared),
                "actual_origin": _diagnostic_host(actual_host, jwks_uri),
            },
        )


def _diagnostic_host(canonical: str | None, raw: str) -> str:
    """Return ``canonical`` if present, else a best-effort host from
    ``raw``, else the empty string.

    Used to keep ``expected_origin`` / ``actual_origin`` host-shaped
    in the mismatch detail payload even when canonicalization failed.
    Falls through to ``_extract_host`` (the same URL/bare-host parser
    the canonicalization step uses) for the best-effort path, so the
    diagnostic value still reflects "the host the operator/verifier
    pointed at" rather than the full URL surface.
    """
    if canonical is not None:
        return canonical
    host = _extract_host(raw)
    return host or ""


def _origin_host(value: str) -> str | None:
    """Return the host portion of a URL or bare origin, canonicalized
    for byte-equality comparison.

    Delegates to :func:`canonicalize_host` for the package-wide
    IDNA-2008 (UTS#46) convention shared with ``jwks.py``,
    ``ip_pinned_transport.py``, and ``revocation_fetcher.py``.
    IDNA-2008 preserves Eszett (``ß``) and final-sigma rather than
    mapping them away (which IDNA-2003 does), matching the
    canonicalization the request-signing spec mandates for
    cross-implementation byte-equality. IP literals short-circuit
    through ``ipaddress.ip_address`` so they're not rejected by
    IDNA-2008's reject-purely-numeric-label rule.

    **Bare-host and URL forms are normalized symmetrically.** A bare
    host like ``"keys.brand.com"`` is processed through the same
    ``urlsplit`` path as a full URL (with a synthetic scheme prepended)
    so port, userinfo, query, and fragment all strip consistently.
    Without that synthesis, a declarant supplying
    ``"keys.brand.com:8443"`` as a bare host would canonicalize to
    ``"keys.brand.com:8443"`` while the matching URL form would
    canonicalize to ``"keys.brand.com"`` — a fail-closed asymmetry an
    attacker who controls capabilities could exploit to deny
    verification against the operator's brand.json origin.

    **Trailing-dot equality.** ``host.example.`` and ``host.example``
    are the same FQDN at the protocol layer (the dot denotes the root
    zone). A counterparty serving the dot form while the capability
    declares the no-dot form (or vice versa) must not mismatch.
    :func:`canonicalize_host` strips a single trailing dot before
    encoding.

    Returns ``None`` when the input is structurally invalid (no
    resolvable host, or it parses but contains characters that don't
    survive IDNA); callers treat ``None`` as a binding failure.
    """
    host = _extract_host(value)
    if host is None:
        return None
    if not host:
        return None
    try:
        return canonicalize_host(host)
    except (idna.IDNAError, UnicodeError, UnicodeEncodeError):
        return None


def _extract_host(value: str) -> str | None:
    """Pull the host portion out of ``value``, accepting both URL form
    (``https://keys.brand.com/...``) and bare-host form
    (``keys.brand.com``).

    For URL inputs the host comes from ``urlsplit().hostname``. For
    bare-host inputs we prepend a synthetic ``https://`` scheme and
    re-parse so port / userinfo / query / fragment all strip the same
    way they would for an explicit URL — closing the bare-host vs URL
    asymmetry that the bare-host fallback used to have.

    **Bare IPv6 needs bracket synthesis.** ``urlsplit("https://2001:db8::1")``
    interprets the first ``:`` as the port separator and produces
    ``hostname="2001"``, which then fails canonicalization downstream.
    Detect bare IPv6 (multiple ``:`` and no scheme, not already
    bracketed) and add brackets before re-parsing.
    """
    parts = urlsplit(value)
    if parts.hostname:
        return parts.hostname

    # Schemeless input. Prepend ``https://`` and re-parse.
    # Strip whitespace first so leading-space inputs don't produce
    # ``https:// foo.com`` which then fails to parse a host.
    stripped = value.strip()
    if not stripped:
        return None
    # Bare IPv6 needs brackets to survive urlsplit's port-separator
    # interpretation of ``:``. ``ipaddress.ip_address`` rejects
    # bracketed and dotted-quad-with-port forms; using it as the
    # IPv6 detector is precise.
    if not stripped.startswith("["):
        try:
            ip = ipaddress.ip_address(stripped)
        except ValueError:
            pass
        else:
            if ip.version == 6:
                stripped = f"[{stripped}]"
    parts = urlsplit(f"https://{stripped}")
    return parts.hostname or None


__all__ = [
    "CodeFamily",
    "check_key_origin_consistency",
]
