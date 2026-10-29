"""Every valid newer payload converts down to a valid older payload, or fails structurally.

Each source release is swept to every older release this SDK converts to:
3.2 to 3.1 and 3.0, and 3.3-beta.1 to 3.2, 3.1 and 3.0. Sources: each bundled
schema example of the source release, its ESA audience-evidence product and
capabilities vectors, the canonical-valid validation parity cases, and the
release's fixture file (``tests/fixtures/version_conversion_sweep*.json``).
The 3.3 sweep also re-runs the 3.2 fixtures, which are valid 3.3 payloads. A
conversion may raise ``ADCPVersionConversionError`` only where
``EXPECTED_STRUCTURED_ERRORS`` says so; any other result must validate against
the target release's bundled schema.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from adcp.compat.versions import ADCPVersionConversionError, convert_payload
from adcp.validation import validate_request, validate_response
from adcp.validation.schema_loader import get_portable_schema, list_validator_keys

FIXTURES = Path(__file__).parent / "fixtures"
#: Source release -> the older releases it is converted to.
TARGETS = {"3.2": ("3.1", "3.0"), "3.3-beta.1": ("3.2", "3.1", "3.0")}
#: Source release -> its fixture files and compliance bundle (test vectors).
FIXTURE_FILES = {
    "3.2": ("version_conversion_sweep.json",),
    "3.3-beta.1": ("version_conversion_sweep_3_3.json", "version_conversion_sweep.json"),
}
COMPLIANCE = {"3.2": "3.2.4", "3.3-beta.1": "3.3.0-beta.1"}

#: (case id, target) -> pointer of the intentional structured error, per source.
EXPECTED_STRUCTURED_ERRORS: dict[str, dict[tuple[str, str], str]] = {
    "3.2": {
        # Test-controller scenarios added after 3.0 have no 3.0 spelling.
        ("comply_test_controller::request::example-10", "3.0"): "/scenario",
        ("comply_test_controller::sync::example-0", "3.0"): "/scenarios/1",
        # convert_payload cannot invent a legacy format_id; the creative dialect
        # adapters supply one before conversion on real 3.0 boundaries.
        ("list_creatives::sync::example-0", "3.0"): "/creatives/1",
        ("list_creatives::sync::example-1", "3.0"): "/creatives/1",
        ("sync_creatives::request::example-0", "3.0"): "/creatives/0",
        ("sync_creatives::request::example-1", "3.0"): "/creatives/0",
        ("sync_creatives::request::example-2", "3.0"): "/creatives/1",
        # Older accounts need a brand/operator key; an account_id reference has none.
        ("sync_accounts::request::example-5", "3.0"): "/accounts/0",
        ("sync_accounts::request::example-6", "3.0"): "/accounts/0",
        ("sync_accounts::sync::example-5", "3.1"): "",
        ("sync_accounts::sync::example-5", "3.0"): "",
        # Wholesale discovery does not exist in 3.0.
        ("parity::get_signals.valid_wholesale", "3.0"): "",
        # 3.0 media_buy sellers must declare account capabilities.
        ("fixture::issue1621.get_adcp_capabilities.geo_regions", "3.0"): "",
        ("vector::audience_evidence.capabilities", "3.0"): "",
        # Requests are never repaired: a config left without older event types
        # fails instead of silently changing the subscription.
        (
            "fixture::sync_accounts.request.notification_configs",
            "3.1",
        ): "/accounts/0/notification_configs/0/event_types",
        (
            "fixture::sync_accounts.request.notification_configs",
            "3.0",
        ): "/accounts/0/notification_configs/0/event_types",
    },
    "3.3-beta.1": {
        # Test-controller scenarios added after 3.0 have no 3.0 spelling.
        ("comply_test_controller::request::example-7", "3.0"): "/scenario",
        ("comply_test_controller::request::example-11", "3.0"): "/scenario",
        ("comply_test_controller::sync::example-0", "3.0"): "/scenarios/1",
        # The forced hold-failure arm (``unable``) answers a 3.3-only
        # controller scenario; no older buyer could have registered it.
        ("comply_test_controller::sync::example-9", "3.2"): "/forced/arm",
        ("comply_test_controller::sync::example-9", "3.1"): "/forced/arm",
        ("comply_test_controller::sync::example-9", "3.0"): "/forced/arm",
        # As in the 3.2 sweep: no legacy format_id, no older account key,
        # no 3.0 wholesale discovery, no 3.0 account capabilities.
        ("list_creatives::sync::example-0", "3.0"): "/creatives/1",
        ("list_creatives::sync::example-1", "3.0"): "/creatives/1",
        ("sync_creatives::request::example-0", "3.0"): "/creatives/0",
        ("sync_creatives::request::example-1", "3.0"): "/creatives/0",
        ("sync_creatives::request::example-2", "3.0"): "/creatives/1",
        ("sync_accounts::request::example-5", "3.0"): "/accounts/0",
        ("sync_accounts::request::example-6", "3.0"): "/accounts/0",
        ("sync_accounts::sync::example-5", "3.1"): "",
        ("sync_accounts::sync::example-5", "3.0"): "",
        ("parity::get_signals.valid_wholesale", "3.0"): "",
        ("fixture::issue1621.get_adcp_capabilities.geo_regions", "3.0"): "",
        ("vector::audience_evidence.capabilities", "3.0"): "",
        (
            "fixture::sync_accounts.request.notification_configs",
            "3.1",
        ): "/accounts/0/notification_configs/0/event_types",
        (
            "fixture::sync_accounts.request.notification_configs",
            "3.0",
        ): "/accounts/0/notification_configs/0/event_types",
        # Webhook ``authentication`` is optional in 3.3 and required below it.
        # The SDK never invents a credential, and a request is never repaired.
        **{
            (f"fixture::issue1667.create_media_buy.{webhook}_without_authentication", target): (
                f"/{webhook}"
            )
            for webhook in ("reporting_webhook", "artifact_webhook")
            for target in ("3.2", "3.1", "3.0")
        },
        # Minute-level dayparts are refused, never rounded to whole hours.
        **{
            (
                "fixture::issue1667.create_media_buy.daypart_clock_times",
                target,
            ): "/packages/0/targeting_overlay/daypart_targets/1/start_time"
            for target in ("3.2", "3.1", "3.0")
        },
        # 3.0 signals require coverage_percentage and pricing_options, which
        # the 3.3 listing never had (the 3.2 and 3.1 conversions succeed).
        ("fixture::issue1667.get_signals.age_determination_bases", "3.0"): "/signals/0",
    },
}


def _validate(tool: str, direction: str, payload: dict[str, Any], version: str) -> Any:
    validator = validate_request if direction == "request" else validate_response
    return validator(tool, payload, version=version)


def _cases(source: str) -> list[tuple[str, str, str, str, dict[str, Any]]]:
    cases = []
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        for key in list_validator_keys(version=source):
            tool, direction = key.split("::")
            schema = get_portable_schema(tool, direction, version=source) or {}
            for index, example in enumerate(schema.get("examples") or []):
                data = example.get("data") if isinstance(example, dict) else None
                if isinstance(data, dict):
                    cases.append((f"{key}::example-{index}", tool, direction, data))
    finally:
        logging.disable(previous)
    for case in json.loads((FIXTURES / "validation-parity-cases.json").read_text()):
        if case["canonical_valid"]:
            cases.append(
                (f"parity::{case['id']}", case["tool"], case["direction"], case["payload"])
            )
    vectors = json.loads(
        files("adcp")
        .joinpath(f"_compliance/{COMPLIANCE[source]}/test-vectors/audience-evidence/vectors.json")
        .read_text()
    )
    product = deepcopy(vectors["product"])
    product["audience_evidence"] = [vectors["evidence"]]
    product["included_signals"] = [
        {
            "signal_ref": {"scope": "product", "signal_id": "adults_25_34"},
            "name": "Adults 25-34",
            "value_type": "binary",
            "demographic_predicate": {"age": {"min": 25, "max": 34, "include_unknown": False}},
            "restricted_attributes": ["age"],
        }
    ]
    cases.append(
        (
            "vector::audience_evidence.get_products",
            "get_products",
            "sync",
            {"status": "completed", "cache_scope": "public", "products": [product]},
        )
    )
    cases.append(
        (
            "vector::audience_evidence.capabilities",
            "get_adcp_capabilities",
            "sync",
            vectors["capability_response"],
        )
    )
    for name in FIXTURE_FILES[source]:
        for case in json.loads((FIXTURES / name).read_text())["cases"]:
            cases.append(
                (f"fixture::{case['id']}", case["tool"], case["direction"], case["payload"])
            )
    return [(source, *case) for case in cases]


CASES = [case for source in TARGETS for case in _cases(source)]
SWEEP = [(*case, target) for case in CASES for target in TARGETS[case[0]]]


def _id(case: tuple[Any, ...]) -> str:
    return f"{case[0]}::{case[1]}" + (f"->{case[-1]}" if len(case) == 6 else "")


def test_sweep_covers_every_bundled_example_and_fixture() -> None:
    for source in TARGETS:
        ids = [case_id for case_source, case_id, *_ in CASES if case_source == source]
        assert len(set(ids)) == len(ids)
        fixtures = sum(
            len(json.loads((FIXTURES / name).read_text())["cases"])
            for name in FIXTURE_FILES[source]
        )
        assert sum(case_id.startswith("fixture::") for case_id in ids) == fixtures
        assert len(ids) > 90
        # Every expectation names a case and target that the sweep runs.
        runs = {(case_id, target) for case_id in ids for target in TARGETS[source]}
        assert EXPECTED_STRUCTURED_ERRORS[source].keys() <= runs


def _convert(
    source: str, case_id: str, tool: str, direction: str, payload: dict[str, Any], target: str
) -> None:
    expected = EXPECTED_STRUCTURED_ERRORS[source].get((case_id, target))
    try:
        result = convert_payload(
            tool,
            payload,
            direction=direction,
            source_version=source,
            target_version=target,
        )
    except ADCPVersionConversionError as exc:
        assert expected is not None, f"unexpected structured error: {exc}"
        assert exc.pointer == expected
        return
    assert expected is None, "expected a structured error; update EXPECTED_STRUCTURED_ERRORS"
    outcome = _validate(tool, direction, result.payload, target)
    assert outcome.valid, outcome.issues


@pytest.mark.parametrize(
    ("source", "case_id", "tool", "direction", "payload", "target"),
    SWEEP,
    ids=[_id(case) for case in SWEEP],
)
def test_downgrade_is_valid_in_the_target_or_a_structured_error(
    source: str, case_id: str, tool: str, direction: str, payload: dict[str, Any], target: str
) -> None:
    if get_portable_schema(tool, direction, version=target) is None:
        pytest.skip(f"{tool} {direction} does not exist in AdCP {target}")
    assert _validate(tool, direction, payload, source).valid, f"sweep inputs must be valid {source}"
    _convert(source, case_id, tool, direction, payload, target)


STATUSLESS = [case for case in SWEEP if case[3] != "request" and "status" in case[4]]


@pytest.mark.parametrize(
    ("source", "case_id", "tool", "direction", "payload", "target"),
    STATUSLESS,
    ids=[_id(case) for case in STATUSLESS],
)
def test_status_defaulted_responses_get_the_same_guarantee(
    source: str, case_id: str, tool: str, direction: str, payload: dict[str, Any], target: str
) -> None:
    # The SDK validators default a missing status envelope; so does the
    # downgrade's final check, which must not switch off without it.
    payload = {key: value for key, value in payload.items() if key != "status"}
    if get_portable_schema(tool, direction, version=target) is None:
        pytest.skip(f"{tool} {direction} does not exist in AdCP {target}")
    if not _validate(tool, direction, payload, source).valid:
        pytest.skip(f"not valid {source} without its status")
    _convert(source, case_id, tool, direction, payload, target)
