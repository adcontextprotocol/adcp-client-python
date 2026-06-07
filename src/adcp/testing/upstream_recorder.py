"""Reference upstream-traffic recorder for ``query_upstream_traffic`` conformance.

The AdCP ``query_upstream_traffic`` scenario asks a seller to report the
outbound HTTP calls it made while servicing a buyer's request — the
mechanism that raises the bar against façade adapters that satisfy AdCP
schema requirements with synthetic placeholders without forwarding data
upstream (see the ``UpstreamTrafficSuccess`` branch of
``comply-test-controller-response.json``).

:class:`UpstreamRecorder` is a test-only helper adopters can wire into
their httpx-based upstream client so a ``comply_test_controller``
handler can answer ``query_upstream_traffic`` with a spec-shaped
``recorded_calls`` array.

Primary adapter — httpx async event hooks::

    recorder = UpstreamRecorder()
    async with recorder.principal_scope("agent.example.com"):
        client = httpx.AsyncClient(event_hooks=recorder.httpx_hooks())
        await client.post("https://api.example.com/v2/audience/upload", json=...)

    result = recorder.query(principal="agent.example.com")
    response = result.to_response_dict()  # shape for query_upstream_traffic

Secondary clients (``requests`` / ``aiohttp``) have no native httpx-style
hook surface; adopters call :meth:`UpstreamRecorder.record` directly from
their own client wrapper as a manual escape hatch.

Spec obligations this recorder implements:

* **Caller scoping.** Records are keyed on the principal bound via
  :meth:`principal_scope` / :meth:`run_with_principal`. :meth:`query`
  requires a principal and returns only that principal's calls —
  cross-caller traffic is never returned regardless of ``since_timestamp``.
* **Secret redaction.** Values at keys matching the normative
  case-insensitive :data:`SECRET_KEY_PATTERN` are replaced with
  ``[redacted]`` at record time across the three surfaces that reach the
  wire: decoded JSON request bodies (recursively), form-urlencoded
  request bodies (by key), and URL query parameters (by key, with the
  presigned-signature params covered by :data:`URL_QUERY_SECRET_PATTERN`).
* **Fail-closed scoping.** Recording outside any principal scope drops
  the call (it is unattributable); an empty / non-string principal raises
  :class:`UpstreamRecorderScopeError` rather than silently matching nothing.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping

    import httpx

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

Purpose = Literal[
    "platform_primary",
    "measurement",
    "attribution",
    "creative_serving",
    "identity",
    "other",
]

# Normative redaction pattern from the UpstreamTrafficSuccess branch of
# comply-test-controller-response.json. Adopters MUST recursively redact
# values at keys matching this case-insensitive pattern before emission.
SECRET_KEY_PATTERN = re.compile(
    r"^(authorization|credentials?|token|api[_-]?key|password|secret|"
    r"client[_-]secret|refresh[_-]token|access[_-]token|bearer|"
    r"session[_-]token|offering[_-]token|cookie|set[_-]cookie)$",
    re.IGNORECASE,
)

REDACTED = "[redacted]"

# Presigned-URL signature params live only in query strings and are not
# covered by the normative body pattern (extending the body pattern would
# weaken its semantics). These are matched only against URL query-param keys.
URL_QUERY_SECRET_PATTERN = re.compile(
    r"^(x-amz-signature|signature|x-goog-signature)$",
    re.IGNORECASE,
)

# Adopters SHOULD cap individual payload size at 64 KiB; payloads exceeding
# that SHOULD be truncated with a trailing marker (per the payload field).
DEFAULT_MAX_PAYLOAD_BYTES = 65536
_TRUNCATION_MARKER = "[…truncated]"

# ContextVar carries the principal across ``await`` boundaries within a
# principal scope. None means "no scope bound" — calls recorded there are
# unattributable and dropped (fail-closed).
_principal_var: ContextVar[str | None] = ContextVar("adcp_upstream_principal", default=None)


class UpstreamRecorderScopeError(RuntimeError):
    """Raised when a principal scope is opened or queried with a bad principal.

    A principal must be a non-empty string. Binding or querying with an
    empty / non-string principal is a wiring bug, not a no-match — failing
    closed prevents traffic from silently leaking into an unqueryable or
    globally readable bucket.
    """


def _require_principal(principal: Any) -> str:
    if not isinstance(principal, str) or not principal:
        raise UpstreamRecorderScopeError(f"principal must be a non-empty string, got {principal!r}")
    return principal


def _redact(value: Any) -> Any:
    """Recursively replace secret-keyed values with ``[redacted]``.

    Walks dicts and lists. A dict value is redacted when its key matches
    :data:`SECRET_KEY_PATTERN`; otherwise the value is recursed into.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, sub in value.items():
            if isinstance(key, str) and SECRET_KEY_PATTERN.match(key):
                out[key] = REDACTED
            else:
                out[key] = _redact(sub)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _query_key_is_secret(key: str) -> bool:
    """A query-param key is secret if it matches the body pattern or a signature param."""
    return bool(SECRET_KEY_PATTERN.match(key) or URL_QUERY_SECRET_PATTERN.match(key))


def _redact_url(url: str) -> str:
    """Redact secret-keyed query-param values, preserving everything else.

    Presigned-URL tokens (``X-Amz-Signature``), ``?api_key=`` and
    ``?access_token=`` query auth, and any other secret-keyed param land on
    the wire via both ``url`` and ``endpoint``; redact at the source.
    """
    split = urlsplit(url)
    if not split.query:
        return url
    pairs = parse_qsl(split.query, keep_blank_values=True)
    if not any(_query_key_is_secret(key) for key, _ in pairs):
        return url
    redacted = [(key, REDACTED if _query_key_is_secret(key) else val) for key, val in pairs]
    return urlunsplit(split._replace(query=urlencode(redacted)))


def _redact_form_body(text: str) -> str:
    """Key-redact a form-urlencoded body, re-encoding the result."""
    pairs = parse_qsl(text, keep_blank_values=True)
    redacted = [(key, REDACTED if SECRET_KEY_PATTERN.match(key) else val) for key, val in pairs]
    return urlencode(redacted)


@dataclass(frozen=True)
class RecordedCall:
    """One outbound HTTP call captured by :class:`UpstreamRecorder`.

    Field names mirror the ``recorded_calls[]`` item shape of the
    ``UpstreamTrafficSuccess`` response so :meth:`to_recorded_call_dict`
    is a near-identity projection. This reference recorder emits
    ``attestation_mode="raw"`` — the load-bearing assertion target for
    ``payload_must_contain`` — and leaves digest mode to adopters whose
    privacy policy requires it.
    """

    method: HttpMethod
    url: str
    host: str
    path: str
    content_type: str
    payload: Any
    payload_length: int
    timestamp: datetime
    status_code: int | None = None
    purpose: Purpose | None = None
    principal: str = ""

    @property
    def endpoint(self) -> str:
        """Composed ``<METHOD> <URL>`` string for ``endpoint_pattern`` matching."""
        return f"{self.method} {self.url}"

    def to_recorded_call_dict(self) -> dict[str, Any]:
        """Project to a ``recorded_calls[]`` item (raw attestation)."""
        item: dict[str, Any] = {
            "method": self.method,
            "endpoint": self.endpoint,
            "url": self.url,
            "host": self.host,
            "path": self.path,
            "content_type": self.content_type,
            "attestation_mode": "raw",
            "payload": self.payload,
            "payload_length": self.payload_length,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.status_code is not None:
            item["status_code"] = self.status_code
        if self.purpose is not None:
            item["purpose"] = self.purpose
        return item


@dataclass(frozen=True)
class UpstreamTrafficResult:
    """Outcome of :meth:`UpstreamRecorder.query`, shaped for the wire response."""

    recorded_calls: tuple[RecordedCall, ...]
    total_count: int
    since_timestamp: datetime
    truncated: bool

    def to_response_dict(self) -> dict[str, Any]:
        """Project to an ``UpstreamTrafficSuccess`` payload.

        The ``success`` / ``recorded_calls`` / ``total_count`` /
        ``since_timestamp`` fields are required by the response schema; a
        ``comply_test_controller`` handler returns this directly.
        """
        return {
            "success": True,
            "recorded_calls": [call.to_recorded_call_dict() for call in self.recorded_calls],
            "total_count": self.total_count,
            "truncated": self.truncated,
            "since_timestamp": self.since_timestamp.isoformat(),
        }


def _compile_endpoint_pattern(pattern: str) -> re.Pattern[str]:
    """Compile an ``endpoint_pattern`` to an anchored regex.

    Normative grammar: ``*`` matches zero or more characters of any kind
    (including ``/``); every other character is literal. Patterns are
    anchored (full-string match, not substring).
    """
    parts = [re.escape(seg) for seg in pattern.split("*")]
    return re.compile("^" + ".*".join(parts) + "$")


class UpstreamRecorder:
    """Records principal-scoped outbound HTTP traffic for conformance tests.

    Bind a principal with :meth:`principal_scope` (sync) or
    :meth:`run_with_principal` (async), make httpx calls through a client
    wired with :meth:`httpx_hooks`, then :meth:`query` the buffer.

    Args:
        max_payload_bytes: Per-call payload cap. Payloads whose UTF-8
            length exceeds this are truncated with a trailing marker.
            Defaults to 64 KiB per the spec recommendation.
        purpose: Optional classifier called with each :class:`RecordedCall`
            (pre-purpose) to tag the call's semantic role. Calls without a
            purpose are treated as ``other`` by ``purpose_filter`` matching.
    """

    def __init__(
        self,
        *,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        purpose: Callable[[RecordedCall], Purpose | None] | None = None,
    ) -> None:
        self._max_payload_bytes = max_payload_bytes
        self._purpose = purpose
        # Principal-keyed buffer; multi-tenant sandboxes MUST key on the
        # invocation's auth principal, not just process-global.
        self._buffer: dict[str, list[RecordedCall]] = {}

    # -- principal scoping ------------------------------------------------

    @contextmanager
    def principal_scope(self, principal: str) -> Iterator[None]:
        """Bind ``principal`` for the duration of the ``with`` block.

        Calls recorded inside the block are attributed to ``principal``.
        Raises :class:`UpstreamRecorderScopeError` if ``principal`` is
        empty or not a string.
        """
        token = _principal_var.set(_require_principal(principal))
        try:
            yield
        finally:
            _principal_var.reset(token)

    @asynccontextmanager
    async def principal_scope_async(self, principal: str) -> AsyncIterator[None]:
        """Async sibling of :meth:`principal_scope`.

        The ContextVar binding survives ``await`` boundaries within the
        block. Note that ``asyncio.create_task`` copies the current
        context at spawn time, so tasks spawned inside the block inherit
        the principal; tasks spawned outside it record under no scope and
        are dropped (fail-closed).
        """
        token = _principal_var.set(_require_principal(principal))
        try:
            yield
        finally:
            _principal_var.reset(token)

    async def run_with_principal(self, principal: str, coro: Awaitable[Any]) -> Any:
        """Await ``coro`` with ``principal`` bound for its duration."""
        async with self.principal_scope_async(principal):
            return await coro

    # -- recording --------------------------------------------------------

    def record(
        self,
        *,
        method: str,
        url: str,
        content_type: str,
        payload: Any = None,
        headers: Mapping[str, str] | None = None,
        status_code: int | None = None,
        timestamp: datetime | None = None,
        principal: str | None = None,
    ) -> RecordedCall | None:
        """Record one outbound call. Manual escape hatch for non-httpx clients.

        Returns the stored :class:`RecordedCall`, or ``None`` when no
        principal is bound and none is supplied — an unattributable call is
        dropped rather than recorded globally.

        Redaction is applied here, at record time, across the surfaces that
        reach the wire: secret-keyed query-param values in ``url`` (which
        also feeds ``endpoint``) and secret-keyed values in ``payload``
        (JSON and form-urlencoded bodies) are replaced with ``[redacted]``
        before storage. The ``headers`` argument carries no header to the
        wire (the recorded-call shape has no header field) and is unused.
        """
        bound = principal if principal is not None else _principal_var.get()
        if not bound:
            return None
        bound = _require_principal(bound)

        redacted_url = _redact_url(url)
        split = urlsplit(redacted_url)
        redacted_payload, payload_length = self._normalize_payload(payload, content_type)

        call = RecordedCall(
            method=method.upper(),  # type: ignore[arg-type]
            url=redacted_url,
            host=split.hostname or "",
            path=split.path,
            content_type=content_type,
            payload=redacted_payload,
            payload_length=payload_length,
            timestamp=timestamp or datetime.now(timezone.utc),
            status_code=status_code,
            principal=bound,
        )
        if self._purpose is not None:
            purpose = self._purpose(call)
            if purpose is not None:
                call = replace(call, purpose=purpose)
        self._buffer.setdefault(bound, []).append(call)
        return call

    def _normalize_payload(self, payload: Any, content_type: str) -> tuple[Any, int]:
        """Redact and size a payload; return ``(redacted_payload, byte_length)``.

        JSON-shaped bodies are decoded, recursively redacted, and re-encoded.
        Form-urlencoded bodies are parsed, key-redacted, and re-encoded.
        Other bodies are coerced to a string and emitted as-is (no structure
        to walk). ``payload_length`` is the UTF-8 byte length of the *emitted*
        value — after redaction and any truncation — matching the spec's
        raw-mode definition (``payload_length`` MUST equal the emitted bytes).
        """
        if payload is None:
            return None, 0

        ct = content_type.lower()
        is_json = "json" in ct
        is_form = "application/x-www-form-urlencoded" in ct

        decoded: Any = payload
        if isinstance(payload, (bytes, bytearray)):
            text = payload.decode("utf-8", errors="replace")
            if is_json:
                try:
                    decoded = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    decoded = text
            else:
                decoded = text
        elif isinstance(payload, str) and is_json:
            try:
                decoded = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                decoded = payload

        if isinstance(decoded, (dict, list)):
            redacted = _redact(decoded)
            measured = json.dumps(redacted, separators=(",", ":")).encode("utf-8")
            if len(measured) > self._max_payload_bytes:
                # Object payloads can't carry an inline truncation marker
                # without breaking JSON; truncate the serialized form to a
                # string and report the emitted (truncated) byte length.
                return self._truncate_string(measured.decode("utf-8", errors="ignore"))
            return redacted, len(measured)

        text = decoded if isinstance(decoded, str) else str(decoded)
        if is_form:
            text = _redact_form_body(text)
        encoded = text.encode("utf-8")
        if len(encoded) > self._max_payload_bytes:
            return self._truncate_string(text)
        return text, len(encoded)

    def _truncate_string(self, text: str) -> tuple[str, int]:
        """Truncate ``text`` to fit within ``max_payload_bytes`` including marker.

        Reserves room for ``_TRUNCATION_MARKER`` so the emitted string stays
        within the schema's ``payload`` ``maxLength``, and reports the UTF-8
        byte length of the emitted (truncated) value per the raw-mode contract.
        """
        marker_bytes = len(_TRUNCATION_MARKER.encode("utf-8"))
        budget = max(self._max_payload_bytes - marker_bytes, 0)
        head = text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        emitted = head + _TRUNCATION_MARKER
        return emitted, len(emitted.encode("utf-8"))

    # -- httpx integration ------------------------------------------------

    def httpx_hooks(self) -> dict[str, list[Callable[..., Awaitable[None]]]]:
        """Return ``event_hooks`` for an :class:`httpx.AsyncClient`.

        Pass to ``httpx.AsyncClient(event_hooks=recorder.httpx_hooks())``.
        The response hook records the call once the status code is known,
        so each call is captured exactly once with its ``status_code``.
        """
        return {"response": [self._on_response]}

    async def _on_response(self, response: httpx.Response) -> None:
        import httpx

        request = response.request
        content_type = request.headers.get("content-type", "")
        # httpx exposes the request body bytes via `request.content`; a
        # streaming request body (generator / async-iterator) has not been
        # read and raises RequestNotRead — there is nothing to record.
        try:
            payload: Any = bytes(request.content) if request.content else None
        except httpx.RequestNotRead:
            payload = None
        self.record(
            method=request.method,
            url=str(request.url),
            content_type=content_type,
            payload=payload,
            headers=request.headers,
            status_code=response.status_code,
        )

    # -- querying ---------------------------------------------------------

    def query(
        self,
        *,
        principal: str,
        since_timestamp: datetime | None = None,
        endpoint_pattern: str | None = None,
        limit: int = 100,
    ) -> UpstreamTrafficResult:
        """Return calls recorded for ``principal``, scoped and ordered.

        Caller scoping is enforced: only ``principal``'s calls are
        considered — cross-caller traffic is never returned. Results are
        ordered by ``timestamp`` ascending and capped at ``limit``;
        ``total_count`` / ``truncated`` reflect the full match set before
        the cap.

        Args:
            principal: Authenticated caller identity. Required and validated
                — derive it from the ``comply_test_controller`` invocation's
                auth context, never from caller-supplied request params.
            since_timestamp: Only return calls at or after this time.
                ``None`` returns the whole buffer (session start).
            endpoint_pattern: Optional ``<METHOD> <URL>`` glob. ``*`` matches
                any run of characters; anchored full-string match.
            limit: Maximum calls to return (default 100).
        """
        principal = _require_principal(principal)
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")

        calls = self._buffer.get(principal, [])
        matched = [
            call for call in calls if (since_timestamp is None or call.timestamp >= since_timestamp)
        ]
        if endpoint_pattern is not None:
            regex = _compile_endpoint_pattern(endpoint_pattern)
            matched = [call for call in matched if regex.match(call.endpoint)]

        matched.sort(key=lambda c: c.timestamp)
        total = len(matched)
        page = tuple(matched[:limit])
        effective_since = since_timestamp or (
            page[0].timestamp if page else datetime.now(timezone.utc)
        )
        return UpstreamTrafficResult(
            recorded_calls=page,
            total_count=total,
            since_timestamp=effective_since,
            truncated=total > len(page),
        )

    # -- introspection ----------------------------------------------------

    def debug(self) -> dict[str, int]:
        """Return per-principal call counts for test introspection."""
        return {principal: len(calls) for principal, calls in self._buffer.items()}

    def clear(self) -> None:
        """Drop all recorded calls."""
        self._buffer.clear()


__all__ = [
    "SECRET_KEY_PATTERN",
    "URL_QUERY_SECRET_PATTERN",
    "RecordedCall",
    "UpstreamRecorder",
    "UpstreamRecorderScopeError",
    "UpstreamTrafficResult",
]
