from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from adcp.exceptions import RegistryError
from adcp.registry import RegistryClient

PROVENANCE = {"type": "agency_allowlist", "context": "test"}


def response_for(body):
    count = len(body["identifiers"])
    return {
        "resolved": [
            {
                "identifier": item,
                "property_rid": "rid",
                "classification": "property",
                "status": "existing",
                "source": "adagents_json",
            }
            for item in body["identifiers"]
        ],
        "summary": {"total": count, "resolved": count, "created": 0, "excluded": 0, "not_found": 0},
        "server_timestamp": "2026-10-17T00:00:00Z",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["lookup", "resolve"])
async def test_typed_endpoint_auth_chunking_order_and_concurrency(mode):
    seen, active, peak = [], 0, 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        body = json.loads(request.content)
        seen.append((request, body))
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, json=response_for(body))

    identifiers = [
        {"type": "android_package", "value": f"com.example.app{i}"} for i in range(20001)
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        registry = RegistryClient(
            base_url="https://test-registry.com", client=http, max_bulk_response_bytes=10_000_000
        )
        result = await registry.resolve_identifiers(
            identifiers,
            mode=mode,
            provenance=PROVENANCE,
            concurrency=2,
            max_request_bytes=10_000_000,
            auth_token="secret" if mode == "resolve" else None,
        )
    assert [len(body["identifiers"]) for _, body in seen] == [10000, 10000, 1]
    assert peak == 2
    assert [row["identifier"] for row in result["resolved"]] == identifiers
    assert result["summary"]["total"] == 20001
    for request, body in seen:
        assert str(request.url) == "https://test-registry.com/api/registry/resolve"
        assert body["provenance"] == PROVENANCE
        assert body["mode"] == mode
        assert request.headers.get("authorization") == (
            "Bearer secret" if mode == "resolve" else None
        )


@pytest.mark.asyncio
async def test_byte_size_limit_and_retry_after(monkeypatch):
    seen, attempts = [], 0
    sleep = AsyncMock()
    monkeypatch.setattr("adcp.registry.asyncio.sleep", sleep)

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "busy"})
        seen.append(request)
        return httpx.Response(200, json=response_for(json.loads(request.content)))

    identifiers = [{"type": "domain", "value": f"site{i}.com"} for i in range(12)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await RegistryClient(client=http).resolve_identifiers(
            identifiers, mode="lookup", provenance=PROVENANCE, max_request_bytes=200
        )
    sleep.assert_awaited_once_with(2.0)
    assert len(seen) > 1
    assert all(len(request.content) <= 200 for request in seen)
    assert result["summary"]["total"] == 12


@pytest.mark.asyncio
async def test_exhausted_retry_preserves_structured_error():
    async def handler(request):
        return httpx.Response(503, headers={"retry-after": "7"}, json={"error": "busy"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RegistryError) as error:
            await RegistryClient(client=http).resolve_identifiers(
                [{"type": "domain", "value": "example.com"}],
                mode="lookup",
                provenance=PROVENANCE,
                max_retries=0,
            )
    assert error.value.status_code == 503
    assert error.value.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_invalid_json_is_a_safe_registry_error():
    async def handler(request):
        return httpx.Response(200, content=b"<html>private-upstream-value</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RegistryError, match="invalid JSON response") as error:
            await RegistryClient(client=http).resolve_identifiers(
                [{"type": "domain", "value": "example.com"}],
                mode="lookup",
                provenance=PROVENANCE,
            )
    assert "private-upstream-value" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,value",
    [
        (("server_timestamp",), None),
        (("summary",), {}),
        (("summary", "total"), True),
        (("summary", "resolved"), -1),
        (("resolved", 0, "identifier"), {"type": "domain"}),
        (("resolved", 0, "property_rid"), 123),
        (("resolved", 0, "classification"), []),
        (("resolved", 0, "status"), "private-upstream-value"),
        (("resolved", 0, "source"), {}),
    ],
)
async def test_malformed_resolve_contract_is_a_registry_error(path, value):
    body = response_for({"identifiers": [{"type": "domain", "value": "example.com"}]})
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    async def handler(request):
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RegistryError, match="invalid (response|summary)") as error:
            await RegistryClient(client=http).resolve_identifiers(
                [{"type": "domain", "value": "example.com"}],
                mode="lookup",
                provenance=PROVENANCE,
            )
    assert "private-upstream-value" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_source", [False, True])
async def test_nullable_resolve_fields_must_still_be_present(omit_source):
    body = response_for({"identifiers": [{"type": "domain", "value": "example.com"}]})
    body["resolved"][0].update(property_rid=None, source=None, status="excluded")
    body["summary"].update(resolved=0, excluded=1)
    if omit_source:
        del body["resolved"][0]["source"]

    async def handler(request):
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        resolve = RegistryClient(client=http).resolve_identifiers(
            [{"type": "domain", "value": "example.com"}],
            mode="lookup",
            provenance=PROVENANCE,
        )
        if omit_source:
            with pytest.raises(RegistryError, match="invalid response"):
                await resolve
        else:
            assert await resolve == body
