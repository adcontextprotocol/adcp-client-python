"""The spec's report-definition vectors, run through the SDK's validator.

AdCP 3.3.0-beta.6 (adcontextprotocol/adcp#8983) adds report-definition
``contract_version`` 1.2: a definition whose ``dimensions`` include
``media_buy_id`` must also include ``package_id``. The signed bundle ships
reference verdicts in ``test-vectors/reporting-report-definition/``. Each case
replaces whole top-level members of the base definition, and
``expected.schema_valid`` is the verdict of ``core/reporting-report-definition.json``;
the SDK's packaged validator must reach the same one. Advisory findings (a 1.0
or 1.1 media-buy definition without ``package_id``) never fail a definition and
are not graded here.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from typing import Any

import pytest

from adcp import get_adcp_spec_version
from adcp.validation.schema_loader import get_named_validator

VERSION = get_adcp_spec_version()
ROOT = files("adcp").joinpath("_compliance", VERSION, "test-vectors", "reporting-report-definition")
VECTORS: dict[str, Any] = json.loads(ROOT.joinpath("vectors.json").read_text())
BASE: dict[str, Any] = json.loads(ROOT.joinpath(VECTORS["base"]).read_text())


def test_the_vectors_cover_the_rule_and_its_exemptions() -> None:
    verdicts = {case["expected"]["schema_valid"] for case in VECTORS["cases"]}
    assert verdicts == {True, False}
    assert BASE["contract_version"] == "1.2"


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["id"])
def test_report_definition_vector(case: dict[str, Any]) -> None:
    validator = get_named_validator("core/reporting-report-definition.json", version=VERSION)
    assert validator is not None
    definition = {**deepcopy(BASE), **deepcopy(case["overrides"])}
    assert validator.is_valid(definition) is case["expected"]["schema_valid"], case["id"]
