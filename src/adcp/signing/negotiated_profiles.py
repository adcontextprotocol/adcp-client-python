"""Bind negotiated signing profiles to exact, actively dispatched requests.

MCP sends from a long-lived writer task, so caller ContextVars cannot carry a
per-call profile to its HTTP hook. This per-client registry crosses that task
boundary without trusting arbitrary version fields from an HTTP body.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from adcp.signing._strict_json import StrictJsonError, parse_strict_json
from adcp.signing.autosign import SigningProfileVersion, signing_profile_for_adcp_version


def _fingerprint(tool: str, params: dict[str, Any]) -> bytes:
    encoded = json.dumps([tool, params], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).digest()


def _invocation(body: bytes) -> tuple[str, dict[str, Any]] | None:
    try:
        payload = parse_strict_json(body)
    except StrictJsonError:
        return None
    if not isinstance(payload, dict):
        return None
    params = payload.get("params")
    if payload.get("method") == "tools/call" and isinstance(params, dict):
        tool, arguments = params.get("name"), params.get("arguments")
        if isinstance(tool, str) and isinstance(arguments, dict):
            return tool, arguments
        return None
    # A2A v1 REST uses a root message; its JSON-RPC compatibility transport
    # wraps the same message in params. DataParts keep the same data object.
    message = payload.get("message")
    if message is None and isinstance(params, dict):
        message = params.get("message")
    if not isinstance(message, dict):
        return None
    parts = message.get("parts")
    if not isinstance(parts, list):
        return None
    invocations = []
    for part in parts:
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str) and text.startswith("Execute tool: "):
            header, separator, arguments_text = text.partition("\nParameters: ")
            if separator:
                try:
                    arguments = parse_strict_json(arguments_text.encode("utf-8"))
                except StrictJsonError:
                    continue
                if isinstance(arguments, dict):
                    invocations.append((header.removeprefix("Execute tool: "), arguments))
            continue
        data = part.get("data") if isinstance(part, dict) else None
        if not isinstance(data, dict):
            continue
        tool, arguments = data.get("skill"), data.get("parameters")
        if isinstance(tool, str) and isinstance(arguments, dict):
            invocations.append((tool, arguments))
    return invocations[0] if len(invocations) == 1 else None


class NegotiatedSigningProfiles:
    """An owning client's currently dispatched parameter fingerprints.

    Bind only parameters emitted by that client's negotiated converter, after
    idempotency injection. ``resolve`` never derives a profile from a body's
    version: the body must match a registered invocation exactly. Registrations
    are reference counted so equal concurrent calls cannot erase each other.
    """

    def __init__(self) -> None:
        self._profiles: dict[bytes, tuple[SigningProfileVersion, int]] = {}

    @contextmanager
    def bind(self, tool: str, params: dict[str, Any], version: str | None) -> Iterator[None]:
        if version is None or not version.startswith("3."):
            yield
            return
        profile = signing_profile_for_adcp_version(version)
        key = _fingerprint(tool, params)
        existing, count = self._profiles.get(key, (profile, 0))
        if existing != profile:
            raise RuntimeError("Conflicting signing profiles for an identical outbound request")
        self._profiles[key] = (profile, count + 1)
        try:
            yield
        finally:
            _, remaining = self._profiles[key]
            if remaining == 1:
                del self._profiles[key]
            else:
                self._profiles[key] = (profile, remaining - 1)

    def resolve(self, body: bytes) -> SigningProfileVersion | None:
        invocation = _invocation(body)
        if invocation is None:
            return None
        entry = self._profiles.get(_fingerprint(*invocation))
        return entry[0] if entry is not None else None
