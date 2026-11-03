"""AdCP 3.3.0-beta.4 reporting-subscription rules (adcp#8717, #8718, #8724).

* Per-package ``added_at``: a package added to a confirmed buy is owed from
  its own confirmation, it resolves then, and adding it is never a cutover.
* A grant reduced to ``allowed_tasks`` without ``get_reporting_status`` is a
  loss of reporting authority at the reduction, refused with
  ``SCOPE_INSUFFICIENT``; adding it back is a re-grant.
* ``SUBSCRIPTIONS_REQUIRE_ACCOUNT_ID_KEY`` and the spec's ``offering-checks.json``.
* ``reset_state`` and ``SetAccountTimezoneResult`` on the compliance controller.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from adcp.exceptions import ConfigurationError
from adcp.reporting.subscriptions import (
    SubscriptionBuy,
    check_subscription_offerings,
    explain_subscription_coverage,
)
from adcp.server.test_controller import (
    INSECURE_ALLOW_ALL,
    TestControllerStore,
    _handle_test_controller,
    check_test_controller_store,
)
from adcp.testing.reporting import offerings, reporting_test_kit
from tests.test_reporting_subscription_parity import CASES, at, buys, windows
from tests.test_reporting_subscription_parity import offerings as case_offerings

SPEC = Path(__file__).parent / "fixtures" / "reporting_subscriptions" / "spec"
OFFERING_CHECKS = json.loads((SPEC / "offering-checks.json").read_text())


# -- SUBSCRIPTIONS_REQUIRE_ACCOUNT_ID_KEY -------------------------------------------------


@pytest.mark.parametrize(
    "case", OFFERING_CHECKS["cases"], ids=[case["id"] for case in OFFERING_CHECKS["cases"]]
)
def test_offering_checks_vectors(case: dict[str, Any]) -> None:
    failures = check_subscription_offerings(
        case["input"]["offerings"], case["input"].get("row_schemas")
    )
    assert [item.as_dict() for item in failures] == case["expected"]
    for failure in failures:
        error = failure.error()
        assert (error.code, error.recovery, error.dispatched) == (
            failure.code,
            "correctable",
            None,
        )
        assert error.context["offering_id"] == failure.offering_id
        assert failure.offering_id in str(error)


# -- per-package added_at ------------------------------------------------------------------


def _case(case_id: str) -> dict[str, Any]:
    return copy.deepcopy(next(case for case in CASES if case["id"] == case_id)["input"])


def test_from_media_buy_reads_each_package_added_at() -> None:
    (buy,) = buys(_case("package-added-mid-period-is-owed-from-the-period-containing-added-at"))
    added = {package.package_id: package.added_at for package in buy.packages}
    assert any(value is not None for value in added.values())
    later = next(package_id for package_id, value in added.items() if value is not None)
    assert buy.package_confirmed_at(later) == added[later]
    earlier = next(package_id for package_id, value in added.items() if value is None)
    assert buy.package_confirmed_at(earlier) == buy.confirmed_at


def test_an_added_at_at_or_before_confirmation_has_no_effect() -> None:
    raw = {
        "media_buy_id": "mb_1",
        "confirmed_at": "2026-11-03T12:00:00Z",
        "start_time": "2026-11-03T00:00:00Z",
        "end_time": "2026-11-10T00:00:00Z",
        "packages": [
            {
                "package_id": "pkg_1",
                "reporting_delivery_offering_ids": ["off"],
                "added_at": "2026-11-01T00:00:00Z",
            }
        ],
    }
    buy = SubscriptionBuy.from_media_buy(raw)
    assert buy.package_confirmed_at("pkg_1") == buy.confirmed_at


def _explain(input_: dict[str, Any], start: str, end: str, media_buy_id: str = "mb_1"):
    account = input_["account"]
    from adcp.reporting.subscriptions import SubscriptionAuthorization

    return explain_subscription_coverage(
        subscription=windows(input_["versions"], input_.get("inactive_intervals", [])),
        account_id=account["account_id"],
        buys=buys(input_),
        offerings=case_offerings(input_),
        account_timezone=account.get("timezone"),
        account_operator=account.get("operator"),
        now=at(input_["now"]),
        authorizations=[
            SubscriptionAuthorization(at(grant["authorized_at"]), at(grant.get("unauthorized_at")))
            for grant in account.get("grants", ())
        ],
        coverage=input_.get("coverage"),
        status_retention_days=input_.get("status_retention_days"),
        period={"start": start, "end": end},
        media_buy_id=media_buy_id,
    )


def test_a_period_before_every_resolving_package_was_added_is_before_package_added() -> None:
    input_ = _case("package-added-then-removed-composes-with-package-exclusions")
    # 11-04 and 11-05 end before pkg_2's added_at; pkg_1 is taken out by the
    # overridden entry, so the exception rule removes them.
    (trace,) = _explain(input_, "2026-11-04T00:00:00Z", "2026-11-05T00:00:00Z")
    assert trace.reason == "excluded:overridden", trace
    (trace,) = _explain(input_, "2026-11-06T00:00:00Z", "2026-11-07T00:00:00Z")
    assert trace.reason == "owed", trace
    # A buy whose only resolving package was added later.
    input_["buys"][0]["packages"] = [input_["buys"][0]["packages"][1]]
    input_["coverage"] = {"excluded": []}
    (trace,) = _explain(input_, "2026-11-05T00:00:00Z", "2026-11-06T00:00:00Z")
    assert trace.reason == "before_package_added", trace
    assert trace.verdict == "not_owed"
    (trace,) = _explain(input_, "2026-11-06T00:00:00Z", "2026-11-07T00:00:00Z")
    assert trace.reason == "owed", trace


SUBSCRIPTION = {
    "subscription_id": "analytics-daily",
    "scope": {"all_authorized_accounts": True},
    "selector": {
        "feed_purpose": "analytics",
        "period_duration": "P1D",
        "required_finality": "snapshot",
    },
    "reconciliation_mode": "delivery_only",
}
ACCOUNTS = [{"account_id": "acc_a", "timezone": "UTC"}]


async def test_a_package_added_later_keeps_buyer_and_seller_in_agreement() -> None:
    async with reporting_test_kit(
        start="2026-11-01T00:00:00Z",
        accounts=ACCOUNTS,
        offerings=[offerings.daily_analytics()],
        buys=[
            {
                "media_buy_id": "mb_1",
                "account_id": "acc_a",
                "confirmed_at": "2026-11-01T00:00:00Z",
                "start_time": "2026-11-01T00:00:00Z",
                "end_time": "2026-11-30T00:00:00Z",
                "packages": [],
            }
        ],
    ) as kit:
        buyer = kit.buyer("agency-x")
        await buyer.reporting.subscribe(SUBSCRIPTION)
        await kit.clock.advance_to("2026-11-03T12:00:00Z")
        package_id = await kit.buys.add_package("mb_1", {}, principal="agency-x")
        from adcp.types import GetMediaBuysRequest

        served = await kit.buyer("agency-x").get_media_buys(
            GetMediaBuysRequest.model_validate({"account": {"account_id": "acc_a"}})
        )
        data = served.data
        if hasattr(data, "model_dump"):
            data = data.model_dump(mode="json", exclude_none=True)
        dumped = data["media_buys"][0]["packages"]
        assert [(item["package_id"], item.get("added_at")) for item in dumped] == [
            (package_id, "2026-11-03T12:00:00Z")
        ]
        await kit.clock.advance_to("2026-11-06T00:00:00Z")
        report = await buyer.reporting.reconcile(now=kit.clock.now())
        owed = sorted(line.period["start"] for line in report.periods if line.status != "not_owed")
        # The period containing added_at is owed in full; nothing before it.
        assert owed == [
            "2026-11-03T00:00:00Z",
            "2026-11-04T00:00:00Z",
            "2026-11-05T00:00:00Z",
        ]
        await kit.assert_agreement("agency-x")


# -- a reduced grant without get_reporting_status ---------------------------------------


async def _state(kit, principal: str) -> Any:
    from adcp._principal_ref import PrincipalRef

    (state,) = await kit.service.principals.reporting_subscriptions(PrincipalRef(None, principal))
    return state


async def test_a_reduction_without_get_reporting_status_is_a_loss_and_restoring_regrants() -> None:
    from adcp._principal_ref import PrincipalRef

    async with reporting_test_kit(
        start="2026-11-01T00:00:00Z",
        accounts=ACCOUNTS,
        offerings=[offerings.daily_analytics()],
        buys=[
            {
                "media_buy_id": "mb_1",
                "account_id": "acc_a",
                "confirmed_at": "2026-11-01T00:00:00Z",
                "start_time": "2026-11-01T00:00:00Z",
                "end_time": "2026-11-30T00:00:00Z",
            }
        ],
    ) as kit:
        buyer = kit.buyer("agency-x")
        await buyer.reporting.subscribe(SUBSCRIPTION)
        engine = kit.service._subscriptions.engine
        ref = PrincipalRef(None, "agency-x")
        await kit.clock.advance_to("2026-11-03T12:00:00Z")
        # A reduction that keeps get_reporting_status is not a loss.
        await kit.grants.reduce(
            "agency-x", "acc_a", ["get_reporting_status", "list_accounts", "get_media_buys"]
        )
        assert not (await _state(kit, "agency-x")).accounts_unauthorized
        await kit.clock.advance_to("2026-11-04T12:00:00Z")
        reduced = await kit.grants.reduce("agency-x", "acc_a", ["list_accounts", "get_media_buys"])
        (entry,) = (await _state(kit, "agency-x")).accounts_unauthorized
        assert entry.account_id == "acc_a"
        assert entry.unauthorized_at.isoformat().replace("+00:00", "Z") == reduced["simulated_now"]
        # Still visible: get_reporting_status is out of scope, not unknown.
        assert await engine.holds_grant(ref, "acc_a")
        assert not await engine.grants_task(ref, "acc_a", "get_reporting_status")
        await kit.clock.advance_to("2026-11-06T12:00:00Z")
        restored = await kit.grants.reduce(
            "agency-x", "acc_a", ["get_reporting_status", "list_accounts", "get_media_buys"]
        )
        assert not (await _state(kit, "agency-x")).accounts_unauthorized
        from adcp.testing.reporting import _list_accounts_request, _plain_accounts

        listed = await buyer.list_accounts(_list_accounts_request())
        (shown,) = _plain_accounts(listed)
        authorization = shown["authorization"]
        # A re-grant inside retention: the chain continues.
        assert authorization["authorized_at"] == "2026-11-01T00:00:00Z"
        assert authorization["regranted_at"] == restored["simulated_now"]
        await kit.clock.advance_to("2026-11-08T00:00:00Z")
        await kit.assert_agreement("agency-x")


async def test_a_revoked_grant_stays_unknown() -> None:
    from adcp._principal_ref import PrincipalRef

    async with reporting_test_kit(
        accounts=ACCOUNTS, offerings=[offerings.daily_analytics()]
    ) as kit:
        await kit.buyer("agency-x").reporting.subscribe(SUBSCRIPTION)
        await kit.grants.revoke("agency-x", "acc_a")
        engine = kit.service._subscriptions.engine
        assert not await engine.holds_grant(PrincipalRef(None, "agency-x"), "acc_a")


# -- the compliance controller ------------------------------------------------------------


class _ProbeOnly(TestControllerStore):
    async def reporting_subscription_probe(self, operation, account_id=None, **_: Any):
        return {"simulated_now": "2030-01-01T00:00:00Z", "account_id": account_id}


class _ProbeAndReset(_ProbeOnly):
    def __init__(self) -> None:
        self.resets = 0

    async def reset_state(self, *, context=None):
        self.resets += 1
        return {}


def test_a_probe_without_reset_state_is_refused_at_registration() -> None:
    with pytest.raises(ConfigurationError, match="reset_state"):
        check_test_controller_store(_ProbeOnly())
    check_test_controller_store(_ProbeAndReset())
    check_test_controller_store(TestControllerStore())


async def test_reset_state_is_listed_dispatched_and_probe_results_nest_under_simulated() -> None:
    store = _ProbeAndReset()

    async def call(params: dict[str, Any]) -> dict[str, Any]:
        return await _handle_test_controller(store, params, account_resolver=INSECURE_ALLOW_ALL)

    listed = await call({"scenario": "list_scenarios"})
    assert {"reporting_subscription_probe", "reset_state"} <= set(listed["scenarios"])
    account = {"account_id": "acc_a", "sandbox": True}
    assert await call({"scenario": "reset_state", "account": account}) == {"success": True}
    assert store.resets == 1
    probed = await call(
        {
            "scenario": "reporting_subscription_probe",
            "account": account,
            "params": {"operation": "revoke_grant", "account_id": "acc_a"},
        }
    )
    assert probed == {
        "success": True,
        "simulated": {"simulated_now": "2030-01-01T00:00:00Z", "account_id": "acc_a"},
    }


# -- differential regressions (parity with the TypeScript SDK) -----------------------------

#: Minimized scenarios from the spec's differential generator (adcontextprotocol/adcp
#: 8ee1bfa2) on which an SDK disagreed with the reference model; the expected rows are
#: the reference model's. Vendored from adcontextprotocol/adcp-client#3620.
DIFFERENTIAL = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "reporting_subscriptions"
        / "differential-regressions.json"
    ).read_text()
)


@pytest.mark.parametrize(
    "case", DIFFERENTIAL["cases"], ids=[case["id"] for case in DIFFERENTIAL["cases"]]
)
def test_differential_regressions_match_the_reference_model(case: dict[str, Any]) -> None:
    from tests.test_reporting_subscription_parity import periods, rows

    expected = [
        (
            at(row["period_start"]),
            row["subscription_version"],
            row["offering_id"].encode(),
            row["media_buy_id"].encode(),
            at(row["period_end"]),
            row.get("stub", False),
        )
        for row in case["expected"]
    ]
    assert rows(periods(copy.deepcopy(case["input"]))) == expected, case["rule"]


def test_a_billing_feed_owes_a_stub_at_an_account_timezone_cutover() -> None:
    """The cutover text, not the reference model, as the TypeScript SDK does.

    ``core/reporting-subscription.json`` ``cutover`` applies the billing stub
    to every cutover, account-timezone changes included; the 8ee1bfa2 reference
    model emits none at a timezone cutover (three differential disagreements,
    billing offerings only), which the spec fixes for beta.5.
    """
    from tests.test_reporting_subscription_parity import periods, rows

    input_ = _case("account-timezone-change-is-a-cutover")
    input_["versions"][0]["configuration"]["selector"]["feed_purpose"] = "billing"
    input_["offerings"][0]["feed_purpose"] = "billing"
    owed = [(start, end, stub) for start, *_rest, end, stub in rows(periods(input_))]
    # New York to Los Angeles: E is 05:00Z, the first Los Angeles boundary 08:00Z.
    assert (at("2026-11-05T05:00:00Z"), at("2026-11-05T08:00:00Z"), True) in owed
    assert [item for item in owed if item[2]] == [
        (at("2026-11-05T05:00:00Z"), at("2026-11-05T08:00:00Z"), True)
    ]
