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


#: The two spellings a product format declaration uses for its v1 refs, in the
#: order they are probed. They are disjoint: no type in this package declares
#: both, so the first hit is the only hit.
_V1_REF_SPELLINGS = ("v1_format_ref", "legacy_format_refs")


def declared_v1_format_refs(declaration: object) -> tuple[LegacyFormatId, ...]:
    """Return the v1 refs *declaration* asserts, in whichever spelling it uses.

    Two types in this package are called a product format declaration and they
    name this field differently:

    * the sixteen generated ``ProductFormatDeclaration`` arms declare
      ``v1_format_ref`` as a real field, which is what
      ``domains.core.product.Product.format_options`` holds;
    * ``canonical_creative.Format`` exposes ``legacy_format_refs``, a property
      over private state populated from ``v1_format_ref`` at construction.

    Reading either spelling directly raises ``AttributeError`` on the other
    type, so every reader goes through here. A declaration that asserts no refs
    returns an empty tuple; one that is not a declaration at all raises
    ``TypeError``, because the case worth refusing loudly is the type nobody
    meant to pass — a ``getattr`` default would answer "asserts no v1 refs",
    which is a wrong answer with no signal rather than a crash.
    """
    for name in _V1_REF_SPELLINGS:
        if not hasattr(declaration, name):
            continue
        refs = getattr(declaration, name)
        return tuple(coerce_format_id(ref) for ref in refs) if refs else ()
    raise TypeError(
        f"{type(declaration).__name__} declares neither "
        f"{' nor '.join(_V1_REF_SPELLINGS)}; it is not a product format declaration"
    )
