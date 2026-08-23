"""Request-body size cap middleware — closes #239.

ASGI middleware that rejects HTTP requests whose body exceeds a
configurable byte cap. Without this guard, every MCP/A2A tool call
reaches ``Pydantic.model_validate`` with no input size bound, letting
a single attacker send arbitrarily large JSON and exhaust CPU/memory
at the validation step. (PR #238 security review flagged this when
typed-dispatch made per-request validation unconditional.)

Installed once at server bind time — before FastMCP or the a2a-sdk
``Starlette`` app handle the request — so oversized bodies are cut
off at the ASGI boundary, well before any parser allocates for them.

.. note::
   **What this guard does NOT bound.** The middleware caps bytes per
   request, not duration. A slow-loris caller sending 1 byte every 30
   seconds stays under the cap forever while tying up a worker.
   Bound duration at the layer above — uvicorn ``--timeout-keep-alive``
   and a reverse-proxy read timeout (nginx ``client_body_timeout``,
   Envoy ``request_timeout``) are the standard levers. Docs at
   ``docs/handler-authoring.md`` spell this out alongside the size cap.

   **Memory budgeting.** The middleware buffers the full body up to
   the cap before replaying to the inner app (simpler than streaming
   passthrough, and the cap bounds the allocation). Operators sizing
   worker counts should budget roughly ``workers × concurrency ×
   max_request_size`` of RSS. Upstream proxies enforcing a smaller
   per-connection cap reduce this ceiling for hostile-tenant
   deployments.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

# 10 MB — generous enough for realistic AdCP payloads (multi-package
# create_media_buy with embedded creatives/assets can run ~1–2 MB) but
# small enough that adversarial traffic can't trivially exhaust a
# single-worker server. Sellers who legitimately need more override via
# ``serve(..., max_request_size=N)``.
DEFAULT_MAX_REQUEST_BYTES: int = 10 * 1024 * 1024


def make_replay_receive(chunks: Sequence[bytes]) -> Callable[[], Any]:
    """Return an ASGI ``receive`` callable that replays buffered body chunks.

    Once the body is drained, subsequent reads return ``http.disconnect``.
    This is important for streaming applications, which may keep polling the
    receive channel while producing a response.
    """
    index = 0
    chunks_count = len(chunks)

    async def replay_receive() -> dict[str, Any]:
        nonlocal index
        if index < chunks_count:
            body = chunks[index]
            index += 1
            return {
                "type": "http.request",
                "body": body,
                "more_body": index < chunks_count,
            }
        return {"type": "http.disconnect"}

    return replay_receive


class RequestSizeLimitMiddleware:
    """Reject HTTP requests whose body exceeds ``max_bytes``.

    Two layers:

    1. **Content-Length fast-fail.** If the client advertises a body
       bigger than the cap, we reject before reading a single byte.
    2. **Streaming accounting.** For chunked transfers (``Transfer-
       Encoding: chunked`` with no Content-Length) the middleware
       buffers and counts bytes as they arrive; when the total crosses
       the cap it stops reading and returns ``413 Payload Too Large``.

    GET / HEAD / OPTIONS bypass both — they don't carry request bodies
    in any spec we talk to.

    The response payload is deliberately minimal. AdCP has no transport
    error shape for oversized requests (errors/recovery are for
    application-layer failures), so we return a plain HTTP 413 —
    the standard shape every HTTP client understands.
    """

    def __init__(self, app: Any, max_bytes: int = DEFAULT_MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        # Pattern 1 — Content-Length fast-fail.
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    # Malformed header; fall through to the streaming
                    # check. RFC 9110 §8.6 allows rejecting with 400,
                    # but the cap governs regardless of how the client
                    # framed the request.
                    logger.debug(
                        "malformed Content-Length header (%r); falling through to streaming check",
                        value,
                    )
                    break
                if declared > self.max_bytes:
                    await self._send_413(send, self.max_bytes)
                    return
                break

        # Pattern 2 — buffer + count. We read the whole body (up to the
        # cap) before handing it to the inner app. Buffering is fine
        # because (a) we're bounding the buffer size by the cap, and
        # (b) ASGI apps downstream (FastMCP, a2a-sdk) already read the
        # full body into memory before parsing — we're not adding a new
        # RAM cost, we're enforcing one that already exists.
        chunks: list[bytes] = []
        total = 0
        more_body = True
        while more_body:
            msg = await receive()
            msg_type = msg.get("type")
            if msg_type == "http.disconnect":
                # Client gave up before sending the full body. The
                # inner app hasn't been invoked yet, so there's nothing
                # to unwind — just drop the request on the floor
                # silently. If the app were invoked earlier, we'd need
                # to propagate the disconnect; it isn't, so we don't.
                return
            if msg_type != "http.request":
                # ASGI http scope only defines http.request and
                # http.disconnect (spec §HTTP). Anything else is a
                # protocol bug upstream. Skip defensively — don't
                # truncate the body to an empty chunk as a silent
                # fallback.
                logger.debug(
                    "unexpected ASGI message type %r in http scope; skipping",
                    msg_type,
                )
                continue
            body = msg.get("body", b"")
            total += len(body)
            if total > self.max_bytes:
                await self._send_413(send, self.max_bytes)
                return
            chunks.append(body)
            more_body = bool(msg.get("more_body", False))

        # Body fit within the cap — replay to the app.
        await self.app(scope, make_replay_receive(chunks), send)

    @staticmethod
    async def _send_413(send: Any, max_bytes: int) -> None:
        """Emit an HTTP 413 Payload Too Large response.

        Includes the cap in the body so legitimate clients know the
        limit they hit — the cap isn't a secret (documented SDK
        default + deployment config), and a bare "too large" without
        a number forces adopters to grep the source or docs.
        """
        body = f"Payload too large. Maximum request body size is {max_bytes} bytes.\n".encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )
