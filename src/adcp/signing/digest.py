"""Content-Digest (RFC 9530) support for the AdCP request-signing profile.

Only `sha-256` is required. The header value is an RFC 8941 Dictionary
(`sha-256=:<base64>:[, sha-512=:<base64>:]`), parsed by ``http-sf`` with
duplicate keys refused. A digest matches when the sha-256 entry's decoded
bytes equal `sha256(body)`.
"""

from __future__ import annotations

import hashlib
import hmac

from adcp.signing._rfc9421 import parse_dictionary, serialize, translate_legacy_binary


def compute_content_digest_sha256(body: bytes) -> str:
    """Return the Content-Digest header value for a body (sha-256 only)."""
    return serialize({"sha-256": (hashlib.sha256(body).digest(), {})})


def content_digest_matches(header_value: str, body: bytes) -> bool:
    """Return True iff the sha-256 entry in the header matches the body.

    RFC 8941 byte sequences are standard Base64. The Base64URL spelling some
    AdCP 3.0/3.1 senders emitted is still accepted: a field that does not
    parse as written is translated and parsed strictly once more.
    """
    try:
        members = parse_dictionary(header_value)
    except ValueError:
        try:
            members = parse_dictionary(translate_legacy_binary(header_value))
        except ValueError:
            return False
    entry = members.get("sha-256")
    if entry is None or not isinstance(entry[0], bytes):
        return False
    return hmac.compare_digest(entry[0], hashlib.sha256(body).digest())
