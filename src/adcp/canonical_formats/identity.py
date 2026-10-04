"""Format identity normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from pydantic import AnyUrl

from adcp.types.legacy import FormatReferenceStructuredObject, LegacyFormatId

# Default ports per RFC 3986 §3.2.3 — stripped during canonicalization
# so ``https://x.example:443`` matches ``https://x.example``.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def canonicalize_agent_url(raw: object) -> str:
    """Return ``raw`` with scheme + host lowercased and default port stripped.

    Per ``core/format-id.json`` (normative): callers MUST canonicalize
    ``agent_url`` before comparing two ``FormatId`` values for identity.
    Pydantic's ``AnyUrl`` does trailing-slash normalization but not
    RFC 3986 §6 host-casefolding or default-port stripping.

    Non-throwing: malformed inputs round-trip as-is. Identity comparison
    should normalize what it can without turning lookup helpers into URL
    validators.
    """
    text = str(raw)
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.hostname:
        return text
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None
    # ``urlsplit().hostname`` deliberately removes the brackets around an
    # IPv6 literal. Put them back when rebuilding the authority; otherwise
    # the result is no longer a valid URL and downstream safety checks can
    # mistake a private IPv6 address for an ordinary hostname.
    authority_host = f"[{host}]" if ":" in host else host
    netloc = authority_host if port is None else f"{authority_host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def coerce_format_id(value: object) -> LegacyFormatId:
    """Return ``value`` as a ``LegacyFormatId``.

    ``value`` is a ``FormatReferenceStructuredObject``, a mapping of its
    fields, or unvalidated adopter input that ``LegacyFormatId`` judges.

    Generated model fields carry the schema class
    ``FormatReferenceStructuredObject``, whose ``agent_url`` is an ``AnyUrl``.
    ``LegacyFormatId`` narrows that field to ``str`` to preserve the wire
    spelling, so a reference taken off a model — or a mapping that model's
    ``model_dump()`` produced — needs its ``agent_url`` stringified before it
    validates. Stringifying is sufficient on its own:
    :func:`canonicalize_agent_url` runs at comparison time, where
    ``core/format-id.json`` requires it.

    Anything else validates exactly as ``LegacyFormatId.model_validate`` does.
    """
    if isinstance(value, LegacyFormatId):
        return value
    if isinstance(value, FormatReferenceStructuredObject):
        body = value.model_dump()
    elif isinstance(value, Mapping):
        body = dict(value)
    else:
        return LegacyFormatId.model_validate(value)
    agent_url = body.get("agent_url")
    if isinstance(agent_url, AnyUrl):
        body["agent_url"] = str(agent_url)
    return LegacyFormatId.model_validate(body)
