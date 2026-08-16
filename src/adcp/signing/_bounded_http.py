"""Small streaming body readers shared by signing discovery fetchers."""

from __future__ import annotations

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


__all__ = ["ResponseTooLargeError", "async_read_limited_bytes", "read_limited_bytes"]
