"""``CoverageTrace``: why a period is or is not owed, checked against the spec's vectors.

For every canonical vector, every period of every offering a buy lists, near
the buy's flight, is explained, and the trace's verdict must equal the owed
rule's: owed exactly when the vector's expected rows owe it. The reason must
be a spec reason code whose verdict matches, taken from the first failing
rule in the spec's fixed order.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adcp.reporting.subscriptions import COVERAGE_REASON_CODES, COVERAGE_RULE_ORDER
from adcp.reporting.subscriptions._clock import PeriodCalendar
from adcp.reporting.subscriptions.explain import (
    EXPLAIN_TEMPLATES,
    CoverageTrace,
    coverage_reasons,
    diff_coverage_traces,
    explain_subscription_coverage,
)
from tests.test_reporting_subscription_parity import (
    CASES,
    SPEC,
    at,
    buys,
    offerings,
    pinned_tzdata,  # noqa: F401 - the module fixture pins tzdata 2026f
    windows,
)

#: Periods examined per (buy, offering, zone) on each side of the owed rows.
_MARGIN = 3
_CAP = 120


def _kwargs(input_: dict[str, Any]) -> dict[str, Any]:
    from adcp.reporting.subscriptions import SubscriptionAuthorization

    account = input_["account"]
    return {
        "subscription": windows(input_["versions"], input_.get("inactive_intervals", [])),
        "account_id": account["account_id"],
        "buys": buys(input_),
        "offerings": offerings(input_),
        "account_timezone": account.get("timezone"),
        "account_operator": account.get("operator"),
        "now": at(input_["now"]),
        "authorizations": [
            SubscriptionAuthorization(at(grant["authorized_at"]), at(grant.get("unauthorized_at")))
            for grant in account.get("grants", ())
        ],
        "coverage": input_.get("coverage"),
        "backfill_on_regrant": bool(input_.get("backfill_on_regrant", False)),
        "status_retention_days": input_.get("status_retention_days"),
    }


def _calendars(kwargs: dict[str, Any], offering) -> list[PeriodCalendar]:
    zones = {kwargs["account_timezone"]}
    for entry in (kwargs.get("coverage") or {}).get("timezone_cutovers", ()):
        zones.update({entry.get("timezone"), entry.get("previous_timezone")})
    overrides = {None}
    for window in kwargs["subscription"]:
        overrides.add((window.configuration.get("selector") or {}).get("period_timezone"))
    result = []
    names = set()
    for override in sorted(overrides, key=str):
        for zone in sorted(z for z in zones if z) or [None]:
            try:
                calendar = offering.calendar(zone, override)
            except Exception:  # noqa: BLE001 - a zone this offering cannot use
                continue
            if calendar.zone_name not in names:
                names.add(calendar.zone_name)
                result.append(calendar)
    return result


def _candidate_periods(case: dict[str, Any], kwargs: dict[str, Any]):
    """(buy, offering, start, end) near each buy's flight and the owed rows."""
    by_id = {item.offering_id: item for item in kwargs["offerings"]}
    now = kwargs["now"]
    expected = case["expected"]
    for buy in kwargs["buys"]:
        listed = sorted(
            {oid for _p, ids in buy.product_offering_ids() for oid in ids if oid in by_id}
        )
        rows = [row for row in expected if row["media_buy_id"] == buy.media_buy_id]
        anchors = [buy.start_time, min(buy.stop_time, now)]
        anchors += [at(row["period_start"]) for row in rows]
        anchors += [at(row["period_end"]) for row in rows]
        low, high = min(anchors), max(anchors)
        for row in rows:
            if row.get("stub"):
                # A billing stub is an owed period off the incoming calendar.
                offering = by_id[row["offering_id"]]
                yield buy, offering, at(row["period_start"]), at(row["period_end"])
        for oid in listed:
            for calendar in _calendars(kwargs, by_id[oid]):
                start = calendar.floor(low)
                for _ in range(_MARGIN):
                    start = calendar.floor(start - timedelta(microseconds=1))
                seen = 0
                for period_start, period_end in calendar.periods(
                    starts_from=start, starts_before=high + timedelta(days=3)
                ):
                    yield buy, by_id[oid], period_start, period_end
                    seen += 1
                    if seen >= _CAP:
                        break


def _owed_rows(case: dict[str, Any]) -> set[tuple]:
    return {
        (row["media_buy_id"], row["offering_id"], at(row["period_start"]), at(row["period_end"]))
        for row in case["expected"]
    }


VECTOR_CASES = [case for case in CASES if "expected_error" not in case]


def test_the_vocabulary_is_the_spec_reason_codes_file() -> None:
    document = json.loads((SPEC / "reason-codes.json").read_text())
    reasons = coverage_reasons()
    assert [item["code"] for item in document["reasons"]] == list(reasons)
    for item in document["reasons"]:
        assert COVERAGE_REASON_CODES[item["code"]] == item.get("rule")
        assert reasons[item["code"]].verdict == item["verdict"]
    assert tuple(document["rule_order"]) == COVERAGE_RULE_ORDER
    # Every reason renders one sentence.
    assert set(EXPLAIN_TEMPLATES) == set(reasons)


@pytest.mark.parametrize("case", VECTOR_CASES, ids=[case["id"] for case in VECTOR_CASES])
def test_trace_verdicts_match_the_owed_rule_on_every_vector(case: dict[str, Any]) -> None:
    kwargs = _kwargs(case["input"])
    owed = _owed_rows(case)
    explained: set[tuple] = set()
    reasons = coverage_reasons()
    for buy, offering, start, end in _candidate_periods(case, kwargs):
        try:
            traces = explain_subscription_coverage(
                **kwargs,
                period={"start": start, "end": end},
                media_buy_id=buy.media_buy_id,
                offering_id=offering.offering_id,
            )
        except Exception as error:  # noqa: BLE001
            # A period of a zone the offering never uses, or outside any version.
            if "not a period" in str(error):
                continue
            raise
        for trace in traces:
            key = (buy.media_buy_id, offering.offering_id, start, end)
            is_owed = key in owed
            assert trace.reason in reasons
            assert reasons[trace.reason].verdict == trace.verdict
            if trace.reason == "period_not_ended":
                assert end > kwargs["now"] and not is_owed
                continue
            assert (trace.verdict == "owed") == is_owed, (
                case["id"],
                trace.to_dict(),
                case.get("rule"),
            )
            # The steps follow the rule order, and only the last one fails.
            rules = [step.rule for step in trace.steps]
            assert rules == [rule for rule in COVERAGE_RULE_ORDER if rule in rules]
            failed = [step for step in trace.steps if step.result == "fail"]
            assert len(failed) == (0 if trace.verdict == "owed" else 1)
            if failed:
                assert failed[0] is trace.steps[-1]
                rule = reasons[trace.reason].rule
                assert rule == failed[0].rule or (
                    trace.reason == "cutover_stretch" and failed[0].rule == "timezone"
                ), (trace.reason, failed[0].rule)
            if trace.verdict == "owed":
                explained.add(key)
    # Every owed row was examined and explained as owed.
    assert owed <= explained, sorted(owed - explained)


def _scenario() -> dict[str, Any]:
    from adcp.reporting.subscriptions import (
        SubscriptionBuy,
        SubscriptionBuyPackage,
        SubscriptionOffering,
    )

    offering = SubscriptionOffering(
        offering_id="daily_pkg",
        report_definition_id="rd_daily",
        reporting_profile="standard",
        feed_purpose="analytics",
        period_duration="P1D",
        alignment="account_timezone",
        delivery_sla="PT6H",
        supported_finality=frozenset({"snapshot", "official"}),
        reconciliation_mode="delivery_only",
        dimensions=frozenset({"package"}),
    )
    state = {
        "subscription_id": "analytics-daily",
        "subscription_version": 1,
        "state": "active",
        "accepted_at": "2026-10-20T12:00:00Z",
        "configuration": {
            "subscription_id": "analytics-daily",
            "subscription_version": 1,
            "active": True,
            "scope": {"all_authorized_accounts": True},
            "selector": {
                "feed_purpose": "analytics",
                "period_duration": "P1D",
                "required_finality": "snapshot",
                "dimensions": ["package"],
            },
            "reconciliation_mode": "delivery_only",
        },
    }
    buy = SubscriptionBuy(
        media_buy_id="mb_123",
        confirmed_at=datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
        start_time=datetime(2026, 11, 3, 5, tzinfo=timezone.utc),
        end_time=datetime(2026, 11, 30, 5, tzinfo=timezone.utc),
        packages=(SubscriptionBuyPackage("pkg_1", ("daily_pkg",)),),
    )
    return {
        "subscription": state,
        "account_id": "acc_a",
        "buys": [buy],
        "offerings": [offering],
        "account_timezone": "America/New_York",
        "now": datetime(2026, 11, 10, tzinfo=timezone.utc),
    }


def test_a_local_date_is_read_in_the_offering_zone_and_explained() -> None:
    traces = explain_subscription_coverage(**_scenario(), period="2026-11-03")
    assert len(traces) == 1
    trace = traces[0]
    assert trace.subject["period"] == {
        "start": "2026-11-03T05:00:00Z",
        "end": "2026-11-04T05:00:00Z",
        "timezone": "America/New_York",
    }
    assert (trace.verdict, trace.reason, trace.rule) == (
        "not_owed",
        "before_confirmation",
        "confirmation",
    )
    assert trace.explanation == (
        "Not owed: the period ends 2026-11-04T05:00Z, before mb_123 was confirmed at "
        "2026-11-04T10:00Z."
    )
    assert [step.rule for step in trace.steps] == [
        "scope",
        "authorization",
        "version",
        "activity",
        "offering",
        "flight",
        "confirmation",
    ]
    assert trace.steps[-1].facts == {"confirmed_at": "2026-11-04T10:00:00Z"}
    assert trace.inputs_digest.startswith("sha256:")
    # The confirmation day itself is owed in full.
    owed = explain_subscription_coverage(**_scenario(), period="2026-11-04")[0]
    assert (owed.verdict, owed.reason) == ("owed", "owed")
    assert owed.explanation.endswith("due at 2026-11-05T11:00Z.")
    assert len(owed.steps) == len(COVERAGE_RULE_ORDER)


def test_a_period_that_has_not_ended_is_not_owed_yet() -> None:
    trace = explain_subscription_coverage(**_scenario(), period="2026-11-10")[0]
    assert (trace.verdict, trace.reason) == ("not_owed", "period_not_ended")


def test_buyer_and_seller_traces_of_one_period_agree_and_diff_names_the_first_rule() -> None:
    buyer = explain_subscription_coverage(**_scenario(), period="2026-11-05")[0]
    seller = explain_subscription_coverage(
        **_scenario(),
        period="2026-11-05",
        side="seller",
        principal_id="agency-x",
        seller_only={"frozen_at": "2026-11-06T05:03:11Z"},
    )[0]
    assert buyer.inputs_digest == seller.inputs_digest
    assert diff_coverage_traces(buyer, seller) is None
    assert seller.seller_only == {"buy_visible": True, "frozen_at": "2026-11-06T05:03:11Z"}
    assert "seller_only" not in seller.for_buyer().to_dict()
    # A stale buyer read (the buy confirmed later) differs at the confirmation rule.
    stale = _scenario()
    from dataclasses import replace

    stale["buys"] = [
        replace(stale["buys"][0], confirmed_at=datetime(2026, 11, 6, 10, tzinfo=timezone.utc))
    ]
    late = explain_subscription_coverage(**stale, period="2026-11-05")[0]
    difference = diff_coverage_traces(late, seller)
    assert difference is not None
    assert difference.rule == "confirmation"
    assert not difference.same_inputs
    assert "different state" in difference.describe()


def test_a_trace_round_trips_through_json() -> None:
    trace = explain_subscription_coverage(**_scenario(), period="2026-11-03")[0]
    again = CoverageTrace.from_dict(json.loads(trace.to_json()))
    assert again == trace


def test_seller_only_reason_for_a_buy_the_caller_cannot_see() -> None:
    trace = explain_subscription_coverage(
        **_scenario(), period="2026-11-05", side="seller", invisible_media_buy_ids={"mb_123"}
    )[0]
    assert (trace.reason, trace.rule) == ("buy_not_visible", "scope")
    with pytest.raises(ValueError, match="seller-only"):
        explain_subscription_coverage(
            **_scenario(), period="2026-11-05", invisible_media_buy_ids={"mb_123"}
        )


def test_a_range_must_be_a_period_of_the_schedule() -> None:
    from adcp.reporting.subscriptions import CoverageInputError

    with pytest.raises(CoverageInputError, match="not a period"):
        explain_subscription_coverage(
            **_scenario(), period={"start": "2026-11-03T00:00:00Z", "end": "2026-11-04T00:00:00Z"}
        )


def test_the_templates_are_one_sentence_each() -> None:
    for code, template in EXPLAIN_TEMPLATES.items():
        assert template.endswith("."), code


@pytest.mark.asyncio
async def test_the_managed_seller_explains_what_its_ledger_owes() -> None:
    from adcp.reporting import PrincipalRef
    from adcp.reporting.subscriptions import SubscriptionBuy
    from tests.test_reporting_subscription_managed import ACCOUNT, AGENCY, START, _ctx, _World

    world = _World()
    await world.subscribe()
    world.clock.set(START + timedelta(hours=3, minutes=30))
    turn = await world.service.run_worker()
    assert turn.subscription_obligations_written == 2
    principal = PrincipalRef(None, AGENCY)
    status = await world.platform.get_reporting_status({"view": "periods"}, _ctx())
    ledger = {item["reporting_obligation_id"] for item in status["periods"]}

    traces = await world.service.explain_coverage(principal, ACCOUNT, period="2026-11-01")
    owed = [trace for trace in traces if trace.verdict == "owed"]
    assert {trace.seller_only["reporting_obligation_id"] for trace in owed} == ledger
    assert all(trace.side == "seller" and trace.seller_only["buy_visible"] for trace in traces)
    # The first hour ends before the buy starts; the fourth has not ended.
    reasons = {trace.subject["period"]["start"]: trace.reason for trace in traces}
    assert reasons["2026-11-01T00:00:00Z"] == "outside_flight"
    assert reasons["2026-11-01T03:00:00Z"] == "after_stop"
    one = await world.service.explain_coverage(
        principal, ACCOUNT, period="2026-11-01T01:30:00Z", subscription_id="analytics-hourly"
    )
    assert [(t.verdict, t.reason) for t in one] == [("owed", "owed")]
    assert "seller_only" not in one[0].for_buyer().to_dict()
    # A buy the caller cannot see is explained, seller-side only.
    hidden = SubscriptionBuy(
        media_buy_id="media-buy-ui",
        confirmed_at=START,
        start_time=START,
        end_time=START + timedelta(hours=3),
    )
    unseen = await world.service.explain_coverage(
        principal,
        ACCOUNT,
        period="2026-11-01T01:30:00Z",
        media_buy_id="media-buy-ui",
        invisible_buys=[hidden],
    )
    assert [t.reason for t in unseen] == ["buy_not_visible"]
    with pytest.raises(KeyError):
        await world.service.explain_coverage(
            principal, ACCOUNT, period="2026-11-01", subscription_id="missing"
        )


def test_the_cli_diff_names_the_first_differing_rule(tmp_path, capsys) -> None:
    from adcp.reporting.cli import main

    buyer = explain_subscription_coverage(**_scenario(), period="2026-11-05")[0]
    seller = explain_subscription_coverage(
        **_scenario(), period="2026-11-05", side="seller", principal_id="agency-x"
    )[0]
    (tmp_path / "buyer.json").write_text(buyer.to_json())
    (tmp_path / "seller.json").write_text(json.dumps([seller.to_dict()]))
    assert main(["diff", str(tmp_path / "buyer.json"), str(tmp_path / "seller.json")]) == 0
    assert "agree" in capsys.readouterr().out
    stale = _scenario()
    from dataclasses import replace

    stale["buys"] = [
        replace(stale["buys"][0], confirmed_at=datetime(2026, 11, 6, 10, tzinfo=timezone.utc))
    ]
    late = explain_subscription_coverage(**stale, period="2026-11-05")[0]
    (tmp_path / "late.json").write_text(late.to_json())
    assert main(["diff", str(tmp_path / "late.json"), str(tmp_path / "seller.json")]) == 1
    out = capsys.readouterr().out
    assert "rule 'confirmation'" in out and "different state" in out


def test_the_cli_explains_from_the_seller_service(monkeypatch, capsys) -> None:
    import sys
    import types

    from adcp.reporting.cli import main

    calls: list[tuple] = []

    class Service:
        async def explain_coverage(self, principal, account_id, **kwargs):
            calls.append((principal, account_id, kwargs))
            return explain_subscription_coverage(
                **_scenario(), period=kwargs["period"], side="seller", principal_id="agency-x"
            )

    module = types.ModuleType("explain_cli_app")
    module.service = Service()
    monkeypatch.setitem(sys.modules, "explain_cli_app", module)
    argv = ["explain", "--app", "explain_cli_app:service", "--principal", "agency-x"]
    assert main([*argv, "--account", "acc_a", "--period", "2026-11-03"]) == 0
    out = capsys.readouterr().out
    assert "before_confirmation" in out and "was confirmed at 2026-11-04T10:00Z" in out
    assert calls[0][1] == "acc_a" and calls[0][0].principal_id == "agency-x"
    assert main([*argv, "--account", "acc_a", "--period", "2026-11-03", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document[0]["reason"] == "before_confirmation"


def test_traces_on_either_side_of_the_period_end_differ_in_their_inputs() -> None:
    before = _scenario() | {"now": datetime(2026, 11, 5, 6, tzinfo=timezone.utc)}
    after = _scenario() | {"now": datetime(2026, 11, 6, 6, tzinfo=timezone.utc)}
    open_ = explain_subscription_coverage(**before, period="2026-11-05")[0]
    ended = explain_subscription_coverage(**after, period="2026-11-05")[0]
    assert (open_.reason, ended.reason) == ("period_not_ended", "owed")
    difference = diff_coverage_traces(open_, ended)
    assert difference is not None and not difference.same_inputs
    # Later, with the period still ended, the digest is unchanged.
    later = _scenario() | {"now": datetime(2026, 11, 20, tzinfo=timezone.utc)}
    assert explain_subscription_coverage(**later, period="2026-11-05")[0].inputs_digest == (
        ended.inputs_digest
    )


def test_a_period_ended_before_an_open_revocation_is_owed_withheld() -> None:
    from adcp.reporting.subscriptions import SubscriptionAuthorization

    scenario = _scenario()
    scenario["authorizations"] = [
        SubscriptionAuthorization(
            datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc)
        )
    ]
    scenario["status_retention_days"] = 30
    trace = explain_subscription_coverage(**scenario, period="2026-11-05")[0]
    assert (trace.verdict, trace.reason) == ("owed", "owed:withheld")
    assert "lost authority for acc_a at 2026-11-07T00:00Z" in trace.explanation


def test_an_account_resolved_source_zone_stays_out_of_the_digest() -> None:
    from dataclasses import replace

    from adcp.reporting.subscriptions.explain import _offering_facts

    base = _scenario()["offerings"][0]
    resolved = replace(
        base,
        alignment="source_timezone",
        period_timezone="America/New_York",
        period_timezone_policy="account_resolved",
    )
    other = replace(resolved, period_timezone="Europe/Paris")
    # The seller resolved the zone; a buyer may not have: the facts agree.
    assert _offering_facts(resolved) == _offering_facts(other)
    assert _offering_facts(resolved)["period_timezone"] is None
    fixed = replace(resolved, period_timezone_policy="fixed")
    assert _offering_facts(fixed)["period_timezone"] == "America/New_York"


def test_flight_candidates_list_each_period_once_across_zones() -> None:
    from adcp.reporting.subscriptions import AccountTimezoneChange
    from adcp.reporting.subscriptions.explain import CoverageExplainer

    scenario = _scenario()
    scenario["timezone_changes"] = [
        AccountTimezoneChange(
            datetime(2026, 11, 3, 5, tzinfo=timezone.utc),
            "America/Chicago",
            previous_timezone="America/New_York",
        )
    ]
    explainer = CoverageExplainer(**scenario)
    keys = [
        (c.buy.media_buy_id, c.offering.offering_id, c.start, c.end)
        for c in explainer.flight_candidates()
    ]
    assert len(keys) == len(set(keys))
