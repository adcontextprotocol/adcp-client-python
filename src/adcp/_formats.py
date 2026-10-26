"""Strict wire-format checks shared by schema validation and the wire types.

The bundled JSON Schema validator and the generated models must agree on
which strings are a ``format: date-time`` or a ``format: uri``
(``adcp.validation.schema_loader``), and the wire-preserving scalar types
(``adcp.types._wire``) keep a string's spelling only when it passes the same
check -- a string pydantic's lenient parser accepts but the schema rejects
(``"2026-10-01 00:00:00+00:00"``, ``"https:x.example"``) is normalized
instead, as it always was. This module is a leaf: it imports nothing from
``adcp.types`` or ``adcp.validation``, so both can import it.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import quote

__all__ = [
    "RFC3339_DATE_TIME",
    "UUID_TEXT",
    "is_plain_uri",
    "is_rfc3339_date_time",
    "is_uri_syntax",
    "is_uuid_text",
    "uri_host",
]

RFC3339_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)$",
    re.ASCII,
)

#: The hyphenated 8-4-4-4-12 form, either case: the only spelling
#: ``format: uuid`` admits. Braces, ``urn:uuid:`` and the bare 32 hex digits
#: are accepted by ``uuid.UUID`` and normalized.
UUID_TEXT = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# scheme "://" [userinfo "@"] host [":" port] -- enough to read the host a
# URI's own text names, for comparison with what the URL parser reports.
_URI_AUTHORITY = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://(?:[^/?#@]*@)?(\[[^\]]*\]|[^/?#:]*)")

# A strict SUBSET of RFC 3986: scheme, a plain ASCII reg-name host, an
# optional port, and path/query/fragment made of unreserved, sub-delimiter
# and well-formed percent-escape characters only. No userinfo, IP literal,
# non-ASCII or stray ``%``, so anything it matches is a valid URI whose host
# every parser reads the same way; everything else takes the full check.
_PCHAR = r"(?:[A-Za-z0-9._~!$&'()*+,;=:@/-]|%[0-9A-Fa-f]{2})"
_PLAIN_URI = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.\-]*://[A-Za-z0-9.\-]+(?::[0-9]*)?"
    rf"(?:/{_PCHAR}*)?(?:\?(?:{_PCHAR}|\?)*)?(?:#(?:{_PCHAR}|\?)*)?$",
    re.ASCII,
)


def is_rfc3339_date_time(text: str) -> bool:
    """Whether ``text`` is an RFC 3339 date-time.

    The grammar checks the time and offset ranges; only the calendar is
    checked here, because Python 3.10's parser accepts three or six
    fractional digits where RFC 3339 permits any positive number, and the
    check must neither coerce nor truncate the value.
    """
    if RFC3339_DATE_TIME.fullmatch(text) is None:
        return False
    try:
        date.fromisoformat(text[:10])
    except ValueError:
        return False
    return True


def is_uri_syntax(text: str) -> bool:
    """Whether ``text`` is RFC 3986 URI syntax, with non-ASCII escaped first.

    Whitespace, C1 controls and a backslash fail here: pydantic's ``AnyUrl``
    trims or rewrites them, which is exactly what must not be hidden. Only
    ASCII is checked as written, so a malformed percent escape stays visible;
    ``AnyUrl`` still checks internationalized hosts.
    """
    from rfc3986_validator import validate_rfc3986

    if any(char.isspace() or 0x7F <= ord(char) <= 0x9F for char in text):
        return False
    try:
        syntax = (
            text
            if text.isascii()
            else "".join(char if char.isascii() else quote(char, safe="") for char in text)
        )
    except UnicodeEncodeError:
        return False
    return bool(validate_rfc3986(syntax))


def is_plain_uri(text: str) -> bool:
    """Whether ``text`` is a URI the fast subset grammar accepts.

    True implies :func:`is_uri_syntax` is true and that the host the text
    names is the host a URL parser reports (lower-cased). The common case,
    checked in a fraction of the full RFC 3986 validator's time.
    """
    # fullmatch: ``$`` alone would also accept a single trailing newline.
    return _PLAIN_URI.fullmatch(text) is not None


def is_uuid_text(text: str) -> bool:
    """Whether ``text`` is the hyphenated 8-4-4-4-12 UUID spelling."""
    return UUID_TEXT.fullmatch(text) is not None


def uri_host(text: str) -> str | None:
    """The host ``text`` names in its authority, lower-cased, or ``None``."""
    match = _URI_AUTHORITY.match(text)
    if match is None:
        return None
    return match.group(1).lower()
