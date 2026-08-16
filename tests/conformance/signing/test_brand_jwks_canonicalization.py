"""Proves ``_canonicalize_url`` emits a URL the transport layer accepts.

The brand.json walk canonicalizes every hop URL before it is used for
three things: the outbound fetch, the redirect-loop ``seen`` key, and
the origin gate in ``_default_jwks_uri``. All three break when the
canonicalizer rebuilds the authority from the *de-bracketed* IPv6
literal that ``urlsplit(...).hostname`` returns:

* ``https://[::1]/x`` became ``https://::1/x`` — a string with no
  parseable host, which the IP-pinned transport builder refuses.
* ``https://[2001:db8::1]:8443/x`` became ``https://2001:db8::1:8443/x``
  — re-parsing that raises because the port is no longer separable.
* A brand.json and an ``agent.url`` on the same IPv6 host compared
  unequal because one side was bracketed and the other was not.

These tests pin the round-trip contract: canonicalizer output must be
a well-formed URL that re-parses to the same host and port, and that
the SSRF-validating transport builder accepts.

They also pin the fetch path's exception contract — a transport-side
SSRF refusal must surface as ``BrandJsonResolverError("fetch_failed")``,
not as a raw ``SSRFValidationError`` leaking through the resolver.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from adcp.signing.brand_jwks import (
    BrandJsonResolverError,
    _canonicalize_url,
    _default_jwks_uri,
    _fetch_brand_json,
)
from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport

# ----- IPv6 bracket preservation -----


def test_canonicalize_preserves_ipv6_brackets() -> None:
    assert _canonicalize_url("https://[::1]/x", allow_private=True) == "https://[::1]/x"


def test_canonicalize_ipv6_with_port_stays_unambiguous() -> None:
    result = _canonicalize_url("https://[2001:DB8:0:0::1]:8443/x", allow_private=True)
    assert result == "https://[2001:db8::1]:8443/x"
    reparsed = urlsplit(result)
    assert reparsed.hostname == "2001:db8::1"
    assert reparsed.port == 8443


def test_canonicalize_ipv6_strips_default_port() -> None:
    assert (
        _canonicalize_url("https://[2001:db8::1]:443/x", allow_private=True)
        == "https://[2001:db8::1]/x"
    )


def test_canonicalized_ipv6_url_is_accepted_by_the_transport_builder() -> None:
    url = _canonicalize_url("https://[::1]/x", allow_private=True)
    # Must not raise: the canonicalizer's output is fed straight to this
    # builder at every redirect hop.
    build_async_ip_pinned_transport(url, allow_private=True)


def test_canonicalize_ipv6_same_origin_agent_gets_default_jwks_uri() -> None:
    brand = _canonicalize_url("https://[::1]/.well-known/brand.json", allow_private=True)
    assert _default_jwks_uri("https://[::1]/agent", brand) == "https://[::1]/.well-known/jwks.json"


# ----- host aliasing closed by routing through the shared canonicalizer -----


def test_canonicalize_strips_trailing_root_dot() -> None:
    assert _canonicalize_url("https://Brand.Example./x", allow_private=False) == (
        "https://brand.example/x"
    )


def test_canonicalize_encodes_u_label_to_a_label() -> None:
    assert _canonicalize_url("https://bücher.example/x", allow_private=False) == (
        "https://xn--bcher-kva.example/x"
    )


def test_canonicalize_rejects_host_the_idna_encoder_refuses() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _canonicalize_url("https://foo_bar.example/x", allow_private=False)
    assert exc.value.code == "invalid_url"


# ----- fetch path exception contract -----


async def test_fetch_brand_json_maps_transport_ssrf_refusal_to_resolver_error() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        await _fetch_brand_json(
            start_url="https://localhost/.well-known/brand.json",
            current_etag=None,
            max_redirects=3,
            allow_private=False,
            timeout_seconds=1.0,
        )
    assert exc.value.code == "fetch_failed"


# ---------- the origin gate must compare like with like ----------


@pytest.mark.parametrize(
    ("agent_url", "brand_url"),
    [
        ("https://Brand.Example/agent", "https://brand.example/brand.json"),
        ("https://brand.example:443/agent", "https://brand.example/brand.json"),
        ("https://brand.example./agent", "https://brand.example/brand.json"),
        ("https://user@brand.example/agent", "https://brand.example/brand.json"),
        ("https://bücher.example/agent", "https://bücher.example/brand.json"),
        ("https://[::1]/agent", "https://[::1]/brand.json"),
    ],
    ids=["case", "default-port", "root-dot", "userinfo", "u-label", "ipv6"],
)
def test_same_origin_spelled_differently_still_defaults_the_jwks_uri(
    agent_url: str, brand_url: str
) -> None:
    """The gate compares origins, not spellings.

    ``final_brand_url`` arrives already canonicalized while ``agent.url`` is
    raw as published, so building one side with ``.netloc`` and the other from
    the canonicalizer compares a canonical string to a raw one. Every row here
    is a publisher who spelled the SAME origin on both sides and was told their
    agent lives somewhere else — the confusing diagnostic this gate exists to
    avoid, produced by the gate itself.

    ``userinfo`` is in the list for a sharper reason: ``.netloc`` is the one
    accessor that retains it, so ``https://user@brand.example`` compared
    unequal to ``https://brand.example`` and the trust decision turned on a
    credential that is not part of the origin at all.
    """
    assert _default_jwks_uri(agent_url, brand_url).endswith("/.well-known/jwks.json")


def test_cross_origin_agent_is_still_rejected() -> None:
    """The fence the gate exists for, unmoved by the canonicalization above.

    An attacker-controlled brand.json naming an agent on another origin must
    not make that origin's JWKS authoritative. Canonicalizing both sides closes
    spelling differences; it must not close genuine origin differences.
    """
    with pytest.raises(BrandJsonResolverError) as exc:
        _default_jwks_uri("https://evil.example/agent", "https://brand.example/brand.json")
    assert exc.value.code == "jwks_origin_mismatch"


def test_ipv6_agent_origin_keeps_its_brackets_in_the_defaulted_uri() -> None:
    """The defaulted jwks_uri is fetched, so it must re-parse to the same host."""
    uri = _default_jwks_uri("https://[2001:db8::1]:8443/agent", "https://[2001:db8::1]:8443/b.json")
    assert uri == "https://[2001:db8::1]:8443/.well-known/jwks.json"
    assert urlsplit(uri).hostname == "2001:db8::1"
    assert urlsplit(uri).port == 8443
