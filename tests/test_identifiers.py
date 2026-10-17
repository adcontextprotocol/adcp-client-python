from __future__ import annotations

import httpx
import pytest

from adcp.identifiers import (
    IdentifierValidationError,
    normalize_domain,
    normalize_identifier,
    validate_publisher_domain,
)
from adcp.registry import RegistryClient


@pytest.mark.parametrize(
    "value,canonical",
    [
        (" Example.COM. ", "example.com"),
        ("*.EXAMPLE.com", "*.example.com"),
        ("bücher.de", "xn--bcher-kva.de"),
        ("faß.de", "xn--fa-hia.de"),
        ("*.straße.de", "*.xn--strae-oqa.de"),
        ("news.example.com", "news.example.com"),
    ],
)
def test_hostname_normalization_reports_rewrites(value, canonical):
    result = normalize_domain(value)
    assert result.value == canonical
    assert result.rewritten == (value != canonical)


@pytest.mark.parametrize(
    "value",
    [
        "https://www.example.com/path",
        "not a domain",
        "a..com",
        "-a.com",
        "a.com:443",
        "user@a.com",
        "127.0.0.1",
        "a.*.com",
        "",
        "localhost",
        "*.example.com",
    ],
)
def test_publisher_domains_reject_non_hostnames(value):
    with pytest.raises(IdentifierValidationError):
        validate_publisher_domain(value)


@pytest.mark.parametrize("value", ["foo\u200dbar.com", "xn--a.com"])
def test_invalid_idna_is_rejected_without_changing_the_domain(value):
    with pytest.raises(IdentifierValidationError) as error:
        normalize_domain(value)
    assert error.value.identifier_type == "domain"
    assert error.value.reason == "invalid_idna"


@pytest.mark.parametrize(
    "kind,value,canonical",
    [
        ("subdomain", "NEWS.Example.com.", "news.example.com"),
        ("ios_bundle", " com.Example.App ", "com.Example.App"),
        ("android_package", "com.example.app", "com.example.app"),
        ("apple_app_store_id", "123456", "123456"),
        ("roku_store_id", "123", "123"),
        ("fire_tv_asin", "b012345678", "B012345678"),
        ("samsung_app_id", "G1234", "G1234"),
        ("rss_url", "https://example.com:8443/feed.xml", "https://example.com:8443/feed.xml"),
    ],
)
def test_typed_identifier_normalization(kind, value, canonical):
    assert normalize_identifier(kind, value).as_dict() == {"type": kind, "value": canonical}


@pytest.mark.parametrize(
    "kind,value",
    [
        ("subdomain", "*.example.com"),
        ("android_package", "https://play.google.com/id"),
        ("apple_app_store_id", "id123"),
        ("fire_tv_asin", "123"),
        ("ios_bundle", "com app"),
        ("unknown", "foo"),
    ],
)
def test_invalid_identifiers_return_typed_error(kind, value):
    with pytest.raises(IdentifierValidationError) as error:
        normalize_identifier(kind, value)
    assert error.value.reason


@pytest.mark.parametrize(
    "value",
    [
        "https://[invalid/rss",
        "https://bad_host.example/rss",
        "https://example.com:not-a-port/rss",
        "https://example.com:65536/rss",
        "https://@example.com/rss",
    ],
)
def test_invalid_rss_urls_always_report_the_declared_identifier_type(value):
    with pytest.raises(IdentifierValidationError) as error:
        normalize_identifier("rss_url", value)
    assert error.value.identifier_type == "rss_url"
    assert error.value.reason == "expected_http_url"
    assert value not in str(error.value)


@pytest.mark.asyncio
async def test_registry_rejects_invalid_domains_before_network():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        registry = RegistryClient(client=http)
        for value in ["https://www.example.com/path", "*.example.com", "not a domain"]:
            with pytest.raises(IdentifierValidationError):
                await registry.save_property(value, [], auth_token="secret")
            with pytest.raises(IdentifierValidationError):
                await registry.lookup_property(value)
            with pytest.raises(IdentifierValidationError):
                await registry.lookup_properties(["good.com", value])
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,canonical", [("EXAMPLE.com.", "example.com"), ("faß.de", "xn--fa-hia.de")]
)
async def test_lookup_normalizes_wire_domain_and_preserves_input_keys(value, canonical):
    import json

    async def handler(request):
        assert json.loads(request.content)["domains"] == [canonical]
        return httpx.Response(200, json={"results": {canonical: None}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await RegistryClient(client=http).lookup_properties([value])
    assert result == {value: None}
