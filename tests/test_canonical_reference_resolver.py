from __future__ import annotations

import hashlib
import json
import socket
from typing import Any

import httpx
import pytest

from adcp.canonical_formats import (
    CanonicalReferenceResolver,
    CanonicalReferenceStatus,
    parse_canonical_reference,
)
from adcp.types.generated_poc.core.platform_extension_ref import PlatformExtensionReference

PUBLIC_IP = "93.184.216.34"
BASE_URI = "https://formats.adcontextprotocol.org/schemas/custom.json"


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _reference(body: bytes, uri: str = BASE_URI) -> dict[str, str]:
    return {"uri": uri, "digest": _digest(body)}


def _install_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str] | None = None) -> None:
    host_map = mapping or {}

    def fake_getaddrinfo(host: str, *_args, **_kwargs):
        ip = host_map.get(host, PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _resolver_for_response(response: httpx.Response) -> CanonicalReferenceResolver:
    transport = httpx.MockTransport(lambda _request: response)
    return CanonicalReferenceResolver(transport_factory=lambda _host, _ip: transport)


def test_platform_extension_resolves_digest_match_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dns(monkeypatch)
    body = b'{"name":"meta_pixel","version":"1.0.0"}'
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    resolver = CanonicalReferenceResolver(transport_factory=lambda _host, _ip: transport)

    first = resolver.resolve_platform_extension(_reference(body))
    second = resolver.resolve_platform_extension(_reference(body))

    assert first.status is CanonicalReferenceStatus.RESOLVED
    assert first.body == body
    assert second.status is CanonicalReferenceStatus.RESOLVED
    assert second.from_cache is True
    assert calls == 1


def test_format_schema_validates_body_cached_by_platform_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dns(monkeypatch)
    body = b'{"$schema":"http://json-schema.org/draft-07/schema#","type":7}'
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    resolver = CanonicalReferenceResolver(transport_factory=lambda _host, _ip: transport)

    extension = resolver.resolve_platform_extension(_reference(body))
    schema = resolver.resolve_format_schema(_reference(body))

    assert extension.status is CanonicalReferenceStatus.RESOLVED
    assert schema.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert schema.from_cache is True
    assert calls == 1


def test_invalid_format_schema_fetch_is_reused_for_platform_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dns(monkeypatch)
    body = b'{"$schema":"http://json-schema.org/draft-07/schema#","type":7}'
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    resolver = CanonicalReferenceResolver(transport_factory=lambda _host, _ip: transport)

    schema = resolver.resolve_format_schema(_reference(body))
    extension = resolver.resolve_platform_extension(_reference(body))

    assert schema.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert schema.from_cache is False
    assert extension.status is CanonicalReferenceStatus.RESOLVED
    assert extension.from_cache is True
    assert calls == 1


def test_cache_view_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b"{}"
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_platform_extension(_reference(body))

    assert result.status is CanonicalReferenceStatus.RESOLVED
    with pytest.raises(TypeError):
        resolver.cache["extra"] = result  # type: ignore[index]


def test_compact_uri_at_digest_reference_form_is_supported() -> None:
    body = b"{}"
    parsed, error = parse_canonical_reference(f"{BASE_URI}@{_digest(body)}")

    assert error is None
    assert parsed is not None
    assert parsed.uri == BASE_URI
    assert parsed.digest == _digest(body)


def test_sdk_platform_extension_reference_object_is_supported() -> None:
    body = b"{}"
    model = PlatformExtensionReference(uri=BASE_URI, digest=_digest(body))

    parsed_model, model_error = parse_canonical_reference(model)
    parsed_dump, dump_error = parse_canonical_reference(model.model_dump())

    assert model_error is None
    assert parsed_model is not None
    assert parsed_model.uri == BASE_URI
    assert parsed_model.digest == _digest(body)
    assert dump_error is None
    assert parsed_dump is not None
    assert parsed_dump.uri == BASE_URI
    assert parsed_dump.digest == _digest(body)


def test_digest_mismatch_is_substitution_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    resolver = _resolver_for_response(httpx.Response(200, content=b'{"actual":true}'))

    result = resolver.resolve_platform_extension(
        {"uri": BASE_URI, "digest": _digest(b'{"expected":true}')}
    )

    assert result.status is CanonicalReferenceStatus.DIGEST_MISMATCH
    assert result.body is None


def test_invalid_json_schema_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b'{"$schema":"http://json-schema.org/draft-07/schema#","type":7}'
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.INVALID_SCHEMA


def test_draft_2020_12_format_schema_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = (
        b'{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        b'"type":"object","properties":{"headline":{"type":"string"}}}'
    )
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.RESOLVED
    assert result.document == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"headline": {"type": "string"}},
    }


def test_missing_schema_declaration_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b'{"type":"object"}'
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert result.message == "format_schema must declare $schema"


def test_draft_2019_09_format_schema_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b'{"$schema":"https://json-schema.org/draft/2019-09/schema","type":"object"}'
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert result.message == "format_schema must declare Draft-07 or Draft 2020-12"


def test_off_origin_ref_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b'{"type":"object","properties":{"x":{"$ref":"https://evil.com/schema.json"}}}'
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert result.message == "off-origin $ref is not allowed"


def test_aao_catalog_ref_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = (
        b'{"$schema":"http://json-schema.org/draft-07/schema#",'
        b'"properties":{"x":{"$ref":"https://creative.adcontextprotocol.org/shared.json#/$defs/x"}}}'
    )
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.RESOLVED


def test_file_ref_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b'{"type":"object","properties":{"x":{"$ref":"file:///etc/passwd"}}}'
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert result.message == (
        "$ref URLs must be intra-document, same-origin HTTPS, or trusted AAO HTTPS"
    )


def test_http_reference_url_is_blocked_before_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b"{}"
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_platform_extension(
        {"uri": "http://formats.adcontextprotocol.org/schema.json", "digest": _digest(body)}
    )

    assert result.status is CanonicalReferenceStatus.BLOCKED_UNSAFE_URL


def test_metadata_and_rfc1918_addresses_are_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(
        monkeypatch,
        {
            "169.254.169.254": "169.254.169.254",
            "kubernetes.default.svc": PUBLIC_IP,
            "internal.adcontextprotocol.org": "10.0.0.12",
            "vault.internal": PUBLIC_IP,
        },
    )
    body = b"{}"
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    metadata = resolver.resolve_platform_extension(
        {"uri": "https://169.254.169.254/schema.json", "digest": _digest(body)}
    )
    private = resolver.resolve_platform_extension(
        {"uri": "https://internal.adcontextprotocol.org/schema.json", "digest": _digest(body)}
    )
    kubernetes = resolver.resolve_platform_extension(
        {"uri": "https://kubernetes.default.svc/schema.json", "digest": _digest(body)}
    )
    internal_name = resolver.resolve_platform_extension(
        {"uri": "https://vault.internal/schema.json", "digest": _digest(body)}
    )

    assert metadata.status is CanonicalReferenceStatus.BLOCKED_UNSAFE_URL
    assert private.status is CanonicalReferenceStatus.BLOCKED_UNSAFE_URL
    assert kubernetes.status is CanonicalReferenceStatus.BLOCKED_UNSAFE_URL
    assert internal_name.status is CanonicalReferenceStatus.BLOCKED_UNSAFE_URL


def test_nested_id_metadata_base_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch, {"169.254.169.254": "169.254.169.254"})
    body = (
        b'{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        b'"$id":"https://169.254.169.254/latest/","$ref":"meta-data"}'
    )
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert result.message == "off-origin $id is not allowed"


def test_cached_document_mutation_does_not_poison_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = (
        b'{"$schema":"http://json-schema.org/draft-07/schema#",'
        b'"type":"object","properties":{"headline":{"type":"string"}}}'
    )
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    first = resolver.resolve_format_schema(_reference(body))
    assert isinstance(first.document, dict)
    first.document["type"] = 7
    second = resolver.resolve_format_schema(_reference(body))

    assert second.status is CanonicalReferenceStatus.RESOLVED
    assert isinstance(second.document, dict)
    assert second.document["type"] == "object"


def test_cache_view_document_mutation_does_not_poison_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dns(monkeypatch)
    body = (
        b'{"$schema":"http://json-schema.org/draft-07/schema#",'
        b'"type":"object","properties":{"headline":{"type":"string"}}}'
    )
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))
    assert result.reference is not None
    cached = resolver.cache[result.reference.cache_key]
    assert isinstance(cached.document, dict)
    cached.document["type"] = 7
    second = resolver.resolve_format_schema(_reference(body))

    assert second.status is CanonicalReferenceStatus.RESOLVED
    assert isinstance(second.document, dict)
    assert second.document["type"] == "object"


def test_cache_values_document_mutation_does_not_poison_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dns(monkeypatch)
    body = (
        b'{"$schema":"http://json-schema.org/draft-07/schema#",'
        b'"type":"object","properties":{"headline":{"type":"string"}}}'
    )
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_format_schema(_reference(body))
    assert result.status is CanonicalReferenceStatus.RESOLVED
    cached = next(iter(resolver.cache.values()))
    assert isinstance(cached.document, dict)
    cached.document["type"] = 7
    second = resolver.resolve_format_schema(_reference(body))

    assert second.status is CanonicalReferenceStatus.RESOLVED
    assert isinstance(second.document, dict)
    assert second.document["type"] == "object"


def test_cgnat_address_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch, {"cgnat.adcontextprotocol.org": "100.64.0.10"})
    body = b"{}"
    resolver = _resolver_for_response(httpx.Response(200, content=body))

    result = resolver.resolve_platform_extension(
        {"uri": "https://cgnat.adcontextprotocol.org/schema.json", "digest": _digest(body)}
    )

    assert result.status is CanonicalReferenceStatus.BLOCKED_UNSAFE_URL


def test_redirect_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b"{}"
    resolver = _resolver_for_response(
        httpx.Response(302, headers={"Location": "https://evil.com/schema.json"})
    )

    result = resolver.resolve_platform_extension(_reference(body))

    assert result.status is CanonicalReferenceStatus.BLOCKED_UNSAFE_URL


def test_timeout_degrades_to_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b"{}"
    transport = httpx.MockTransport(
        lambda _request: (_ for _ in ()).throw(httpx.TimeoutException("slow"))
    )
    resolver = CanonicalReferenceResolver(transport_factory=lambda _host, _ip: transport)

    result = resolver.resolve_platform_extension(_reference(body))

    assert result.status is CanonicalReferenceStatus.NETWORK_ERROR
    assert result.body is None


def test_body_size_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b"x" * 16
    resolver = CanonicalReferenceResolver(
        max_body_bytes=8,
        transport_factory=lambda _host, _ip: httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body)
        ),
    )

    result = resolver.resolve_platform_extension(_reference(body))

    assert result.status is CanonicalReferenceStatus.BODY_TOO_LARGE


def test_excessive_ref_count_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    body = b'{"allOf":[{"$ref":"#/defs/a"},{"$ref":"#/defs/b"}]}'
    resolver = CanonicalReferenceResolver(
        max_schema_refs=1,
        transport_factory=lambda _host, _ip: httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body)
        ),
    )

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert result.message == "format_schema exceeds $ref count bound"


def test_excessive_schema_ids_stop_before_dns_amplification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_calls = 0

    def counted_getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        nonlocal dns_calls
        del host, args, kwargs
        dns_calls += 1
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr("socket.getaddrinfo", counted_getaddrinfo)
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "allOf": [{"$id": f"child-{index}"} for index in range(100)],
    }
    body = json.dumps(document).encode()
    resolver = CanonicalReferenceResolver(
        max_schema_ids=2,
        transport_factory=lambda _host, _ip: httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body)
        ),
    )

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert result.message == "format_schema exceeds $id count bound"
    # One lookup validates the fetched document URL, then at most the two
    # configured $id values are resolved before the walker stops.
    assert dns_calls <= 3


def test_deep_schema_nesting_returns_structured_invalid_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dns(monkeypatch)
    node: dict[str, Any] = {"type": "string"}
    for _ in range(10):
        node = {"items": [node]}
    body = json.dumps(node).encode()
    resolver = CanonicalReferenceResolver(
        max_schema_depth=4,
        transport_factory=lambda _host, _ip: httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body)
        ),
    )

    result = resolver.resolve_format_schema(_reference(body))

    assert result.status is CanonicalReferenceStatus.INVALID_SCHEMA
    assert result.message == "format_schema exceeds nesting depth bound"
