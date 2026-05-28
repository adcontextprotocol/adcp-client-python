"""Fetch and cache immutable canonical-format reference documents.

AdCP 3.1 uses the same ``uri`` + ``sha256:<digest>`` reference shape for
``ProductFormatDeclaration.format_schema`` and ``platform_extensions``. This
module gives SDK adopters a hardened resolver for those references:

* HTTPS-only, redirect-free fetches.
* public-network SSRF validation before connect.
* DNS-rebinding defense via IP-pinned ``httpx`` transport by default.
* streaming body cap and digest verification.
* immutable cache keyed by ``uri@digest``.
* optional JSON Schema validation for ``format_schema`` documents, including
  a conservative ``$ref`` sandbox.

The helper returns structured results instead of raising for normal negative
outcomes so callers can surface stable conformance diagnostics without
leaking internal network details.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit

import httpx
import idna
import jsonschema

from adcp.signing._idna_canonicalize import canonicalize_host
from adcp.signing.ip_pinned_transport import IpPinnedTransport
from adcp.signing.jwks import BLOCKED_METADATA_IPS

DEFAULT_REFERENCE_TIMEOUT_SECONDS = 5.0
DEFAULT_REFERENCE_BODY_LIMIT_BYTES = 1024 * 1024
DEFAULT_MAX_SCHEMA_REFS = 256
DEFAULT_MAX_REF_DEPTH = 8
DEFAULT_MAX_SCHEMA_KEYWORDS = 10_000
DEFAULT_MAX_SCHEMA_DEPTH = 128

_DIGEST_PREFIX = "sha256:"
_DIGEST_HEX_LENGTH = 64
_REFERENCE_MARKER = "@sha256:"
_MAX_RESOLVED_ADDRESSES = 32

_SPECIAL_USE_SUFFIXES = (
    "localhost",
    "test",
    "invalid",
    "example",
    "local",
    "internal",
    "onion",
)
_SPECIAL_USE_HOSTS = {
    "example.com",
    "example.net",
    "example.org",
    "metadata",
    "metadata.google.internal",
    "kubernetes.default.svc",
}
_TRUSTED_SCHEMA_REF_ORIGIN = ("https", "creative.adcontextprotocol.org", 443)
_DRAFT_7_URIS = {
    "http://json-schema.org/draft-07/schema",
    "http://json-schema.org/draft-07/schema#",
    "https://json-schema.org/draft-07/schema",
    "https://json-schema.org/draft-07/schema#",
}
_DRAFT_2020_12_URIS = {
    "https://json-schema.org/draft/2020-12/schema",
    "https://json-schema.org/draft/2020-12/schema#",
    "http://json-schema.org/draft/2020-12/schema",
    "http://json-schema.org/draft/2020-12/schema#",
}
_REF_KEYWORDS = frozenset({"$ref", "$recursiveRef", "$dynamicRef"})


class CanonicalReferenceStatus(str, Enum):
    """Stable resolver outcomes for conformance diagnostics."""

    RESOLVED = "resolved"
    INVALID_REFERENCE = "invalid_reference"
    BLOCKED_UNSAFE_URL = "blocked_unsafe_url"
    NETWORK_ERROR = "network_error"
    BODY_TOO_LARGE = "body_too_large"
    DIGEST_MISMATCH = "digest_mismatch"
    INVALID_SCHEMA = "invalid_schema"


@dataclass(frozen=True)
class CanonicalReference:
    """Normalized immutable reference.

    Callers may pass either this dataclass, a mapping with ``uri`` and
    ``digest`` keys, or a compact string in ``uri@sha256:<digest>`` form.
    """

    uri: str
    digest: str

    @property
    def cache_key(self) -> str:
        return f"{self.uri}@{self.digest}"


@dataclass(frozen=True)
class CanonicalReferenceResult:
    """Structured resolver result.

    ``body`` is populated only on successful fetches. ``document`` is populated
    for successful ``format_schema`` resolution after JSON parsing. ``message``
    is intentionally coarse and should be safe to show in diagnostics.
    """

    status: CanonicalReferenceStatus
    reference: CanonicalReference | None = None
    body: bytes | None = None
    document: Any | None = None
    message: str | None = None
    from_cache: bool = False

    @property
    def resolved(self) -> bool:
        return self.status is CanonicalReferenceStatus.RESOLVED


@dataclass(frozen=True)
class _ResolvedTarget:
    uri: str
    host: str
    resolved_ip: str


TransportFactory = Callable[[str, str], httpx.BaseTransport]


class CanonicalReferenceResolver:
    """Resolve immutable ``format_schema`` and ``platform_extensions`` refs.

    The resolver owns an in-memory cache keyed by ``uri@digest``. Construct one
    per adopter component or request context; no process-global mutable
    configuration is used.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_REFERENCE_TIMEOUT_SECONDS,
        max_body_bytes: int = DEFAULT_REFERENCE_BODY_LIMIT_BYTES,
        max_schema_refs: int = DEFAULT_MAX_SCHEMA_REFS,
        max_ref_depth: int = DEFAULT_MAX_REF_DEPTH,
        max_schema_keywords: int = DEFAULT_MAX_SCHEMA_KEYWORDS,
        max_schema_depth: int = DEFAULT_MAX_SCHEMA_DEPTH,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_body_bytes = max_body_bytes
        self._max_schema_refs = max_schema_refs
        self._max_ref_depth = max_ref_depth
        self._max_schema_keywords = max_schema_keywords
        self._max_schema_depth = max_schema_depth
        self._transport_factory = transport_factory
        self._cache: dict[str, CanonicalReferenceResult] = {}

    @property
    def cache(self) -> Mapping[str, CanonicalReferenceResult]:
        """Read-only view of resolved immutable references."""

        return MappingProxyType({key: _copy_result(value) for key, value in self._cache.items()})

    def resolve_platform_extension(
        self,
        reference: CanonicalReference | Mapping[str, Any] | str,
    ) -> CanonicalReferenceResult:
        """Fetch and digest-verify a ``platform_extensions`` reference."""

        parsed, error = parse_canonical_reference(reference)
        if error is not None:
            return error
        assert parsed is not None
        cached = self._cache.get(parsed.cache_key)
        if cached is not None:
            return _copy_result(cached, from_cache=True)

        fetch_result = self._fetch(parsed)
        if fetch_result.status is CanonicalReferenceStatus.RESOLVED:
            self._cache[parsed.cache_key] = _copy_result(fetch_result)
        return fetch_result

    def resolve_format_schema(
        self,
        reference: CanonicalReference | Mapping[str, Any] | str,
    ) -> CanonicalReferenceResult:
        """Fetch, digest-verify, and validate a ``format_schema`` document."""

        parsed, error = parse_canonical_reference(reference)
        if error is not None:
            return error
        assert parsed is not None
        cached = self._cache.get(parsed.cache_key)
        if cached is not None:
            if cached.document is None and cached.body is not None:
                schema_result = self._validate_schema(parsed, cached.body)
                if schema_result.status is CanonicalReferenceStatus.RESOLVED:
                    self._cache[parsed.cache_key] = _copy_result(schema_result)
                return _copy_result(schema_result, from_cache=True)
            return _copy_result(cached, from_cache=True)

        fetch_result = self._fetch(parsed)
        if fetch_result.status is not CanonicalReferenceStatus.RESOLVED:
            return fetch_result
        assert fetch_result.body is not None
        self._cache[parsed.cache_key] = _copy_result(fetch_result)

        schema_result = self._validate_schema(parsed, fetch_result.body)
        if schema_result.status is CanonicalReferenceStatus.RESOLVED:
            self._cache[parsed.cache_key] = _copy_result(schema_result)
        return schema_result

    def _fetch(self, reference: CanonicalReference) -> CanonicalReferenceResult:
        resolved, unsafe = _resolve_public_https(reference.uri)
        if unsafe is not None:
            return CanonicalReferenceResult(
                status=CanonicalReferenceStatus.BLOCKED_UNSAFE_URL,
                reference=reference,
                message=unsafe,
            )
        assert resolved is not None

        transport = (
            self._transport_factory(resolved.host, resolved.resolved_ip)
            if self._transport_factory is not None
            else IpPinnedTransport(hostname=resolved.host, resolved_ip=resolved.resolved_ip)
        )
        try:
            with httpx.Client(
                transport=transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    "GET",
                    reference.uri,
                    headers={"Accept": "application/schema+json, application/json"},
                ) as response:
                    if 300 <= response.status_code < 400:
                        return CanonicalReferenceResult(
                            status=CanonicalReferenceStatus.BLOCKED_UNSAFE_URL,
                            reference=reference,
                            message="redirects are not allowed for canonical references",
                        )
                    if response.status_code != 200:
                        return CanonicalReferenceResult(
                            status=CanonicalReferenceStatus.NETWORK_ERROR,
                            reference=reference,
                            message="reference fetch failed",
                        )
                    body = _read_capped_body(response, self._max_body_bytes)
        except _BodyTooLargeError:
            return CanonicalReferenceResult(
                status=CanonicalReferenceStatus.BODY_TOO_LARGE,
                reference=reference,
                message="reference body exceeded configured size cap",
            )
        except httpx.HTTPError:
            return CanonicalReferenceResult(
                status=CanonicalReferenceStatus.NETWORK_ERROR,
                reference=reference,
                message="reference fetch failed",
            )

        actual_digest = _DIGEST_PREFIX + hashlib.sha256(body).hexdigest()
        if actual_digest != reference.digest:
            return CanonicalReferenceResult(
                status=CanonicalReferenceStatus.DIGEST_MISMATCH,
                reference=reference,
                message="reference digest mismatch",
            )
        return CanonicalReferenceResult(
            status=CanonicalReferenceStatus.RESOLVED,
            reference=reference,
            body=body,
        )

    def _validate_schema(
        self,
        reference: CanonicalReference,
        body: bytes,
    ) -> CanonicalReferenceResult:
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return CanonicalReferenceResult(
                status=CanonicalReferenceStatus.INVALID_SCHEMA,
                reference=reference,
                body=body,
                message="format_schema is not valid UTF-8 JSON",
            )
        if not isinstance(document, dict):
            return CanonicalReferenceResult(
                status=CanonicalReferenceStatus.INVALID_SCHEMA,
                reference=reference,
                body=body,
                message="format_schema root must be a JSON object",
            )

        ref_error = _validate_schema_refs(
            document,
            base_uri=reference.uri,
            max_refs=self._max_schema_refs,
            max_ref_depth=self._max_ref_depth,
            max_keywords=self._max_schema_keywords,
            max_depth=self._max_schema_depth,
        )
        if ref_error is not None:
            return CanonicalReferenceResult(
                status=CanonicalReferenceStatus.INVALID_SCHEMA,
                reference=reference,
                body=body,
                message=ref_error,
            )

        validator_class, draft_error = _validator_class_for_schema(document)
        if draft_error is not None:
            return CanonicalReferenceResult(
                status=CanonicalReferenceStatus.INVALID_SCHEMA,
                reference=reference,
                body=body,
                message=draft_error,
            )
        try:
            validator_class.check_schema(document)
        except (jsonschema.exceptions.SchemaError, RecursionError):
            return CanonicalReferenceResult(
                status=CanonicalReferenceStatus.INVALID_SCHEMA,
                reference=reference,
                body=body,
                message="format_schema failed JSON Schema validation",
            )

        return CanonicalReferenceResult(
            status=CanonicalReferenceStatus.RESOLVED,
            reference=reference,
            body=body,
            document=document,
        )


def parse_canonical_reference(
    reference: CanonicalReference | Mapping[str, Any] | str | Any,
) -> tuple[CanonicalReference | None, CanonicalReferenceResult | None]:
    """Parse supported reference inputs into a normalized dataclass."""

    if isinstance(reference, CanonicalReference):
        parsed = reference
    elif isinstance(reference, str):
        if _REFERENCE_MARKER not in reference:
            return None, _invalid_reference("reference must use uri@sha256:<digest> form")
        compact_uri, digest_hex = reference.rsplit(_REFERENCE_MARKER, 1)
        parsed = CanonicalReference(uri=compact_uri, digest=f"{_DIGEST_PREFIX}{digest_hex}")
    elif isinstance(reference, Mapping):
        mapped_uri = reference.get("uri")
        mapped_digest = reference.get("digest")
        if mapped_uri is None or mapped_digest is None:
            return None, _invalid_reference("reference mapping requires uri and digest")
        parsed = CanonicalReference(uri=str(mapped_uri), digest=str(mapped_digest))
    elif hasattr(reference, "uri") and hasattr(reference, "digest"):
        raw_uri = getattr(reference, "uri")
        raw_digest = getattr(reference, "digest")
        if raw_uri is None or raw_digest is None:
            return None, _invalid_reference("reference object requires uri and digest")
        parsed = CanonicalReference(uri=str(raw_uri), digest=str(raw_digest))
    else:
        return None, _invalid_reference("unsupported canonical reference type")

    if not _is_valid_digest(parsed.digest):
        return None, _invalid_reference("digest must be sha256:<64 lowercase hex characters>")
    if not parsed.uri:
        return None, _invalid_reference("reference URI is empty")
    return parsed, None


def _copy_result(
    result: CanonicalReferenceResult,
    *,
    from_cache: bool | None = None,
) -> CanonicalReferenceResult:
    return CanonicalReferenceResult(
        status=result.status,
        reference=result.reference,
        body=result.body,
        document=_clone_document(result.document),
        message=result.message,
        from_cache=result.from_cache if from_cache is None else from_cache,
    )


def _clone_document(document: Any | None) -> Any | None:
    return None if document is None else deepcopy(document)


def _invalid_reference(message: str) -> CanonicalReferenceResult:
    return CanonicalReferenceResult(
        status=CanonicalReferenceStatus.INVALID_REFERENCE,
        message=message,
    )


def _is_valid_digest(digest: str) -> bool:
    if not digest.startswith(_DIGEST_PREFIX):
        return False
    hex_part = digest[len(_DIGEST_PREFIX) :]
    if len(hex_part) != _DIGEST_HEX_LENGTH:
        return False
    return all(ch in "0123456789abcdef" for ch in hex_part)


class _BodyTooLargeError(Exception):
    pass


def _read_capped_body(response: httpx.Response, max_body_bytes: int) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > max_body_bytes:
            raise _BodyTooLargeError
    return bytes(body)


def _resolve_public_https(uri: str) -> tuple[_ResolvedTarget | None, str | None]:
    parts = urlsplit(uri)
    unsafe = _validate_https_url_parts(parts)
    if unsafe is not None:
        return None, unsafe

    host = parts.hostname
    assert host is not None
    try:
        canonical_host = canonicalize_host(host)
    except (idna.IDNAError, UnicodeError, UnicodeEncodeError):
        return None, "reference host is not IDNA-valid"

    unsafe = _validate_special_use_host(canonical_host)
    if unsafe is not None:
        return None, unsafe

    try:
        infos = socket.getaddrinfo(canonical_host, None)
    except OSError:
        return None, "reference host could not be resolved"

    accepted_ip: str | None = None
    for _family, _type, _proto, _canonname, sockaddr in infos[:_MAX_RESOLVED_ADDRESSES]:
        ip_str = str(sockaddr[0])
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return None, "reference host resolved to an invalid address"
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        unsafe = _validate_public_ip(ip)
        if unsafe is not None:
            return None, unsafe
        if accepted_ip is None:
            accepted_ip = str(ip)

    if accepted_ip is None:
        return None, "reference host resolved no usable addresses"
    return _ResolvedTarget(uri=uri, host=canonical_host, resolved_ip=accepted_ip), None


def _validate_https_url_parts(parts: SplitResult) -> str | None:
    if parts.scheme != "https":
        return "canonical references must use https"
    if parts.username or parts.password:
        return "canonical reference URLs must not include userinfo"
    if not parts.hostname:
        return "canonical reference URL has no host"
    try:
        _ = parts.port
    except ValueError:
        return "canonical reference URL has an invalid port"
    return None


def _validate_special_use_host(host: str) -> str | None:
    stripped = host.rstrip(".")
    if stripped in _SPECIAL_USE_HOSTS:
        return "special-use host is not allowed"
    for suffix in _SPECIAL_USE_SUFFIXES:
        if stripped == suffix or stripped.endswith(f".{suffix}"):
            return "special-use host is not allowed"
    return None


def _validate_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if str(ip) in BLOCKED_METADATA_IPS:
        return "cloud metadata address is not allowed"
    if not ip.is_global:
        return "non-public resolved address is not allowed"
    return None


def _validator_class_for_schema(
    document: dict[str, Any],
) -> tuple[type[jsonschema.protocols.Validator], str | None]:
    raw_draft = document.get("$schema")
    if raw_draft is None:
        return jsonschema.Draft7Validator, "format_schema must declare $schema"
    if not isinstance(raw_draft, str):
        return jsonschema.Draft7Validator, "$schema must be a string when present"
    if raw_draft in _DRAFT_7_URIS:
        return jsonschema.Draft7Validator, None
    if raw_draft in _DRAFT_2020_12_URIS:
        return jsonschema.Draft202012Validator, None
    return jsonschema.Draft7Validator, "format_schema must declare Draft-07 or Draft 2020-12"


def _validate_schema_refs(
    document: Any,
    *,
    base_uri: str,
    max_refs: int,
    max_ref_depth: int,
    max_keywords: int,
    max_depth: int,
) -> str | None:
    state = _SchemaRefState(max_refs=max_refs, max_keywords=max_keywords)
    return _walk_schema_refs(
        document,
        base_uri=base_uri,
        max_ref_depth=max_ref_depth,
        max_depth=max_depth,
        state=state,
        depth=0,
    )


@dataclass
class _SchemaRefState:
    max_refs: int
    max_keywords: int
    refs: int = 0
    keywords: int = 0


def _walk_schema_refs(
    node: Any,
    *,
    base_uri: str,
    max_ref_depth: int,
    max_depth: int,
    state: _SchemaRefState,
    depth: int,
) -> str | None:
    if depth > max_depth:
        return "format_schema exceeds nesting depth bound"
    if isinstance(node, dict):
        state.keywords += len(node)
        if state.keywords > state.max_keywords:
            return "format_schema exceeds keyword bound"
        next_base_uri = base_uri
        raw_id = node.get("$id")
        if raw_id is not None:
            if not isinstance(raw_id, str):
                return "$id must be a string"
            id_error, resolved_id = _validate_schema_id_value(raw_id, base_uri=base_uri)
            if id_error is not None:
                return id_error
            assert resolved_id is not None
            next_base_uri = resolved_id
        for key, value in node.items():
            if key in _REF_KEYWORDS:
                if not isinstance(value, str):
                    return f"{key} must be a string"
                state.refs += 1
                if state.refs > state.max_refs:
                    return "format_schema exceeds $ref count bound"
                error = _validate_schema_ref_value(
                    value,
                    base_uri=next_base_uri,
                    max_ref_depth=max_ref_depth,
                )
                if error is not None:
                    return error
            error = _walk_schema_refs(
                value,
                base_uri=next_base_uri,
                max_ref_depth=max_ref_depth,
                max_depth=max_depth,
                state=state,
                depth=depth + 1,
            )
            if error is not None:
                return error
    elif isinstance(node, list):
        for item in node:
            error = _walk_schema_refs(
                item,
                base_uri=base_uri,
                max_ref_depth=max_ref_depth,
                max_depth=max_depth,
                state=state,
                depth=depth + 1,
            )
            if error is not None:
                return error
    return None


def _validate_schema_ref_value(ref: str, *, base_uri: str, max_ref_depth: int) -> str | None:
    if ref.startswith("#"):
        return _validate_ref_fragment_depth(ref, max_ref_depth)

    joined = urljoin(base_uri, ref)
    parts = urlsplit(joined)
    if parts.scheme != "https":
        return "$ref URLs must be intra-document, same-origin HTTPS, or trusted AAO HTTPS"
    if not parts.hostname:
        return "$ref URL has no host"
    if not _same_origin(base_uri, joined) and not _trusted_aao_catalog_origin(parts):
        return "off-origin $ref is not allowed"
    unsafe_parts = _validate_https_url_parts(parts)
    if unsafe_parts is not None:
        return unsafe_parts
    unsafe_ref = _validate_ref_fragment_depth(parts.fragment, max_ref_depth)
    if unsafe_ref is not None:
        return unsafe_ref
    _resolved, unsafe_url = _resolve_public_https(joined)
    if unsafe_url is not None:
        return unsafe_url
    return None


def _validate_schema_id_value(id_value: str, *, base_uri: str) -> tuple[str | None, str | None]:
    if id_value.startswith("#"):
        return None, urljoin(base_uri, id_value)

    joined = urljoin(base_uri, id_value)
    parts = urlsplit(joined)
    if parts.scheme != "https":
        return "$id URLs must be intra-document, same-origin HTTPS, or trusted AAO HTTPS", None
    if not parts.hostname:
        return "$id URL has no host", None
    if not _same_origin(base_uri, joined) and not _trusted_aao_catalog_origin(parts):
        return "off-origin $id is not allowed", None
    unsafe_parts = _validate_https_url_parts(parts)
    if unsafe_parts is not None:
        return unsafe_parts, None
    _resolved, unsafe_url = _resolve_public_https(joined)
    if unsafe_url is not None:
        return unsafe_url, None
    return None, joined


def _validate_ref_fragment_depth(fragment: str, max_ref_depth: int) -> str | None:
    if not fragment:
        return None
    pointer = fragment[1:] if fragment.startswith("#") else fragment
    if not pointer.startswith("/"):
        return None
    depth = len([segment for segment in pointer.split("/") if segment])
    if depth > max_ref_depth:
        return "format_schema exceeds $ref depth bound"
    return None


def _same_origin(left: str, right: str) -> bool:
    left_parts = urlsplit(left)
    right_parts = urlsplit(right)
    try:
        left_origin = _origin_tuple(left_parts)
        right_origin = _origin_tuple(right_parts)
    except ValueError:
        return False
    return left_origin == right_origin


def _origin_tuple(parts: SplitResult) -> tuple[str, str, int]:
    if not parts.hostname:
        raise ValueError("missing host")
    host = canonicalize_host(parts.hostname)
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    return parts.scheme.lower(), host, port


def _trusted_aao_catalog_origin(parts: SplitResult) -> bool:
    try:
        return _origin_tuple(parts) == _TRUSTED_SCHEMA_REF_ORIGIN
    except (ValueError, idna.IDNAError, UnicodeError, UnicodeEncodeError):
        return False


__all__ = [
    "CanonicalReference",
    "CanonicalReferenceResolver",
    "CanonicalReferenceResult",
    "CanonicalReferenceStatus",
    "DEFAULT_REFERENCE_BODY_LIMIT_BYTES",
    "DEFAULT_REFERENCE_TIMEOUT_SECONDS",
    "parse_canonical_reference",
]
