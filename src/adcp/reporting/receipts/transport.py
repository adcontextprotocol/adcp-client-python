"""Request-local lossless receipt JSON, before transport/model normalization."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from adcp.reporting.canonical_json import MAX_SAFE_INTEGER
from adcp.reporting.receipts.wire import TASK

# The standard serve() body limiter applies first. Direct mounts also bound
# this additional receipt-only capture; a larger body is rejected, never rounded.
MAX_RECEIPT_BODY_BYTES = 10 * 1024 * 1024
RAW_RECEIPT_BODY_SCOPE_KEY = "adcp.receipt_ingress.raw_body"


def receipt_body_receive(
    scope: dict[str, Any],
    receive: Callable[[], Awaitable[Any]],
    *,
    limit: int = MAX_RECEIPT_BODY_BYTES,
) -> Callable[[], Awaitable[Any]]:
    """Tee only the bytes delivered to this mounted HTTP request's decoder.

    No caller metadata, global cache, request ID lookup or auth shortcut is
    involved. The existing upstream body cap still applies first.
    """
    captured: bytearray | None = bytearray()

    async def capture() -> Any:
        nonlocal captured
        message = await receive()
        if message.get("type") == "http.request":
            chunk = message.get("body", b"")
            if captured is not None:
                if len(captured) + len(chunk) > limit:
                    captured = None
                else:
                    captured.extend(chunk)
            if not message.get("more_body", False):
                scope[RAW_RECEIPT_BODY_SCOPE_KEY] = (
                    bytes(captured) if captured is not None else None
                )
        return message

    return capture


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON object")
        result[key] = value
    return result


def _exact_integer(value: Decimal) -> int:
    """Accept protobuf's 1.0 spelling only when its *raw text* is an exact integer.

    A binary float cannot establish this: 1.000000000000000000001 has already
    rounded to 1.0 there. Decimal examines the authenticated HTTP bytes without
    rounding and bounds the value before any potentially large integer allocation.
    """
    try:
        if (
            value.is_finite()
            and value.copy_abs() <= MAX_SAFE_INTEGER
            and value == value.to_integral_value()
        ):
            return int(value)
    except (InvalidOperation, ValueError, OverflowError):
        pass
    raise ValueError("receipt JSON requires exact safe integers")


def _receipt_numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _exact_integer(value)
    if type(value) is dict:
        return {key: _receipt_numbers(item) for key, item in value.items()}
    if type(value) is list:
        return [_receipt_numbers(item) for item in value]
    return value


def _raw_number(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        # Preserve route identification even for an unrepresentable exponent.
        # This sentinel can only produce an invalid empty request below; it is
        # never handed to a handler as an accepted numeric value.
        return Decimal("NaN")


def _raw_json(body: str | bytes) -> Any:
    return json.loads(
        body,
        object_pairs_hook=_unique_object,
        parse_float=_raw_number,
        parse_constant=_raw_number,
    )


def _parameters(params: dict[str, Any], task: str) -> dict[str, Any]:
    if task == "get_reporting_status":
        from adcp.reporting.feed.request import transport_parameters

        return transport_parameters(params)
    return dict(_receipt_numbers(params))


def _a2a_receipt_invocation(body: bytes | None, *, task: str = TASK) -> dict[str, Any] | None:
    try:
        if body is None:
            return None
        envelope = _raw_json(body)
        if (
            type(envelope) is not dict
            or envelope.get("jsonrpc") != "2.0"
            or envelope.get("method")
            not in {"message/send", "message/stream", "SendMessage", "SendStreamingMessage"}
        ):
            return None
        parts = envelope["params"]["message"]["parts"]
        invocations = []
        for part in parts:
            if type(part) is not dict:
                return None
            data = part.get("data")
            if data is None and isinstance(part.get("text"), str):
                try:
                    data = _raw_json(part["text"])
                except ValueError:
                    continue
            if type(data) is dict and data.get("skill"):
                invocations.append(data)
        if len(invocations) == 1 and invocations[0]["skill"] == task:
            return dict(invocations[0])
    except (ValueError, TypeError, KeyError, RecursionError):
        pass
    return None


def a2a_receipt_parameters(body: bytes | None, *, task: str = TASK) -> dict[str, Any] | None:
    """Distinguish an exact receipt route from invalid receipt parameters.

    None means no uniquely identified standard invocation. An empty dictionary
    means that invocation's parameters are invalid and must reach the ordinary
    whole-shape rejection. Neither case can authorize a domain write.
    """
    invocation = _a2a_receipt_invocation(body, task=task)
    if invocation is None:
        return None
    try:
        params = invocation.get("parameters")
        if type(params) is dict:
            return _parameters(params, task)
    except (ValueError, TypeError, RecursionError):
        pass
    return {}


def a2a_receipt_has_invalid_unicode(body: bytes | None) -> bool:
    """Scope the upstream protobuf codec error boundary to this exact raw route."""
    if body is None or _a2a_receipt_invocation(body) is None:
        return False
    try:
        pending = [_raw_json(body)]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                return True
            if type(value) is dict:
                pending.extend(value)
                pending.extend(value.values())
            elif type(value) is list:
                pending.extend(value)
    except (ValueError, TypeError, RecursionError):
        pass
    return False


def mcp_receipt_parameters(body: bytes | None, *, task: str = TASK) -> dict[str, Any]:
    """The already selected tools/call must match this exact receipt invocation."""
    try:
        if body is None:
            return {}
        envelope = _raw_json(body)
        if (
            type(envelope) is dict
            and envelope.get("jsonrpc") == "2.0"
            and envelope.get("method") == "tools/call"
            and envelope["params"]["name"] == task
            and type(envelope["params"].get("arguments")) is dict
        ):
            return _parameters(envelope["params"]["arguments"], task)
    except (ValueError, TypeError, KeyError, RecursionError):
        pass
    return {}
