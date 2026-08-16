"""Remote signing documents are bounded while streaming, not after buffering."""

from __future__ import annotations

import httpx
import pytest

from adcp.signing.jwks import async_default_jwks_fetcher, default_jwks_fetcher
from adcp.signing.revocation_fetcher import (
    RevocationListFetchError,
    async_default_revocation_list_fetcher,
    default_revocation_list_fetcher,
)


class _SyncChunks(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.read = 0

    def __iter__(self):  # type: ignore[no-untyped-def]
        for chunk in (b"xxxx", b"yyyy", b"zzzz"):
            self.read += 1
            yield chunk


class _AsyncChunks(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.read = 0

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in (b"xxxx", b"yyyy", b"zzzz"):
            self.read += 1
            yield chunk


def test_sync_jwks_stops_reading_chunked_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _SyncChunks()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(
        "adcp.signing.ip_pinned_transport.build_ip_pinned_transport",
        lambda uri, **kwargs: transport,
    )
    with pytest.raises(ValueError, match="exceeds 5 bytes"):
        default_jwks_fetcher("https://keys.example/jwks", max_body_bytes=5)
    assert stream.read == 2


@pytest.mark.asyncio
async def test_async_jwks_stops_reading_chunked_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _AsyncChunks()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        lambda uri, **kwargs: transport,
    )
    with pytest.raises(ValueError, match="exceeds 5 bytes"):
        await async_default_jwks_fetcher("https://keys.example/jwks", max_body_bytes=5)
    assert stream.read == 2


def test_sync_revocation_stops_reading_chunked_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _SyncChunks()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(
        "adcp.signing.ip_pinned_transport.build_ip_pinned_transport",
        lambda uri, **kwargs: transport,
    )
    with pytest.raises(RevocationListFetchError, match="exceeds 5 bytes"):
        default_revocation_list_fetcher("https://gov.example/list", max_body_bytes=5)
    assert stream.read == 2


@pytest.mark.asyncio
async def test_async_revocation_stops_reading_chunked_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _AsyncChunks()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        lambda uri, **kwargs: transport,
    )
    with pytest.raises(RevocationListFetchError, match="exceeds 5 bytes"):
        await async_default_revocation_list_fetcher("https://gov.example/list", max_body_bytes=5)
    assert stream.read == 2
