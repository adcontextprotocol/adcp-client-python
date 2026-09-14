"""AdCP 3.2.0-rc.3 media-buy runtime impact: null-clear and frequency caps.

Two rc.3 changesets have behaviour the schemas alone cannot deliver:

* **#7466** introduced `core/targeting-input.json`, where `null` on a dimension
  is a *clear command*. Every client request serializes with
  `exclude_none=True`, which silently discards it.
* **#7449 / d44756a** added the MediaBuy-level `frequency_cap` (one counter
  shared across packages), its `update_media_buy_frequency_cap` action, and a
  package-qualified `update_frequency_caps`. The action is structured-only, so
  it is absent from the deprecated flat enum the SDK's whitelists were built
  from.

Both fail *silently* in the schema-only version: the request succeeds, the
mutation does not happen, and nothing says so. Every test below names the wrong
answer it prevents.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp._null_clear import nullable_request_paths, preserve_explicit_nulls
from adcp.media_buy_actions import action_update_fields, route_media_buy_action
from adcp.types import ControlMediaBuyRequest, UpdateMediaBuyRequest
from adcp.validation import validate_request


def _control(**patch: Any) -> ControlMediaBuyRequest:
    return ControlMediaBuyRequest.model_validate(
        {
            "idempotency_key": "rc3-control-00000001",
            "account": {"account_id": "account-1"},
            "media_buy_id": "buy-1",
            "revision": 1,
            **patch,
        }
    )


def _update(**patch: Any) -> UpdateMediaBuyRequest:
    return UpdateMediaBuyRequest.model_validate(
        {
            "idempotency_key": "rc3-update-000000001",
            "account": {"account_id": "account-1"},
            "media_buy_id": "buy-1",
            **patch,
        }
    )


def _serialize(request: Any, task: str) -> dict[str, Any]:
    """Exactly what the client puts on the wire."""
    params = request.model_dump(mode="json", exclude_none=True)
    return preserve_explicit_nulls(request, params, task_name=task)


# -- which paths are nullable ------------------------------------------------


def test_nullable_paths_come_from_the_schema_not_the_annotation() -> None:
    # Every optional generated field is annotated `T | None = None`, so the
    # annotation cannot tell "nullable on the wire" from "optional in Python".
    # `canceled` is the cautionary case: the schema permits only `true`, so
    # emitting null would turn a meaningless keyword argument into a request no
    # seller can accept.
    paths = nullable_request_paths("control_media_buy")
    assert ("frequency_cap",) in paths
    assert ("daily_budget_cap",) in paths
    assert ("budget_cap_timezone",) in paths
    assert ("packages", "daily_budget_cap") in paths
    assert ("canceled",) not in paths
    assert ("packages", "canceled") not in paths
    assert ("paused",) not in paths
    assert ("media_buy_id",) not in paths


def test_targeting_input_dimensions_are_discovered_under_packages() -> None:
    # rc.3's whole point: a dimension inside targeting_overlay can be cleared.
    # Recording the path under `packages` without an index is what makes it
    # match any element at apply time.
    paths = nullable_request_paths("update_media_buy")
    assert ("packages", "targeting_overlay", "geo_countries") in paths
    assert ("new_packages", "targeting_overlay", "geo_countries") in paths


def test_an_unknown_task_degrades_instead_of_raising() -> None:
    # An older pin, or a task this bundle does not define, must fall back to
    # today's exclude_none behaviour rather than failing the request.
    assert nullable_request_paths("not_a_real_adcp_task") == frozenset()


# -- the three-state contract ------------------------------------------------


def test_an_omitted_field_stays_absent() -> None:
    wire = _serialize(_control(paused=True), "control_media_buy")
    assert "frequency_cap" not in wire
    assert "daily_budget_cap" not in wire


def test_an_explicit_null_frequency_cap_survives_as_a_clear() -> None:
    # The regression this module exists for. Before the fix the key was
    # dropped, so "remove the cap" became "leave the cap alone" -- and the
    # request still succeeded.
    wire = _serialize(_control(frequency_cap=None), "control_media_buy")
    assert wire["frequency_cap"] is None
    assert validate_request("control_media_buy", wire).valid


def test_an_explicit_null_frequency_cap_survives_on_update_too() -> None:
    wire = _serialize(_update(frequency_cap=None), "update_media_buy")
    assert wire["frequency_cap"] is None
    assert validate_request("update_media_buy", wire).valid


def test_a_replacement_frequency_cap_round_trips() -> None:
    cap = {"max_impressions": 3, "per": "individuals", "window": {"interval": 1, "unit": "days"}}
    wire = _serialize(_update(frequency_cap=cap), "update_media_buy")
    assert wire["frequency_cap"]["max_impressions"] == 3
    assert validate_request("update_media_buy", wire).valid


def test_a_meaningless_explicit_null_is_still_dropped() -> None:
    # `canceled` is Literal[True] in the schema. Restoring this null would
    # produce a request every seller must reject, so schema-awareness is not a
    # nicety -- it is what keeps the fix from breaking working callers.
    request = _control(paused=True)
    assert request.canceled is None
    assert "canceled" in request.model_fields_set or request.canceled is None
    wire = _serialize(request, "control_media_buy")
    assert "canceled" not in wire
    assert validate_request("control_media_buy", wire).valid


def test_numeric_zero_is_not_confused_with_a_clear() -> None:
    # 0 and null are different commands: a zero daily cap pauses delivery for
    # the day, a null removes the cap entirely.
    wire = _serialize(_control(daily_budget_cap=0), "control_media_buy")
    assert wire["daily_budget_cap"] == 0
    cleared = _serialize(_control(daily_budget_cap=None), "control_media_buy")
    assert cleared["daily_budget_cap"] is None


def test_a_cleared_targeting_dimension_survives_inside_a_package() -> None:
    # Nested and inside a list, which is where a naive top-level-only fix
    # would quietly stop working.
    wire = _serialize(
        _update(
            packages=[
                {
                    "package_id": "pkg-1",
                    "targeting_overlay": {"geo_countries": None},
                }
            ]
        ),
        "update_media_buy",
    )
    overlay = wire["packages"][0]["targeting_overlay"]
    assert overlay == {"geo_countries": None}
    assert validate_request("update_media_buy", wire).valid


def test_clears_survive_at_the_right_index_in_a_multi_package_patch() -> None:
    # zip() walks the models and the dumped list in lockstep; an off-by-one
    # here would clear a dimension on the wrong package.
    wire = _serialize(
        _update(
            packages=[
                {"package_id": "pkg-1", "targeting_overlay": {"geo_countries": ["US"]}},
                {"package_id": "pkg-2", "targeting_overlay": {"geo_countries": None}},
            ]
        ),
        "update_media_buy",
    )
    assert wire["packages"][0]["targeting_overlay"]["geo_countries"] == ["US"]
    assert wire["packages"][1]["targeting_overlay"]["geo_countries"] is None
    assert validate_request("update_media_buy", wire).valid


def test_a_replaced_dimension_is_not_turned_into_a_clear() -> None:
    wire = _serialize(
        _update(
            packages=[{"package_id": "pkg-1", "targeting_overlay": {"geo_countries": ["US", "CA"]}}]
        ),
        "update_media_buy",
    )
    assert wire["packages"][0]["targeting_overlay"]["geo_countries"] == ["US", "CA"]


# -- frequency-cap actions ---------------------------------------------------


def test_the_structured_only_action_routes_instead_of_failing() -> None:
    # `update_media_buy_frequency_cap` is absent from the deprecated flat
    # `media_buy_valid_action` enum, so an SDK built only on that enum rejects
    # a valid rc.3 mutation as invalid_action.
    assert route_media_buy_action("update_media_buy_frequency_cap") is not None
    assert route_media_buy_action("update_media_buy_frequency_cap").value == "control_media_buy"


def test_update_fields_merges_both_enum_metadata_blocks() -> None:
    # rc.3 is explicit that SDKs MUST merge the flat enum's block with
    # `core/media-buy-available-action-id.json`. Reading only the first omits
    # the aggregate cap entirely.
    fields = action_update_fields()
    assert fields["update_media_buy_frequency_cap"] == ("frequency_cap",)
    # And the pre-existing block is still there, now package-qualified.
    assert fields["update_frequency_caps"] == ("packages[].targeting_overlay.frequency_cap",)
    assert fields["pause"] == ("paused",)


def test_the_aggregate_cap_is_kept_distinct_from_per_package_capping() -> None:
    # Two different things: `update_frequency_caps` is per-package
    # targeting-overlay capping; the aggregate action replaces or clears one
    # counter shared across packages. Collapsing them would let a seller that
    # advertised only per-package capping appear to support the aggregate cap.
    fields = action_update_fields()
    assert fields["update_media_buy_frequency_cap"] != fields["update_frequency_caps"]


def test_the_aggregate_cap_is_not_rolled_up_under_a_coarse_legacy_action() -> None:
    from adcp.decisioning.update_media_buy import _ACTION_CANDIDATES

    # No coarse fallbacks: a seller advertising the legacy `update_packages`
    # has not thereby advertised the aggregate cap.
    assert _ACTION_CANDIDATES["update_media_buy_frequency_cap"] == (
        "update_media_buy_frequency_cap",
    )


def test_an_aggregate_cap_mutation_is_routed_not_reported_unknown() -> None:
    from adcp.decisioning.update_media_buy import (
        UNKNOWN_UPDATE_ACTION,
        decompose_update_media_buy,
    )

    patch = {
        "media_buy_id": "buy-1",
        "frequency_cap": {
            "max_impressions": 3,
            "per": "individuals",
            "window": {"interval": 1, "unit": "days"},
        },
    }
    mutations = decompose_update_media_buy(patch, {"media_buy_id": "buy-1"})
    actions = {mutation.action for mutation in mutations}
    assert "update_media_buy_frequency_cap" in actions
    assert UNKNOWN_UPDATE_ACTION not in actions


def test_clearing_the_aggregate_cap_is_also_routed() -> None:
    from adcp.decisioning.update_media_buy import decompose_update_media_buy

    current = {
        "media_buy_id": "buy-1",
        "frequency_cap": {
            "max_impressions": 3,
            "per": "individuals",
            "window": {"interval": 1, "unit": "days"},
        },
    }
    mutations = decompose_update_media_buy(
        {"media_buy_id": "buy-1", "frequency_cap": None}, current
    )
    match = next(m for m in mutations if m.action == "update_media_buy_frequency_cap")
    assert match.after is None
    assert match.before is not None


@pytest.mark.parametrize(
    "action",
    ["update_frequency_caps", "update_media_buy_frequency_cap"],
)
def test_both_frequency_cap_actions_route_somewhere(action: str) -> None:
    assert route_media_buy_action(action) is not None
