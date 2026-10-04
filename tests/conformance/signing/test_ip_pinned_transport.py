"""Tests for :mod:`adcp.signing.ip_pinned_transport`.

Three kinds of coverage:

1. **Contract tests** — fail fast if httpcore's private-backend API
   shifts shape between versions. These protect against silent upstream
   breakage of the ``_backends`` subpackage we reach into.
2. **Rebinding simulation** — monkey-patch :func:`socket.getaddrinfo`
   so the first resolution returns a safe IP and a hypothetical
   second resolution would return a dangerous one. The pinned
   transport MUST connect to the first IP, and the second resolution
   MUST never happen.
3. **SSRF integration** — the fetchers defer resolution to
   :func:`resolve_and_validate_host`; reserved-range and
   cloud-metadata IPs still reject at construction.
4. **Both httpx generations** — the ``2``-suffixed transports pin
   through httpcore2 for an ``httpx2`` client, which is what MCP SDK
   v2 runs on. An in-process loopback server proves the pin connects
   and that a wrong-host refusal fires before any socket opens.
"""

from __future__ import annotations

import inspect
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpcore
import httpcore2
import httpx
import httpx2
import pytest
from httpcore._backends.anyio import AnyIOBackend  # type: ignore[attr-defined]
from httpcore._backends.sync import SyncBackend  # type: ignore[attr-defined]
from httpcore2._backends.anyio import AnyIOBackend as AnyIOBackend2  # type: ignore[attr-defined]
from httpcore2._backends.sync import SyncBackend as SyncBackend2  # type: ignore[attr-defined]

from adcp.signing import (
    AsyncIpPinnedTransport,
    AsyncIpPinnedTransport2,
    IpPinnedTransport,
    IpPinnedTransport2,
    SSRFValidationError,
    abuild_ip_pinned_transport,
    build_async_ip_pinned_transport,
    build_async_ip_pinned_transport2,
    build_ip_pinned_transport,
    build_ip_pinned_transport2,
    resolve_and_validate_host,
)

# -- contract tests ---------------------------------------------------


def test_httpcore_sync_backend_connect_tcp_signature_unchanged() -> None:
    """If httpcore changes ``SyncBackend.connect_tcp``, the pinned
    transport silently breaks. This test fails fast on upgrade so we
    notice during CI, not during a real rebinding attempt.
    """
    sig = inspect.signature(SyncBackend.connect_tcp)
    # Required positional: host, port. Then the rest are kwargs with
    # defaults. If any of these vanish, override becomes wrong.
    params = list(sig.parameters)
    assert params[0] == "self"
    assert params[1] == "host"
    assert params[2] == "port"
    assert "timeout" in params
    assert "local_address" in params
    assert "socket_options" in params


def test_httpcore_async_backend_connect_tcp_signature_unchanged() -> None:
    sig = inspect.signature(AnyIOBackend.connect_tcp)
    params = list(sig.parameters)
    assert params[0] == "self"
    assert params[1] == "host"
    assert params[2] == "port"
    assert "timeout" in params
    assert "local_address" in params
    assert "socket_options" in params


def test_httpcore_connection_pool_accepts_network_backend() -> None:
    """``ConnectionPool(network_backend=...)`` is the public extension
    point we rely on."""
    sig = inspect.signature(httpcore.ConnectionPool.__init__)
    assert "network_backend" in sig.parameters
    sig_async = inspect.signature(httpcore.AsyncConnectionPool.__init__)
    assert "network_backend" in sig_async.parameters


def test_httpcore2_backends_match_the_httpcore_signatures() -> None:
    """The ``2``-suffixed transports pin through the same extension
    point in the httpcore2 generation. One signature test covers both,
    because the port is only valid while the two agree."""
    assert inspect.signature(SyncBackend2.connect_tcp) == inspect.signature(SyncBackend.connect_tcp)
    assert inspect.signature(AnyIOBackend2.connect_tcp) == inspect.signature(
        AnyIOBackend.connect_tcp
    )
    assert "network_backend" in inspect.signature(httpcore2.ConnectionPool.__init__).parameters
    assert "network_backend" in inspect.signature(httpcore2.AsyncConnectionPool.__init__).parameters


# -- resolve_and_validate_host ---------------------------------------


def test_resolve_returns_tuple_of_host_ip_port() -> None:
    host, ip, port = resolve_and_validate_host("https://example.com/jwks")
    assert host == "example.com"
    assert port == 443
    # Accepted IP is a string form, not a wrapped ipaddress object.
    assert isinstance(ip, str)
    # example.com resolves publicly; we just check the ip isn't private.
    import ipaddress

    parsed = ipaddress.ip_address(ip)
    assert not parsed.is_private
    assert not parsed.is_loopback


def test_resolve_defaults_http_port_80() -> None:
    # Even though we normally refuse non-https elsewhere, the helper
    # itself is scheme-agnostic for the port default.
    host, _ip, port = resolve_and_validate_host("http://example.com/jwks")
    assert host == "example.com"
    assert port == 80


def test_resolve_rejects_non_http_scheme() -> None:
    # Error wording is generic (not "JWKS URI") since the helper is
    # used by revocation fetchers and custom-transport callers too.
    with pytest.raises(SSRFValidationError, match="SSRF-validated"):
        resolve_and_validate_host("ftp://example.com/jwks")


def test_resolve_normalizes_idn_hostname_to_punycode() -> None:
    """Unicode hostnames get IDNA-encoded to the ASCII form httpx
    passes to httpcore — otherwise the backend's hostname-match fails
    and the pin silently falls through to the parent's unpinned
    connect_tcp, reopening the TOCTOU.
    """

    # Patch getaddrinfo to short-circuit DNS for the IDN test host.
    def fake_getaddrinfo(host, _port, *_args, **_kwargs):
        # Must be called with the ASCII-encoded form.
        assert (
            host == "xn--mnchen-3ya.example"
        ), f"resolve_and_validate_host should IDNA-encode; got {host!r}"
        return [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        host, _ip, _port = resolve_and_validate_host("https://münchen.example/")
    assert host == "xn--mnchen-3ya.example"


def test_resolve_strips_trailing_dot_fqdn() -> None:
    """An FQDN URL form (trailing dot) must compare equal to the
    non-FQDN form so the backend pin fires either way.
    """

    def fake_getaddrinfo(host, _port, *_args, **_kwargs):
        assert host == "example.com", f"trailing dot not stripped; got {host!r}"
        return [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        host, _ip, _port = resolve_and_validate_host("https://example.com./jwks")
    assert host == "example.com"


def test_resolve_rejects_private_result_without_allow_private() -> None:
    # Simulate getaddrinfo returning a private IP — a rebinding
    # attacker's payload.
    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        return [(socket.AF_INET, 0, 0, "", ("10.0.0.1", 0))]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(SSRFValidationError, match="reserved range"):
            resolve_and_validate_host("https://example.com/")


def test_resolve_rejects_cloud_metadata_ip_even_with_allow_private() -> None:
    """Cloud metadata IPs are blocked unconditionally — not even
    ``allow_private=True`` unlocks them."""

    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        return [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(SSRFValidationError, match="metadata"):
            resolve_and_validate_host("https://example.com/", allow_private=True)


# -- rebinding simulation --------------------------------------------


def test_transport_pins_first_resolution_against_rebinding() -> None:
    """Attacker scenario: TTL=0 DNS returns a safe IP first (passes
    validation), then returns a metadata IP on the second resolution.
    The transport MUST ignore the second resolution and connect to
    the first IP.
    """
    call_count = {"n": 0}

    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Safe public IP — passes validation.
            return [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))]
        # Any subsequent lookup would return cloud metadata.
        return [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        transport = build_ip_pinned_transport("https://attacker.example/")

    # One resolution happened during build; nothing else is allowed.
    assert call_count["n"] == 1

    # Inspect the backend the transport installed: the pinned IP must
    # be the first resolution, and the hostname match must be
    # case-insensitive for safety.
    pool = transport._pool
    backend = pool._network_backend  # type: ignore[attr-defined]
    assert backend._resolved_ip == "8.8.8.8"
    assert backend._hostname == "attacker.example"


def test_async_transport_pins_first_resolution_against_rebinding() -> None:
    call_count = {"n": 0}

    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        call_count["n"] += 1
        return [(socket.AF_INET, 0, 0, "", ("1.1.1.1", 0))]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        transport = build_async_ip_pinned_transport("https://attacker.example/")

    assert call_count["n"] == 1
    pool = transport._pool
    backend = pool._network_backend  # type: ignore[attr-defined]
    assert backend._resolved_ip == "1.1.1.1"
    assert backend._hostname == "attacker.example"


def test_abuild_alias_emits_deprecation_warning() -> None:
    """Legacy alias still works but warns. Remove after downstream
    migration lands."""

    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        return [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.warns(DeprecationWarning, match="build_async_ip_pinned_transport"):
            transport = abuild_ip_pinned_transport("https://example.com/")
    assert isinstance(transport, AsyncIpPinnedTransport)


def test_backend_connect_tcp_swaps_hostname_for_pinned_ip() -> None:
    """Directly test the backend's override — ``connect_tcp`` with the
    pinned hostname calls the parent with the resolved IP instead.
    """
    from adcp.signing.ip_pinned_transport import _IpPinnedSyncBackend

    backend = _IpPinnedSyncBackend(hostname="attacker.example", resolved_ip="198.51.100.30")

    captured = {}

    def _fake_parent_connect(self, *, host, port, timeout, local_address, socket_options):
        captured["host"] = host
        captured["port"] = port
        return object()  # stand-in for a NetworkStream

    with patch.object(SyncBackend, "connect_tcp", _fake_parent_connect):
        backend.connect_tcp(host="attacker.example", port=443)

    assert captured["host"] == "198.51.100.30"
    assert captured["port"] == 443


def test_backend_connect_tcp_refuses_wrong_host() -> None:
    """Reuse of a transport for a DIFFERENT host MUST raise.

    Falling through to the parent's unpinned ``connect_tcp`` would
    silently re-open the DNS-rebinding TOCTOU — exactly what the
    pin exists to close. Agents observed caching one transport in
    a dict keyed by base URL and reusing it, which would hit this
    path in production.
    """
    from adcp.signing.ip_pinned_transport import _IpPinnedSyncBackend

    backend = _IpPinnedSyncBackend(hostname="attacker.example", resolved_ip="198.51.100.40")
    with pytest.raises(RuntimeError, match="pinned to 'attacker.example'"):
        backend.connect_tcp(host="other.example", port=443)


def test_backend_accepts_trailing_dot_fqdn_form() -> None:
    """A caller pinning ``host.`` and connecting to ``host`` (or vice
    versa) must still fire the pin — trailing dots are stripped on
    both sides.
    """
    from adcp.signing.ip_pinned_transport import _IpPinnedSyncBackend

    backend = _IpPinnedSyncBackend(hostname="attacker.example.", resolved_ip="198.51.100.45")
    captured = {}

    def _fake_parent_connect(self, *, host, port, timeout, local_address, socket_options):
        captured["host"] = host
        return object()

    with patch.object(SyncBackend, "connect_tcp", _fake_parent_connect):
        backend.connect_tcp(host="attacker.example", port=443)
    assert captured["host"] == "198.51.100.45"


def test_backend_accepts_idn_punycode_form() -> None:
    """httpx IDNA-encodes Unicode hostnames before calling httpcore.
    The backend stored the punycode form, so the comparison against
    a punycode input must succeed and the pin must fire.
    """
    from adcp.signing.ip_pinned_transport import _IpPinnedSyncBackend

    # Pin the Unicode form; _normalize_pin_host encodes it to punycode.
    backend = _IpPinnedSyncBackend(hostname="münchen.example", resolved_ip="198.51.100.55")
    captured = {}

    def _fake_parent_connect(self, *, host, port, timeout, local_address, socket_options):
        captured["host"] = host
        return object()

    with patch.object(SyncBackend, "connect_tcp", _fake_parent_connect):
        # httpx passes the punycode form.
        backend.connect_tcp(host="xn--mnchen-3ya.example", port=443)
    assert captured["host"] == "198.51.100.55"


def test_backend_hostname_match_is_case_insensitive() -> None:
    from adcp.signing.ip_pinned_transport import _IpPinnedSyncBackend

    backend = _IpPinnedSyncBackend(hostname="Attacker.Example", resolved_ip="198.51.100.50")
    captured = {}

    def _fake_parent_connect(self, *, host, port, timeout, local_address, socket_options):
        captured["host"] = host
        return object()

    with patch.object(SyncBackend, "connect_tcp", _fake_parent_connect):
        backend.connect_tcp(host="ATTACKER.example", port=443)

    assert captured["host"] == "198.51.100.50"


# -- real-network smoke (optional, skipped if no internet) ------------


def _internet_ok() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _internet_ok(), reason="no outbound internet")
def test_real_tls_handshake_still_validates_hostname() -> None:
    """End-to-end sanity: with the pinned transport, TLS cert
    validation still runs against the hostname (not the IP). A
    successful GET against a public HTTPS endpoint proves the TLS
    SNI + cert validation paths are intact.
    """
    import httpx

    transport = build_ip_pinned_transport("https://example.com/")
    # Generous timeout — this test is inherently network-dependent and
    # real TLS handshakes occasionally slow-run on constrained CI
    # machines. The intent is "handshake didn't reject", not speed.
    with httpx.Client(transport=transport, timeout=60.0) as client:
        response = client.get("https://example.com/")
    assert response.status_code == 200


@pytest.mark.skipif(not _internet_ok(), reason="no outbound internet")
def test_transport_type_is_httpx_httptransport() -> None:
    """The returned transport IS an httpx.HTTPTransport instance so
    callers can use it with httpx.Client without type-gymnastics."""
    import httpx

    transport = build_ip_pinned_transport("https://example.com/")
    assert isinstance(transport, httpx.HTTPTransport)
    assert isinstance(transport, IpPinnedTransport)

    atransport = build_async_ip_pinned_transport("https://example.com/")
    assert isinstance(atransport, httpx.AsyncHTTPTransport)
    assert isinstance(atransport, AsyncIpPinnedTransport)


# -- the httpx2 generation --------------------------------------------
#
# ``mcp>=2.0`` runs on httpx2, so every install carries both httpx
# generations and an MCP ``httpx_client_factory`` has to return an
# httpx2 client. These tests exercise the pin through a real httpx2
# client against an in-process loopback server: no name resolution and
# no traffic off the machine, so the subject is the httpx binding and
# nothing else.


@contextmanager
def _loopback_server():
    """Serve 200 on 127.0.0.1 and record every path actually requested.

    The recorded list is the proof a refusal fired BEFORE the socket
    opened: an empty list means no request was issued.
    """
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            received.append(self.path)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            """Keep the test output clean."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_transport2_types_are_httpx2_transports() -> None:
    """The pinned httpx2 transports ARE httpx2 transports, and are not
    accepted by the httpx generation. The two packages are unrelated, so
    a transport that satisfied both would be the surprise."""
    transport = IpPinnedTransport2(hostname="localhost", resolved_ip="127.0.0.1")
    assert isinstance(transport, httpx2.BaseTransport)
    assert isinstance(transport, httpx2.HTTPTransport)
    assert not isinstance(transport, httpx.BaseTransport)

    atransport = AsyncIpPinnedTransport2(hostname="localhost", resolved_ip="127.0.0.1")
    assert isinstance(atransport, httpx2.AsyncBaseTransport)
    assert isinstance(atransport, httpx2.AsyncHTTPTransport)
    assert not isinstance(atransport, httpx.AsyncBaseTransport)


def test_transport2_serves_a_request_from_an_httpx2_client() -> None:
    with _loopback_server() as (port, received):
        transport = IpPinnedTransport2(hostname="localhost", resolved_ip="127.0.0.1")
        with httpx2.Client(transport=transport, timeout=10.0) as client:
            response = client.get(f"http://localhost:{port}/pinned")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert received == ["/pinned"]


async def test_async_transport2_serves_a_request_from_an_httpx2_async_client() -> None:
    with _loopback_server() as (port, received):
        transport = AsyncIpPinnedTransport2(hostname="localhost", resolved_ip="127.0.0.1")
        async with httpx2.AsyncClient(transport=transport, timeout=10.0) as client:
            response = await client.get(f"http://localhost:{port}/pinned")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert received == ["/pinned"]


async def test_async_transport2_refuses_a_second_host_before_connecting() -> None:
    """The fail-closed wrong-host refusal holds in the httpx2
    generation, and it holds at connect time: the server records no
    request."""
    with _loopback_server() as (port, received):
        transport = AsyncIpPinnedTransport2(hostname="pinned.example", resolved_ip="127.0.0.1")
        async with httpx2.AsyncClient(transport=transport, timeout=10.0) as client:
            with pytest.raises(RuntimeError, match="pinned to 'pinned.example'"):
                await client.get(f"http://localhost:{port}/other")

    assert received == []


@pytest.mark.parametrize("build", [build_ip_pinned_transport2, build_async_ip_pinned_transport2])
def test_builders2_pin_the_first_resolution_against_rebinding(build) -> None:
    """Same single-resolution contract as the httpx builders: one
    lookup at build time, and the pin holds the IP it returned. A
    second lookup would return cloud metadata, so a build that resolved
    twice fails here."""
    call_count = {"n": 0}

    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))]
        return [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        transport = build("https://attacker.example/")

    assert call_count["n"] == 1
    backend = transport._pool._network_backend  # type: ignore[attr-defined]
    assert backend._resolved_ip == "8.8.8.8"
    assert backend._hostname == "attacker.example"


def test_builders2_refuse_a_blocked_address() -> None:
    """The address policy is the shared one — the generation changes the
    transport, never the SSRF decision."""

    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        return [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(SSRFValidationError):
            build_ip_pinned_transport2("https://metadata.example/", allow_private=True)
        with pytest.raises(SSRFValidationError):
            build_async_ip_pinned_transport2("https://metadata.example/", allow_private=True)


# -- one destination policy, both generations ------------------------
#
# The four builders share ``_resolve_pin``, so the httpx and httpx2
# generations take the same keywords and make the same address decision.
# These tests run every case through all four builders: a builder that
# drops a keyword, or forwards it to the wrong gate, fails per builder.

_BUILDERS = pytest.mark.parametrize(
    "build",
    [
        build_ip_pinned_transport,
        build_async_ip_pinned_transport,
        build_ip_pinned_transport2,
        build_async_ip_pinned_transport2,
    ],
    ids=["httpx-sync", "httpx-async", "httpx2-sync", "httpx2-async"],
)

_TRANSPORT_TYPES = {
    build_ip_pinned_transport: IpPinnedTransport,
    build_async_ip_pinned_transport: AsyncIpPinnedTransport,
    build_ip_pinned_transport2: IpPinnedTransport2,
    build_async_ip_pinned_transport2: AsyncIpPinnedTransport2,
}

# Representative addresses per destination class. The full range tables
# are graded in ``test_jwks.py``; here the subject is that each builder
# reaches the right gate.
_PRIVATE = ["10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1", "fd12:3456::1", "::1"]
_SPECIAL_USE = [
    "100.64.0.1",  # RFC 6598 carrier-grade NAT
    "169.254.1.1",  # link-local
    "192.0.2.1",  # RFC 5737 documentation
    "198.18.0.1",  # RFC 2544 benchmarking
    "224.0.0.1",  # multicast
    "0.0.0.0",  # unspecified
    "fe80::1",  # IPv6 link-local
    "2001:db8::1",  # RFC 3849 documentation
]
_ALWAYS_REFUSED = ["169.254.169.254", "100.100.100.200", "192.0.0.192", "fd00:ec2::254"]
_PUBLIC = ["8.8.8.8", "2606:4700:4700::1111"]

_FLAGS = [
    pytest.param(False, False, id="default"),
    pytest.param(True, False, id="allow_private"),
    pytest.param(False, True, id="allow_special_use"),
    pytest.param(True, True, id="both"),
]


def _resolving_to(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        return [(family, 0, 0, "", (ip, 0))]

    return patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo)


def _expected_admitted(ip: str, *, allow_private: bool, allow_special_use: bool) -> bool:
    if ip in _ALWAYS_REFUSED:
        return False
    if ip in _PRIVATE:
        return allow_private
    if ip in _SPECIAL_USE:
        return allow_special_use
    assert ip in _PUBLIC
    return True


def test_builders_share_one_keyword_surface() -> None:
    """The httpx2 builders take exactly the httpx builders' options."""
    expected = inspect.signature(build_ip_pinned_transport).parameters
    for build in (
        build_async_ip_pinned_transport,
        build_ip_pinned_transport2,
        build_async_ip_pinned_transport2,
    ):
        assert inspect.signature(build).parameters == expected, build.__name__
    assert list(expected) == [
        "uri",
        "allow_private",
        "allow_special_use",
        "allowed_ports",
        "verify",
    ]


@_BUILDERS
@pytest.mark.parametrize(("allow_private", "allow_special_use"), _FLAGS)
@pytest.mark.parametrize("ip", _PRIVATE + _SPECIAL_USE + _ALWAYS_REFUSED + _PUBLIC)
def test_builders_apply_the_destination_policy(
    build, ip: str, allow_private: bool, allow_special_use: bool
) -> None:
    """Private destinations need ``allow_private``, special-use ranges need
    ``allow_special_use``, cloud metadata is refused under every
    combination, and public addresses need neither."""
    admitted = _expected_admitted(
        ip, allow_private=allow_private, allow_special_use=allow_special_use
    )
    with _resolving_to(ip):
        if not admitted:
            with pytest.raises(SSRFValidationError):
                build(
                    "https://dest.example/",
                    allow_private=allow_private,
                    allow_special_use=allow_special_use,
                )
            return
        transport = build(
            "https://dest.example/",
            allow_private=allow_private,
            allow_special_use=allow_special_use,
        )

    assert isinstance(transport, _TRANSPORT_TYPES[build])
    backend = transport._pool._network_backend  # type: ignore[attr-defined]
    assert backend._resolved_ip == ip
    assert backend._hostname == "dest.example"


@_BUILDERS
def test_builders_refuse_special_use_under_allow_private_with_the_gate_named(build) -> None:
    """The refusal names the gate that would admit the address."""
    with _resolving_to("100.64.0.1"):
        with pytest.raises(SSRFValidationError, match="allow_private does not admit it"):
            build("https://dest.example/", allow_private=True)


@_BUILDERS
@pytest.mark.parametrize("refused", ["169.254.169.254", "100.64.0.1"])
def test_builders_refuse_a_host_with_any_refused_answer(build, refused: str) -> None:
    """One refused answer refuses the host, even when another answer is
    admitted: the builder does not skip ahead and pin the admitted one."""

    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        return [
            (socket.AF_INET, 0, 0, "", ("10.0.0.7", 0)),
            (socket.AF_INET, 0, 0, "", (refused, 0)),
        ]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(SSRFValidationError):
            build("https://dest.example/", allow_private=True)


@_BUILDERS
def test_builders_resolve_once_and_pin_the_first_admitted_answer(build) -> None:
    calls = {"n": 0}

    def fake_getaddrinfo(_host, _port, *_args, **_kwargs):
        calls["n"] += 1
        return [
            (socket.AF_INET, 0, 0, "", ("10.0.0.7", 0)),
            (socket.AF_INET, 0, 0, "", ("10.0.0.8", 0)),
        ]

    with patch("adcp.signing.jwks.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        transport = build("https://dest.example/", allow_private=True)

    assert calls["n"] == 1
    assert transport._pool._network_backend._resolved_ip == "10.0.0.7"  # type: ignore[attr-defined]


@_BUILDERS
def test_builders_enforce_the_port_allowlist(build) -> None:
    with _resolving_to("8.8.8.8"):
        with pytest.raises(SSRFValidationError):
            build("https://dest.example:8080/", allowed_ports=frozenset({443}))
        build("https://dest.example/", allowed_ports=frozenset({443}))


# -- client behaviour over a real loopback socket, both generations --


@contextmanager
def _redirecting_server(location: str):
    """Answer every GET with a 302 to ``location`` and record the paths."""
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            received.append(self.path)
            self.send_response(302)
            self.send_header("Location", location.format(port=self.server.server_port))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            """Keep the test output clean."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


_GENERATIONS = pytest.mark.parametrize(
    ("transport_cls", "client_cls", "is_async"),
    [
        (IpPinnedTransport, httpx.Client, False),
        (AsyncIpPinnedTransport, httpx.AsyncClient, True),
        (IpPinnedTransport2, httpx2.Client, False),
        (AsyncIpPinnedTransport2, httpx2.AsyncClient, True),
    ],
    ids=["httpx-sync", "httpx-async", "httpx2-sync", "httpx2-async"],
)


async def _get(client_cls, is_async: bool, url: str, **client_kwargs):
    if is_async:
        async with client_cls(**client_kwargs) as client:
            return await client.get(url)
    with client_cls(**client_kwargs) as client:
        return client.get(url)


@_GENERATIONS
async def test_pinned_client_connects_to_the_pinned_ip_not_the_name(
    transport_cls, client_cls, is_async
) -> None:
    """The URL's hostname never resolves: ``pinned.invalid`` cannot, so a
    served request proves the connect went to the pinned IP."""
    with _loopback_server() as (port, received):
        transport = transport_cls(hostname="pinned.invalid", resolved_ip="127.0.0.1")
        response = await _get(
            client_cls,
            is_async,
            f"http://pinned.invalid:{port}/pinned",
            transport=transport,
            timeout=10.0,
            trust_env=False,
        )
    assert response.status_code == 200
    assert received == ["/pinned"]


@_GENERATIONS
async def test_pinned_client_refuses_a_second_host_before_connecting(
    transport_cls, client_cls, is_async
) -> None:
    with _loopback_server() as (port, received):
        transport = transport_cls(hostname="pinned.example", resolved_ip="127.0.0.1")
        with pytest.raises(RuntimeError, match="pinned to 'pinned.example'"):
            await _get(
                client_cls,
                is_async,
                f"http://localhost:{port}/other",
                transport=transport,
                timeout=10.0,
                trust_env=False,
            )
    assert received == []


@_GENERATIONS
async def test_pinned_client_without_redirects_returns_the_redirect(
    transport_cls, client_cls, is_async
) -> None:
    """``follow_redirects=False`` hands the 3xx back; nothing dials the
    Location."""
    with _redirecting_server("http://elsewhere.invalid:{port}/next") as (port, received):
        transport = transport_cls(hostname="pinned.invalid", resolved_ip="127.0.0.1")
        response = await _get(
            client_cls,
            is_async,
            f"http://pinned.invalid:{port}/start",
            transport=transport,
            timeout=10.0,
            trust_env=False,
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert received == ["/start"]


@_GENERATIONS
async def test_pinned_client_refuses_a_cross_host_redirect(
    transport_cls, client_cls, is_async
) -> None:
    """A client that does follow redirects still cannot leave the pin: the
    hop to another host is refused before its connect."""
    with _redirecting_server("http://elsewhere.invalid:{port}/next") as (port, received):
        transport = transport_cls(hostname="pinned.invalid", resolved_ip="127.0.0.1")
        with pytest.raises(RuntimeError, match="pinned to 'pinned.invalid'"):
            await _get(
                client_cls,
                is_async,
                f"http://pinned.invalid:{port}/start",
                transport=transport,
                timeout=10.0,
                trust_env=False,
                follow_redirects=True,
            )
    assert received == ["/start"]


@_GENERATIONS
async def test_pinned_client_with_trust_env_false_ignores_environment_proxies(
    transport_cls, client_cls, is_async, monkeypatch
) -> None:
    """``trust_env=False`` keeps ``HTTP_PROXY`` from re-routing the request:
    it reaches the pinned origin and the proxy sees nothing."""
    with _loopback_server() as (proxy_port, proxied), _loopback_server() as (port, received):
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy_port}")
        transport = transport_cls(hostname="pinned.invalid", resolved_ip="127.0.0.1")
        response = await _get(
            client_cls,
            is_async,
            f"http://pinned.invalid:{port}/direct",
            transport=transport,
            timeout=10.0,
            trust_env=False,
        )
    assert response.status_code == 200
    assert received == ["/direct"]
    assert proxied == []


@_GENERATIONS
async def test_environment_proxy_does_not_displace_an_explicit_pinned_transport(
    transport_cls, client_cls, is_async, monkeypatch
) -> None:
    """Both generations skip environment proxies for a client built with an
    explicit ``transport=``, even under ``trust_env=True``, so ``HTTP_PROXY``
    cannot route around the pin. The package still sets ``trust_env=False``
    on every pinned client; this pins the client behaviour that backs it.
    A client given ``proxy=`` or ``mounts=`` explicitly is outside the pin.
    """
    with _loopback_server() as (proxy_port, proxied), _loopback_server() as (port, received):
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy_port}")
        transport = transport_cls(hostname="pinned.invalid", resolved_ip="127.0.0.1")
        response = await _get(
            client_cls,
            is_async,
            f"http://pinned.invalid:{port}/direct",
            transport=transport,
            timeout=10.0,
            trust_env=True,
        )
        # Control: without an explicit transport the same client does use
        # the environment proxy, so the assertion above is not vacuous.
        control = await _get(
            client_cls, is_async, f"http://pinned.invalid:{port}/control", timeout=10.0
        )
    assert response.status_code == 200
    assert control.status_code == 200
    assert received == ["/direct"]
    assert proxied == [f"http://pinned.invalid:{port}/control"]
