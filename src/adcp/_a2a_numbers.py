"""Keep JSON integers integral across the A2A protobuf ``Struct`` boundary.

A2A carries skill parameters and results in ``Part.data``, a protobuf
``google.protobuf.Value``. ``Struct`` stores every JSON number as a double,
and the Python protobuf JSON printer writes a double back out as ``7.0``.
Without correction a buyer's ``{"count": 7}`` reaches the handler as
``7.0`` and leaves the A2A response as ``7.0``, while MCP, which never
round-trips through protobuf, keeps ``7``. AdCP requires ``context`` to be
echoed unchanged, and decoders that bind JSON integers to integer types
(Go, Rust, Java) reject ``7.0``.

Both directions apply the same rule: a finite double with no fractional
part, within the IEEE 754 exactly-representable integer range, is written
as an integer. A ``Struct`` cannot tell ``7`` from ``7.0`` -- both arrive as
the same double -- so a buyer's ``7.0`` comes back as ``7``. Both spellings
are the same JSON number. Beyond 2**53 a double cannot say which integer the
sender wrote, so those values stay doubles; ``Struct`` already lost the exact
value before the SDK saw it.
"""

from __future__ import annotations

import json
from typing import Any

#: Largest magnitude at which every integer has an exact double.
_MAX_EXACT_INTEGER = 2**53


def _restore(value: Any) -> tuple[Any, bool]:
    if type(value) is float:
        if value.is_integer() and abs(value) <= _MAX_EXACT_INTEGER:
            return int(value), True
        return value, False
    if type(value) is dict:
        changed = False
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key], item_changed = _restore(item)
            changed = changed or item_changed
        return (out, True) if changed else (value, False)
    if type(value) is list:
        changed = False
        items: list[Any] = []
        for item in value:
            restored, item_changed = _restore(item)
            items.append(restored)
            changed = changed or item_changed
        return (items, True) if changed else (value, False)
    return value, False


def restore_struct_integers(value: Any) -> Any:
    """Return ``value`` with integral ``Struct`` doubles written as ``int``."""
    return _restore(value)[0]


def _rewrite_json(payload: bytes, **dumps_kwargs: Any) -> bytes:
    try:
        decoded = json.loads(payload)
    except ValueError:
        return payload
    restored, changed = _restore(decoded)
    if not changed:
        return payload
    return json.dumps(restored, **dumps_kwargs).encode("utf-8")


# Starlette's ``JSONResponse.render`` settings, so only the numbers change.
_JSON_RESPONSE_DUMPS: dict[str, Any] = {
    "ensure_ascii": False,
    "allow_nan": False,
    "indent": None,
    "separators": (",", ":"),
}


def _rewrite_sse(chunk: bytes) -> bytes:
    lines = chunk.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith(b"data:"):
            continue
        body = line[len(b"data:") :]
        stripped = body.rstrip(b"\r\n")
        ending = body[len(stripped) :]
        lead = b" " if stripped.startswith(b" ") else b""
        rewritten = _rewrite_json(stripped[len(lead) :])
        lines[index] = b"data:" + lead + rewritten + ending
    return b"".join(lines)


def _route_path(scope: Any) -> str:
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root) :]
    return path or "/"


def _content_type(message: Any) -> bytes:
    for name, value in message.get("headers", ()):
        if name.lower() == b"content-type":
            return bytes(value).split(b";", 1)[0].strip().lower()
    return b""


class A2AStructNumberMiddleware:
    """Write integral ``Struct`` doubles as JSON integers in A2A JSON-RPC responses.

    Covers the JSON body of ``message/send`` / ``SendMessage`` / ``tasks/get``
    and every ``data:`` event of a streaming response. a2a-sdk prints each
    protobuf response with ``MessageToDict``, so this is the one place every
    JSON-RPC result passes through after the last ``Struct`` round trip.
    """

    def __init__(self, app: Any, *, rpc_path: str = "/") -> None:
        self.app = app
        self.rpc_path = rpc_path

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or _route_path(scope) != self.rpc_path
        ):
            await self.app(scope, receive, send)
            return

        start: Any = None
        mode = ""
        body = bytearray()
        pending = b""

        async def rewrite_send(message: Any) -> None:
            nonlocal start, mode, pending
            if message["type"] == "http.response.start":
                content_type = _content_type(message)
                if content_type == b"application/json":
                    # Hold the start until the body is final: the rewrite
                    # changes Content-Length.
                    mode, start = "json", message
                    return
                if content_type == b"text/event-stream":
                    mode = "sse"
                await send(message)
                return
            if message["type"] != "http.response.body" or not mode:
                await send(message)
                return
            more_body = message.get("more_body", False)
            if mode == "json":
                body.extend(message.get("body", b""))
                if more_body:
                    return
                rewritten = _rewrite_json(bytes(body), **_JSON_RESPONSE_DUMPS)
                headers = [
                    (name, value)
                    for name, value in start.get("headers", ())
                    if name.lower() != b"content-length"
                ]
                headers.append((b"content-length", str(len(rewritten)).encode("ascii")))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": rewritten})
                return
            # SSE: rewrite whole lines only; carry a partial line forward.
            data = pending + message.get("body", b"")
            if more_body:
                cut = data.rfind(b"\n") + 1
                data, pending = data[:cut], data[cut:]
            else:
                pending = b""
            await send({**message, "body": _rewrite_sse(data)})

        await self.app(scope, receive, rewrite_send)
