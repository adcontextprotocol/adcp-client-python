from __future__ import annotations

from adcp.decisioning import (
    UNKNOWN_UPDATE_ACTION,
    decompose_update_media_buy,
    disallowed_update_media_buy_mutations,
    is_update_media_buy_mutation_allowed,
    normalize_update_media_buy_allowed_actions,
    requested_update_media_buy_actions,
)


def test_decompose_update_media_buy_accepts_pydantic_request() -> None:
    from adcp.types import UpdateMediaBuyRequest

    req = UpdateMediaBuyRequest(
        account={"account_id": "acct_a"},
        media_buy_id="mb_1",
        idempotency_key="idem_bbbb1234567890",
        paused=True,
        packages=[{"package_id": "pkg_1", "budget": 125.0, "pacing": "even"}],
    )

    mutations = decompose_update_media_buy(req)

    assert [mutation.action for mutation in mutations] == [
        "pause",
        "update_budget",
        "update_pacing",
    ]
    assert mutations[0].field_paths == ("paused",)
    assert mutations[1].field_paths == ("packages[0].budget",)
    assert mutations[1].package_id == "pkg_1"
    assert mutations[1].resolution == "coarse"
    assert mutations[1].allowed_action_candidates == ("update_budget", "update_packages")


def test_decompose_update_media_buy_extracts_top_level_actions() -> None:
    patch = {
        "account": {"account_id": "acct_a"},
        "media_buy_id": "mb_1",
        "idempotency_key": "idem_bbbb1234567890",
        "canceled": True,
        "cancellation_reason": "buyer request",
        "new_packages": [{"product_id": "prod_1"}],
    }

    mutations = decompose_update_media_buy(patch)

    assert [mutation.action for mutation in mutations] == ["cancel", "add_packages"]
    assert mutations[0].field_paths == ("canceled", "cancellation_reason")
    assert mutations[0].after == {
        "canceled": True,
        "cancellation_reason": "buyer request",
    }
    assert mutations[1].field_paths == ("new_packages",)


def test_decompose_update_media_buy_uses_current_state_for_budget_actions() -> None:
    current = {"packages": [{"package_id": "pkg_1", "budget": 100.0}]}

    increase = decompose_update_media_buy(
        {"packages": [{"package_id": "pkg_1", "budget": 125.0}]},
        current,
    )
    decrease = decompose_update_media_buy(
        {"packages": [{"package_id": "pkg_1", "budget": 80.0}]},
        current,
    )

    assert [mutation.action for mutation in increase] == ["increase_budget"]
    assert increase[0].before == 100.0
    assert increase[0].after == 125.0
    assert increase[0].allowed_action_candidates == (
        "increase_budget",
        "update_budget",
        "update_packages",
    )
    assert [mutation.action for mutation in decrease] == ["decrease_budget"]


def test_decompose_update_media_buy_detects_budget_reallocation_batch() -> None:
    current = {
        "packages": [
            {"package_id": "pkg_1", "budget": 100.0},
            {"package_id": "pkg_2", "budget": 200.0},
        ]
    }

    mutations = decompose_update_media_buy(
        {
            "packages": [
                {"package_id": "pkg_1", "budget": 125.0},
                {"package_id": "pkg_2", "budget": 175.0},
            ]
        },
        current,
    )

    assert [mutation.action for mutation in mutations] == ["reallocate_budget"]
    assert mutations[0].field_paths == ("packages[0].budget", "packages[1].budget")
    assert mutations[0].before == {"pkg_1": 100.0, "pkg_2": 200.0}
    assert mutations[0].after == {"pkg_1": 125.0, "pkg_2": 175.0}


def test_decompose_update_media_buy_uses_current_state_for_flight_date_actions() -> None:
    current = {"end_time": "2026-06-01T00:00:00Z"}

    extend = decompose_update_media_buy({"end_time": "2026-06-15T00:00:00Z"}, current)
    shorten = decompose_update_media_buy({"end_time": "2026-05-15T00:00:00Z"}, current)
    unknown_direction = decompose_update_media_buy({"end_time": "2026-06-15T00:00:00Z"})

    assert [mutation.action for mutation in extend] == ["extend_flight"]
    assert extend[0].resolution == "fine"
    assert [mutation.action for mutation in shorten] == ["shorten_flight"]
    assert [mutation.action for mutation in unknown_direction] == ["update_dates"]
    assert unknown_direction[0].resolution == "coarse"


def test_decompose_update_media_buy_splits_targeting_and_frequency_cap() -> None:
    mutations = decompose_update_media_buy(
        {
            "packages": [
                {
                    "package_id": "pkg_1",
                    "targeting_overlay": {
                        "geo_country_any_of": ["US"],
                        "frequency_cap": {"impressions": 5, "duration": "P1D"},
                    },
                    "keyword_targets_add": [
                        {"keyword": "sports", "match_type": "exact"},
                    ],
                }
            ]
        }
    )

    assert [mutation.action for mutation in mutations] == [
        "update_targeting",
        "update_targeting",
        "update_frequency_caps",
    ]
    assert mutations[1].field_paths == ("packages[0].targeting_overlay.geo_country_any_of",)
    assert mutations[2].field_paths == ("packages[0].targeting_overlay.frequency_cap",)


def test_decompose_update_media_buy_extracts_package_actions() -> None:
    mutations = decompose_update_media_buy(
        {
            "packages": [
                {
                    "package_id": "pkg_1",
                    "paused": False,
                    "creative_assignments": [{"creative_id": "cr_1"}],
                    "creatives": [{"creative_id": "cr_2", "format": "display"}],
                },
                {
                    "package_id": "pkg_2",
                    "canceled": True,
                    "cancellation_reason": "underperforming",
                },
            ]
        }
    )

    assert [mutation.action for mutation in mutations] == [
        "resume",
        "update_creative_assignments",
        "replace_creative",
        "remove_packages",
    ]
    assert mutations[0].package_id == "pkg_1"
    assert mutations[-1].package_id == "pkg_2"


def test_decompose_update_media_buy_keeps_unmapped_fields_visible() -> None:
    mutations = decompose_update_media_buy(
        {
            "reporting_webhook": {"url": "https://example.com/reports"},
            "custom_field": "value",
            "packages": [{"package_id": "pkg_1", "seller_extension": True}],
        }
    )

    assert [mutation.action for mutation in mutations] == [
        UNKNOWN_UPDATE_ACTION,
        UNKNOWN_UPDATE_ACTION,
        UNKNOWN_UPDATE_ACTION,
    ]
    assert [mutation.field_paths for mutation in mutations] == [
        ("packages[0].seller_extension",),
        ("reporting_webhook",),
        ("custom_field",),
    ]


def test_requested_actions_are_ordered_and_deduplicated() -> None:
    actions = requested_update_media_buy_actions(
        {
            "paused": True,
            "packages": [
                {"package_id": "pkg_1", "pacing": "even"},
                {"package_id": "pkg_2", "pacing": "asap"},
            ],
        }
    )

    assert actions == ("pause", "update_pacing")


def test_disallowed_update_media_buy_mutations_match_candidate_actions() -> None:
    current = {"packages": [{"package_id": "pkg_1", "budget": 100.0}]}
    mutation = decompose_update_media_buy(
        {"packages": [{"package_id": "pkg_1", "budget": 125.0}]},
        current,
    )[0]

    assert mutation.action == "increase_budget"
    assert is_update_media_buy_mutation_allowed(mutation, {"update_budget"})
    assert disallowed_update_media_buy_mutations(
        {"packages": [{"package_id": "pkg_1", "budget": 125.0}]},
        {"pause"},
        current,
    ) == [mutation]


def test_allowed_action_helpers_accept_wire_available_actions() -> None:
    from adcp.types.generated_poc.core.media_buy_available_action import (
        MediaBuyAvailableAction,
    )

    current = {"packages": [{"package_id": "pkg_1", "budget": 100.0}]}
    mutation = decompose_update_media_buy(
        {"packages": [{"package_id": "pkg_1", "budget": 125.0}]},
        current,
    )[0]
    available_actions = [
        {"action": "pause", "mode": "self_serve"},
        MediaBuyAvailableAction(action="update_budget", mode="self_serve"),
    ]

    assert normalize_update_media_buy_allowed_actions(available_actions) == (
        "pause",
        "update_budget",
    )
    assert mutation.is_allowed_by(available_actions)
    assert (
        disallowed_update_media_buy_mutations(
            {"packages": [{"package_id": "pkg_1", "budget": 125.0}]},
            available_actions,
            current,
        )
        == []
    )
