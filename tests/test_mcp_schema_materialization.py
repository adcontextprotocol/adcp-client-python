"""Versioned public schema construction is cached without sharing mutable state."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from importlib.resources import files
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from jsonschema import FormatChecker
from jsonschema.validators import validator_for

from adcp.server import ADCPHandler, create_mcp_server, mcp_tools
from adcp.server.a2a_server import create_a2a_server
from adcp.validation import schema_loader as loader
from adcp.validation.schema_validator import validate_request

PINS = ("3.2.0-rc.3", "3.2.0-rc.6", "3.2.0-beta.6")

# Canonical JSON digests captured from unchanged e9a1c8fc before adding
# the materialization cache. These are transformed public schemas,
# not a claim of byte identity with the signed upstream schema files.
EXPECTED_PUBLIC_SHA256 = {
    "3.2.0-rc.3": "6656874ca37ea0732e65a5f0313c8ead7b56ed12460bdbae297c4c648fa26f14",
    "3.2.0-rc.6": "d712168e85932dfabad8a76b49a24aa6411dc11748832d18dd0ae00ad7106b3e",
    "3.2.0-beta.6": "e9a10b9f1ee5654a51219c91329089f591c953b972e57a913450a1b9acdb810c",
}


class SchemaHandler(ADCPHandler):
    def __init__(self) -> None:
        self.calls = 0

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        self.calls += 1
        return {"products": []}

    async def get_reporting_status(self, params: Any, context: Any = None) -> dict[str, Any]:
        raise AssertionError("schema discovery must not execute a reporting task")


class PinnedSchemaHandler(SchemaHandler):
    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version

    def get_adcp_version(self) -> str:
        return self.version


@pytest.fixture(autouse=True)
def isolated_loader():
    loader._reset_for_tests()
    yield
    loader._reset_for_tests()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def mutable_ids(value: Any) -> set[int]:
    """Reject aliases within a result as well as between independent results."""
    seen: set[int] = set()
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, (dict, list)):
            assert id(node) not in seen
            seen.add(id(node))
            pending.extend(node.values() if isinstance(node, dict) else node)
    return seen


def public_definitions(version: str) -> list[dict[str, Any]]:
    return mcp_tools.get_tools_for_handler(PinnedSchemaHandler(version))


def count_materializations(monkeypatch):
    original = loader._self_contained_schema
    calls: Counter[tuple[str, str]] = Counter()
    lock = threading.Lock()

    def counted(state, path, schema):
        with lock:
            calls[state.bundle_key, str(path)] += 1
        return original(state, path, schema)

    monkeypatch.setattr(loader, "_self_contained_schema", counted)
    return calls


@pytest.mark.parametrize("version", PINS)
def test_repeated_public_schema_construction_materializes_once(monkeypatch, version):
    calls = count_materializations(monkeypatch)
    first = loader.get_mcp_schema("get_products", "request", version=version)
    assert first is not None
    original = canonical(first)
    second = loader.get_mcp_schema("get_products", "request", version=version)
    assert canonical(second) == original
    assert not mutable_ids(first).intersection(mutable_ids(second))
    first["properties"]["brief"] = {"type": "array"}
    third = loader.get_mcp_schema("get_products", "request", version=version)
    assert canonical(third) == original
    assert not mutable_ids(second).intersection(mutable_ids(third))
    assert list(calls.values()) == [1]


def test_public_handler_pins_have_isolated_materializations(monkeypatch):
    calls = count_materializations(monkeypatch)
    saved = {}
    for version in PINS:
        definitions = public_definitions(version)
        saved[version] = canonical(definitions)
        assert hashlib.sha256(saved[version]).hexdigest() == EXPECTED_PUBLIC_SHA256[version]
        for definition in definitions:
            definition["inputSchema"].clear()
            if "outputSchema" in definition:
                definition["outputSchema"].clear()
    for version in reversed(PINS):
        definitions = public_definitions(version)
        assert canonical(definitions) == saved[version]
    assert {key[0] for key in calls} == set(PINS)
    assert calls and set(calls.values()) == {1}
    assert len(set(saved.values())) == len(PINS)


def test_loader_reset_discards_materializations(monkeypatch):
    calls = count_materializations(monkeypatch)
    first = loader.get_mcp_schema("get_products", "request", version=PINS[1])
    loader._reset_for_tests()
    second = loader.get_mcp_schema("get_products", "request", version=PINS[1])
    assert first == second
    assert not mutable_ids(first).intersection(mutable_ids(second))
    assert list(calls.values()) == [2]


def test_failed_materialization_is_not_cached(monkeypatch):
    original = loader._self_contained_schema
    attempts = 0

    def transient_failure(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("missing fixture reference")
        return original(*args, **kwargs)

    monkeypatch.setattr(loader, "_self_contained_schema", transient_failure)
    missing = loader.get_mcp_schema("get_products", "request", version=PINS[1])
    assert missing is None
    restored = loader.get_mcp_schema("get_products", "request", version=PINS[1])
    assert restored is not None
    cached = loader.get_mcp_schema("get_products", "request", version=PINS[1])
    assert cached == restored
    assert attempts == 2


def test_concurrent_first_callers_share_one_materialization(monkeypatch):
    workers = 8
    ready = threading.Barrier(workers + 1)
    entered = threading.Event()
    release = threading.Event()
    count_lock = threading.Lock()
    original = loader._self_contained_schema
    attempts = 0

    def blocked(*args, **kwargs):
        nonlocal attempts
        with count_lock:
            attempts += 1
        entered.set()
        released = release.wait(timeout=20)
        assert released
        return original(*args, **kwargs)

    def load():
        ready.wait(timeout=20)
        return loader.get_mcp_schema("get_products", "request", version=PINS[1])

    monkeypatch.setattr(loader, "_self_contained_schema", blocked)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(load) for _ in range(workers)]
        try:
            ready.wait(timeout=20)
            started = entered.wait(timeout=20)
            assert started
        finally:
            release.set()
        results = [future.result(timeout=30) for future in pending]
    assert results[0] is not None and all(value == results[0] for value in results)
    seen: set[int] = set()
    for result in results:
        identities = mutable_ids(result)
        assert not seen.intersection(identities)
        seen.update(identities)
    assert attempts == 1


def test_pinned_schemas_do_not_copy_superseded_current_models(monkeypatch):
    class SupersededSchema(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("copied a current-model schema that the pin replaces")

    def unexpected_generation(*args, **kwargs):
        raise AssertionError("pinned discovery generated current-model schemas")

    definition = next(t for t in mcp_tools.ADCP_TOOL_DEFINITIONS if t["name"] == "get_products")
    monkeypatch.setitem(definition, "inputSchema", SupersededSchema())
    monkeypatch.setitem(definition, "outputSchema", SupersededSchema())
    monkeypatch.setattr(mcp_tools, "_ensure_pydantic_schemas_applied", unexpected_generation)
    definitions = public_definitions(PINS[1])
    assert hashlib.sha256(canonical(definitions)).hexdigest() == (EXPECTED_PUBLIC_SHA256[PINS[1]])


def test_current_model_fallback_retains_exact_definitions_and_mutation_isolation():
    first = mcp_tools.get_tools_for_handler(SchemaHandler())
    names = {definition["name"] for definition in first}
    expected = [t for t in mcp_tools.ADCP_TOOL_DEFINITIONS if t["name"] in names]
    assert canonical(first) == canonical(expected)
    snapshot = canonical(first)
    for definition in first:
        mutable_ids(definition)
        definition["inputSchema"].clear()
    repeated = mcp_tools.get_tools_for_handler(SchemaHandler())
    assert canonical(repeated) == snapshot


def test_unsupported_public_pin_never_reuses_a_warm_supported_schema():
    public_definitions(PINS[1])
    with pytest.raises(ValueError, match="no bundled AdCP schemas"):
        public_definitions("3.2.0-rc.999")
    unsupported = loader.get_mcp_schema("get_products", "request", version="3.2.0-rc.999")
    assert unsupported is None


@pytest.mark.parametrize("version", PINS)
@pytest.mark.asyncio
async def test_mutation_cannot_change_mounted_discovery_registration_or_validation(version):
    initial = public_definitions(version)
    expected = next(t for t in initial if t["name"] == "get_products")
    expected_input = deepcopy(expected["inputSchema"])
    expected_output = deepcopy(expected["outputSchema"])
    expected["inputSchema"]["properties"]["brief"] = {"type": "array"}
    expected["outputSchema"].clear()
    handler = PinnedSchemaHandler(version)
    mcp = create_mcp_server(handler, stateless_http=True, allowed_hosts=["test"])
    mcp.settings.json_response = True
    app = mcp.streamable_http_app()
    headers = {"accept": "application/json, text/event-stream"}
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=True,
        ) as client:
            response = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers=headers,
            )
            assert response.status_code == 200
            tools = response.json()["result"]["tools"]
            advertised = next(t for t in tools if t["name"] == "get_products")
            assert advertised["inputSchema"] == expected_input
            assert advertised["outputSchema"] == expected_output
            response = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "get_products", "arguments": {"brief": []}},
                },
                headers=headers,
            )
            assert response.status_code == 200
            result = response.json()
            assert "error" in result or result.get("result", {}).get("isError") is True
    assert handler.calls == 0
    assert not validate_request("get_products", {"brief": []}, version=version).valid
    a2a = create_a2a_server(PinnedSchemaHandler(version), name="schema-materialization")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a2a), base_url="http://test"
    ) as client:
        for path in ("/.well-known/agent.json", "/.well-known/agent-card.json"):
            response = await client.get(path)
            assert response.status_code == 200
            assert "get_products" in {skill["id"] for skill in response.json()["skills"]}
    repeated = public_definitions(version)
    assert hashlib.sha256(canonical(repeated)).hexdigest() == (EXPECTED_PUBLIC_SHA256[version])


def test_cached_rc6_schema_retains_all_signed_summary_and_period_controls():
    from tests.test_rc6_adoption import patched

    fixture = json.loads(
        files("adcp")
        .joinpath("_compliance", PINS[1], "test-vectors/reporting-summary/complete-summary.json")
        .read_bytes()
    )
    cold = loader.get_mcp_schema("get_reporting_status", "sync", version=PINS[1])
    saved = canonical(cold)
    assert cold is not None
    cold.clear()
    warm = loader.get_mcp_schema("get_reporting_status", "sync", version=PINS[1])
    assert canonical(warm) == saved
    checker = FormatChecker()
    checker.checks("date-time")(loader._is_rfc3339_date_time)
    validator = validator_for(warm)(warm, format_checker=checker)
    assert len(fixture["cases"]) == 22
    for case in fixture["cases"]:
        payload = patched(fixture["response"], case["patch"])
        assert validator.is_valid(payload) is case["valid"], case["name"]
