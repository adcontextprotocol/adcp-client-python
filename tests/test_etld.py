"""Tests for :mod:`adcp.signing.etld`.

Behavior under test:

* eTLD+1 derivation across single-label public suffixes (``.com``) and
  multi-label public suffixes (``.co.uk``, ``.s3.amazonaws.com``).
* URL inputs and bare-host inputs accepted symmetrically.
* Failure-closed convention: IP literals, single-label hosts, and
  hosts that ARE public suffixes return ``None`` / ``False``.
* Case-insensitive comparison.
"""

from __future__ import annotations

import pytest

from adcp.signing.etld import (
    BrandDomainValidationError,
    host_from,
    is_development_brand_domain,
    registrable_domain,
    same_development_brand_domain,
    same_registrable_domain,
    validate_brand_domain,
)

# ----- host_from -----


def test_host_from_url() -> None:
    assert host_from("https://ads.brand.com/path") == "ads.brand.com"


def test_host_from_url_lowercases() -> None:
    assert host_from("https://ADS.Brand.Com/") == "ads.brand.com"


def test_host_from_bare_host() -> None:
    assert host_from("Brand.Com") == "brand.com"


def test_host_from_strips_trailing_root_dot() -> None:
    assert host_from("brand.com.") == "brand.com"


def test_host_from_url_with_no_host_raises() -> None:
    with pytest.raises(ValueError):
        host_from("http://")


def test_host_from_empty_raises() -> None:
    with pytest.raises(ValueError):
        host_from("")


def test_host_from_url_with_port() -> None:
    assert host_from("https://ads.brand.com:8443/") == "ads.brand.com"


# ----- registrable_domain -----


def test_registrable_domain_simple_com() -> None:
    assert registrable_domain("brand.com") == "brand.com"


def test_registrable_domain_subdomain() -> None:
    assert registrable_domain("ads.brand.com") == "brand.com"


def test_registrable_domain_deep_subdomain() -> None:
    assert registrable_domain("a.b.c.brand.com") == "brand.com"


def test_registrable_domain_multi_label_suffix_co_uk() -> None:
    # ``co.uk`` is a public suffix; eTLD+1 is ``example.co.uk``.
    assert registrable_domain("ads.example.co.uk") == "example.co.uk"


def test_registrable_domain_url_input() -> None:
    assert registrable_domain("https://ads.brand.com/path?q=1") == "brand.com"


def test_registrable_domain_case_normalized() -> None:
    assert registrable_domain("ADS.Brand.Com") == "brand.com"


def test_registrable_domain_ipv4_returns_none() -> None:
    # IP literals are not eTLD+1-bindable per the failure-closed convention.
    assert registrable_domain("192.0.2.1") is None


def test_registrable_domain_ipv6_returns_none() -> None:
    assert registrable_domain("https://[2001:db8::1]/") is None


def test_registrable_domain_single_label_returns_none() -> None:
    # ``localhost`` has no public suffix → fail closed.
    assert registrable_domain("localhost") is None


def test_registrable_domain_bare_public_suffix_returns_none() -> None:
    # ``co.uk`` is itself a public suffix with no domain label preceding it.
    assert registrable_domain("co.uk") is None


# ----- same_registrable_domain -----


def test_same_registrable_domain_subdomain_pair() -> None:
    assert same_registrable_domain("ads.brand.com", "brand.com") is True


def test_same_registrable_domain_different_subdomains() -> None:
    assert same_registrable_domain("ads.brand.com", "creative.brand.com") is True


def test_same_registrable_domain_different_etld1() -> None:
    assert same_registrable_domain("brand.com", "rival.com") is False


def test_same_registrable_domain_mixed_url_and_host() -> None:
    assert same_registrable_domain("https://ads.brand.com/", "brand.com") is True


def test_same_registrable_domain_case_insensitive() -> None:
    assert same_registrable_domain("ADS.Brand.Com", "brand.COM") is True


def test_same_registrable_domain_ip_fails_closed() -> None:
    # An IP literal must NOT bind to anything — even another IP literal.
    assert same_registrable_domain("192.0.2.1", "192.0.2.1") is False


def test_same_registrable_domain_localhost_fails_closed() -> None:
    assert same_registrable_domain("localhost", "localhost") is False


def test_same_registrable_domain_multi_label_suffix_pair() -> None:
    # Same eTLD+1 under a multi-label suffix.
    assert same_registrable_domain("ads.brand.co.uk", "creative.brand.co.uk") is True


def test_same_registrable_domain_multi_label_suffix_cross() -> None:
    # Different eTLD+1 under same multi-label suffix.
    assert same_registrable_domain("brand.co.uk", "rival.co.uk") is False


def test_same_registrable_domain_cross_tld_with_shared_label() -> None:
    # ``brand.com`` and ``brand.org`` share a label but not an eTLD+1.
    assert same_registrable_domain("brand.com", "brand.org") is False


def test_registrable_domain_psl_private_section_in_scope() -> None:
    # Per ADCP #3690, the PSL PRIVATE section must be in scope so
    # platform-shared suffixes (``vercel.app``, ``pages.dev``,
    # ``github.io``) are treated as suffixes. Without this,
    # ``attacker.vercel.app`` and ``victim.vercel.app`` would share an
    # eTLD+1 and the binding check would authorize an attacker's
    # vercel deployment for a victim's vercel-hosted brand.
    assert registrable_domain("attacker.vercel.app") == "attacker.vercel.app"
    assert registrable_domain("victim.vercel.app") == "victim.vercel.app"
    assert same_registrable_domain("attacker.vercel.app", "victim.vercel.app") is False
    assert registrable_domain("brand.github.io") == "brand.github.io"
    assert registrable_domain("brand.pages.dev") == "brand.pages.dev"


def test_registrable_domain_reserved_tld_returns_none() -> None:
    # ``.example``, ``.test``, ``.invalid``, ``.localhost`` are RFC 2606
    # reserved names — NOT in the PSL — so they fail closed. The spec's
    # documentation examples (``brand.example``) do not bind under our
    # helper; that is correct, since reserved names are not delegated.
    assert registrable_domain("brand.example") is None
    assert registrable_domain("foo.test") is None
    assert registrable_domain("svc.invalid") is None


def test_validate_brand_domain_accepts_public_and_private_psl_names() -> None:
    assert validate_brand_domain("Ads.Brand.COM") == "ads.brand.com"
    assert validate_brand_domain("brand.co.uk") == "brand.co.uk"
    assert validate_brand_domain("tenant.github.io") == "tenant.github.io"


@pytest.mark.parametrize(
    "domain",
    ["localhost", "unknown", "co.uk", "brand.unknown", "1.2.3.4", "https://brand.com"],
)
def test_validate_brand_domain_rejects_non_registrable_names(domain: str) -> None:
    with pytest.raises(BrandDomainValidationError):
        validate_brand_domain(domain)


@pytest.mark.parametrize(
    "domain",
    [
        "brand.localhost",
        "brand.test",
        "brand.example",
        "brand.invalid",
        "example.com",
        "example.net",
        "example.org",
    ],
)
def test_validate_brand_domain_requires_explicit_development_option(domain: str) -> None:
    with pytest.raises(BrandDomainValidationError, match="special-use"):
        validate_brand_domain(domain)
    assert validate_brand_domain(domain, allow_development_domains=True) == domain
    assert is_development_brand_domain(domain) is True


def test_validate_brand_domain_never_allows_mdns_local() -> None:
    assert is_development_brand_domain("brand.local") is False
    with pytest.raises(BrandDomainValidationError, match="special-use"):
        validate_brand_domain("brand.local", allow_development_domains=True)


def test_same_development_brand_domain_uses_test_namespace_boundary() -> None:
    assert same_development_brand_domain("ads.brand.example", "brand.example") is True
    assert same_development_brand_domain("ads.brand.example", "other.example") is False
