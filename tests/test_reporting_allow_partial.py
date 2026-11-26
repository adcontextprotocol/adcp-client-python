"""``coverage_requirement: allow_partial``: publish a partial period's covered buys only.

AdCP 3.2 ``reporting-delivery-config.json``: ``allow_partial`` permits
publication only for the explicitly covered package denominator; every
revision and status response still exposes partial coverage, and covered-subset
totals are never presented as whole-buy totals. ``full`` is unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from adcp.reporting.fixtures import OFFICIAL_OFFERING_ID, SNAPSHOT_OFFERING_ID
from adcp.reporting.inline_source import (
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.ledger import (
    CompleteMediaBuyScope,
    CompletePackageRoster,
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    MissingHistoricalPackageRoster,
    PerMediaBuyPackageRoster,
    ProducerOfferings,
    ReportingConfiguration,
    ReportingProducer,
    ReportingStatusCaller,
    ReportingStatusHandler,
    ReportingSubscriptionGeneration,
    UnsupportedMediaBuyRoster,
    revision_to_wire,
)
from adcp.reporting.ledger._record_rows import (
    _configuration_from_row,
    _configuration_payload,
    _schedule_from_payload,
    _schedule_payload,
)
from adcp.reporting.ledger.store import _config_payload, _fingerprint
from tests.test_reporting_ledger import _configuration
from tests.test_reporting_producer_retry import _ready_capabilities

UTC = timezone.utc
CALLER = ReportingStatusCaller(account_id="acct_1", consumer_id="buyer_1")
# Activated 00:20, hourly: the first owed period is [01:00, 02:00).
FIRST_CLOSE = datetime(2026, 9, 1, 2, 30, tzinfo=UTC)
PACKAGE_OFFERINGS = ProducerOfferings(
    snapshot_offering_id=SNAPSHOT_OFFERING_ID,
    official_offering_id=OFFICIAL_OFFERING_ID,
    requested_dimensions=("package",),
)


def _all_scope(**overrides: Any) -> ReportingConfiguration:
    overrides.setdefault("required_finality", "snapshot")
    return _configuration(media_buy_ids=(), media_buy_scope="all_media_buys", **overrides)


def _partial(**overrides: Any) -> ReportingConfiguration:
    return _all_scope(coverage_requirement="allow_partial", **overrides)


async def _scope(configuration, period):
    return CompleteMediaBuyScope(("mb_a", "mb_b"))


async def _one_unknown(configuration, period):
    """``mb_a`` is provable; ``mb_b``'s packages cannot be proven at the cutoff."""
    return PerMediaBuyPackageRoster(
        {
            "mb_a": CompletePackageRoster(("pkg_a1", "pkg_a2")),
            "mb_b": MissingHistoricalPackageRoster(),
        }
    )


async def _all_unknown(configuration, period):
    return PerMediaBuyPackageRoster(
        {"mb_a": MissingHistoricalPackageRoster(), "mb_b": MissingHistoricalPackageRoster()}
    )


class _Fetch:
    """Answers with package rows for every buy the slice asked about."""

    def __init__(self, extra: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[Any] = []
        self.extra = extra or []

    def __call__(self, request: Any) -> list[dict[str, Any]]:
        self.requests.append(request)
        asked = [item.media_buy_id for item in request.coverage.constituents]
        rows = [
            {"media_buy_id": buy, "package_id": f"pkg_{buy[-1]}1", "impressions": 10}
            for buy in asked
        ]
        return rows + self.extra


def _producer(store, fetch: _Fetch, *, roster=_one_unknown) -> ReportingProducer:
    source = InlineReportingSource(
        capabilities=_ready_capabilities(),
        fetch=fetch,
        staging=InMemoryStagingStore(),
        seals=InMemorySealStore(),
        clock=lambda: FIRST_CLOSE,
    )
    return ReportingProducer(
        source=source,
        object_reader=source.staging,
        offerings=PACKAGE_OFFERINGS,
        store=store,
        clock=lambda: FIRST_CLOSE,
        media_buy_scope_resolver=_scope,
        package_roster_resolver=roster,
    )


async def _period_view(store) -> dict[str, Any]:
    view = await ReportingStatusHandler(store).handle({"view": "periods"}, caller=CALLER)
    (period,) = view["periods"]
    return period


# -- the generation ---------------------------------------------------------------


def test_full_generations_keep_their_exact_stored_bytes_and_digests() -> None:
    explicit = _configuration()
    all_scope = _all_scope()
    assert explicit.coverage_requirement == all_scope.coverage_requirement == "full"
    for configuration in (explicit, all_scope):
        assert "coverage_requirement" not in _config_payload(configuration)["schedule"]
        assert _configuration_payload(configuration)["schedule"] == _schedule_payload(
            configuration.schedule
        )
    # Digests a full generation had before coverage_requirement existed.
    assert (
        _fingerprint(_configuration_payload(explicit))
        == "522e4d70c837a787a0cd6789c4df79e4ef8df6c405a80548d44eb10f90f4241f"
    )
    assert (
        _fingerprint(_config_payload(explicit))
        == "5e93fd9123b046d64d8e1be86cdecede7a75c84954e9c080ea0ad8133b7f98c3"
    )
    assert (
        _fingerprint(_configuration_payload(all_scope))
        == "0efce81ceb871b84a08ab24889b5645e03cd394b62780ca17a458f3528a23c76"
    )
    assert (
        _fingerprint(_config_payload(all_scope))
        == "47882e9d876e240df57d99b4e972d1acb5bfb8986e4f5cd240d87012d554ee67"
    )


def test_allow_partial_is_generation_content_in_the_stored_schedule() -> None:
    partial = _partial()
    stored = _configuration_payload(partial)["schedule"]
    assert stored == {
        **_schedule_payload(partial.schedule),
        "coverage_requirement": "allow_partial",
    }
    # A reader that predates the requirement decodes the same schedule and
    # treats the generation as full, so it withholds rather than over-publishes.
    assert _schedule_from_payload(stored) == partial.schedule
    assert _config_payload(partial)["schedule"]["coverage_requirement"] == "allow_partial"
    assert _fingerprint(_configuration_payload(partial)) != _fingerprint(
        _configuration_payload(_all_scope())
    )


def test_a_stored_row_decodes_its_requirement_and_a_legacy_row_is_full() -> None:
    def row(configuration: ReportingConfiguration) -> tuple[Any, ...]:
        payload = _configuration_payload(configuration)
        return (
            configuration.delivery_config_id,
            configuration.delivery_config_version,
            configuration.account_id,
            configuration.report_definition_id,
            configuration.reporting_profile,
            configuration.feed_purpose,
            configuration.required_finality,
            configuration.account_timezone,
            payload["schedule"],
            list(configuration.media_buy_ids),
            configuration.activated_at,
            configuration.deactivated_at,
            configuration.automated_recovery_window.total_seconds(),
            configuration.status_retention_days,
            payload["definition"],
            configuration.authoritative_party,
            configuration.consumer_id,
            configuration.quarantined,
            "",
            configuration.media_buy_scope,
        )

    assert _configuration_from_row(row(_partial())) == _partial()
    assert _configuration_from_row(row(_all_scope())).coverage_requirement == "full"


def test_the_requirement_is_validated() -> None:
    with pytest.raises(ValueError, match="coverage_requirement"):
        _configuration(coverage_requirement="best_effort")
    with pytest.raises(ValueError, match="requires full coverage"):
        _configuration(
            delivery_config_id="sub_1",
            subscription=ReportingSubscriptionGeneration("sub_1", SNAPSHOT_OFFERING_ID),
            coverage_requirement="allow_partial",
        )


async def test_memory_store_round_trips_and_refuses_a_second_requirement() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _partial()
    await store.put_configuration(configuration)
    await store.put_configuration(configuration)
    assert await store.list_configurations(caller=CALLER) == (configuration,)
    with pytest.raises(LedgerConflictError, match="publish a new version"):
        await store.put_configuration(replace(configuration, coverage_requirement="full"))


# -- publication -------------------------------------------------------------------


async def test_a_mixed_day_publishes_only_the_provable_buy_and_stays_partial() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _partial()
    await store.put_configuration(configuration)
    fetch = _Fetch()
    turn = await _producer(store, fetch).run_configuration(configuration, now=FIRST_CLOSE)

    assert turn.slices_failed == [] and len(turn.revisions_committed) == 1
    # The source is asked for the covered buy only.
    (request,) = fetch.requests
    assert [item.media_buy_id for item in request.coverage.constituents] == ["mb_a"]
    (obligation,) = await store.list_generation_obligations(configuration.generation_key)
    assert obligation.media_buy_ids == ("mb_a", "mb_b")
    assert obligation.coverage_status == "partial"
    (revision,) = await store.list_revisions(
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )
    page = await store.read_revision_rows(
        account_id=obligation.account_id, reporting_revision_id=revision.reporting_revision_id
    )
    assert not page.has_more
    assert {row["media_buy_id"] for row in page.rows} == {"mb_a"}

    period = await _period_view(store)
    coverage = period["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["media_buy_ids"] == ["mb_a", "mb_b"]
    assert coverage["fully_covered_media_buy_ids"] == ["mb_a"]
    assert coverage["unknown_media_buy_ids"] == ["mb_b"]
    assert coverage["covered_package_ids"] == ["pkg_a1", "pkg_a2"]
    assert period["revision_count"] == 1
    # The published revision carries the frozen partial coverage, so its totals
    # are never read as whole-buy totals.
    wire = revision_to_wire(revision, obligation=obligation)
    assert wire["coverage"] == coverage
    assert wire["media_buy_ids"] == ["mb_a", "mb_b"]
    assert wire["row_count"] == 1
    # Partial coverage is what this generation asked for, not a seller fault.
    assert period["health"] != "action_required"
    assert "REPORTING_COVERAGE_INCOMPLETE" not in {issue["code"] for issue in period["issues"]}


async def test_the_same_day_under_full_is_withheld_and_action_required() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _all_scope()
    await store.put_configuration(configuration)
    fetch = _Fetch()
    turn = await _producer(store, fetch).run_configuration(configuration, now=FIRST_CLOSE)

    assert turn.revisions_committed == [] and fetch.requests == []
    period = await _period_view(store)
    assert period["coverage"]["status"] == "partial"
    assert period["health"] == "action_required"
    assert "REPORTING_COVERAGE_INCOMPLETE" in {issue["code"] for issue in period["issues"]}


async def test_a_day_with_nothing_covered_is_withheld_even_under_allow_partial() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _partial()
    await store.put_configuration(configuration)
    fetch = _Fetch()
    turn = await _producer(store, fetch, roster=_all_unknown).run_configuration(
        configuration, now=FIRST_CLOSE
    )

    assert turn.revisions_committed == [] and fetch.requests == []
    period = await _period_view(store)
    assert period["coverage"]["status"] == "unknown"
    assert period["health"] == "action_required"
    assert "REPORTING_COVERAGE_INCOMPLETE" in {issue["code"] for issue in period["issues"]}


async def test_an_unsupported_buy_leaves_the_covered_buy_publishable() -> None:
    async def roster(configuration, period):
        return PerMediaBuyPackageRoster(
            {
                "mb_a": CompletePackageRoster(("pkg_a1",)),
                "mb_b": UnsupportedMediaBuyRoster("provider_limitation", ("pkg_b1",)),
            }
        )

    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _partial()
    await store.put_configuration(configuration)
    fetch = _Fetch()
    turn = await _producer(store, fetch, roster=roster).run_configuration(
        configuration, now=FIRST_CLOSE
    )
    assert len(turn.revisions_committed) == 1
    period = await _period_view(store)
    assert period["coverage"]["unsupported_media_buy_ids"] == ["mb_b"]
    assert period["coverage"]["status"] == "partial"


@pytest.mark.parametrize(
    "row",
    [
        {"media_buy_id": "mb_b", "package_id": "pkg_b1", "impressions": 1},
        {"media_buy_id": "mb_a", "package_id": "pkg_b1", "impressions": 1},
        {"impressions": 1},
    ],
    ids=["uncovered-buy", "uncovered-package", "unattributed"],
)
async def test_a_row_outside_the_covered_denominator_is_never_published(
    row: dict[str, Any],
) -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _partial()
    await store.put_configuration(configuration)
    with pytest.raises(LedgerConflictError) as refused:
        await _producer(store, _Fetch(extra=[row])).run_configuration(
            configuration, now=FIRST_CLOSE
        )

    assert refused.value.code == "ROW_COVERAGE_MISMATCH"
    (obligation,) = await store.list_generation_obligations(configuration.generation_key)
    assert (
        await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        == ()
    )


async def test_an_exact_scope_allow_partial_generation_publishes_its_covered_buys() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _configuration(
        media_buy_ids=("mb_a", "mb_b"),
        required_finality="snapshot",
        coverage_requirement="allow_partial",
    )
    await store.put_configuration(configuration)
    fetch = _Fetch()
    turn = await _producer(store, fetch).run_configuration(configuration, now=FIRST_CLOSE)

    assert len(turn.revisions_committed) == 1
    (request,) = fetch.requests
    assert [item.media_buy_id for item in request.coverage.constituents] == ["mb_a"]
    assert (await _period_view(store))["coverage"]["status"] == "partial"
