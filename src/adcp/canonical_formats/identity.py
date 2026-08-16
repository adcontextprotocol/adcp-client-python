"""Format identity normalization helpers."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

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
