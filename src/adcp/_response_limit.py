"""Streaming response-size enforcement for the agent call paths.

Agent responses are untrusted input. A seller that answers a single tool
call with tens of megabytes can exhaust a buyer's memory long before any
parser or post-hoc size check runs, so the budget has to be enforced
*while the body is read*.

This module wraps the httpx transport underneath an MCP or A2A client with
a counting stream. Every response body — JSON tool results, ``tools/list``,
the A2A agent card, and SSE streams — flows through
:class:`_LimitingTransport`, which:

* pre-rejects a declared ``Content-Length`` above the budget before a single
  body byte is read;
* counts **decoded** bytes as they stream in and aborts the moment the
  running total passes the budget;
* additionally caps each individual SSE event, so a peer cannot slip a huge
  payload past the per-call budget by answering with
  ``Content-Type: text/event-stream``.

Both httpx generations are supported: MCP SDK v2 runs on ``httpx2`` while the
A2A client runs on ``httpx``. The transport and stream classes are built per
module because ``httpx.Response.aclose()`` and ``aiter_raw()`` both
``isinstance``-check the stream against *their own* ``AsyncByteStream``.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import AsyncIterator, Callable, Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from adcp.exceptions import ADCPProtocolError, ADCPResponseTooLargeError

DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
"""Default per-response budget applied to every agent response (4 MiB).

Matches the hosted compliance grader and the cross-SDK default agreed in
adcontextprotocol/adcp#8190. Sellers with larger catalogs are expected to
paginate rather than rely on buyers raising their limit.
"""

# Only codings this module can count the decoded size of are advertised.
# Anything else (br, zstd) would have to be expanded by httpx before we could
# measure it, which is exactly the allocation the budget exists to prevent.
_ACCEPT_ENCODING = "gzip, deflate"
_SUPPORTED_ENCODINGS = frozenset({"", "identity", "gzip", "deflate"})

# SSE event terminators from the WHATWG event-stream grammar. CRLF is listed
# first so a `\r\n\r\n` boundary is consumed whole rather than as `\n\n`.
_EVENT_BOUNDARY = re.compile(b"\r\n\r\n|\n\n|\r\r")
_BOUNDARY_CARRY = 3  # longest terminator minus one: the most a split can hide

_UNSET: Any = object()

_call_max_response_bytes: ContextVar[Any] = ContextVar(
    "adcp_call_max_response_bytes", default=_UNSET
)


def set_call_max_response_bytes(limit: int | None) -> Token[Any]:
    """Override the response budget for the current task call."""
    return _call_max_response_bytes.set(limit)


def reset_call_max_response_bytes(token: Token[Any]) -> None:
    """Undo :func:`set_call_max_response_bytes`."""
    _call_max_response_bytes.reset(token)


def resolve_max_response_bytes(configured: int | None) -> int | None:
    """Return the budget in force: the per-call override, else ``configured``."""
    override = _call_max_response_bytes.get()
    if override is not _UNSET:
        return override  # type: ignore[no-any-return]
    return configured


def validate_max_response_bytes(value: int | None, *, field: str) -> int | None:
    """Validate a configured budget, returning it unchanged."""
    if value is None:
        return None
    if value <= 0:
        raise ValueError(
            f"{field} must be positive, got: {value}. "
            "Use None to disable the response-size budget entirely."
        )
    return value


def find_response_too_large(exc: BaseException) -> ADCPResponseTooLargeError | None:
    """Find an :class:`ADCPResponseTooLargeError` anywhere in ``exc``'s tree.

    Both transports bury transport-level failures: MCP runs its HTTP writer in
    an anyio task group (so the real cause arrives inside an ``ExceptionGroup``)
    and ``a2a-sdk`` re-raises with the original attached as ``__cause__``.
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ADCPResponseTooLargeError):
            return current
        stack.extend(getattr(current, "exceptions", ()) or ())
        stack.append(current.__cause__)
        stack.append(current.__context__)
    return None


class _EventSizeTracker:
    """Measure each SSE event separately as the body streams past.

    A per-call budget alone lets a peer hide an arbitrarily large payload in a
    single event of a stream that is otherwise short, so events are sized
    individually. Only the running length is kept — never the event bytes —
    and the few bytes carried between chunks are just enough to spot a
    terminator split across a chunk boundary.
    """

    __slots__ = ("pending", "_carry")

    def __init__(self) -> None:
        self.pending = 0
        self._carry = b""

    def feed(self, data: bytes) -> Iterator[int]:
        """Yield the length of every event ``data`` completes."""
        buffer = self._carry + data
        cursor = 0
        carried_over = self.pending - len(self._carry)
        for match in _EVENT_BOUNDARY.finditer(buffer):
            yield carried_over + (match.start() - cursor)
            cursor = match.end()
            carried_over = 0
        self.pending = carried_over + (len(buffer) - cursor)
        carry = buffer[-_BOUNDARY_CARRY:]
        # Never carry bytes that belong to an already-closed event: they would
        # read as a second terminator and reset a still-growing one.
        self._carry = carry[len(carry) - self.pending :] if self.pending < len(carry) else carry


class _DecodedCounter:
    """Count decoded body bytes without ever buffering more than the budget.

    ``httpx`` decompresses the body *after* the transport hands it back, so a
    transport-level counter sees wire bytes. Feeding the wire bytes through a
    budgeted ``zlib`` decompressor here measures what the caller will actually
    allocate, and a compression bomb trips the budget on the first chunk that
    expands past it rather than after httpx has materialized it.
    """

    __slots__ = ("total", "_limit", "_encoding", "_decoder", "_probe")

    def __init__(self, *, encoding: str, limit: int) -> None:
        self.total = 0
        self._limit = limit
        self._encoding = encoding
        self._decoder: Any = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        self._probe = bytearray()

    def feed(self, raw: bytes) -> bytes:
        """Consume one wire chunk, returning its decoded bytes."""
        if self._encoding in ("", "identity"):
            self.total += len(raw)
            return raw
        try:
            return self._inflate(raw)
        except zlib.error as exc:  # pragma: no cover - exercised via adapters
            raise ADCPProtocolError(
                "Agent response body is not valid "
                f"{self._encoding}-compressed data; cannot bound its decoded size"
            ) from exc

    def _inflate(self, raw: bytes) -> bytes:
        pending = raw
        if self._decoder is None:
            # `deflate` is sent both zlib-wrapped (RFC 1950) and raw (RFC 1951)
            # in the wild; sniff the two-byte header before picking a window.
            self._probe.extend(raw)
            if len(self._probe) < 2:
                return b""
            cmf, flg = self._probe[0], self._probe[1]
            zlib_wrapped = cmf & 0x0F == 8 and cmf >> 4 <= 7 and ((cmf << 8) | flg) % 31 == 0
            self._decoder = zlib.decompressobj(zlib.MAX_WBITS if zlib_wrapped else -zlib.MAX_WBITS)
            pending = bytes(self._probe)
            self._probe.clear()

        chunks: list[bytes] = []
        while pending:
            # Never ask zlib for more than one byte past the remaining budget:
            # that is enough to detect the overrun, and no more.
            budget = max(1, self._limit - self.total + 1)
            decoded = self._decoder.decompress(pending, budget)
            self.total += len(decoded)
            if decoded:
                chunks.append(decoded)
            tail = self._decoder.unconsumed_tail
            if self.total > self._limit:
                break
            if not decoded and tail == pending:
                # No output and no input consumed: nothing left to decode from
                # this chunk. Bail rather than spin on a crafted stream.
                break
            pending = tail
        return b"".join(chunks)


def _content_encoding(headers: Any) -> str:
    raw = headers.get("content-encoding") or "identity"
    codings = [coding.strip().lower() for coding in raw.split(",") if coding.strip()]
    if not codings:
        return "identity"
    if len(codings) > 1:
        return ",".join(codings)
    return codings[0]


def _is_event_stream(headers: Any) -> bool:
    content_type = headers.get("content-type") or ""
    return content_type.split(";", 1)[0].strip().lower() == "text/event-stream"


def _declared_length(headers: Any) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


_TRANSPORT_CLASSES: dict[str, type] = {}


def _limiting_transport_cls(httpx_module: Any) -> type:
    """Build the limiting transport class bound to one httpx generation."""
    cached = _TRANSPORT_CLASSES.get(httpx_module.__name__)
    if cached is not None:
        return cached

    class _LimitedStream(httpx_module.AsyncByteStream):  # type: ignore[misc]
        """Pass wire bytes through while metering the decoded body."""

        def __init__(
            self,
            stream: Any,
            *,
            limit: int,
            encoding: str,
            event_stream: bool,
            report: Callable[[ADCPResponseTooLargeError], None],
            describe: Callable[[int, int, bool], ADCPResponseTooLargeError],
        ) -> None:
            self._stream = stream
            self._limit = limit
            self._encoding = encoding
            self._event_stream = event_stream
            self._report = report
            self._describe = describe

        async def __aiter__(self) -> AsyncIterator[bytes]:
            counter = _DecodedCounter(encoding=self._encoding, limit=self._limit)
            events = _EventSizeTracker() if self._event_stream else None
            async for chunk in self._stream:
                decoded = counter.feed(chunk)
                if events is not None and decoded:
                    # Per-event first: one giant event reports as what it is
                    # rather than as a stream that happened to run long.
                    for event_bytes in events.feed(decoded):
                        if event_bytes > self._limit:
                            self._fail(event_bytes, per_event=True)
                    if events.pending > self._limit:
                        self._fail(events.pending, per_event=True)
                if counter.total > self._limit:
                    self._fail(counter.total, per_event=False)
                yield chunk

        def _fail(self, received: int, *, per_event: bool) -> None:
            error = self._describe(self._limit, received, per_event)
            self._report(error)
            raise error

        async def aclose(self) -> None:
            aclose = getattr(self._stream, "aclose", None)
            if aclose is not None:
                await aclose()

    class _LimitingTransport(httpx_module.AsyncBaseTransport):  # type: ignore[misc]
        """Wrap a transport so every response body is metered as it is read."""

        def __init__(
            self,
            inner: Any,
            *,
            limit_provider: Callable[[], int | None],
            report: Callable[[ADCPResponseTooLargeError], None],
            describe: Callable[..., ADCPResponseTooLargeError],
            agent_id: str | None,
        ) -> None:
            self._inner = inner
            self._limit_provider = limit_provider
            self._report = report
            self._describe = describe
            self._agent_id = agent_id

        async def handle_async_request(self, request: Any) -> Any:
            response = await self._inner.handle_async_request(request)
            limit = self._limit_provider()
            if limit is None:
                return response

            headers = response.headers
            encoding = _content_encoding(headers)
            if encoding not in _SUPPORTED_ENCODINGS:
                await self._discard(response)
                raise ADCPProtocolError(
                    f"Agent response uses unsupported Content-Encoding {encoding!r}; "
                    f"only {_ACCEPT_ENCODING} are requested, and an unknown coding "
                    "cannot be size-bounded before it is expanded",
                    agent_id=self._agent_id,
                )

            declared = _declared_length(headers)
            # Only pre-reject on a like-for-like length. For an identity body
            # Content-Length *is* the decoded size, so an over-budget
            # declaration is a guaranteed overrun; for a compressed one it
            # describes the wire bytes and says nothing about what they expand
            # to, so that case waits for the decoded count below.
            if declared is not None and declared > limit and encoding in ("", "identity"):
                await self._discard(response)
                error = self._describe(
                    limit, declared, per_event=False, declared=True, url=str(request.url)
                )
                self._report(error)
                raise error

            def describe(
                stream_limit: int, received: int, per_event: bool
            ) -> ADCPResponseTooLargeError:
                return self._describe(
                    stream_limit, received, per_event=per_event, url=str(request.url)
                )

            response.stream = _LimitedStream(
                response.stream,
                limit=limit,
                encoding=encoding,
                event_stream=_is_event_stream(headers),
                report=self._report,
                describe=describe,
            )
            return response

        @staticmethod
        async def _discard(response: Any) -> None:
            aclose = getattr(response.stream, "aclose", None)
            if aclose is not None:
                await aclose()

        async def __aenter__(self) -> Any:
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc_info: Any) -> None:
            await self._inner.__aexit__(*exc_info)

        async def aclose(self) -> None:
            await self._inner.aclose()

    # setdefault so a concurrent first use cannot leave two winners behind.
    return _TRANSPORT_CLASSES.setdefault(httpx_module.__name__, _LimitingTransport)


@dataclass(frozen=True)
class ResponseLimitPolicy:
    """How one client should meter the responses it reads.

    ``limit_provider`` is re-read per request so a per-call override applies to
    a pooled, long-lived client. ``report`` receives every violation before it
    is raised: transports bury exceptions (MCP behind an anyio task group,
    ``a2a-sdk`` behind its own wrapper), so adapters keep a side channel to
    re-raise the typed error at the call site.
    """

    limit_provider: Callable[[], int | None]
    agent_id: str | None = None
    agent_uri: str | None = None
    report: Callable[[ADCPResponseTooLargeError], None] = field(default=lambda error: None)


def _make_describe(
    *, agent_id: str | None, agent_uri: str | None
) -> Callable[..., ADCPResponseTooLargeError]:
    def describe(
        limit: int,
        received: int,
        *,
        per_event: bool = False,
        declared: bool = False,
        url: str | None = None,
    ) -> ADCPResponseTooLargeError:
        return ADCPResponseTooLargeError(
            limit=limit,
            received=received,
            agent_id=agent_id,
            agent_uri=agent_uri,
            per_event=per_event,
            declared=declared,
            url=url,
        )

    return describe


def install_response_limit(
    client: Any,
    *,
    httpx_module: Any,
    policy: ResponseLimitPolicy,
) -> Any:
    """Meter every response this client reads against the live byte budget.

    Wraps the client's transport (and any mounted transports) in place, and
    pins ``Accept-Encoding`` to the codings whose decoded size this module can
    measure. Reaching for ``_transport``/``_mounts`` is deliberate: wrapping
    after construction keeps the client's own ``limits``/``verify``/``proxy``
    wiring intact, which rebuilding the transport here would quietly drop.
    Returns ``client`` so call sites can wrap construction inline.
    """
    describe = _make_describe(agent_id=policy.agent_id, agent_uri=policy.agent_uri)
    transport_cls = _limiting_transport_cls(httpx_module)

    def wrap(transport: Any) -> Any:
        if isinstance(transport, transport_cls):
            return transport
        return transport_cls(
            transport,
            limit_provider=policy.limit_provider,
            report=policy.report,
            describe=describe,
            agent_id=policy.agent_id,
        )

    inner = getattr(client, "_transport", None)
    if inner is not None:
        client._transport = wrap(inner)
    mounts = getattr(client, "_mounts", None)
    if isinstance(mounts, dict):
        for pattern, mounted in list(mounts.items()):
            if mounted is not None:
                mounts[pattern] = wrap(mounted)

    headers = getattr(client, "headers", None)
    if headers is not None:
        headers["accept-encoding"] = _ACCEPT_ENCODING
    return client


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "ResponseLimitPolicy",
    "find_response_too_large",
    "install_response_limit",
    "reset_call_max_response_bytes",
    "resolve_max_response_bytes",
    "set_call_max_response_bytes",
    "validate_max_response_bytes",
]
