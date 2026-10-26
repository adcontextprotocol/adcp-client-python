"""Every valid 3.2 payload converts down to a valid older payload, or fails structurally.

Sources: each bundled 3.2 schema example, the canonical-valid validation
parity cases, the ESA audience-evidence product and capabilities vectors, and
``tests/fixtures/version_conversion_sweep.json``. A conversion may raise
``ADCPVersionConversionError`` only where ``EXPECTED_STRUCTURED_ERRORS`` says
so; any other result must validate against the target release's bundled schema.
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

SOURCE = "3.2"
TARGETS = ("3.1", "3.0")
FIXTURES = Path(__file__).parent / "fixtures"

#: (case id, target) -> pointer of the intentional structured error.
EXPECTED_STRUCTURED_ERRORS = {
    # Test-controller scenarios added after 3.0 have no 3.0 spelling.
    ("comply_test_controller::request::example-10", "3.0"): "/scenario",
    ("comply_test_controller::sync::example-0", "3.0"): "/scenarios/1",
    # convert_payload cannot invent a legacy format_id; the creative dialect
    # adapters supply one before conversion on real 3.0 boundaries. A request
    # fails, while a response drops the creative and records the loss.
    ("sync_creatives::request::example-0", "3.0"): "/creatives/0",
    ("sync_creatives::request::example-1", "3.0"): "/creatives/0",
    ("sync_creatives::request::example-2", "3.0"): "/creatives/0",
    # 3.0 accounts need a brand/operator key; an account_id reference has none.
    ("sync_accounts::request::example-5", "3.0"): "/accounts/0",
    ("sync_accounts::request::example-6", "3.0"): "/accounts/0",
    # Wholesale discovery does not exist in 3.0.
    ("parity::get_signals.valid_wholesale", "3.0"): "",
    # 3.0 media_buy sellers must declare account capabilities.
    ("fixture::issue1621.get_adcp_capabilities.geo_regions", "3.0"): "",
    ("vector::audience_evidence.capabilities", "3.0"): "",
}


def _validate(tool: str, direction: str, payload: dict[str, Any], version: str) -> Any:
    validator = validate_request if direction == "request" else validate_response
    return validator(tool, payload, version=version)


def _cases() -> list[tuple[str, str, str, dict[str, Any]]]:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        cases = []
        for key in list_validator_keys(version=SOURCE):
            tool, direction = key.split("::")
            schema = get_portable_schema(tool, direction, version=SOURCE) or {}
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
        .joinpath("_compliance/3.2.4/test-vectors/audience-evidence/vectors.json")
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
    for case in json.loads((FIXTURES / "version_conversion_sweep.json").read_text())["cases"]:
        cases.append((f"fixture::{case['id']}", case["tool"], case["direction"], case["payload"]))
    return cases


CASES = _cases()


def test_sweep_covers_every_bundled_example_and_fixture() -> None:
    assert len({case_id for case_id, *_ in CASES}) == len(CASES)
    assert sum(case_id.startswith("fixture::") for case_id, *_ in CASES) == 4
    assert len(CASES) > 90


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize(
    ("case_id", "tool", "direction", "payload"), CASES, ids=[case[0] for case in CASES]
)
def test_downgrade_is_valid_in_the_target_or_a_structured_error(
    case_id: str, tool: str, direction: str, payload: dict[str, Any], target: str
) -> None:
    assert _validate(tool, direction, payload, SOURCE).valid, "sweep inputs must be valid 3.2"
    if get_portable_schema(tool, direction, version=target) is None:
        pytest.skip(f"{tool} {direction} does not exist in AdCP {target}")
    expected = EXPECTED_STRUCTURED_ERRORS.get((case_id, target))
    try:
        result = convert_payload(
            tool,
            payload,
            direction=direction,
            source_version=SOURCE,
            target_version=target,
        )
    except ADCPVersionConversionError as exc:
        assert expected is not None, f"unexpected structured error: {exc}"
        assert exc.pointer == expected
        return
    assert expected is None, "expected a structured error; update EXPECTED_STRUCTURED_ERRORS"
    outcome = _validate(tool, direction, result.payload, target)
    assert outcome.valid, outcome.issues
