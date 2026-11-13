"""An open object keeps the keys it does not declare (#1614).

JSON Schema reads an absent ``additionalProperties`` as open, and the
generator sets ``extra='forbid'`` only where a schema object is closed. In
SDK 9 ``AdCPBaseModel`` defaulted to ``extra='ignore'``, so a property the
pinned model did not declare -- a field a newer spec added, a sibling arm's
property the schema admits -- was dropped on parse and absent after dump. SDK
10 defaults to ``extra='allow'``: the key survives in ``model_extra`` and is
written back, ``null`` included. ``tests/conformance/test_wire_round_trip.py``
holds every generated model to it with no ``field_dropped`` allowance.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ConfigDict, ValidationError

from adcp.types.base import AdCPBaseModel


class Open(AdCPBaseModel):
    x: int | None = None


class Closed(AdCPBaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int | None = None


def test_the_default_policy_is_allow() -> None:
    assert AdCPBaseModel.model_config["extra"] == "allow"
    assert Open.model_config["extra"] == "allow"


def test_an_unknown_key_survives_parse_and_dump() -> None:
    document = {"x": 1, "added_in_3_4": {"nested": [1, 2]}, "nothing": None}
    parsed = Open.model_validate_json(json.dumps(document))
    assert parsed.model_extra == {"added_in_3_4": {"nested": [1, 2]}, "nothing": None}
    assert parsed.model_dump(mode="json") == document
    assert json.loads(parsed.model_dump_json()) == document


def test_include_and_exclude_apply_to_a_null_unknown_key() -> None:
    parsed = Open.model_validate({"x": 1, "nothing": None})
    assert parsed.model_dump(mode="json", exclude={"nothing"}) == {"x": 1}
    assert parsed.model_dump(mode="json", include={"x"}) == {"x": 1}


def test_a_stray_keyword_argument_goes_to_the_wire() -> None:
    """Documented: a closed schema object refuses it; an open one carries it."""
    assert Open(x=1, stray="s").model_dump(mode="json") == {"x": 1, "stray": "s"}
    with pytest.raises(ValidationError):
        Closed(x=1, stray="s")


def test_a_generated_closed_object_refuses_an_unknown_key() -> None:
    # AdCP 3.3.0-beta.6 opens every schema without a structural reason to stay
    # closed (DR-0036); the bidding policy's mutually exclusive modes keep it closed.
    from adcp.types.domains.core.bidding_policy import BiddingPolicy

    assert BiddingPolicy.model_config["extra"] == "forbid"
    BiddingPolicy.model_validate({"automatic": True})
    with pytest.raises(ValidationError):
        BiddingPolicy.model_validate({"automatic": True, "unknown": 1})


def test_a_generated_open_object_keeps_an_unknown_key() -> None:
    from adcp.types.domains.core.targeting_input import TargetingOverlayInput

    assert TargetingOverlayInput.model_config["extra"] == "allow"
    document = {"geo_regions": ["US-NY"], "added_in_3_4": {"nested": None}, "nothing": None}
    parsed = TargetingOverlayInput.model_validate_json(json.dumps(document))
    assert parsed.model_extra == {"added_in_3_4": {"nested": None}, "nothing": None}
    assert parsed.model_dump(mode="json") == document


def test_the_generated_tree_follows_its_schemas() -> None:
    """Closed schema objects forbid; everything else allows. Nothing ignores."""
    import importlib
    import pkgutil

    import adcp.types.domains as domains

    policies: dict[str, int] = {}
    for info in pkgutil.walk_packages(domains.__path__, prefix=domains.__name__ + "."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue
        for obj in list(vars(module).values()):
            if (
                isinstance(obj, type)
                and issubclass(obj, AdCPBaseModel)
                and obj.__module__ == module.__name__
            ):
                policy = str(obj.model_config.get("extra"))
                policies[policy] = policies.get(policy, 0) + 1
    assert set(policies) <= {"allow", "forbid"}, policies
    assert policies.get("ignore", 0) == 0
    # Since 3.3.0-beta.6 (DR-0036) a schema is closed only for a structural
    # reason, so closed objects are a small minority.
    assert policies["allow"] > 1000 and policies["forbid"] > 50, policies
