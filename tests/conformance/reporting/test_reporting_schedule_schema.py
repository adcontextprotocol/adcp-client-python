"""#1179: execute the exact cached rejection and the version-scoped SDK correction.

This is Python evidence, not approval of another SDK or a cross-language pin.
Every designated blocking compatible lane must independently succeed with this
scenario; an unsupported-schema outcome is only valid in an unsupported lane.
"""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from jsonschema import Draft7Validator, FormatChecker
from jsonschema.validators import validator_for

from adcp.reporting.ledger import InMemoryReportingLedgerStore
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.types import GetReportingStatusResponse
from adcp.validation import schema_loader

from ._feed_support import MountedFeed, feed_harness
from ._generation_support import START, configuration
from ._projection_support import projection_harness

PIN = "3.2.0-rc.3"
RULE = "/allOf/2/then/not"
CACHED = (
    (
        "media-buy/get-reporting-status-response.json",
        30561,
        "498774fa2a15ce1487183d3df4f982436936427d104879279524ed5d38d8e726",
    ),
    (
        "bundled/media-buy/get-reporting-status-response.json",
        314212,
        "578ea8233c4022c528dee3b53bcb88884b36033c71d77a0eb00bee4bc67c72e3",
    ),
    (
        "mcp/2026-07-28/profiles/production/media-buy/get-reporting-status-response.json",
        171397,
        "6b68df71ef16d3317a20e2b4f23f4c385f1f9bbb36bddc80515c719e56868b74",
    ),
)


def formats():
    checker = FormatChecker()
    checker.checks("date-time")(schema_loader._is_rfc3339_date_time)
    return checker


async def deterministic_summary():
    store = InMemoryReportingLedgerStore(clock=lambda: START + timedelta(minutes=30))
    await store.put_configuration(replace(configuration(), deactivated_at=None))
    return await ReportingStatusHandler(store).handle(
        {"adcp_version": "3.2-rc.3"},
        caller=ReportingStatusCaller("acct_a", "https://buyer.example.test/agent"),
    )


def assert_original_rejection(raw, relative=CACHED[0][0]):
    validator = schema_loader.get_named_validator(relative, version=PIN)
    assert validator is not None
    if relative.startswith("mcp/"):
        validator = validator_for(validator.schema)(validator.schema, format_checker=formats())
    without_expectation = {k: v for k, v in raw.items() if k != "next_expected_at"}
    validator.validate(without_expectation)
    errors = list(validator.iter_errors(raw))
    assert len(errors) == 1, errors
    error = errors[0]
    assert error.validator == "not"
    assert list(error.absolute_schema_path) == ["allOf", 2, "then", "not"]
    return {
        "validator": error.validator,
        "schema_pointer": RULE,
        "instance_path": list(error.absolute_path),
        "message": error.message,
    }


def invalid_summaries(raw):
    for field in ("scope_closed", "coverage_complete"):
        for value in (False, None):
            invalid = deepcopy(raw)
            if value is None:
                del invalid["scope"][field]
            else:
                invalid["scope"][field] = value
            yield invalid
    for value in (None, 5, "2026-09-01", "2026-09-01T02:00:00", "not-a-time"):
        yield {**raw, "next_expected_at": value}
    yield {**raw, "health": "pretend-complete"}
    yield {**raw, "status": "pretend-completed"}
    yield {**raw, "view": "periods"}  # summary cannot evade the periods requirements
    invalid = deepcopy(raw)
    invalid["obligation_counts"]["total"] = "0"
    yield invalid


@pytest.mark.parametrize("relative,expected_bytes,expected_sha256", CACHED)
async def test_exact_unmodified_cached_rule_rejects_the_required_payload(
    relative, expected_bytes, expected_sha256
):
    root = schema_loader._resolve_schema_root(PIN)
    assert root is not None
    file = root.root / relative
    original = file.read_bytes()
    assert len(original) == expected_bytes
    assert hashlib.sha256(original).hexdigest() == expected_sha256
    schema = json.loads(original)
    assert schema["$schema"] == (
        "https://json-schema.org/draft/2020-12/schema"
        if relative.startswith("mcp/")
        else "http://json-schema.org/draft-07/schema#"
    )
    validator = schema_loader.get_named_validator(relative, version=PIN)
    assert validator is not None and validator.schema == schema
    raw = await deterministic_summary()
    assert raw["health"] == "complete"
    assert raw["scope"]["scope_closed"] is raw["scope"]["coverage_complete"] is True
    assert raw["next_expected_at"] == "2026-09-01T02:00:00Z"
    rejection = assert_original_rejection(raw, relative)
    effective = schema_loader._effective_task_schema(
        schema, "get_reporting_status", "sync", bundle_key=PIN
    )
    reconstructed = deepcopy(effective)
    reconstructed["allOf"][2]["then"]["not"] = {"required": ["next_expected_at"]}
    assert reconstructed == schema  # only this pointer differs; all guards survive
    assert file.read_bytes() == original
    print(
        json.dumps(
            {
                "cached_rule_reproduction": {
                    "schema_file": str(file),
                    "schema_uri": f"https://adcontextprotocol.org/schemas/{PIN}/{relative}",
                    "version": PIN,
                    "bytes": len(original),
                    "sha256": expected_sha256,
                    "dialect": schema["$schema"],
                    "payload": raw,
                    "exact_rejection": rejection,
                    "effective_correction": {"removed_pointer": RULE, "other_changes": []},
                }
            },
            sort_keys=True,
        )
    )


async def test_effective_validation_advertisement_and_remaining_constraints():
    raw = await deterministic_summary()
    GetReportingStatusResponse.model_validate(raw)
    validator = schema_loader.get_validator("get_reporting_status", "sync", version=PIN)
    assert validator is not None
    validators = [validator]
    for load in (
        schema_loader.get_schema,
        schema_loader.get_portable_schema,
        schema_loader.get_mcp_schema,
    ):
        schema = load("get_reporting_status", "sync", version=PIN)
        assert schema is not None
        validators.append(validator_for(schema)(deepcopy(schema), format_checker=formats()))
        saved = deepcopy(schema)
        schema.clear()
        assert load("get_reporting_status", "sync", version=PIN) == saved
    for validator in validators:
        validator.validate(raw)
        for invalid in invalid_summaries(raw):
            assert not validator.is_valid(invalid), invalid


@pytest.mark.parametrize("mutation", ["other-pin", "changed-if", "changed-then", "moved-rule"])
def test_correction_is_limited_to_the_known_rule_and_version(mutation):
    schema = schema_loader.get_named_schema_document(CACHED[0][0], version=PIN)
    assert schema is not None
    version = PIN
    if mutation == "other-pin":
        version = "3.2.0-beta.4"
    elif mutation == "changed-if":
        schema["allOf"][2]["if"]["properties"]["health"]["const"] = "healthy"
    elif mutation == "changed-then":
        schema["allOf"][2]["then"]["required"] = ["unreviewed-condition"]
    else:
        schema["allOf"].insert(0, {})
    original = deepcopy(schema)
    assert (
        schema_loader._effective_task_schema(
            schema, "get_reporting_status", "sync", bundle_key=version
        )
        == original
    )
    assert schema == original


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("mode", ["core", "projection"])
@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("version", [None, PIN])
async def test_complete_future_expectation_on_actual_summary_mounts(
    backend, mode, notifications, version
):
    factory = feed_harness if mode == "core" else projection_harness
    async with factory(backend, notifications=notifications) as h:
        h.clock.now = START + timedelta(minutes=30)
        h.store._clock = h.clock  # supported deterministic store clock, including PG captures
        config = replace(configuration(), deactivated_at=None)
        await h.store.put_configuration(config)
        if mode == "projection":
            await h.projection.activate(account_id=config.account_id)
        mounted = MountedFeed(h, version=version, hydrated=True, registry_kind="oauth")
        identity = SimpleNamespace(
            obligation=config,
            binding=SimpleNamespace(consumer_id="https://buyer.example.test/agent"),
        )
        mounted.authorize(identity)
        request = {
            "adcp_version": "3.2-rc.3",
            "account": {"account_id": config.account_id},
            "view": "summary",
        }
        async with mounted.client() as client:
            _, inventory = await mounted.mcp(client, inventory=True)
            output = next(
                t["outputSchema"] for t in inventory["tools"] if t["name"] == "get_reporting_status"
            )
            advertised = Draft7Validator(output, format_checker=formats())
            for path in ("/.well-known/agent.json", "/.well-known/agent-card.json"):
                card = await client.get(path)
                assert card.status_code == 200
                assert "get_reporting_status" in {s["id"] for s in card.json()["skills"]}
            results = []
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                if transport == "mcp":
                    _, raw = await mounted.mcp(client, request)
                else:
                    _, raw = await mounted.a2a(client, request, v1=transport == "a2a-1.0")
                assert raw.get("health") == "complete", raw
                assert raw["next_expected_at"] == "2026-09-01T02:00:00Z"
                assert_original_rejection(raw)
                schema_loader.get_validator("get_reporting_status", "sync", version=PIN).validate(
                    raw
                )
                advertised.validate(raw)
                GetReportingStatusResponse.model_validate(raw)
                for invalid in invalid_summaries(raw):
                    assert not advertised.is_valid(invalid), invalid
                results.append(raw)
            assert results[0] == results[1] == results[2]
