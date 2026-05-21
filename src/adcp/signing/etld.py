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
themselves public suffixes) yield ``None`` from :func:`registrable_domain`
and ``False`` from :func:`same_registrable_domain`. Callers must treat
None / False as a binding failure, not a soft skip.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

import tldextract


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
    """Return the hostname portion of a URL, or pass a bare host through.

    Normalizes case and trims a single trailing dot (the FQDN root
    separator) so ``Example.COM.`` and ``example.com`` compare equal.

    Raises :class:`ValueError` on input that is a URL with no parseable
    host (``"http://"``) or empty after normalization. URL inputs MUST
    use a scheme — a bare ``//example.com`` is treated as a bare host,
    which is by design: bare-host inputs to this helper come from
    ``brand_url`` fields whose schema already constrains them, so a
    bare-host input is never an attacker-controlled URL.
    """
    if "://" in value:
        parts = urlsplit(value)
        host = parts.hostname
        if not host:
            raise ValueError(f"URL has no host: {value!r}")
        return host.lower()
    stripped = value.strip().rstrip(".").lower()
    if not stripped:
        raise ValueError("host is empty")
    return stripped


def registrable_domain(host_or_url: str) -> str | None:
    """Return the eTLD+1 (registrable domain) for ``host_or_url``.

    Accepts a full URL (``https://ads.brand.example/...``) or a bare
    host (``ads.brand.example``). Returns ``None`` when the input has
    no eTLD+1:

    * IP literals (v4 and v6) — IP addresses are not eTLD+1-bindable.
    * Single-label hosts (``localhost``, ``intranet``).
    * Hosts that are themselves a public suffix (``co.uk``).

    The returned domain is lowercased.

    Callers performing a binding check should treat ``None`` as a
    failure (the agent's host has no registrable domain to bind
    against), NOT as "no opinion".
    """
    host = host_from(host_or_url)
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
