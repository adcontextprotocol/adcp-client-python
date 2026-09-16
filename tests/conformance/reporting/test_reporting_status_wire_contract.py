"""The authoritative read, pure projection and status wire share one invariant."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import ReportingDeliveryEscalation
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_projection import StatusProjectionInput, project_status_scope
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.outbox import ReportingStatusScope
from adcp.validation.schema_loader import get_named_validator

from . import test_reporting_status_projection_contract as _contract
from ._generation_support import END, NOW, START, configuration, obligation_for, revision_for
from .test_reporting_notification_outbox import statement

status_harness = _contract.status_harness
CALLER = ReportingStatusCaller("acct_a", "buyer")


def validate_read(payload):
    validator = get_named_validator("media-buy/get-reporting-status-response.json")
    assert validator is not None
    errors = list(validator.iter_errors(payload))
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)
    if payload["view"] == "summary":
        assert_issue_invariant(payload["health"], payload["issues"])
    for period in payload.get("periods", []):
        assert_issue_invariant(period["health"], period.get("issues", []))


def assert_issue_invariant(health, issues):
    if health in {"delayed", "action_required"}:
        assert any(i["severity"] == health for i in issues)
    else:
        assert not issues


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "complete",
        "filtered_empty",
        "media",
        "action",
        "partial",
        "none",
        "unknown",
        "history",
    ],
)
async def test_whole_summary_and_periods_schema_including_empty_scope(status_harness, case):
    h = status_harness
    config = configuration()
    request = {}
    if case != "empty":
        await h.ledger.put_configuration(config)
        obligation = obligation_for(config)
        if case in {"partial", "none", "unknown"}:
            obligation = replace(obligation, coverage_status=case)
        await h.ledger.commit_obligation(obligation)
        if case not in {"action", "partial", "none", "unknown"}:
            await h.ledger.commit_revision(*revision_for(obligation))
    if case == "filtered_empty":
        request = {"feed_purposes": ["billing"]}
    elif case == "media":
        request = {"media_buy_ids": ["mb_acct_a"]}
    elif case == "history":
        request = {
            "period": {"start": (START - timedelta(days=1)).isoformat(), "end": END.isoformat()}
        }
    handler = ReportingStatusHandler(h.ledger, consumer_status_enabled=True)
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    for view in ("summary", "periods"):
        response = handler.render_snapshot(
            {**request, "view": view}, caller=CALLER, snapshot=snapshot
        )
        validate_read(response)
        if view != "summary":
            continue
        assert "next_expected_at" not in response
        if case in {"empty", "filtered_empty"}:
            assert response["health"] == "complete"
            assert response["scope"]["scope_closed"] is True
            assert response["coverage"]["status"] == "full"
            assert response["coverage"]["evaluated_at"] == response["ledger_as_of"]
            assert response["obligation_counts"]["total"] == 0
        if case == "history":
            assert response["health"] == "action_required"
            assert not response["scope"]["coverage_complete"]
            assert any(i["code"] == "HISTORY_UNAVAILABLE" for i in response["issues"])
        if case == "media":
            assert not response["scope"]["all_accessible_media_buys"]
            assert response["scope"]["media_buy_ids"] == ["mb_acct_a"]


async def test_current_unreadable_leaf_cannot_be_masked_by_a_readable_superseded_revision(
    status_harness,
):
    h = status_harness
    obligation, first, _ = await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    replacement, rows = revision_for(obligation, suffix="replacement")
    replacement = replace(
        replacement,
        supersedes_reporting_revision_id=first.reporting_revision_id,
        created_at=NOW,
        observed_at=NOW,
        readable=False,
    )
    await h.ledger.commit_revision(replacement, rows)
    await h.drain()
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    handler = ReportingStatusHandler(h.ledger)
    response = handler.render_snapshot({}, caller=CALLER, snapshot=snapshot)
    validate_read(response)
    assert response["health"] == "action_required"
    (event,) = await h.events()
    assert event.cause.issue_ids == tuple(sorted(i["issue_id"] for i in response["issues"]))
    assert {i["code"] for i in response["issues"]} == {"RESOURCE_EXPIRED"}


async def test_period_feed_media_filters_bind_cursor_and_match_connection_bound_page(
    status_harness,
):
    h = status_harness
    analytics = configuration()
    billing = replace(analytics, delivery_config_id="billing", feed_purpose="billing")
    for config in (analytics, billing):
        await h.ledger.put_configuration(config)
        obligation = replace(
            obligation_for(config), reporting_obligation_id=config.delivery_config_id
        )
        await h.ledger.commit_obligation(obligation)
        await h.ledger.commit_revision(*revision_for(obligation, suffix=config.delivery_config_id))
    request = {
        "view": "periods",
        "feed_purposes": ["billing"],
        "media_buy_ids": ["mb_acct_a"],
        "period": {"start": START.isoformat(), "end": END.isoformat()},
    }
    handler = ReportingStatusHandler(h.ledger, page_size=1)
    first = await handler.handle(request, caller=CALLER)
    validate_read(first)
    assert first["scope"]["feed_purposes"] == ["billing"]
    assert first["periods"][0]["delivery_config_id"] == "billing"
    cursor = first["pagination"]["cursor"]
    for change in (
        {"feed_purposes": ["analytics"]},
        {"media_buy_ids": ["other"]},
        {"period": {"start": START.isoformat(), "end": NOW.isoformat()}},
    ):
        with pytest.raises(LedgerConflictError, match="different snapshot"):
            await handler.handle(
                {**request, **change, "pagination": {"cursor": cursor}}, caller=CALLER
            )
    boundary = await h.ledger.open_snapshot(
        account_id="acct_a", filters_fingerprint="filter-contract"
    )
    page = await h.ledger.read_page(
        snapshot=boundary,
        consumer_id="buyer",
        delivery_config_ids=None,
        media_buy_ids=["mb_acct_a"],
        feed_purposes=["billing"],
        period_start=START,
        period_end=END,
        offset=0,
        limit=100,
        changes_after_sequence=None,
    )
    assert {o.delivery_config_id for o in page.obligations} == {"billing"}
    assert {r.reporting_revision_id for r in page.revisions} == {"rpr_acct_a_billing"}
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    value = StatusProjectionInput(
        snapshot,
        ReportingStatusScope("acct_a"),
        feed_purposes=("billing",),
        media_buy_ids=("mb_acct_a",),
        period_start=START,
        period_end=END,
    )
    result = project_status_scope(value)
    assert [p.obligation for p in result.obligations] == list(page.obligations)
    assert project_status_scope(replace(value, period_end=NOW)).fingerprint != result.fingerprint


async def test_mixed_generation_retention_uses_the_latest_common_boundary(status_harness):
    h = status_harness
    h.clock.now = START + timedelta(days=30)
    older = replace(configuration(), status_retention_days=30)
    newer = replace(
        older,
        delivery_config_version=2,
        status_retention_days=7,
        activated_at=START + timedelta(days=25),
        deactivated_at=None,
    )
    for config in (older, newer):
        await h.ledger.put_configuration(config)
    handler = ReportingStatusHandler(h.ledger)
    response = await handler.handle(
        {"period": {"start": START.isoformat(), "end": h.clock().isoformat()}}, caller=CALLER
    )
    validate_read(response)
    assert response["scope"]["ledger_retained_from"] == newer.activated_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert response["health"] == "action_required"
    assert len(response["issues"]) == 2


@pytest.mark.parametrize(
    "change,code",
    [
        ({"period_source_timezone": "America/New_York"}, "INVALID_STATUS_PERIOD"),
        ({"period_end": END + timedelta(minutes=1)}, "OBLIGATION_IDENTITY_MISMATCH"),
        ({"status_as_of": NOW + timedelta(seconds=1)}, "INVALID_STATUS_TIME"),
        ({"status_as_of": START - timedelta(seconds=1)}, "INVALID_STATUS_TIME"),
        ({"consumer_status": "revision_missing", "status_as_of": END}, "STATUS_NOT_DUE"),
    ],
)
async def test_ingest_validates_locked_calendar_and_observation(status_harness, change, code):
    h = status_harness
    obligation, _, _ = await h.seed()
    with pytest.raises(LedgerConflictError) as exc:
        await h.ledger.record_consumer_status(replace(statement(obligation), **change))
    assert exc.value.code == code
    assert not await h.ledger.list_consumer_statuses(account_id="acct_a", consumer_id="buyer")


def test_escalation_wire_seconds_are_exact_integers():
    with pytest.raises(ValueError):
        ReportingDeliveryEscalation(consumer_mismatch_escalation=timedelta(microseconds=1))
