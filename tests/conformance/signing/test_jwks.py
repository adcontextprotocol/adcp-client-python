"""Unit tests for the JWKS resolver and SSRF validation."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
from typing import Any
from unittest.mock import patch

import pytest

from adcp.signing import (
    DEFAULT_ALLOWED_PORTS,
    AsyncCachingJwksResolver,
    CachingJwksResolver,
    SignatureVerificationError,
    SSRFValidationError,
    StaticJwksResolver,
    jwks,
    validate_jwks_uri,
    validate_resolved_ip,
    validate_uri_static,
)

# ---- SSRF validation ----


def _addrinfo(ip: str) -> tuple[int, int, int, str, tuple]:
    """Build a `getaddrinfo` record with the sockaddr shape matching the family.

    AF_INET carries a 2-tuple `(addr, port)`; AF_INET6 a 4-tuple
    `(addr, port, flowinfo, scope_id)`. Parametrised tests mix both families,
    and a v6 address in a v4-shaped record would exercise a resolution the
    stdlib never actually produces.
    """
    if ":" in ip:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))


def test_validate_uri_static_canonicalizes_without_dns() -> None:
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        side_effect=AssertionError("static validation must not resolve DNS"),
    ):
        host = validate_uri_static(
            "https://münchen.example.:8443/callback",
            allowed_ports=frozenset({8443}),
        )

    assert host == "xn--mnchen-3ya.example"


@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/passwd",
        "https:///missing-host",
        "https://example.com:not-a-port/callback",
        "https://example.com:8080/callback",
    ],
)
def test_validate_uri_static_rejects_invalid_authority(uri: str) -> None:
    with pytest.raises(SSRFValidationError):
        validate_uri_static(uri, allowed_ports=DEFAULT_ALLOWED_PORTS)


@pytest.mark.parametrize(
    "resolved_ip",
    [
        "192.88.99.1",
        "192.31.196.1",
        "192.52.193.1",
        "192.175.48.1",
        "2001:20::1",
    ],
)
def test_validate_resolved_ip_applies_extra_special_use_ranges(resolved_ip: str) -> None:
    with pytest.raises(SSRFValidationError, match="reserved range"):
        validate_resolved_ip(ipaddress.ip_address(resolved_ip))


def test_validate_resolved_ip_unwraps_ipv4_mapped_metadata() -> None:
    with pytest.raises(SSRFValidationError, match="metadata"):
        validate_resolved_ip(ipaddress.ip_address("::ffff:169.254.169.254"))


def test_validate_resolved_ip_accepts_public_address() -> None:
    validate_resolved_ip(ipaddress.ip_address("93.184.216.34"))


@pytest.mark.parametrize(
    "host_or_url",
    [
        "http://127.0.0.1/jwks.json",
        "https://10.0.0.1/jwks.json",
        "https://192.168.1.1/jwks.json",
        "https://172.16.0.1/jwks.json",
        "https://169.254.169.254/jwks.json",  # AWS/GCP metadata
        "http://localhost/jwks.json",
        "http://[::1]/jwks.json",
    ],
)
def test_ssrf_rejects_reserved_and_metadata(host_or_url: str) -> None:
    with pytest.raises(SSRFValidationError):
        validate_jwks_uri(host_or_url)


def test_ssrf_rejects_non_http_scheme() -> None:
    with pytest.raises(SSRFValidationError):
        validate_jwks_uri("file:///etc/passwd")


def test_ssrf_allows_public_hostnames() -> None:
    # Mock getaddrinfo to return a public IP so we don't hit the real DNS
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
    ):
        validate_jwks_uri("https://example.com/jwks.json")


def test_ssrf_allow_private_override() -> None:
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("127.0.0.1", 0))],
    ):
        validate_jwks_uri("http://localhost:8080/jwks.json", allow_private=True)


def test_ssrf_metadata_ip_blocked_even_with_allow_private() -> None:
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("169.254.169.254", 0))],
    ):
        with pytest.raises(SSRFValidationError):
            validate_jwks_uri("http://metadata.internal/jwks.json", allow_private=True)


def test_ssrf_rejects_ipv4_mapped_ipv6_metadata() -> None:
    # `::ffff:169.254.169.254` — IPv4-mapped IPv6. On Python 3.10 the direct
    # flag checks on this form are False; we must unwrap to the embedded IPv4
    # before checking, or the block list is silently bypassed.
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(10, 1, 6, "", ("::ffff:169.254.169.254", 0, 0, 0))],
    ):
        with pytest.raises(SSRFValidationError):
            validate_jwks_uri("https://[::ffff:169.254.169.254]/jwks")


def test_ssrf_blocks_oracle_metadata() -> None:
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("192.0.0.192", 0))],
    ):
        with pytest.raises(SSRFValidationError):
            validate_jwks_uri("http://oracle-metadata.example/jwks.json")


@pytest.mark.parametrize(
    ("resolved_ip", "why"),
    [
        ("100.64.0.1", "RFC 6598 CGNAT lower bound"),
        ("100.100.100.1", "CGNAT mid-range — Alibaba metadata's neighbourhood"),
        ("100.127.255.254", "RFC 6598 CGNAT upper bound"),
        ("192.88.99.0", "RFC 7526 6to4 relay anycast lower bound"),
        ("192.88.99.1", "RFC 7526 deprecated 6to4 relay anycast"),
        ("192.88.99.255", "RFC 7526 6to4 relay anycast upper bound"),
        ("192.31.196.1", "RFC 7535 AS112-v4 anycast"),
        ("192.52.193.1", "RFC 7450 AMT anycast"),
        ("192.175.48.1", "RFC 7534 AS112 direct delegation"),
        ("2001:20::1", "RFC 7343 ORCHIDv2 (IPv6)"),
    ],
)
def test_ssrf_blocks_ranges_python_flags_miss(resolved_ip: str, why: str) -> None:
    """Reserved ranges that `ipaddress`'s own flags do not classify.

    Every range here is non-reserved under all six flags on the whole
    supported interpreter matrix (3.10-3.13) — verified empirically, not
    assumed. `is_private` is False across 100.64.0.0/10 because RFC 6598
    designates *shared* address space rather than private space, and it
    remains False on 3.12.9 / 3.13.11, so the entry is load-bearing on every
    supported version rather than redundant on newer ones.

    AdCP 3.1.1 names 100.64.0.0/10 in the deny list a fetcher MUST apply
    ("Webhook URL validation (SSRF)", step 2). The remainder are IANA
    special-use anycast and non-routable identifier space, never a legitimate
    JWKS or webhook destination.
    """
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[_addrinfo(resolved_ip)],
    ):
        with pytest.raises(SSRFValidationError, match="reserved range"):
            validate_jwks_uri("https://buyer-supplied.example/jwks.json")


@pytest.mark.parametrize(
    ("resolved_ip", "why"),
    [
        ("2002:a9fe:a9fe::", "6to4 2002::/16 embedding 169.254.169.254"),
        ("2002:0a00:0001::", "6to4 2002::/16 embedding 10.0.0.1"),
        ("2001::1", "Teredo 2001::/32"),
        ("64:ff9b::a9fe:a9fe", "NAT64 64:ff9b::/96 embedding 169.254.169.254"),
        ("64:ff9b::a00:1", "NAT64 64:ff9b::/96 embedding 10.0.0.1"),
        ("192.0.0.8", "IPv4 NAT64 dummy address"),
    ],
)
def test_ssrf_flag_covered_special_ranges_stay_blocked(resolved_ip: str, why: str) -> None:
    """Special-use ranges the flags already cover, pinned against regression.

    These are deliberately NOT in `_EXTRA_BLOCKED_NETWORKS`: `ipaddress`
    classifies the whole prefix reserved on every supported version, so the
    embedded IPv4 in the tunnel forms needs no decoding — the address is
    rejected before anything is unwrapped.

    That makes the block a dependency on CPython's classification rather than
    on our own list, which is exactly the kind of assumption worth pinning: if
    a future release reclassified any of these, the deny list would need an
    explicit entry and this test is what would say so.
    """
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[_addrinfo(resolved_ip)],
    ):
        with pytest.raises(SSRFValidationError):
            validate_jwks_uri("https://buyer-supplied.example/jwks.json")


@pytest.mark.parametrize(
    ("resolved_ip", "why"),
    [
        ("2606:4700:4700::1111", "public IPv6"),
        ("2001:4860:4860::8888", "public IPv6, different allocation"),
    ],
)
def test_ssrf_ipv6_accepted_against_ipv4_only_extra_networks(resolved_ip: str, why: str) -> None:
    """A public IPv6 resolution must pass, not raise, not crash.

    `_EXTRA_BLOCKED_NETWORKS` holds only IPv4 networks, and the membership
    test runs against a possibly-IPv6 address. CPython's
    `_BaseNetwork.__contains__` returns False on a version mismatch rather
    than raising (only the ordering operators raise), so a v6 address falls
    through to the flag checks. That behaviour is load-bearing here, so pin
    it with a test rather than leaving it as an assumption about CPython.
    """
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(10, 1, 6, "", (resolved_ip, 0, 0, 0))],
    ):
        validate_jwks_uri("https://ipv6-host.example/jwks.json")


# Every range `allow_private` must leave blocked, with the RFC that reserves
# it. `allow_private` admits destinations on a network the operator controls
# (RFC 1918, RFC 4193, loopback — see `_PRIVATE_DESTINATION_NETWORKS`); none of
# these is one, so none may be relaxed by it.
#
# Rows marked `is_private` are the trap this table exists to pin:
# `ipaddress.is_private` reports True for them, so any implementation that
# relaxes the flag wholesale admits documentation, benchmarking and tunnel
# space along with RFC 1918.
_SPECIAL_USE_NOT_PRIVATE = [
    ("100.64.0.1", "RFC 6598 carrier-grade NAT — AdCP 3.1.1 names it MUST-reject"),
    ("100.127.255.254", "RFC 6598 upper bound"),
    ("192.88.99.1", "RFC 7526 deprecated 6to4 relay anycast"),
    ("192.31.196.1", "RFC 7535 AS112-v4 anycast"),
    ("192.52.193.1", "RFC 7450 AMT anycast"),
    ("192.175.48.1", "RFC 7534 AS112 direct delegation"),
    ("2001:20::1", "RFC 7343 ORCHIDv2"),
    ("169.254.1.1", "RFC 3927 link-local — autoconfiguration and metadata space"),
    ("fe80::1", "RFC 4291 link-local"),
    ("224.0.0.1", "RFC 5771 multicast — not a unicast destination"),
    ("ff02::1", "RFC 4291 multicast"),
    ("0.0.0.0", "RFC 1122 unspecified"),
    ("::", "RFC 4291 unspecified"),
    ("192.0.2.1", "RFC 5737 TEST-NET-1 documentation (is_private)"),
    ("198.51.100.1", "RFC 5737 TEST-NET-2 documentation (is_private)"),
    ("203.0.113.1", "RFC 5737 TEST-NET-3 documentation (is_private)"),
    ("2001:db8::1", "RFC 3849 documentation (is_private)"),
    ("198.18.0.1", "RFC 2544 benchmarking (is_private)"),
    ("192.0.0.1", "RFC 6890 IETF protocol assignments (is_private)"),
    ("240.0.0.1", "RFC 1112 reserved 240.0.0.0/4 (is_private)"),
    ("2002::1", "RFC 3056 6to4 (is_private)"),
    ("2001::1", "RFC 4380 Teredo (is_private)"),
    ("64:ff9b::1", "RFC 6052 NAT64 well-known prefix"),
    ("100::1", "RFC 6666 discard-only (is_private)"),
]


@pytest.mark.parametrize(("resolved_ip", "why"), _SPECIAL_USE_NOT_PRIVATE)
def test_ssrf_special_use_ranges_stay_blocked_under_allow_private(
    resolved_ip: str, why: str
) -> None:
    """`allow_private=True` admits private destinations and nothing else.

    The flag's purpose is a destination on a network the operator controls.
    Relaxing the special-use ranges along with it hands a caller who enabled
    one thing a second thing it never asked for — shared carrier space,
    unattributable anycast relays, autoconfiguration space — on the paths
    where the dialled URL is publisher- or buyer-supplied.
    """
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[_addrinfo(resolved_ip)],
    ):
        with pytest.raises(SSRFValidationError, match="reserved range"):
            validate_jwks_uri("https://buyer-supplied.example/jwks.json", allow_private=True)


@pytest.mark.parametrize(("resolved_ip", "why"), _SPECIAL_USE_NOT_PRIVATE)
def test_ssrf_special_use_ranges_admitted_only_by_their_own_flag(
    resolved_ip: str, why: str
) -> None:
    """`allow_special_use=True` is the one way to admit the same set.

    The escape hatch the previous `allow_private` behavior provided, now
    reachable only by asking for it.
    """
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[_addrinfo(resolved_ip)],
    ):
        validate_jwks_uri(
            "https://operator-chosen.example/jwks.json",
            allow_special_use=True,
        )


@pytest.mark.parametrize(
    ("resolved_ip", "why"),
    [
        ("10.0.0.1", "RFC 1918 10/8"),
        ("172.17.0.1", "RFC 1918 172.16/12 — the container-bridge case"),
        ("192.168.1.1", "RFC 1918 192.168/16"),
        ("127.0.0.1", "RFC 1122 loopback — a test's own origin"),
        ("::1", "RFC 4291 loopback"),
        ("fd00::1", "RFC 4193 unique local"),
    ],
)
def test_ssrf_private_destinations_honour_allow_private(resolved_ip: str, why: str) -> None:
    """The ranges `allow_private` exists for keep working.

    Loopback test origins and container-bridge addresses are what the flag is
    documented for; narrowing its scope must not reach them.
    """
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[_addrinfo(resolved_ip)],
    ):
        validate_jwks_uri("https://local-origin.example/jwks.json", allow_private=True)

    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[_addrinfo(resolved_ip)],
    ):
        with pytest.raises(SSRFValidationError, match="reserved range"):
            validate_jwks_uri("https://local-origin.example/jwks.json")


def test_private_destination_networks_are_a_subset_of_the_base_policy() -> None:
    """Neither gate may admit an address the policy did not already block.

    `allow_private=False, allow_special_use=False` has to refuse exactly what
    the single pre-split predicate refused. That holds only while the private
    set is a subset of the base policy's union — if a range were added to
    `_PRIVATE_DESTINATION_NETWORKS` that the union does not cover, the split
    would turn a refusal into an acceptance for the default posture.
    """
    for network in jwks._PRIVATE_DESTINATION_NETWORKS:
        for address in (network.network_address, network.broadcast_address):
            assert jwks._is_blocked_by_base_policy(address), (
                f"{address} is in _PRIVATE_DESTINATION_NETWORKS but the base policy "
                f"does not block it, so the default posture now accepts it"
            )


def test_private_and_special_use_sets_are_disjoint() -> None:
    """One address, one gate. A range in both would be admitted by either flag."""
    for network in jwks._PRIVATE_DESTINATION_NETWORKS:
        for address in (network.network_address, network.broadcast_address):
            assert not any(address in extra for extra in jwks._EXTRA_BLOCKED_NETWORKS), (
                f"{address} is in both _PRIVATE_DESTINATION_NETWORKS and "
                f"_EXTRA_BLOCKED_NETWORKS"
            )


def test_ssrf_alibaba_metadata_blocked_despite_allow_private() -> None:
    """Regression guard for the interaction between the new CGNAT range and
    the metadata list: 100.100.100.200 sits inside 100.64.0.0/10, and the
    metadata check must keep winning so `allow_private` cannot unblock it."""
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("100.100.100.200", 0))],
    ):
        with pytest.raises(SSRFValidationError, match="metadata"):
            validate_jwks_uri("http://alibaba-metadata.example/jwks.json", allow_private=True)


@pytest.mark.parametrize(
    ("resolved_ip", "why"),
    [
        ("169.254.169.254", "AWS/Azure/GCP — inside the link-local special-use range"),
        ("100.100.100.200", "Alibaba — inside the RFC 6598 special-use range"),
        ("192.0.0.192", "Oracle — inside RFC 6890 protocol assignments"),
        ("fd00:ec2::254", "AWS IPv6 — inside RFC 4193 unique-local"),
    ],
)
def test_ssrf_metadata_blocked_under_both_relaxations(resolved_ip: str, why: str) -> None:
    """No combination of flags reaches a cloud metadata endpoint.

    Every entry in `BLOCKED_METADATA_IPS` sits inside a range one of the two
    gates admits, so the unconditional metadata check has to run before either
    of them. `allow_special_use` is a second way to attempt this, so it is
    graded alongside `allow_private` rather than inferred from it.
    """
    for kwargs in (
        {"allow_private": True},
        {"allow_special_use": True},
        {"allow_private": True, "allow_special_use": True},
    ):
        with patch(
            "adcp.signing.jwks.socket.getaddrinfo",
            return_value=[_addrinfo(resolved_ip)],
        ):
            with pytest.raises(SSRFValidationError, match="metadata"):
                validate_jwks_uri("http://metadata-probe.example/jwks.json", **kwargs)


def test_ssrf_caps_resolved_address_scan() -> None:
    # Build 100 records where the first 32 are public and the 33rd is internal.
    # With the cap at 32, the scan stops before reaching the loopback address.
    infos = [(2, 1, 6, "", ("93.184.216.34", 0))] * 32 + [(2, 1, 6, "", ("127.0.0.1", 0))] * 68
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=infos,
    ):
        # Must pass: the validator stops scanning before the internal IP.
        validate_jwks_uri("https://example.com/jwks.json")


# ---- Port allowlist (opt-in operator hardening) ----
# Rationale lives in adcp.signing.jwks.DEFAULT_ALLOWED_PORTS docstring.


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com:9443/jwks.json",  # Tomcat default — legitimate
        "https://example.com:4443/jwks.json",  # Spring Boot default — legitimate
        "https://example.com:8080/jwks.json",  # buyer's path-routed gateway
        "http://example.com:80/jwks.json",  # plain HTTP — scheme check is separate
    ],
)
def test_ssrf_default_imposes_no_port_filter(uri: str) -> None:
    """Without explicit ``allowed_ports``, any port that satisfies the
    scheme check passes. AdCP doesn't restrict ``pushNotificationConfig.url``
    to standard ports — :8443/:9443/:4443 are all legitimate buyer
    deployments."""
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
    ):
        validate_jwks_uri(uri)


@pytest.mark.parametrize(
    "uri,port",
    [
        ("https://example.com:25/jwks.json", 25),  # SMTP
        ("https://example.com:6379/jwks.json", 6379),  # Redis
        ("https://example.com:11211/jwks.json", 11211),  # Memcached
        ("https://example.com:8080/jwks.json", 8080),  # generic HTTP-alt
    ],
)
def test_ssrf_rejects_disallowed_ports_when_hardening(uri: str, port: int) -> None:
    """Operators opt into the hardening posture by passing
    ``DEFAULT_ALLOWED_PORTS`` or a custom set; non-allowlisted ports
    then reject."""
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
    ):
        with pytest.raises(SSRFValidationError, match=f"port {port} not allowed"):
            validate_jwks_uri(uri, allowed_ports=DEFAULT_ALLOWED_PORTS)


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/jwks.json",  # implicit :443
        "https://example.com:443/jwks.json",
        "https://example.com:8443/jwks.json",
    ],
)
def test_ssrf_default_allowlist_passes_canonical_https_ports(uri: str) -> None:
    """``DEFAULT_ALLOWED_PORTS = {443, 8443}`` is the recommended hardening
    set; both canonical-https and HTTPS-alt pass."""
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
    ):
        validate_jwks_uri(uri, allowed_ports=DEFAULT_ALLOWED_PORTS)


def test_ssrf_allowed_ports_custom_set() -> None:
    """Adopters with trusted on-prem deployments can permit non-standard ports."""
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
    ):
        validate_jwks_uri(
            "https://example.com:9000/jwks.json",
            allowed_ports=frozenset({443, 9000}),
        )


def test_ssrf_empty_allowlist_rejects_every_port() -> None:
    """``allowed_ports=frozenset()`` is meaningful: no port satisfies the
    set. Distinct from ``allowed_ports=None`` (no filter at all). Used
    by deployments that want to fail closed unless a port is explicitly
    permitted."""
    with patch(
        "adcp.signing.jwks.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
    ):
        with pytest.raises(SSRFValidationError, match="port 443 not allowed"):
            validate_jwks_uri(
                "https://example.com/jwks.json",
                allowed_ports=frozenset(),
            )


# ---- CachingJwksResolver ----


def _make_jwks(*kids: str) -> dict[str, Any]:
    return {"keys": [{"kid": k, "kty": "OKP", "crv": "Ed25519", "x": "stub"} for k in kids]}


def test_caching_resolver_hits_cache_on_known_kid() -> None:
    calls = 0

    def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _make_jwks("k1", "k2")

    resolver = CachingJwksResolver("https://example.com/jwks.json", fetcher=fetcher)
    assert resolver("k1") is not None
    assert resolver("k1") is not None
    assert calls == 1


def test_caching_resolver_returns_none_for_unknown_kid() -> None:
    fetcher = lambda uri, **kw: _make_jwks("k1")  # noqa: E731
    resolver = CachingJwksResolver("https://example.com/jwks.json", fetcher=fetcher)
    assert resolver("unknown") is None


def test_caching_resolver_refetches_after_cooldown() -> None:
    calls = 0

    def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        # On first call, only k1 exists; on later calls, k2 appears
        return _make_jwks("k1", "k2") if calls > 1 else _make_jwks("k1")

    clock = {"t": 0.0}
    resolver = CachingJwksResolver(
        "https://example.com/jwks.json",
        fetcher=fetcher,
        cooldown_seconds=30.0,
        clock=lambda: clock["t"],
    )
    assert resolver("k1") is not None
    assert resolver("k2") is None  # no refresh because cooldown not elapsed
    assert calls == 1

    clock["t"] = 31.0  # cooldown elapsed
    assert resolver("k2") is not None
    assert calls == 2


def test_caching_resolver_revalidates_known_kid_after_max_age() -> None:
    calls = 0

    def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _make_jwks("k1") if calls == 1 else _make_jwks("replacement")

    clock = {"t": 0.0}
    resolver = CachingJwksResolver(
        "https://example.com/jwks.json",
        fetcher=fetcher,
        max_age_seconds=60.0,
        clock=lambda: clock["t"],
    )
    assert resolver("k1") is not None
    clock["t"] = 61.0
    assert resolver("k1") is None
    assert calls == 2


def test_sync_caching_resolver_single_flights_concurrent_expiry_refresh() -> None:
    calls = 0
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        nonlocal calls
        del uri, allow_private
        calls += 1
        if calls == 1:
            return _make_jwks("k1")
        refresh_started.set()
        assert release_refresh.wait(timeout=5)
        return _make_jwks("replacement")

    clock = {"t": 0.0}
    resolver = CachingJwksResolver(
        "https://example.com/jwks.json",
        fetcher=fetcher,
        max_age_seconds=60.0,
        clock=lambda: clock["t"],
    )
    assert resolver("k1") is not None

    class _ObservedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._guard = threading.Lock()
            self._attempts = 0
            self.second_attempted = threading.Event()

        def __enter__(self) -> None:
            with self._guard:
                self._attempts += 1
                if self._attempts == 2:
                    self.second_attempted.set()
            self._lock.acquire()

        def __exit__(self, *args: object) -> None:
            self._lock.release()

    observed_lock = _ObservedLock()
    resolver._refresh_lock = observed_lock
    clock["t"] = 61.0
    start = threading.Barrier(3)
    results: list[dict[str, Any] | None] = []
    errors: list[Exception] = []

    def resolve_expired() -> None:
        try:
            start.wait()
            results.append(resolver("replacement"))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=resolve_expired) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    assert refresh_started.wait(timeout=5)
    assert observed_lock.second_attempted.wait(timeout=5)
    release_refresh.set()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert all(result is not None for result in results)
    assert calls == 2


async def test_async_caching_resolver_revalidates_known_kid_after_max_age() -> None:
    calls = 0

    async def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _make_jwks("k1") if calls == 1 else _make_jwks("replacement")

    clock = {"t": 0.0}
    resolver = AsyncCachingJwksResolver(
        "https://example.com/jwks.json",
        fetcher=fetcher,
        max_age_seconds=60.0,
        clock=lambda: clock["t"],
    )
    assert await resolver("k1") is not None
    clock["t"] = 61.0
    assert await resolver("k1") is None
    assert calls == 2


async def test_async_caching_resolver_single_flights_concurrent_expiry_refresh() -> None:
    calls = 0
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        nonlocal calls
        del uri, allow_private
        calls += 1
        if calls == 1:
            return _make_jwks("k1")
        refresh_started.set()
        await release_refresh.wait()
        return _make_jwks("replacement")

    clock = {"t": 0.0}
    resolver = AsyncCachingJwksResolver(
        "https://example.com/jwks.json",
        fetcher=fetcher,
        max_age_seconds=60.0,
        clock=lambda: clock["t"],
    )
    assert await resolver("k1") is not None

    clock["t"] = 61.0
    first = asyncio.create_task(resolver("replacement"))
    await refresh_started.wait()
    second = asyncio.create_task(resolver("replacement"))
    await asyncio.sleep(0)

    assert not second.done()
    release_refresh.set()
    assert await asyncio.gather(first, second) == [
        _make_jwks("replacement")["keys"][0],
        _make_jwks("replacement")["keys"][0],
    ]
    assert calls == 2


def test_caching_resolver_wraps_ssrf_as_untrusted() -> None:
    def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        raise SSRFValidationError("blocked")

    resolver = CachingJwksResolver("https://example.com/jwks.json", fetcher=fetcher)
    with pytest.raises(SignatureVerificationError) as exc:
        resolver("k1")
    assert exc.value.code == "request_signature_jwks_untrusted"


def test_caching_resolver_wraps_network_failure_as_unavailable() -> None:
    import httpx

    def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        raise httpx.ConnectError("dns failed")

    resolver = CachingJwksResolver("https://example.com/jwks.json", fetcher=fetcher)
    with pytest.raises(SignatureVerificationError) as exc:
        resolver("k1")
    assert exc.value.code == "request_signature_jwks_unavailable"


def test_caching_resolver_wraps_malformed_key_as_unavailable() -> None:
    def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        return {"keys": [1]}

    resolver = CachingJwksResolver("https://example.com/jwks.json", fetcher=fetcher)
    with pytest.raises(SignatureVerificationError) as exc:
        resolver("k1")
    assert exc.value.code == "request_signature_jwks_unavailable"
    assert isinstance(exc.value.__cause__, ValueError)


async def test_async_caching_resolver_wraps_malformed_key_as_unavailable() -> None:
    async def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        return {"keys": [1]}

    resolver = AsyncCachingJwksResolver("https://example.com/jwks.json", fetcher=fetcher)
    with pytest.raises(SignatureVerificationError) as exc:
        await resolver("k1")
    assert exc.value.code == "request_signature_jwks_unavailable"
    assert isinstance(exc.value.__cause__, ValueError)


# ---- StaticJwksResolver ----


def test_static_resolver() -> None:
    resolver = StaticJwksResolver(_make_jwks("k1", "k2"))
    assert resolver("k1") is not None
    assert resolver("unknown") is None
