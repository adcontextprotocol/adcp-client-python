"""Bound what a JSON body becomes before anything parses it.

A byte cap bounds ingress, not memory. 4 MiB of ``[[[...]]]`` or ``[1,1,1,...]``
is on the order of two million JSON nodes, and deeply nested input exhausts the
stack of a recursive-descent parser or a generated validator before any schema
check runs. AdCP 3.3 (``security.mdx`` §Response Size Bounds item 6 and
§Inbound body bounds item 3) therefore asks for two parse bounds after the byte
cap passes:

* a maximum nesting depth of :data:`MAX_JSON_DEPTH` (64), and
* at most :data:`MAX_JSON_MEMBERS` (100,000) aggregate object members and
  array elements.

Both are the task-body caps of the attestation resolution fetch contract.

The bounds are enforced by :class:`JsonBoundsScanner`, a counting scanner that
reads the bytes once and keeps only a small container stack — never the
document. It aborts at depth 65 or member 100,001, so the check cannot itself
become the allocation primitive it guards against. A full ``json.loads``
followed by a walk would already have allocated the document it was meant to
refuse.

The scanner is incremental (feed it chunks as they stream past) and tolerant:
it validates the JSON grammar only as far as it needs to count accurately. The
moment the input stops being JSON it stops scanning and leaves the verdict to
the real parser, which rejects that input at or before the same point, so
everything the parser could materialize has already been counted. A non-JSON
body — an HTML error page, say — therefore passes through unchanged rather than
surfacing as a bounds violation.

Two encodings would let a parser read a document this scanner cannot follow:
``json.loads(bytes)`` (and so ``httpx.Response.json()``) transparently decodes
UTF-16 and UTF-32, and strips a UTF-8 byte-order mark. The UTF-8 BOM is skipped
here; anything that looks like UTF-16 or UTF-32 (a UTF-16/32 BOM, or a NUL
byte where a JSON token should be) is refused outright with
``kind="encoding"``. RFC 8259 §8.1 requires UTF-8 between systems, so no
conforming peer is affected.
"""

from __future__ import annotations

import json
import re
from itertools import accumulate
from typing import Any, Literal

MAX_JSON_DEPTH = 64
"""Maximum JSON nesting depth (``[[...]]`` 64 deep passes; 65 is rejected)."""

MAX_JSON_MEMBERS = 100_000
"""Maximum aggregate object members plus array elements in one document."""

DEFAULT_MAX_INBOUND_BODY_BYTES = 4 * 1024 * 1024
"""Spec ceiling for an inbound push or webhook body (4 MiB, 4,194,304 bytes).

A tighter per-feature cap governs: AdCP webhook deliveries keep the 1 MB
payload limit (``webhooks.mdx`` §Security requirements), see
:data:`adcp.webhook_receiver.DEFAULT_MAX_WEBHOOK_BODY_BYTES`.
"""

JsonBoundKind = Literal["depth", "members", "encoding"]

_UTF8_BOM = b"\xef\xbb\xbf"
# Prefixes json.detect_encoding() reads as UTF-16 or UTF-32.
_WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")
_BOM_PROBE = 4  # longest byte-order mark

_WHITESPACE = re.compile(rb"[ \t\n\r]*")
# The body of a string up to (not including) its closing quote. A backslash
# followed by any byte is one escape, so `\"` never closes the string. A lone
# backslash at the very end of a chunk is left unconsumed and handled by the
# caller, since its escaped byte is still in flight.
_STRING_BODY = re.compile(rb'[^"\\]*(?:\\.[^"\\]*)*', re.DOTALL)
# A bare scalar run: numbers, true/false/null, and NaN/Infinity, which
# Python's json module accepts. NUL is excluded so a UTF-16/32 body trips the
# encoding check instead of hiding inside a "scalar".
_SCALAR_BODY = re.compile(rb'[^ \t\n\r\[\]{},:"\x00]*')

# Fast-path helpers for a buffered body (see _within_bounds_fast).
_STRING_TOKEN = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"', re.DOTALL)
_NOT_BRACKETS = bytes(b for b in range(256) if b not in b"[]{}")
_BRACKET_DELTA = {ord("["): 1, ord("{"): 1, ord("]"): -1, ord("}"): -1}

_LINE_END = re.compile(rb"[\r\n]")

# What the scanner expects next, outside a string or scalar.
_VALUE = 0  # a value (document start, after ':' or after ',' in an array)
_ARRAY_FIRST = 1  # just after '[': a value or ']'
_OBJECT_FIRST = 2  # just after '{': a key or '}'
_KEY = 3  # after ',' in an object: a key
_COLON = 4  # after a key: ':'
_AFTER_VALUE = 5  # after a value: ',' or a closer
_DONE = 6  # the root value is complete: only whitespace may follow

_OPEN_OBJECT = 0x7B  # {
_CLOSE_OBJECT = 0x7D  # }
_OPEN_ARRAY = 0x5B  # [
_CLOSE_ARRAY = 0x5D  # ]
_COMMA = 0x2C
_COLON_BYTE = 0x3A
_QUOTE = 0x22
_BACKSLASH = 0x5C
_NUL = 0x00


class JsonBoundsError(ValueError):
    """A JSON document exceeded the depth or member bound, or is not UTF-8.

    ``kind`` names the bound, ``limit`` is the bound in force and ``observed``
    the count that crossed it (for ``kind="encoding"`` both are 0). The message
    never quotes the document: it has passed none of the string hygiene that
    would make an excerpt safe to log.
    """

    def __init__(self, kind: JsonBoundKind, *, limit: int, observed: int) -> None:
        self.kind: JsonBoundKind = kind
        self.limit = limit
        self.observed = observed
        if kind == "depth":
            message = f"JSON nesting depth exceeds {limit}"
        elif kind == "members":
            message = f"JSON document has more than {limit} object members and array elements"
        else:
            message = "JSON body is not UTF-8 (UTF-16/UTF-32 bodies are refused)"
        super().__init__(message)


class JsonBoundsScanner:
    """Incrementally enforce the depth and member bounds over JSON bytes.

    Feed decoded bytes in arbitrary chunks with :meth:`feed`;
    :class:`JsonBoundsError` is raised from the chunk that crosses a bound.
    Only a container-type stack (at most ``max_depth`` entries) and a few
    counters are retained between chunks. :meth:`reset` readies the scanner
    for the next document, e.g. the next SSE event.
    """

    __slots__ = (
        "max_depth",
        "max_members",
        "members",
        "stopped",
        "_stack",
        "_expect",
        "_in_string",
        "_string_is_key",
        "_escape_pending",
        "_in_scalar",
        "_started",
        "_head",
    )

    def __init__(
        self,
        *,
        max_depth: int = MAX_JSON_DEPTH,
        max_members: int = MAX_JSON_MEMBERS,
    ) -> None:
        if max_depth < 1 or max_members < 1:
            raise ValueError("JSON bounds must be positive")
        self.max_depth = max_depth
        self.max_members = max_members
        self.members = 0
        # Set once the input stops being JSON; further bytes are ignored and
        # the real parser is left to reject the document.
        self.stopped = False
        self._stack = bytearray()  # 1 = object, 0 = array
        self._expect = _VALUE
        self._in_string = False
        self._string_is_key = False
        self._escape_pending = False
        self._in_scalar = False
        self._started = False
        self._head = b""

    @property
    def depth(self) -> int:
        """Current container nesting depth."""
        return len(self._stack)

    def reset(self) -> None:
        """Forget the current document and start counting a new one."""
        self.members = 0
        self.stopped = False
        self._stack.clear()
        self._expect = _VALUE
        self._in_string = False
        self._string_is_key = False
        self._escape_pending = False
        self._in_scalar = False
        self._started = False
        self._head = b""

    def feed(self, data: bytes | bytearray | memoryview) -> None:
        """Scan one chunk, raising :class:`JsonBoundsError` on a violation."""
        if self.stopped or not data:
            return
        chunk = bytes(data) if not isinstance(data, bytes) else data
        pos = 0
        if not self._started:
            # Hold the first few bytes until a byte-order mark can be judged
            # whole. A body that never reaches _BOM_PROBE bytes cannot exceed
            # either bound anyway.
            chunk = self._head + chunk
            if len(chunk) < _BOM_PROBE:
                self._head = chunk
                return
            self._head = b""
            pos = self._start(chunk)
        end = len(chunk)
        stack = self._stack
        while pos < end:
            if self._in_string:
                if self._escape_pending:
                    self._escape_pending = False
                    pos += 1
                    continue
                pos = _STRING_BODY.match(chunk, pos).end()  # type: ignore[union-attr]
                if pos >= end:
                    return
                if chunk[pos] == _BACKSLASH:
                    # Backslash is the last byte of the chunk; the byte it
                    # escapes arrives with the next one.
                    self._escape_pending = True
                    pos += 1
                    continue
                # Closing quote.
                pos += 1
                self._in_string = False
                if self._string_is_key:
                    self._expect = _COLON
                else:
                    self._expect = _AFTER_VALUE if stack else _DONE
                continue
            if self._in_scalar:
                pos = _SCALAR_BODY.match(chunk, pos).end()  # type: ignore[union-attr]
                if pos >= end:
                    return
                self._in_scalar = False
                self._expect = _AFTER_VALUE if stack else _DONE
                continue

            pos = _WHITESPACE.match(chunk, pos).end()  # type: ignore[union-attr]
            if pos >= end:
                return
            byte = chunk[pos]
            expect = self._expect
            if byte == _NUL:
                raise JsonBoundsError("encoding", limit=0, observed=0)
            if byte == _OPEN_OBJECT or byte == _OPEN_ARRAY:
                if expect != _VALUE and expect != _ARRAY_FIRST:
                    self.stopped = True
                    return
                self._count_value()
                if len(stack) >= self.max_depth:
                    raise JsonBoundsError("depth", limit=self.max_depth, observed=len(stack) + 1)
                if byte == _OPEN_OBJECT:
                    stack.append(1)
                    self._expect = _OBJECT_FIRST
                else:
                    stack.append(0)
                    self._expect = _ARRAY_FIRST
                pos += 1
            elif byte == _CLOSE_OBJECT or byte == _CLOSE_ARRAY:
                is_object = byte == _CLOSE_OBJECT
                first = _OBJECT_FIRST if is_object else _ARRAY_FIRST
                if (
                    not stack
                    or stack[-1] != (1 if is_object else 0)
                    or (expect != first and expect != _AFTER_VALUE)
                ):
                    self.stopped = True
                    return
                stack.pop()
                self._expect = _AFTER_VALUE if stack else _DONE
                pos += 1
            elif byte == _COMMA:
                if expect != _AFTER_VALUE or not stack:
                    self.stopped = True
                    return
                self._expect = _KEY if stack[-1] else _VALUE
                pos += 1
            elif byte == _COLON_BYTE:
                if expect != _COLON:
                    self.stopped = True
                    return
                self._expect = _VALUE
                pos += 1
            elif byte == _QUOTE:
                if expect == _OBJECT_FIRST or expect == _KEY:
                    self._string_is_key = True
                elif expect == _VALUE or expect == _ARRAY_FIRST:
                    self._string_is_key = False
                    self._count_value()
                else:
                    self.stopped = True
                    return
                self._in_string = True
                pos += 1
            else:
                if expect != _VALUE and expect != _ARRAY_FIRST:
                    self.stopped = True
                    return
                self._count_value()
                self._in_scalar = True

    def _start(self, chunk: bytes) -> int:
        """Inspect the first bytes for a byte-order mark; return the start offset."""
        self._started = True
        if chunk.startswith(_UTF8_BOM):
            # json.loads(bytes) decodes with utf-8-sig, so the parser skips it.
            return len(_UTF8_BOM)
        if chunk.startswith(_WIDE_BOMS):
            raise JsonBoundsError("encoding", limit=0, observed=0)
        return 0

    def _count_value(self) -> None:
        if not self._stack:
            return  # the root value is not a member of anything
        self.members += 1
        if self.members > self.max_members:
            raise JsonBoundsError("members", limit=self.max_members, observed=self.members)


class SseJsonBoundsScanner:
    """Apply the parse bounds to every event of a ``text/event-stream`` body.

    MCP and A2A both carry one JSON-RPC message per SSE event, in the event's
    ``data`` field. Per the WHATWG event-stream grammar the field's lines are
    joined with ``\\n`` and the event ends at a blank line, so each event is
    one document: its data lines are fed to a :class:`JsonBoundsScanner`,
    which is reset at every event boundary. Only the current field name (a
    few bytes) is buffered; the event data itself is never retained.
    """

    __slots__ = (
        "_json",
        "_field",
        "_mode",
        "_line_len",
        "_has_data",
        "_strip_space",
        "_after_cr",
        "_head",
    )

    # Field names longer than this cannot be "data"; stop buffering them.
    _MAX_FIELD = 8
    _FIELD, _DATA, _SKIP = 0, 1, 2

    def __init__(
        self,
        *,
        max_depth: int = MAX_JSON_DEPTH,
        max_members: int = MAX_JSON_MEMBERS,
    ) -> None:
        self._json = JsonBoundsScanner(max_depth=max_depth, max_members=max_members)
        self._field = b""
        self._mode = self._FIELD
        self._line_len = 0
        self._has_data = False
        self._strip_space = False
        self._after_cr = False
        # Holds the first bytes until a leading byte-order mark, which an
        # event-stream parser strips, can be recognized whole; None after.
        self._head: bytes | None = b""

    def feed(self, data: bytes) -> None:
        """Scan one decoded chunk of the stream."""
        if self._head is not None:
            data = self._head + data
            if len(data) < len(_UTF8_BOM) and _UTF8_BOM.startswith(data):
                self._head = data
                return
            self._head = None
            if data.startswith(_UTF8_BOM):
                data = data[len(_UTF8_BOM) :]
        pos = 0
        end = len(data)
        while pos < end:
            if self._after_cr:
                # A CR already ended the line; a following LF is the same
                # terminator, not an empty line.
                self._after_cr = False
                if data[pos] == 0x0A:
                    pos += 1
                    continue
            match = _LINE_END.search(data, pos)
            stop = match.start() if match is not None else end
            if stop > pos:
                self._line_part(data[pos:stop])
            if match is None:
                return
            self._end_line()
            self._after_cr = data[stop] == 0x0D
            pos = stop + 1

    def _line_part(self, part: bytes) -> None:
        self._line_len += len(part)
        if self._mode == self._SKIP:
            return
        if self._mode == self._FIELD:
            colon = part.find(b":")
            if colon == -1:
                self._field += part
                if len(self._field) > self._MAX_FIELD:
                    self._mode = self._SKIP
                    self._field = b""
                return
            name = self._field + part[:colon]
            self._field = b""
            if name != b"data":
                # Another field, or a comment (empty name).
                self._mode = self._SKIP
                return
            self._mode = self._DATA
            self._strip_space = True
            self._begin_data_line()
            part = part[colon + 1 :]
            if not part:
                return
        if self._strip_space:
            self._strip_space = False
            if part[0] == 0x20:
                part = part[1:]
        self._json.feed(part)

    def _begin_data_line(self) -> None:
        if self._has_data:
            self._json.feed(b"\n")
        self._has_data = True

    def _end_line(self) -> None:
        if self._line_len == 0:
            # Blank line: the event is dispatched, the next one starts fresh.
            if self._has_data:
                self._json.reset()
                self._has_data = False
        elif self._mode == self._FIELD and self._field == b"data":
            # `data` with no colon is a data line with an empty value.
            self._begin_data_line()
        self._field = b""
        self._mode = self._FIELD
        self._line_len = 0
        self._strip_space = False


def check_json_bounds(
    data: bytes | bytearray | memoryview | str,
    *,
    max_depth: int = MAX_JSON_DEPTH,
    max_members: int = MAX_JSON_MEMBERS,
) -> None:
    """Raise :class:`JsonBoundsError` if ``data`` exceeds the parse bounds.

    Does not parse: a document that is not JSON at all passes, and is left to
    whatever parses it next. A ``str`` is checked as its UTF-8 encoding.
    """
    if isinstance(data, str):
        raw: bytes | bytearray | memoryview = data.encode("utf-8", "surrogatepass")
    else:
        raw = data
        if _looks_wide(raw):
            raise JsonBoundsError("encoding", limit=0, observed=0)
    view = bytes(raw) if isinstance(raw, memoryview) else raw
    if _within_bounds_fast(view, max_depth=max_depth, max_members=max_members):
        return
    JsonBoundsScanner(max_depth=max_depth, max_members=max_members).feed(view)


def bounded_json_loads(
    data: bytes | bytearray | str,
    *,
    max_depth: int = MAX_JSON_DEPTH,
    max_members: int = MAX_JSON_MEMBERS,
    **loads_kwargs: Any,
) -> Any:
    """``json.loads`` that refuses an over-deep or over-wide document first.

    :raises JsonBoundsError: the document exceeds a bound; nothing was parsed.
    :raises json.JSONDecodeError: the document is within bounds but malformed.
    """
    check_json_bounds(data, max_depth=max_depth, max_members=max_members)
    return json.loads(data, **loads_kwargs)


def _within_bounds_fast(data: bytes | bytearray, *, max_depth: int, max_members: int) -> bool:
    """Cheap sufficient check for an already-buffered body.

    Every member is the first entry of a container or follows a comma, so
    openers plus commas bound the member count from above. With string tokens
    removed, the running bracket balance bounds the depth from above. Only a
    ``True`` is trusted; ``False`` (which a malformed document can also
    produce) defers to the exact scanner.
    """
    openers = data.count(b"[") + data.count(b"{")
    if openers + data.count(b",") > max_members:
        return False
    if openers <= max_depth:
        return True
    structure = _STRING_TOKEN.sub(b"", data).translate(None, _NOT_BRACKETS)
    return max(accumulate(map(_BRACKET_DELTA.__getitem__, structure)), default=0) <= max_depth


def _looks_wide(data: bytes | bytearray | memoryview) -> bool:
    """True for a short body json.loads would decode as UTF-16 or UTF-32."""
    head = bytes(data[:4])
    return head.startswith(_WIDE_BOMS) or b"\x00" in head


__all__ = [
    "DEFAULT_MAX_INBOUND_BODY_BYTES",
    "MAX_JSON_DEPTH",
    "MAX_JSON_MEMBERS",
    "JsonBoundKind",
    "JsonBoundsError",
    "JsonBoundsScanner",
    "SseJsonBoundsScanner",
    "bounded_json_loads",
    "check_json_bounds",
]
