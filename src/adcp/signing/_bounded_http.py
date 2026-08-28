"""Small streaming body readers shared by signing discovery fetchers."""

from __future__ import annotations

import zlib
from collections.abc import AsyncIterator
from typing import Any

import httpx


class ResponseTooLargeError(ValueError):
    """A decoded HTTP response exceeded its configured byte budget."""

    def __init__(self, *, limit: int, received: int) -> None:
        super().__init__(f"response exceeds {limit} bytes (received at least {received})")
        self.limit = limit
        self.received = received


def _reject_encoded_response(response: httpx.Response) -> None:
    """Reject content codings that would be expanded before our byte limit."""
    content_encoding = response.headers.get("content-encoding", "identity")
    codings = {coding.strip().lower() for coding in content_encoding.split(",")}
    if codings - {"", "identity"}:
        raise ValueError("encoded HTTP responses are not accepted")


def read_limited_bytes(response: httpx.Response, *, limit: int) -> bytes:
    """Stream at most ``limit`` decoded bytes from ``response``."""
    _reject_encoded_response(response)
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = 0
        if declared > limit:
            raise ResponseTooLargeError(limit=limit, received=declared)

    body = bytearray()
    chunk_size = max(1, min(64 * 1024, limit + 1))
    for chunk in response.iter_bytes(chunk_size=chunk_size):
        body.extend(chunk)
        if len(body) > limit:
            raise ResponseTooLargeError(limit=limit, received=len(body))
    return bytes(body)


async def async_read_limited_bytes(response: httpx.Response, *, limit: int) -> bytes:
    """Async counterpart to :func:`read_limited_bytes`."""
    _reject_encoded_response(response)
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = 0
        if declared > limit:
            raise ResponseTooLargeError(limit=limit, received=declared)

    body = bytearray()
    chunk_size = max(1, min(64 * 1024, limit + 1))
    async for chunk in response.aiter_bytes(chunk_size=chunk_size):
        body.extend(chunk)
        if len(body) > limit:
            raise ResponseTooLargeError(limit=limit, received=len(body))
    return bytes(body)


async def async_read_decoded_limited_bytes(
    response: httpx.Response,
    *,
    limit: int,
) -> bytes:
    """Read a bounded HTTP representation with safe gzip/deflate decoding.

    ``httpx.Response.aiter_bytes()`` may allocate a fully expanded compressed
    chunk before yielding it.  This reader consumes raw wire chunks and gives
    zlib only the remaining output budget, so a compression bomb cannot force
    an allocation materially larger than ``limit``.  The returned bytes are
    the decoded representation bytes used for content-addressed protocol
    documents.
    """
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity", "gzip", "deflate"}:
        raise ValueError("unsupported HTTP content encoding")
    content_length = response.headers.get("content-length")
    if content_length is not None and content_encoding in {"", "identity"}:
        try:
            declared = int(content_length)
        except ValueError:
            declared = -1
        if declared > limit:
            raise ResponseTooLargeError(limit=limit, received=declared)

    chunks: list[bytes] = []
    size = 0

    def append(data: bytes) -> None:
        nonlocal size
        size += len(data)
        if size > limit:
            raise ResponseTooLargeError(limit=limit, received=size)
        if data:
            chunks.append(data)

    if response.is_stream_consumed:
        append(response.content)
        return b"".join(chunks)

    async def raw_chunks() -> AsyncIterator[bytes]:
        async for chunk in response.aiter_raw():
            yield chunk

    if content_encoding in {"", "identity"}:
        async for chunk in raw_chunks():
            append(chunk)
        return b"".join(chunks)

    decoder: Any | None = (
        zlib.decompressobj(16 + zlib.MAX_WBITS) if content_encoding == "gzip" else None
    )
    deflate_probe = bytearray()
    try:
        async for raw_chunk in raw_chunks():
            if decoder is None:
                deflate_probe.extend(raw_chunk)
                if len(deflate_probe) < 2:
                    continue
                cmf, flg = deflate_probe[0], deflate_probe[1]
                zlib_wrapped = cmf & 0x0F == 8 and cmf >> 4 <= 7 and ((cmf << 8) | flg) % 31 == 0
                decoder = zlib.decompressobj(zlib.MAX_WBITS if zlib_wrapped else -zlib.MAX_WBITS)
                pending = bytes(deflate_probe)
                deflate_probe.clear()
            else:
                pending = raw_chunk
            while pending:
                decoded = decoder.decompress(pending, limit - size + 1)
                append(decoded)
                pending = decoder.unconsumed_tail
        if decoder is None:
            raise zlib.error("truncated deflate header")
        append(decoder.flush(limit - size + 1))
        if not decoder.eof or decoder.unused_data:
            raise zlib.error("incomplete or trailing compressed data")
    except zlib.error as exc:
        raise ValueError("invalid compressed HTTP response") from exc
    return b"".join(chunks)


__all__ = [
    "ResponseTooLargeError",
    "async_read_decoded_limited_bytes",
    "async_read_limited_bytes",
    "read_limited_bytes",
]
