"""eTLD+1 (registrable domain) helpers for the brand-authorization binding.

Per ADCP request-signing semantics (spec #3690), a buyer agent is bound to
the brand whose ``brand.json`` lists it ONLY when the agent's host and the
brand's host share an eTLD+1 — for example, ``ads.brand.example`` agent
serving ``brand.example``. Cross-eTLD+1 agents are only honored when the
brand explicitly delegates via ``brand.authorized_operators[]`` (SaaS-as-
operator multi-tenancy).

We use the Public Suffix List (via ``tldextract``) rather than naive label
counting because public suffixes are not regular — ``co.uk`` and
``s3.amazonaws.com`` are both single suffixes despite different label
counts, and only a maintained PSL gets this right.

**Network posture: the bundled PSL snapshot is authoritative.** The
extractor is constructed with ``suffix_list_urls=()`` so a verifier never
silently re-fetches the PSL during request processing — both for latency
determinism and because cross-implementation conformance demands a pinned
snapshot. Bumping the floor on ``tldextract`` is how we refresh.

**Failure-closed convention.** Inputs whose eTLD+1 cannot be derived (raw
IP addresses, single-label hosts like ``localhost``, hosts that are
themselves public suffixes, hosts that are not IDNA-encodable — underscore
labels, labels over 63 bytes, leading/trailing-hyphen labels) yield ``None``
from :func:`registrable_domain` and ``False`` from
:func:`same_registrable_domain`. Callers must treat None / False as a
binding failure, not a soft skip.

**Hosts are compared in canonical A-label form.** ``tldextract`` is
IDNA-agnostic: hand it a U-label and it hands back a U-label. Comparing
unencoded hosts made ``straße.de`` and ``xn--strae-oqa.de`` — one host,
two spellings — never compare equal, refusing a legitimate agent whose
brand.json and agent URL disagreed on spelling. :func:`host_from`
therefore delegates to :func:`adcp.signing._idna_canonicalize.canonicalize_host`,
the same normalizer the JWKS, revocation, and key-origin checks use.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

import idna
import tldextract

from ._idna_canonicalize import canonicalize_host


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    """Process-singleton extractor with network refresh disabled.

    First-call PSL parsing is non-trivial (~hundreds of ms on cold disk
    cache); subsequent calls are cheap. The singleton keeps that cost
    paid-once-per-process.

    **Both ICANN and PRIVATE PSL sections are in scope.** Per ADCP
    spec #3690, the eTLD+1 binding must treat platform-shared suffixes
    like ``vercel.app``, ``pages.dev``, and ``github.io`` (in the PSL
    PRIVATE section) as suffixes — otherwise ``attacker.vercel.app``
    and ``victim.vercel.app`` would share an eTLD+1 of ``vercel.app``
    and an attacker's vercel deployment would falsely satisfy the
    binding against a vercel-hosted brand. ``include_psl_private_domains=True``
    closes that vector.
    """
    return tldextract.TLDExtract(
        suffix_list_urls=(),
        fallback_to_snapshot=True,
        include_psl_private_domains=True,
    )


def host_from(value: str) -> str:
    """Return the canonical hostname of a URL, or of a bare host.

    Both branches delegate to
    :func:`adcp.signing._idna_canonicalize.canonicalize_host`, which
    strips a single trailing FQDN-root dot, ASCII-lowercases,
    short-circuits IPv4/IPv6 literals (IDNA-2008 rejects purely-numeric
    labels), and otherwise UTS-46 encodes with
    ``idna.encode(uts46=True, transitional=False)``. So ``Example.COM.``
    and ``example.com`` compare equal, and so do ``straße.de`` and
    ``xn--strae-oqa.de``. Routing *both* branches through it is what
    makes the URL form and the bare-host form of one host normalize
    identically — previously only the bare-host branch trimmed the root
    dot, and it trimmed every trailing dot rather than one.

    Raises :class:`ValueError` on input that is a URL with no parseable
    host (``"http://"``), empty after normalization, or not encodable as
    a hostname. The last case surfaces as ``idna.IDNAError``, which is a
    :class:`ValueError` subclass (via ``UnicodeError``), so the raise
    contract is unchanged for callers catching ``ValueError``. URL inputs
    MUST use a scheme — a bare ``//example.com`` is treated as a bare
    host, which is by design: bare-host inputs to this helper come from
    ``brand_url`` fields whose schema already constrains them, so a
    bare-host input is never an attacker-controlled URL.
    """
    if "://" in value:
        parts = urlsplit(value)
        host = parts.hostname
        if not host:
            raise ValueError(f"URL has no host: {value!r}")
    else:
        host = value.strip()
        # Preserve the pre-existing message for ``""`` / ``"."`` / ``".."``;
        # without this guard ``idna`` would raise "Empty domain" instead.
        if not host.strip("."):
            raise ValueError("host is empty")
    return canonicalize_host(host)


def registrable_domain(host_or_url: str) -> str | None:
    """Return the eTLD+1 (registrable domain) for ``host_or_url``.

    Accepts a full URL (``https://ads.brand.example/...``) or a bare
    host (``ads.brand.example``). Returns ``None`` when the input has
    no eTLD+1:

    * IP literals (v4 and v6) — IP addresses are not eTLD+1-bindable.
    * Single-label hosts (``localhost``, ``intranet``).
    * Hosts that are themselves a public suffix (``co.uk``).
    * Hosts that are not IDNA-encodable (``under_score.brand.com``, a
      label over 63 bytes, ``-lead.brand.com``). Such a string is not a
      hostname, so it has no eTLD+1 to derive. Failing open here would
      let ``under_score.brand.com`` reduce to ``brand.com`` and satisfy
      the binding on a name the encoder rejects.

    The returned domain is lowercased and in canonical A-label form:
    ``straße.de`` returns ``"xn--strae-oqa.de"``. This is a change in
    public return values for IDN inputs; both sides of
    :func:`same_registrable_domain` move together, so the predicate
    stays correct.

    Callers performing a binding check should treat ``None`` as a
    failure (the agent's host has no registrable domain to bind
    against), NOT as "no opinion".
    """
    try:
        host = host_from(host_or_url)
    except (idna.IDNAError, UnicodeError):
        return None
    result = _extractor()(host)
    if not result.domain or not result.suffix:
        return None
    # Compose explicitly rather than using ``ExtractResult.registered_domain``
    # (or ``top_domain_under_public_suffix`` in 5.3+). Either accessor would
    # work, but composing keeps the helper insensitive to the property
    # rename tldextract 5.3 announced.
    return f"{result.domain}.{result.suffix}".lower()


def same_registrable_domain(a: str, b: str) -> bool:
    """Return True iff ``a`` and ``b`` share an eTLD+1.

    Both arguments may be URLs or bare hosts (mixed forms are fine).
    Returns ``False`` when either side has no derivable eTLD+1 — see
    :func:`registrable_domain` for the failure-closed convention.
    """
    da = registrable_domain(a)
    db = registrable_domain(b)
    if da is None or db is None:
        return False
    return da == db


__all__ = [
    "host_from",
    "registrable_domain",
    "same_registrable_domain",
]
