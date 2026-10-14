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

The cumulative budget is per call, which means per response *except* on a
session-lifetime channel: a ``GET`` event-stream (MCP's standalone
notification stream, and the whole legacy SSE transport) carries every
response for the session, so capping its total would be a session ceiling, not
a per-call cap. There the per-event cap is what bounds the allocation.

Both httpx generations are supported: MCP SDK v2 runs on ``httpx2`` while the
A2A client runs on ``httpx``. The transport and stream classes are built per
module because ``httpx.Response.aclose()`` and ``aiter_raw()`` both
``isinstance``-check the stream against *their own* ``AsyncByteStream``.
"""

from __future__ import annotations

import logging
import re
import zlib
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from adcp.exceptions import (
    ADCPError,
    ADCPProtocolError,
    ADCPResponseEncodingError,
    ADCPResponseTooLargeError,
)

logger = logging.getLogger(__name__)

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
_SUPPORTED_ENCODINGS = frozenset({"identity", "gzip", "deflate"})

# Every Accept-Encoding httpx itself may default to, by installed extras. Used
# only to tell "httpx picked this" from "the adopter asked for this", so the
# latter gets a warning when the budget narrows it.
_HTTPX_DEFAULT_ACCEPT_ENCODINGS = frozenset(
    {
        "gzip, deflate",
        "gzip, deflate, br",
        "gzip, deflate, zstd",
        "gzip, deflate, br, zstd",
    }
)

# A compressed body that decodes to at most ``limit`` is at most ``limit`` on
# the wire plus framing overhead — no real compressor expands its input. Slack
# well past that still catches a peer padding the stream with empty deflate
# blocks, which would otherwise stream forever under a decoded-bytes budget.
_WIRE_SLACK_BYTES = 64 * 1024

# Decompress in slices rather than asking zlib for the whole remaining budget
# at once: a bomb must never make the limiter itself allocate a multiple of the
# budget it is defending.
_DECODE_SLICE_BYTES = 64 * 1024

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


_LIMIT_ERRORS = (ADCPResponseTooLargeError, ADCPResponseEncodingError)


def find_response_limit_error(exc: BaseException) -> ADCPError | None:
    """Find a response-budget rejection anywhere in ``exc``'s tree.

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
        if isinstance(current, _LIMIT_ERRORS):
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

    def feed(self, data: bytes) -> list[int]:
        """Return the length of every event ``data`` completes."""
        buffer = self._carry + data
        completed: list[int] = []
        cursor = 0
        carried_over = self.pending - len(self._carry)
        for match in _EVENT_BOUNDARY.finditer(buffer):
            completed.append(carried_over + (match.start() - cursor))
            cursor = match.end()
            carried_over = 0
        self.pending = carried_over + (len(buffer) - cursor)
        carry = buffer[-_BOUNDARY_CARRY:]
        # Never carry bytes that belong to an already-closed event: they would
        # read as a second terminator and reset a still-growing one.
        self._carry = carry[len(carry) - self.pending :] if self.pending < len(carry) else carry
        return completed


class _DecodedCounter:
    """Count decoded body bytes without ever buffering more than the budget.

    ``httpx`` decompresses the body *after* the transport hands it back, so a
    transport-level counter sees wire bytes. Feeding the wire bytes through a
    budgeted ``zlib`` decompressor here measures what the caller will actually
    allocate, and a compression bomb trips the budget on the first chunk that
    expands past it rather than after httpx has materialized it.

    :func:`adcp.signing._bounded_http.async_read_decoded_limited_bytes` runs the
    same budgeted-inflate idea over a whole buffered representation; this one is
    incremental because it has to meter a stream it is only passing through.
    """

    __slots__ = ("total", "raw_total", "_limit", "_encoding", "_decoder", "_probe", "_collect")

    def __init__(self, *, encoding: str, limit: int, collect: bool) -> None:
        self.total = 0
        self.raw_total = 0
        self._limit = limit
        self._encoding = encoding
        self._decoder: Any = None
        self._probe = bytearray()
        # Only an SSE body needs the decoded bytes back, to find event
        # boundaries. For everything else measuring is the whole job, so the
        # expansion is counted and dropped instead of being copied out.
        self._collect = collect

    def feed(self, raw: bytes) -> bytes:
        """Consume one wire chunk, returning its decoded bytes if collected."""
        self.raw_total += len(raw)
        if self._encoding == "identity":
            self.total += len(raw)
            return raw
        try:
            return self._inflate(raw)
        except zlib.error as exc:
            raise ADCPProtocolError(
                "Agent response body is not valid "
                f"{self._encoding}-compressed data; cannot bound its decoded size"
            ) from exc

    def _start_member(self, data: bytes) -> bytes | None:
        """Open a decoder for the next member; None until enough header bytes."""
        if self._encoding == "gzip":
            self._decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            return data
        # `deflate` is sent both zlib-wrapped (RFC 1950) and raw (RFC 1951) in
        # the wild; sniff the two-byte header before picking a window size.
        self._probe.extend(data)
        if len(self._probe) < 2:
            return None
        cmf, flg = self._probe[0], self._probe[1]
        zlib_wrapped = cmf & 0x0F == 8 and cmf >> 4 <= 7 and ((cmf << 8) | flg) % 31 == 0
        self._decoder = zlib.decompressobj(zlib.MAX_WBITS if zlib_wrapped else -zlib.MAX_WBITS)
        pending = bytes(self._probe)
        self._probe.clear()
        return pending

    def _inflate(self, raw: bytes) -> bytes:
        chunks: list[bytes] = []
        pending: bytes | None = raw
        while pending:
            if self._decoder is None:
                pending = self._start_member(pending)
                if pending is None:
                    break
            # Never ask zlib for more than one byte past the remaining budget:
            # that is enough to detect the overrun, and no more.
            budget = max(1, min(self._limit - self.total + 1, _DECODE_SLICE_BYTES))
            decoded = self._decoder.decompress(pending, budget)
            self.total += len(decoded)
            if decoded and self._collect:
                chunks.append(decoded)
            if self.total > self._limit:
                break
            if self._decoder.eof:
                # gzip and deflate members may be concatenated; the bytes after
                # the first member still land in the caller's buffer, so they
                # still count against the budget.
                leftover = self._decoder.unused_data
                self._decoder = None
                if not leftover or leftover == pending:
                    break
                pending = leftover
                continue
            tail = self._decoder.unconsumed_tail
            if not decoded and tail == pending:
                # No output and no input consumed: nothing left to decode from
                # this chunk. Bail rather than spin on a crafted stream.
                break
            pending = tail
        return b"".join(chunks)


def _content_encoding(headers: Any) -> str:
    """Return the single applied coding, or the raw header if there are several."""
    raw = headers.get("content-encoding") or "identity"
    codings = [coding.strip().lower() for coding in raw.split(",") if coding.strip()]
    # `identity` means "no transformation", so it is noise in a coding list.
    applied = [coding for coding in codings if coding != "identity"]
    if not applied:
        return "identity"
    if len(applied) > 1:
        return ", ".join(applied)
    return applied[0]


def _is_event_stream(headers: Any) -> bool:
    content_type = headers.get("content-type") or ""
    return content_type.split(";", 1)[0].strip().lower() == "text/event-stream"


def _safe_url(url: Any) -> str:
    """Render a request URL without the parts that can carry a secret.

    ``httpx.URL.__str__`` prints userinfo and query in the clear (only ``repr``
    masks them), and this string ends up in an exception message adopters log.
    """
    try:
        return str(url.copy_with(userinfo=b"", query=None, fragment=None))
    except Exception:  # pragma: no cover - defensive; URL shape is httpx's
        return str(url)


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
            cumulative: bool,
            report: Callable[[ADCPError], None],
            describe: Callable[[int, int, bool], ADCPResponseTooLargeError],
        ) -> None:
            self._stream = stream
            self._limit = limit
            self._encoding = encoding
            self._event_stream = event_stream
            self._cumulative = cumulative
            self._report = report
            self._describe = describe

        async def __aiter__(self) -> AsyncIterator[bytes]:
            counter = _DecodedCounter(
                encoding=self._encoding, limit=self._limit, collect=self._event_stream
            )
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
                if self._cumulative:
                    if counter.total > self._limit:
                        self._fail(counter.total, per_event=False)
                    if counter.raw_total > self._limit + _WIRE_SLACK_BYTES:
                        # Decoded bytes stayed small but the wire did not: the
                        # peer is padding a compressed stream.
                        self._fail(counter.raw_total, per_event=False)
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
            limit_provider: Callable[[bool], int | None],
            report: Callable[[ADCPError], None],
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
            headers = response.headers
            event_stream = _is_event_stream(headers)
            # A GET event-stream is a session-lifetime channel — MCP's legacy
            # SSE transport and its standalone notification stream both open
            # one — so a cumulative budget there would be a session total, not
            # a per-call cap, and would eventually fail a healthy long session.
            # Each event still carries one agent response and is capped, which
            # is what bounds the allocation. Every other body (including the
            # event-stream answer to a streamable-HTTP POST) is one call, so it
            # gets the cumulative budget too.
            long_lived = event_stream and request.method.upper() == "GET"
            limit = self._limit_provider(long_lived)
            if limit is None:
                return response

            encoding = _content_encoding(headers)
            if encoding not in _SUPPORTED_ENCODINGS:
                await self._discard(response)
                error: ADCPError = ADCPResponseEncodingError(
                    encoding, accepted=_ACCEPT_ENCODING, agent_id=self._agent_id
                )
                self._report(error)
                raise error

            cumulative = not long_lived

            declared = _declared_length(headers)
            # Only pre-reject on a like-for-like length. For an identity body
            # Content-Length *is* the decoded size, so an over-budget
            # declaration is a guaranteed overrun; for a compressed one it
            # describes the wire bytes and says nothing about what they expand
            # to, so that case waits for the decoded count below.
            if cumulative and declared is not None and declared > limit and encoding == "identity":
                await self._discard(response)
                too_large = self._describe(
                    limit, declared, per_event=False, declared=True, url=_safe_url(request.url)
                )
                self._report(too_large)
                raise too_large

            def describe(
                stream_limit: int, received: int, per_event: bool
            ) -> ADCPResponseTooLargeError:
                return self._describe(
                    stream_limit, received, per_event=per_event, url=_safe_url(request.url)
                )

            response.stream = _LimitedStream(
                response.stream,
                limit=limit,
                encoding=encoding,
                event_stream=event_stream,
                cumulative=cumulative,
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
    a pooled, long-lived client. It is passed ``True`` for a session-lifetime
    stream, where a per-call override must *not* apply: MCP opens that stream
    in a background task that keeps a snapshot of whichever call happened to
    trigger the connect, so an override there would outlive its call and rebind
    the budget for the rest of the session.

    ``report`` receives every violation before it is raised: transports bury
    exceptions (MCP behind an anyio task group, ``a2a-sdk`` behind its own
    wrapper), so adapters keep a side channel to re-raise the typed error at
    the call site.
    """

    limit_provider: Callable[[bool], int | None]
    agent_id: str | None = None
    agent_uri: str | None = None
    report: Callable[[ADCPError], None] = field(default=lambda error: None)


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


def unwrap_response_limit(transport: Any) -> Any:
    """Return the transport underneath the limiter, or ``transport`` itself.

    The budget is installed by wrapping, so ``client._transport`` is no longer
    the object the caller handed in. Anything that needs to identify that
    object — an IP-pinned transport, a test — goes through here.
    """
    inner = getattr(transport, "_inner", None)
    return inner if inner is not None and _is_limiting_transport(transport) else transport


def _is_limiting_transport(transport: Any) -> bool:
    return any(isinstance(transport, cls) for cls in _TRANSPORT_CLASSES.values())


def install_response_limit(
    client: Any,
    *,
    httpx_module: Any,
    policy: ResponseLimitPolicy,
    required: bool = True,
) -> Any:
    """Meter every response this client reads against the live byte budget.

    Wraps the client's transport (and any mounted transports) in place, and
    pins ``Accept-Encoding`` to the codings whose decoded size this module can
    measure. Reaching for ``_transport``/``_mounts`` is deliberate: wrapping
    after construction keeps the client's own ``limits``/``verify``/``proxy``
    wiring intact, which rebuilding the transport here would quietly drop.
    Returns ``client`` so call sites can wrap construction inline.

    ``required`` is the SDK's own clients, which must never run uncapped: a
    client this module cannot reach into is a hard error there. An adopter's
    ``httpx_client_factory`` may legitimately return a duck-typed client that
    only looks like one, and that client is theirs to bound — so it warns
    instead, loudly enough that the gap is never silent.
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

    def refuse(reason: str) -> Any:
        message = f"cannot install the response-size budget on {type(client).__name__}: {reason}"
        if required:
            raise TypeError(message)
        logger.warning(
            "%s. Responses from agent %s will NOT be capped; bound this client yourself.",
            message,
            policy.agent_id,
        )
        return client

    # A delegating wrapper passes an attribute sniff — `_transport` reads
    # through to the inner client while our assignment lands on the proxy — so
    # identify the real client type rather than its attributes.
    if not isinstance(client, httpx_module.AsyncClient):
        return refuse(
            f"it is not an {httpx_module.__name__}.AsyncClient, so assigning its "
            "transport would not change what the client actually sends through"
        )

    inner = getattr(client, "_transport", None)
    mounts = getattr(client, "_mounts", None)
    if inner is None and not mounts:
        return refuse("it exposes neither a _transport nor any _mounts")
    if inner is not None:
        client._transport = wrap(inner)
    if isinstance(mounts, dict):
        for pattern, mounted in list(mounts.items()):
            if mounted is not None:
                mounts[pattern] = wrap(mounted)

    headers = getattr(client, "headers", None)
    if headers is not None:
        current = headers.get("accept-encoding")
        if (
            current is not None
            and current != _ACCEPT_ENCODING
            and current not in _HTTPX_DEFAULT_ACCEPT_ENCODINGS
        ):
            logger.warning(
                "Narrowing Accept-Encoding from %r to %r for agent %s: the response "
                "budget can only measure codings it can decode.",
                current,
                _ACCEPT_ENCODING,
                policy.agent_id,
            )
        headers["accept-encoding"] = _ACCEPT_ENCODING
    return client


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "ResponseLimitPolicy",
    "find_response_limit_error",
    "install_response_limit",
    "unwrap_response_limit",
    "reset_call_max_response_bytes",
    "resolve_max_response_bytes",
    "set_call_max_response_bytes",
    "validate_max_response_bytes",
]
