"""Deterministic ads.txt redirect, deadline and SSRF acceptance fixtures."""

import asyncio
import gzip
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from adcp import AdagentsNotFoundError, fetch_adagents, validate_adagents_domain
from adcp.adagents import (
    MAX_ADS_TXT_BYTES,
    MAX_ADS_TXT_REDIRECT_HOPS,
    AdagentsTransportFactory,
    _fetch_ads_txt_managerdomains,
)

INITIAL = "https://publisher.com/ads.txt"
MANIFEST = {
    "authorized_agents": [
        {
            "url": "https://sales.agent.com",
            "authorized_for": "Display",
            "authorization_type": "property_ids",
            "property_ids": ["site"],
        }
    ]
}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))
        ],
    )


def factory_for(
    handler: Callable[[httpx.Request], httpx.Response],
    seen: list[str],
    budgets: list[float] | None = None,
) -> AdagentsTransportFactory:
    @asynccontextmanager
    async def factory(url: str, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        seen.append(url)
        if budgets is not None:
            budgets.append(timeout)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout, follow_redirects=True
        ) as client:
            yield client
        assert client.is_closed

    return factory


async def walk(routes: dict[str, Any], start: str = "publisher.com") -> tuple[list[str], list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        route = routes[str(request.url)]
        if isinstance(route, str):
            return httpx.Response(302, headers={"location": route})
        return route

    result = await _fetch_ads_txt_managerdomains(
        start,
        # All transport is mocked; this only needs to outlast event-loop scheduling
        # jitter under pytest-xdist. These tests check redirect policy, not timing.
        999.0,
        "test-agent",
        None,
        transport_factory=factory_for(handler, seen),
    )
    return result, seen


@pytest.mark.parametrize("first_status", [301, 302, 303, 307, 308])
async def test_apex_www_cdn_then_manager_manifest(first_status: int) -> None:
    requests: list[str] = []
    created: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if url == "https://publisher.com/.well-known/adagents.json":
            return httpx.Response(404)
        if url == INITIAL:
            return httpx.Response(first_status, headers={"location": "//www.publisher.com/ads.txt"})
        if url == "https://www.publisher.com/ads.txt":
            return httpx.Response(302, headers={"location": "https://cdn.manager.net/site/ads.txt"})
        if url == "https://cdn.manager.net/site/ads.txt":
            return httpx.Response(200, text="MANAGERDOMAIN=manager.net\n")
        assert url == "https://manager.net/.well-known/adagents.json"
        return httpx.Response(200, json=MANIFEST)

    factory = factory_for(handler, created)
    assert await fetch_adagents("publisher.com", transport_factory=factory) == MANIFEST
    assert created == requests
    assert len(created) == 5
    validated = await validate_adagents_domain("publisher.com", transport_factory=factory)
    assert validated.valid and validated.discovery_method == "ads_txt_managerdomain"
    assert validated.manager_domain == "manager.net"


@pytest.mark.parametrize(
    "publisher,target",
    [
        ("publisher.co.uk", "www.publisher.co.uk"),
        ("publisher.github.io", "www.publisher.github.io"),
    ],
)
async def test_psl_allows_same_root_before_terminal_off_root(publisher: str, target: str) -> None:
    initial = f"https://{publisher}/ads.txt"
    same_root = f"https://{target}/other.txt"
    result, seen = await walk(
        {
            initial: same_root,
            same_root: "https://cdn.net/ads.txt",
            "https://cdn.net/ads.txt": httpx.Response(200, text="managerdomain=m.com"),
        },
        start=publisher,
    )
    assert result == ["m.com"] and len(seen) == 3


@pytest.mark.parametrize(
    "publisher,neighbor",
    [
        ("publisher.co.uk", "attacker.co.uk"),
        ("publisher.github.io", "attacker.github.io"),
    ],
)
async def test_psl_neighbor_uses_the_one_terminal_off_root_hop(
    publisher: str, neighbor: str
) -> None:
    initial = f"https://{publisher}/ads.txt"
    off_root = f"https://{neighbor}/ads.txt"
    result, seen = await walk(
        {initial: off_root, off_root: "https://cdn.net/ads.txt"}, start=publisher
    )
    assert result == [] and seen == [initial, off_root]


@pytest.mark.parametrize(
    "location",
    [
        "https://cdn.net/other.txt",
        "https://www.cdn.net/ads.txt",
        "https://second.net/ads.txt",
        INITIAL,
        "/relative.txt",
    ],
)
async def test_off_root_destination_must_not_redirect(location: str) -> None:
    result, seen = await walk(
        {INITIAL: "https://cdn.net/ads.txt", "https://cdn.net/ads.txt": location}
    )
    assert result == [] and seen == [INITIAL, "https://cdn.net/ads.txt"]


async def test_relative_locations_and_bounded_same_root_chain() -> None:
    routes: dict[str, Any] = {
        INITIAL: "/a/next.txt",
        "https://publisher.com/a/next.txt": "../final.txt",
        "https://publisher.com/final.txt": httpx.Response(200, text="managerdomain=m.com"),
    }
    result, seen = await walk(routes)
    assert result == ["m.com"] and len(seen) == 3
    routes = {INITIAL: "/0"}
    for index in range(MAX_ADS_TXT_REDIRECT_HOPS - 1):
        routes[f"https://publisher.com/{index}"] = f"/{index + 1}"
    terminal = f"https://publisher.com/{MAX_ADS_TXT_REDIRECT_HOPS - 1}"
    routes[terminal] = httpx.Response(200, text="managerdomain=m.com")
    result, seen = await walk(routes)
    assert result == ["m.com"] and len(seen) == MAX_ADS_TXT_REDIRECT_HOPS + 1
    routes[terminal] = "/one-too-many"
    result, seen = await walk(routes)
    assert result == [] and len(seen) == MAX_ADS_TXT_REDIRECT_HOPS + 1


@pytest.mark.parametrize(
    "location",
    [INITIAL, INITIAL + "#fragment", "https://PUBLISHER.com:443/ads.txt", "/a/../ads.txt"],
)
async def test_repeated_canonical_urls_are_detected_before_a_second_request(location: str) -> None:
    result, seen = await walk({INITIAL: location})
    assert result == [] and seen == [INITIAL]


@pytest.mark.parametrize(
    "location",
    [
        "",
        "http://www.publisher.com/ads.txt",
        "https://127.0.0.1/ads.txt",
        "https://169.254.169.254/ads.txt",
        "https://[::1]/ads.txt",
        "https://user@cdn.net/ads.txt",
        "https://cdn.net:bad/ads.txt",
        "https://[broken",
        "https://cdn.net/\\bad",
        "https://cdn.net/\nignored",
    ],
)
async def test_unsafe_locations_fail_before_connect(location: str) -> None:
    result, seen = await walk({INITIAL: location})
    assert result == [] and seen == [INITIAL]


@pytest.mark.parametrize("status", [300, 304, 305, 401, 403, 404, 429, 500, 503])
async def test_unsuccessful_terminal_status_does_not_parse_directives(status: int) -> None:
    result, seen = await walk({INITIAL: httpx.Response(status, text="managerdomain=m.com")})
    assert result == [] and seen == [INITIAL]


@pytest.mark.parametrize("status", [200, 201, 204])
async def test_success_terminal_status_parses_directives(status: int) -> None:
    result, seen = await walk({INITIAL: httpx.Response(status, text="managerdomain=m.com")})
    assert result == ["m.com"] and seen == [INITIAL]


class CountedBody(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("content_length", [None, str(MAX_ADS_TXT_BYTES + 1), "invalid"])
async def test_terminal_body_is_capped_before_buffering_all_chunks(
    content_length: str | None,
) -> None:
    stream = CountedBody([b" " * MAX_ADS_TXT_BYTES, b"x", b"managerdomain=ignored.com"])
    headers = {"content-length": content_length} if content_length else {}
    result, seen = await walk({INITIAL: httpx.Response(200, headers=headers, stream=stream)})
    assert result == [] and seen == [INITIAL] and stream.closed
    assert stream.read == (0 if content_length and content_length.isdigit() else 2)


@pytest.mark.parametrize("status", [302, 503])
async def test_redirect_and_error_bodies_are_never_read_or_size_gated(status: int) -> None:
    stream = CountedBody([b"x" * (MAX_ADS_TXT_BYTES + 1)])
    routes = {
        INITIAL: httpx.Response(
            status,
            headers={"location": "/final.txt", "content-length": str(MAX_ADS_TXT_BYTES + 1)},
            stream=stream,
        ),
        "https://publisher.com/final.txt": httpx.Response(200, text="managerdomain=m.com"),
    }
    result, _ = await walk(routes)
    assert result == (["m.com"] if status == 302 else [])
    assert stream.read == 0 and stream.closed


async def test_missing_location_and_total_timeout_preserve_original_404() -> None:
    created: list[str] = []
    factory = factory_for(
        lambda r: httpx.Response(404) if r.url.path != "/ads.txt" else httpx.Response(302), created
    )
    with pytest.raises(AdagentsNotFoundError, match="publisher.com"):
        await fetch_adagents("publisher.com", transport_factory=factory)
    assert len(created) == 2
    canceled = asyncio.Event()

    @asynccontextmanager
    async def hanging_factory(url: str, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        try:
            await asyncio.Event().wait()
            async with httpx.AsyncClient() as client:
                yield client
        finally:
            canceled.set()

    assert (
        await _fetch_ads_txt_managerdomains(
            "publisher.com", 0.02, "test", None, transport_factory=hanging_factory
        )
        == []
    )
    assert canceled.is_set()


async def test_custom_client_is_only_used_for_initial_url_and_never_auto_follows() -> None:
    caller_requests: list[str] = []
    owned: list[str] = []

    def first(request: httpx.Request) -> httpx.Response:
        caller_requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://www.publisher.com/ads.txt"})

    def target(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, text="managerdomain=m.com")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(first),
        follow_redirects=True,
        headers={"Authorization": "Bearer secret"},
        cookies={"session": "secret"},
    ) as client:
        assert await _fetch_ads_txt_managerdomains(
            "publisher.com", 1, "test", client, transport_factory=factory_for(target, owned)
        ) == ["m.com"]
        assert not client.is_closed
    assert caller_requests == [INITIAL]
    assert owned == ["https://www.publisher.com/ads.txt"]


async def test_redirect_dns_private_and_mixed_addresses_block_before_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for addresses in [["127.0.0.1"], ["8.8.8.8", "169.254.169.254"]]:
        seen: list[str] = []

        def resolve(host: str, port: int, *a: Any, **kw: Any) -> list[Any]:
            ips = addresses if host == "www.publisher.com" else ["8.8.8.8"]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        factory = factory_for(
            lambda r: httpx.Response(
                302, headers={"location": "https://www.publisher.com/ads.txt"}
            ),
            seen,
        )
        assert (
            await _fetch_ads_txt_managerdomains(
                "publisher.com", 1, "test", None, transport_factory=factory
            )
            == []
        )
        assert seen == [INITIAL]


async def test_every_default_hop_resolves_again_and_builds_a_fresh_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adcp.adagents as adagents
    from adcp.signing.ip_pinned_transport import AsyncIpPinnedTransport

    dns: list[str] = []
    pins: list[tuple[str, str]] = []
    transports: list[AsyncIpPinnedTransport] = []

    def resolve(host: str, port: int, *a: Any, **kw: Any) -> list[Any]:
        dns.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    original = adagents._owned_pinned_client

    def factory(url: str, timeout: float) -> httpx.AsyncClient:
        client = original(url, timeout)
        transport = client._transport
        assert isinstance(transport, AsyncIpPinnedTransport)
        transports.append(transport)
        pins.append((url, transport._pool._network_backend._resolved_ip))
        return client

    monkeypatch.setattr(adagents, "_owned_pinned_client", factory)

    async def serve(transport: AsyncIpPinnedTransport, request: httpx.Request) -> httpx.Response:
        if request.url.host == "publisher.com":
            return httpx.Response(302, headers={"location": "https://www.publisher.com/ads.txt"})
        if request.url.host == "www.publisher.com":
            return httpx.Response(302, headers={"location": "https://cdn.net/ads.txt"})
        return httpx.Response(200, text="managerdomain=m.com")

    monkeypatch.setattr(AsyncIpPinnedTransport, "handle_async_request", serve)
    assert await _fetch_ads_txt_managerdomains("publisher.com", 1, "test", None) == ["m.com"]
    assert [url for url, _ in pins] == [
        INITIAL,
        "https://www.publisher.com/ads.txt",
        "https://cdn.net/ads.txt",
    ]
    assert all(ip == "8.8.8.8" for _, ip in pins)
    assert len({id(transport) for transport in transports}) == 3
    assert dns == ["publisher.com"] * 2 + ["www.publisher.com"] * 2 + ["cdn.net"] * 2


async def test_redirect_rebinding_between_precheck_and_pin_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adcp.signing.ip_pinned_transport import AsyncIpPinnedTransport

    counts: dict[str, int] = {}
    requests: list[str] = []

    def resolve(host: str, port: int, *a: Any, **kw: Any) -> list[Any]:
        counts[host] = counts.get(host, 0) + 1
        ip = "127.0.0.1" if host == "www.publisher.com" and counts[host] == 2 else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    async def serve(transport: AsyncIpPinnedTransport, request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://www.publisher.com/ads.txt"})

    monkeypatch.setattr(AsyncIpPinnedTransport, "handle_async_request", serve)
    assert await _fetch_ads_txt_managerdomains("publisher.com", 1, "test", None) == []
    assert requests == [INITIAL]
    assert counts == {"publisher.com": 2, "www.publisher.com": 2}


async def test_default_redirect_connects_to_pinned_ip_without_third_dns_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpcore
    from httpcore._backends.anyio import AnyIOBackend

    counts: dict[str, int] = {}
    connected: list[str] = []

    def resolve(host: str, port: int, *a: Any, **kw: Any) -> list[Any]:
        counts[host] = counts.get(host, 0) + 1
        ip = "127.0.0.1" if counts[host] > 2 else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    async def connect(self: Any, host: str, port: int, *a: Any, **kw: Any) -> Any:
        connected.append(host)
        raise httpcore.ConnectError("intercepted before TLS")

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"location": "https://www.publisher.com/ads.txt"})
        )
    ) as client:
        assert await _fetch_ads_txt_managerdomains("publisher.com", 1, "test", client) == []
    assert connected == ["8.8.8.8"]
    assert counts == {"publisher.com": 1, "www.publisher.com": 2}


async def test_total_budget_decreases_across_hops_and_factory_time_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adcp.adagents as adagents

    loop = asyncio.get_running_loop()
    original_time = loop.time
    consumed = [0.0]
    budgets: list[float] = []
    requests: list[str] = []

    async def dns(host: str, port: int) -> None:
        return None

    monkeypatch.setattr(adagents, "_dns_validate_host", dns)

    @asynccontextmanager
    async def factory(url: str, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        budgets.append(timeout)
        consumed[0] += 0.4

        def serve(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(302, headers={"location": f"/hop{len(requests)}"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            yield client

    # Call the inner walk with a deterministic clock; no sleeps or event-loop
    # timeout scheduling depend on this patched clock. The outer cancellation
    # boundary is separately tested with a factory waiting on an unset event.
    with monkeypatch.context() as clock:
        clock.setattr(loop, "time", lambda: original_time() + consumed[0])
        with pytest.raises(asyncio.TimeoutError):
            await adagents._follow_ads_txt_redirects("publisher.com", 1, "test", None, factory)
    assert len(requests) == 2
    assert budgets == pytest.approx([1, 0.6, 0.2], abs=0.1)


async def test_compressed_terminal_body_is_decoded_once_and_retains_charset() -> None:
    body = "# café\nmanagerdomain=m.com\n".encode("latin-1")
    compressed = gzip.compress(body)
    result, seen = await walk(
        {
            INITIAL: httpx.Response(
                200,
                content=compressed,
                headers={
                    "content-encoding": "gzip",
                    "content-type": "text/plain; charset=iso-8859-1",
                },
            )
        }
    )
    assert result == ["m.com"] and seen == [INITIAL]


async def test_compressed_body_cap_counts_decoded_bytes() -> None:
    compressed = gzip.compress(b" " * (MAX_ADS_TXT_BYTES + 1))
    assert len(compressed) < MAX_ADS_TXT_BYTES
    result, seen = await walk(
        {
            INITIAL: httpx.Response(
                200,
                content=compressed,
                headers={"content-encoding": "gzip"},
            )
        }
    )
    assert result == [] and seen == [INITIAL]
