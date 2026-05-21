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

The carve-out for publisher ``adagents.json signing_keys`` pins (where
the key origin is the publisher's domain, not the operator's) is the
caller's responsibility: skip this check for the specific (agent,
purpose, role) tuple sourced from a publisher pin.

The webhook profile reuses this check via the
``webhook_signature_key_origin_*`` codes; pass ``code_family="webhook"``
to raise the webhook-family codes instead of the request family.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from adcp.signing.errors import (
    REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISSING,
    WEBHOOK_SIGNATURE_KEY_ORIGIN_MISMATCH,
    WEBHOOK_SIGNATURE_KEY_ORIGIN_MISSING,
    SignatureVerificationError,
)

CodeFamily = Literal["request", "webhook"]

#: Per spec #3690 §"Discovering an agent's signing keys via brand_json_url"
#: step 7. The check is mandatory only when the JWKS source was the
#: operator brand.json (not a publisher pin). The caller decides which
#: branch applies and either calls this function or skips it.
_MISMATCH_CODE: dict[CodeFamily, str] = {
    "request": REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH,
    "webhook": WEBHOOK_SIGNATURE_KEY_ORIGIN_MISMATCH,
}
_MISSING_CODE: dict[CodeFamily, str] = {
    "request": REQUEST_SIGNATURE_KEY_ORIGIN_MISSING,
    "webhook": WEBHOOK_SIGNATURE_KEY_ORIGIN_MISSING,
}


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
        Not consulted by the check itself.
    code_family:
        ``"request"`` (default) or ``"webhook"``. Picks the
        corresponding spec error code family.

    Raises
    ------
    SignatureVerificationError
        With ``code = *_key_origin_missing`` when ``purpose`` is absent
        from ``key_origins``; ``code = *_key_origin_mismatch`` when the
        purpose's declared origin differs from the resolved
        ``jwks_uri`` host (after IDNA-A-label canonicalization).
    """
    declared = (key_origins or {}).get(purpose)
    if declared is None:
        raise SignatureVerificationError(
            _MISSING_CODE[code_family],
            step=7,
            message=(
                f"identity.key_origins.{purpose} declaration missing"
                + (f" (posture={posture})" if posture else "")
            ),
        )

    actual_host = _origin_host(jwks_uri)
    declared_host = _origin_host(declared)
    if actual_host is None or declared_host is None or actual_host != declared_host:
        raise SignatureVerificationError(
            _MISMATCH_CODE[code_family],
            step=7,
            message=(
                f"identity.key_origins.{purpose} declares {declared_host!r} "
                f"but resolved jwks_uri host is {actual_host!r}"
            ),
        )


def _origin_host(value: str) -> str | None:
    """Return the host portion of a URL or bare origin, canonicalized
    for byte-equality comparison.

    Canonicalization mirrors the existing codebase pattern
    (``jwks.py:201``, ``ip_pinned_transport.py:110``,
    ``revocation_fetcher.py:380``): ASCII-lowercase, then
    ``host.encode("idna").decode("ascii")`` to convert IDN U-labels to
    their A-label (Punycode) form. The spec asks for IDNA-2008 strictly
    while stdlib ``encodings.idna`` is IDNA-2003; the divergence is
    rare in practice and matching the package's existing convention
    keeps the canonicalization story coherent. A future IDNA-2008
    migration would update all four callsites together.

    Returns ``None`` when the input is structurally invalid (no scheme
    or no host); callers treat ``None`` as a binding failure.
    """
    parts = urlsplit(value)
    host = parts.hostname
    if not host:
        # Permit bare-host inputs like ``"keys.brand.com"`` —
        # capabilities ``identity.key_origins`` values are not
        # spec-constrained to be full URLs, only to identify an origin.
        host = value.strip().lower()
        if not host or "/" in host or " " in host:
            return None
    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeEncodeError):
        return None


__all__ = [
    "CodeFamily",
    "check_key_origin_consistency",
]
