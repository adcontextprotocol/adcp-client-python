"""The same currency trust boundary against memory and real PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any

import pytest

from adcp.reporting import (
    ReportingContentReading,
    ReportingPinnedDefinition,
    classify_content_mismatch,
)
from adcp.reporting.conformance import (
    ReportingSourceConformanceError,
    run_reporting_source_replay_conformance,
    validate_reporting_source_execution,
)
from adcp.reporting.fixtures import (
    OFFICIAL_OFFERING_ID,
    SNAPSHOT_OFFERING_ID,
    redacted_capabilities,
    redacted_contract_identity,
    redacted_multi_currency_requests,
)
from adcp.reporting.inline_source import (
    InlineFetch,
    InlineFetchResult,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.ledger import (
    CurrencyResolver,
    FixedCurrencyResolver,
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ProducerOfferings,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingCurrencyError,
    ReportingDefinitionBinding,
    ReportingLedgerStore,
    ReportingObligationRecord,
    ReportingProducer,
    ReportingScheduleSpec,
    ReportingStatusCaller,
    ReportingStatusHandler,
    require_single_currency,
    revision_content_sha256,
)
from adcp.reporting.ledger.pg import PgReportingLedgerStore
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    ReportingSourceExecutorResult,
    ReportingSourceSliceRequestV1,
    SourceBatchManifestV1,
    encode_source_batch_manifest_v1,
    parse_verified_source_batch_manifest_v1,
    publication_content_fingerprint_v1,
    reporting_source_capabilities_sha256_v1,
    source_batch_manifest_reference_v1,
)
from adcp.types import GetMediaBuyDeliveryResponse, GetReportingStatusResponse

from ._generation_support import START, UncalledSource, isolated_reporting_pool, revision_for

END = START + timedelta(days=1)
NOW = END + timedelta(days=2)


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ReportingLedgerStore]:
    if request.param == "memory":
        yield InMemoryReportingLedgerStore(clock=lambda: NOW)
    else:
        async with isolated_reporting_pool() as pool:
            ledger = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
            await ledger.create_schema()
            yield ledger


def configuration(
    account: str = "eur", *, pinned_currency: str | None = None
) -> ReportingConfiguration:
    contract = redacted_contract_identity
    units = (("spend", pinned_currency),) if pinned_currency else ()
    return ReportingConfiguration(
        account_id=account,
        delivery_config_id="daily",
        delivery_config_version=1,
        report_definition_id=contract.report_definition_id,
        reporting_profile=contract.reporting_profile,
        feed_purpose="analytics",
        schedule=ReportingScheduleSpec("P1D", "PT1H", period_anchor=START),
        required_finality="snapshot",
        activated_at=START,
        deactivated_at=END,
        media_buy_ids=(f"mb_{account}",),
        definition=ReportingDefinitionBinding(
            report_definition_uri=contract.report_definition_uri,
            report_definition_sha256=contract.report_definition_sha256,
            schema_version=contract.schema_version,
            schema_uri=contract.schema_uri,
            schema_sha256=contract.schema_sha256,
            monetary_metric_units=units,
            monetary_control_total_units=units,
        ),
    )


def capabilities() -> ReportingSourceCapabilitiesV1:
    base = redacted_capabilities()
    draft = base.model_copy(
        update={
            "offerings": [
                offering.model_copy(
                    update={
                        "source_timezone": "UTC",
                        "product_ids": [configuration().report_definition_id],
                    }
                )
                for offering in base.offerings
            ]
        }
    )
    return ReportingSourceCapabilitiesV1.model_validate(
        draft.model_copy(
            update={"capabilities_sha256": reporting_source_capabilities_sha256_v1(draft)}
        )
    )


def source_for(fetch: InlineFetch) -> tuple[InlineReportingSource, InMemoryStagingStore]:
    staging = InMemoryStagingStore()
    return (
        InlineReportingSource(
            capabilities=capabilities(),
            fetch=fetch,
            staging=staging,
            seals=InMemorySealStore(),
            clock=lambda: NOW,
        ),
        staging,
    )


def producer_for(
    store: ReportingLedgerStore,
    source: Any,
    staging: InMemoryStagingStore | None = None,
    *,
    currency: str = "USD",
    resolver: CurrencyResolver | None = None,
) -> ReportingProducer:
    return ReportingProducer(
        store=store,
        source=source,
        object_reader=staging,
        offerings=ProducerOfferings(
            snapshot_offering_id=SNAPSHOT_OFFERING_ID,
            official_offering_id=OFFICIAL_OFFERING_ID,
            publication_namespace="reporting-source:fixture",
            source_scope=dict(redacted_capabilities().source_scope),
            currency=currency,
        ),
        currency_resolver=resolver,
        clock=lambda: NOW,
    )


def money_rows(request: ReportingSourceSliceRequestV1) -> list[dict[str, Any]]:
    return [
        {
            "media_buy_id": request.coverage.constituents[0].media_buy_id,
            "impressions": 5,
            "spend": "0.10",
            "currency": request.currency,
        },
        {
            "media_buy_id": request.coverage.constituents[0].media_buy_id,
            "impressions": 10,
            "spend": "0.20",
            "currency": request.currency,
        },
    ]


async def test_concurrent_accounts_freeze_before_acquisition_and_survive_restarts(
    store: ReportingLedgerStore,
) -> None:
    configs = [configuration("usd"), configuration("eur")]
    currencies = {"usd": "USD", "eur": "EUR"}
    calls: list[str] = []

    async def resolver(config: ReportingConfiguration, candidate: ReportingObligationRecord) -> str:
        assert candidate.currency is None
        assert candidate.generation_key == config.generation_key
        assert candidate.scope_resolved_at == END
        calls.append(candidate.account_id)
        await asyncio.sleep(0)
        return currencies[candidate.account_id]

    requests: list[ReportingSourceSliceRequestV1] = []
    ready = False

    async def fetch(request: ReportingSourceSliceRequestV1) -> InlineFetchResult | None:
        requests.append(request)
        # Model concurrent I/O even in memory (completed coroutines need not
        # yield on Python 3.12), while each worker still holds its lease.
        await asyncio.sleep(0)
        stored = await store.get_obligation(
            account_id=request.identity.account_id,
            reporting_obligation_id=request.identity.reporting_obligation_id,
        )
        assert stored is not None and stored.currency == request.currency
        if not ready:
            return None
        return InlineFetchResult(rows=money_rows(request), currency=request.currency)

    source, staging = source_for(fetch)
    producer = producer_for(store, source, staging, resolver=resolver)
    await asyncio.gather(*(store.put_configuration(config) for config in configs))
    # One producer concurrently leases both accounts' same-name generation.
    turns = await asyncio.gather(producer.run_worker(), producer.run_worker())
    assert {turn.leased.account_id for turn in turns if turn.leased} == {"usd", "eur"}
    assert all(
        len(turn.obligations_committed) == 1 and len(turn.slices_failed) == 1 for turn in turns
    )
    assert sorted(calls) == ["eur", "usd"]
    assert {request.currency for request in requests} == {"EUR", "USD"}
    first_keys = {
        request.identity.account_id: request.identity.source_execution_key for request in requests
    }

    # The resolver's account state and the process default both change before
    # retry. Neither is consulted for an already committed obligation.
    currencies.update(usd="GBP", eur="JPY")
    ready = True
    restarted_store = (
        PgReportingLedgerStore(pool=store._pool, clock=lambda: NOW)
        if isinstance(store, PgReportingLedgerStore)
        else store
    )

    def must_not_resolve(
        config: ReportingConfiguration, candidate: ReportingObligationRecord
    ) -> str:
        raise AssertionError("retry/restart/restatement must not resolve currency")

    restarted = producer_for(
        restarted_store, source, staging, currency="GBP", resolver=must_not_resolve
    )
    results = await asyncio.gather(restarted.run_worker(), restarted.run_worker())
    assert all(len(turn.revisions_committed) == 1 for turn in results)
    assert all(
        request.identity.source_execution_key == first_keys[request.identity.account_id]
        for request in requests
    )

    for config in configs:
        obligation = await restarted_store.find_obligation(
            account_id=config.account_id,
            delivery_config_id="daily",
            delivery_config_version=1,
            period_start=START,
            period_end=END,
        )
        assert obligation is not None
        expected = "EUR" if config.account_id == "eur" else "USD"
        assert obligation.currency == expected
        # A replaced caller object cannot replace the stored currency.
        restated = await restarted.acquire_obligation(
            config,
            replace(obligation, currency="GBP"),
            restate=True,
        )
        assert restated is not None and restated.supersedes_reporting_revision_id
        assert requests[-1].currency == expected
        assert requests[-1].identity.source_execution_key != first_keys[config.account_id]
        assert await restarted.close_elapsed_periods(config) == []
        handler = ReportingStatusHandler(restarted_store)
        payload = await handler.handle(
            {"view": "periods", "context": {"currency": "JPY"}},
            caller=ReportingStatusCaller(account_id=config.account_id, consumer_id="buyer"),
        )
        parsed = GetReportingStatusResponse.model_validate(payload)
        current = next(
            item
            for item in parsed.revisions
            if item.reporting_revision_id == restated.reporting_revision_id
        )
        wire_obligation = parsed.periods[0]
        rows = await restarted_store.read_revision_rows(
            account_id=config.account_id,
            reporting_revision_id=restated.reporting_revision_id,
        )
        assert {row["currency"] for row in rows.rows} == {expected}
        assert dict(restated.control_totals)["spend"] == "0.30"
        assert restated.revision_content_sha256 == revision_content_sha256(
            reporting_revision_id=restated.reporting_revision_id,
            row_count=2,
            control_totals=restated.control_totals,
            reporting_rows=rows.rows,
        )
        reading = ReportingContentReading(
            reporting_revision_id=restated.reporting_revision_id,
            media_buy_ids=config.media_buy_ids,
            metric_names=("impressions", "spend"),
            metric_units={"spend": expected},
            control_total_units={"spend": expected},
        )
        definition = ReportingPinnedDefinition(
            metric_names=("impressions", "spend"),
            metric_units={"spend": expected},
            control_total_units={"spend": expected},
        )
        assert (
            classify_content_mismatch(
                obligation=wire_obligation,
                revision=current,
                reading=reading,
                definition=definition,
            )
            is None
        )
        assert (
            classify_content_mismatch(
                obligation=wire_obligation,
                revision=current,
                reading=replace(reading, metric_units={"spend": "JPY"}),
                definition=definition,
            )
            == "currency_mismatch"
        )
        assert (
            await restarted_store.get_obligation(
                account_id=config.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
        ) == obligation


@pytest.mark.parametrize(
    "invalid", ["usd", "Usd", "US", "EURO", "US1", " EUR", "EUR\n", "ÅBC", "", None, ["USD", "EUR"]]
)
async def test_invalid_resolution_cannot_commit_or_touch_source(
    store: ReportingLedgerStore,
    invalid: Any,
) -> None:
    producer = producer_for(store, UncalledSource(), resolver=lambda config, candidate: invalid)
    with pytest.raises(ReportingCurrencyError, match="INVALID_CURRENCY"):
        await producer.close_elapsed_periods(configuration())
    snapshot = await store.open_snapshot(account_id="eur", filters_fingerprint="")
    assert snapshot.max_sequence == 0


@pytest.mark.parametrize("currency", ["USD", "EUR"])
async def test_fixed_currency_option_and_sync_resolver_are_supported(
    store: ReportingLedgerStore,
    currency: str,
) -> None:
    for account, resolver in [("default", None), ("explicit", FixedCurrencyResolver(currency))]:
        producer = producer_for(store, UncalledSource(), currency=currency, resolver=resolver)
        (obligation,) = await producer.close_elapsed_periods(configuration(account))
        assert obligation.currency == currency


async def test_concurrent_resolutions_converge_on_one_immutable_winner(
    store: ReportingLedgerStore,
) -> None:
    entered = asyncio.Event()
    count = 0

    async def resolve(config: ReportingConfiguration, candidate: ReportingObligationRecord) -> str:
        nonlocal count
        count += 1
        result = "EUR" if count == 1 else "USD"
        if count == 2:
            entered.set()
        await entered.wait()
        return result

    producer = producer_for(store, UncalledSource(), resolver=resolve)
    left, right = await asyncio.gather(
        producer.close_elapsed_periods(configuration()),
        producer.close_elapsed_periods(configuration()),
    )
    assert left == right and left[0].currency in {"USD", "EUR"}
    assert (await store.open_snapshot(account_id="eur", filters_fingerprint="")).max_sequence == 1


async def test_trusted_mixed_scope_is_rejected_at_freeze(store: ReportingLedgerStore) -> None:
    config = replace(configuration(), media_buy_ids=("mb_usd", "mb_eur"))
    currencies = {"mb_usd": "USD", "mb_eur": "EUR"}
    producer = producer_for(
        store,
        UncalledSource(),
        resolver=lambda accepted, candidate: require_single_currency(
            currencies[buy] for buy in candidate.media_buy_ids
        ),
    )
    with pytest.raises(ReportingCurrencyError, match="MIXED_CURRENCY_SCOPE"):
        await producer.close_elapsed_periods(config)
    assert (await store.open_snapshot(account_id="eur", filters_fingerprint="")).max_sequence == 0


@pytest.mark.parametrize("mixed", [False, True])
async def test_pinned_definition_units_are_checked_before_source(
    store: ReportingLedgerStore,
    mixed: bool,
) -> None:
    config = configuration(pinned_currency="USD")
    assert config.definition is not None
    if mixed:
        config = replace(
            config,
            definition=replace(
                config.definition,
                monetary_control_total_units=(("spend", "EUR"),),
            ),
        )
    producer = producer_for(store, UncalledSource(), currency="EUR")
    with pytest.raises(
        ReportingCurrencyError, match="MIXED_CURRENCY_SCOPE" if mixed else "CURRENCY_MISMATCH"
    ):
        await producer.close_elapsed_periods(config)


class CorruptingSource:
    def __init__(
        self, source: InlineReportingSource, corrupt: Callable[[dict[str, Any]], None]
    ) -> None:
        self.source = source
        self.corrupt = corrupt
        self.request: ReportingSourceSliceRequestV1 | None = None
        self.result: ReportingSourceExecutorResult | None = None

    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        return self.source.capabilities

    async def execute(
        self,
        request: ReportingSourceSliceRequestV1,
        *,
        cancel: asyncio.Event,
        heartbeat: Callable[[], None] | None = None,
    ) -> ReportingSourceExecutorResult:
        result = await self.source.execute(request, cancel=cancel)
        assert result.response is not None and result.manifest_bytes is not None
        manifest = parse_verified_source_batch_manifest_v1(
            result.response.manifest, result.manifest_bytes
        )
        payload = manifest.model_dump(mode="json", exclude_none=True)
        self.corrupt(payload)
        payload["content_fingerprint"] = publication_content_fingerprint_v1(payload)
        raw = encode_source_batch_manifest_v1(SourceBatchManifestV1.model_validate(payload))
        self.request = request
        self.result = ReportingSourceExecutorResult.completed(
            request=request,
            manifest=source_batch_manifest_reference_v1("staged-corrupt", raw),
            manifest_bytes=raw,
        )
        return self.result


@pytest.mark.parametrize("corruption", ["manifest_currency", "total_unit", "total_value"])
async def test_adapter_currency_and_monetary_totals_are_only_evidence(
    store: ReportingLedgerStore,
    corruption: str,
) -> None:
    def corrupt(payload: dict[str, Any]) -> None:
        if corruption == "manifest_currency":
            payload["currency"] = "USD"
        else:
            spend = next(item for item in payload["control_totals"] if item["name"] == "spend")
            spend["unit" if corruption == "total_unit" else "value"] = (
                "USD" if corruption == "total_unit" else "99.99"
            )

    source, staging = source_for(money_rows)
    adapter = CorruptingSource(source, corrupt)
    producer = producer_for(store, adapter, staging, currency="EUR")
    config = configuration(pinned_currency="EUR")
    (obligation,) = await producer.close_elapsed_periods(config)
    code = "MONETARY_TOTAL_MISMATCH" if corruption == "total_value" else "CURRENCY_MISMATCH"
    with pytest.raises(ReportingCurrencyError, match=code):
        await producer.acquire_obligation(config, obligation)
    assert (
        await store.list_revisions(
            account_id="eur", reporting_obligation_id=obligation.reporting_obligation_id
        )
        == ()
    )
    if corruption != "total_value":
        assert adapter.request is not None and adapter.result is not None
        with pytest.raises(ReportingSourceConformanceError, match="MANIFEST_MISMATCH"):
            await validate_reporting_source_execution(
                capabilities=adapter.capabilities,
                request=adapter.request.model_copy(
                    update={"deadline_at": datetime.now(timezone.utc) + timedelta(hours=1)}
                ),
                result=adapter.result,
                object_reader=staging,
            )


@pytest.mark.parametrize("corruption", ["row_currency", "mixed_rows", "source_currency"])
async def test_inline_source_rejects_currency_before_aggregation(
    store: ReportingLedgerStore,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    def fetch(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        rows = money_rows(request)
        if corruption == "mixed_rows":
            rows[0]["currency"] = "USD"
        elif corruption == "row_currency":
            for row in rows:
                row["currency"] = "USD"
        return InlineFetchResult(
            rows=rows, currency="USD" if corruption == "source_currency" else None
        )

    def must_not_aggregate(*args: Any) -> None:
        raise AssertionError("mixed/mismatched money was aggregated")

    monkeypatch.setattr("adcp.reporting.inline_source._control_totals", must_not_aggregate)
    source, staging = source_for(fetch)
    producer = producer_for(store, source, staging, currency="EUR")
    config = configuration()
    (obligation,) = await producer.close_elapsed_periods(config)
    request = producer._build_slice(config, obligation, SNAPSHOT_OFFERING_ID, now=NOW)
    result = await source.execute(request, cancel=asyncio.Event())
    assert not result.ok and result.error is not None
    assert result.error.code == "INTEGRITY_FAILED"
    assert (
        "MIXED_CURRENCY_SCOPE" if corruption == "mixed_rows" else "CURRENCY_MISMATCH"
    ) in result.error.safe_message


async def test_official_and_adjustments_keep_frozen_units(store: ReportingLedgerStore) -> None:
    config = replace(configuration(pinned_currency="EUR"), required_finality="official")
    source, staging = source_for(money_rows)
    first = producer_for(store, source, staging, currency="EUR")
    (obligation,) = await first.close_elapsed_periods(config)
    restarted = producer_for(store, source, staging, currency="USD")
    official = await restarted.acquire_obligation(config, obligation)
    assert official is not None and official.finality == "official"
    adjustment = ReportingAdjustmentRecord(
        reporting_adjustment_id="adj_eur",
        account_id="eur",
        adjusts_reporting_revision_id=official.reporting_revision_id,
        reason_code="source_correction",
        accounting_period_start=END,
        accounting_period_end=END + timedelta(days=1),
        control_total_deltas=(("spend", "-0.10"),),
        correction_observed_at=NOW,
        created_at=NOW,
    )
    assert await store.commit_adjustment(adjustment) == adjustment
    assert await restarted.acquire_obligation(config, obligation, restate=True) is None
    assert (
        await store.get_obligation(
            account_id="eur", reporting_obligation_id=obligation.reporting_obligation_id
        )
        == obligation
    )
    with pytest.raises(ReportingCurrencyError, match="MONETARY_TOTAL_MISMATCH"):
        await store.commit_adjustment(
            replace(
                adjustment,
                reporting_adjustment_id="bad_delta",
                control_total_deltas=(("spend", "NaN"),),
            )
        )


async def test_low_level_writes_require_currency_and_validate_frozen_money(
    store: ReportingLedgerStore,
) -> None:
    config = configuration()
    (obligation,) = await producer_for(
        store, UncalledSource(), currency="EUR"
    ).close_elapsed_periods(config)
    assert await store.commit_obligation(replace(obligation, currency="USD")) == obligation
    unknown = replace(
        obligation, reporting_obligation_id="new_unknown", account_id="other", currency=None
    )
    with pytest.raises(ReportingCurrencyError, match="CURRENCY_UNRESOLVED"):
        await store.commit_obligation(unknown)
    revision, _ = revision_for(obligation)
    rows = [{"media_buy_id": "mb_eur", "spend": "0.10", "currency": "USD"}]
    with pytest.raises(ReportingCurrencyError, match="CURRENCY_MISMATCH"):
        await store.commit_revision(replace(revision, control_totals=(("spend", "0.10"),)), rows)
    rows[0]["currency"] = "EUR"
    with pytest.raises(ReportingCurrencyError, match="MONETARY_TOTAL_MISMATCH"):
        await store.commit_revision(replace(revision, control_totals=(("spend", "0.11"),)), rows)
    # The check itself is independent of an adopter's Decimal precision.
    rows[0]["spend"] = "123456789012345678901234567890.12"
    totals = (("spend", rows[0]["spend"]),)
    valid = replace(
        revision,
        control_totals=totals,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision.reporting_revision_id,
            row_count=len(rows),
            control_totals=totals,
            reporting_rows=rows,
        ),
    )
    with localcontext() as context:
        context.prec = 4
        committed = await store.commit_revision(valid, rows)
    assert committed.control_totals[0][1] == rows[0]["spend"]


async def test_pinned_monetary_semantics_are_immutable_in_both_stores(
    store: ReportingLedgerStore,
) -> None:
    config = configuration(pinned_currency="EUR")
    await store.put_configuration(config)
    await store.put_configuration(config)
    assert await store.list_configurations(account_id="eur") == (config,)
    with pytest.raises(LedgerConflictError, match="different content"):
        await store.put_configuration(configuration(pinned_currency="USD"))


async def test_sync_callable_returning_a_coroutine_is_awaited(store: ReportingLedgerStore) -> None:
    async def answer() -> str:
        return "EUR"

    producer = producer_for(store, UncalledSource(), resolver=lambda config, candidate: answer())
    (obligation,) = await producer.close_elapsed_periods(configuration())
    assert obligation.currency == "EUR"


async def test_sealed_source_retry_after_commit_failure_keeps_currency(
    store: ReportingLedgerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fetch(request: ReportingSourceSliceRequestV1) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return money_rows(request)

    source, staging = source_for(fetch)
    config = configuration(pinned_currency="EUR")
    producer = producer_for(store, source, staging, currency="EUR")
    (obligation,) = await producer.close_elapsed_periods(config)
    original_commit = store.commit_revision

    async def crash(*args: Any) -> Any:
        raise RuntimeError("crash after source seal")

    monkeypatch.setattr(store, "commit_revision", crash)
    with pytest.raises(RuntimeError, match="crash after source seal"):
        await producer.acquire_obligation(config, obligation)
    monkeypatch.setattr(store, "commit_revision", original_commit)
    restarted = producer_for(store, source, staging, currency="USD")
    committed = await restarted.acquire_obligation(config, obligation)
    assert committed is not None
    assert calls == 1  # The sealed source publication replays without refetch.
    assert dict(committed.control_totals)["spend"] == "0.30"
    rows = await store.read_revision_rows(
        account_id="eur", reporting_revision_id=committed.reporting_revision_id
    )
    assert {row["currency"] for row in rows.rows} == {"EUR"}


async def test_restored_memory_history_is_readable_but_cannot_be_resolved() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: NOW)
    config = configuration()
    producer = producer_for(store, UncalledSource(), currency="EUR")
    (current,) = await producer.close_elapsed_periods(config)
    # Simulate rehydrating a record written before the optional Python field
    # existed; regular new writes deliberately refuse this representation.
    old = replace(current, currency=None)
    store._obligations[old.reporting_obligation_id] = old

    def must_not_resolve(
        config: ReportingConfiguration, candidate: ReportingObligationRecord
    ) -> str:
        raise AssertionError("legacy history cannot use a current account lookup")

    restarted = producer_for(store, UncalledSource(), resolver=must_not_resolve)
    assert await restarted.close_elapsed_periods(config) == []
    assert await store.commit_obligation(replace(old, currency="USD")) == old
    with pytest.raises(ReportingCurrencyError, match="CURRENCY_UNRESOLVED"):
        await restarted.acquire_obligation(config, replace(old, currency="USD"))
    revision, rows = revision_for(old)
    with pytest.raises(ReportingCurrencyError, match="CURRENCY_UNRESOLVED"):
        await store.commit_revision(revision, rows)
    payload = await ReportingStatusHandler(store).handle(
        {"view": "summary"},
        caller=ReportingStatusCaller(account_id="eur", consumer_id="buyer"),
    )
    assert payload["health"] == "action_required"
    assert payload["issues"][0]["code"] == "HISTORY_UNAVAILABLE"
    assert (
        await store.get_obligation(
            account_id="eur", reporting_obligation_id=old.reporting_obligation_id
        )
        == old
    )


async def test_multi_currency_source_fixtures_pass_replay_conformance() -> None:
    staging = InMemoryStagingStore()
    requests = redacted_multi_currency_requests()
    source = InlineReportingSource(
        capabilities=redacted_capabilities(),
        fetch=money_rows,
        staging=staging,
        clock=lambda: requests[0].period.source_read_cutoff_at,
    )
    manifests = await asyncio.gather(
        *(
            run_reporting_source_replay_conformance(
                executor=source, request=request, object_reader=staging
            )
            for request in requests
        )
    )
    assert {manifest.currency for manifest in manifests} == {"USD", "EUR"}
    assert len({manifest.publication_id for manifest in manifests}) == 2
    for manifest in manifests:
        spend = next(total for total in manifest.control_totals if total.name == "spend")
        assert spend.unit == manifest.currency and spend.value == "0.30"


async def test_non_usd_zero_rows_keep_the_frozen_currency(store: ReportingLedgerStore) -> None:
    source, staging = source_for(lambda request: [])
    producer = producer_for(store, source, staging, currency="EUR")
    config = configuration(pinned_currency="EUR")
    (obligation,) = await producer.close_elapsed_periods(config)
    revision = await producer.acquire_obligation(config, obligation)
    assert revision is not None and revision.row_count == 0
    assert dict(revision.control_totals)["spend"] == "0"
    assert obligation.currency == "EUR"


async def test_manifest_cannot_substitute_a_different_pinned_definition(
    store: ReportingLedgerStore,
) -> None:
    def corrupt(payload: dict[str, Any]) -> None:
        payload["contract"]["report_definition_sha256"] = "f" * 64

    source, staging = source_for(money_rows)
    producer = producer_for(store, CorruptingSource(source, corrupt), staging, currency="EUR")
    config = configuration(pinned_currency="EUR")
    (obligation,) = await producer.close_elapsed_periods(config)
    with pytest.raises(LedgerConflictError) as caught:
        await producer.acquire_obligation(config, obligation)
    assert caught.value.code == "REPORT_DEFINITION_MISMATCH"
    assert (
        await store.list_revisions(
            account_id="eur", reporting_obligation_id=obligation.reporting_obligation_id
        )
        == ()
    )


async def test_postgres_currency_survives_closing_every_application_connection() -> None:
    async with isolated_reporting_pool() as original_pool:
        from psycopg_pool import AsyncConnectionPool

        store = PgReportingLedgerStore(pool=original_pool, clock=lambda: NOW)
        await store.create_schema()
        configs = [configuration("usd"), configuration("eur")]
        currencies = {"usd": "USD", "eur": "EUR"}
        producer = producer_for(
            store,
            UncalledSource(),
            resolver=lambda config, candidate: currencies[candidate.account_id],
        )
        for config in configs:
            await store.put_configuration(config)
            await producer.close_elapsed_periods(config)
        async with original_pool.connection() as connection:
            row = await (await connection.execute("SELECT current_schema()")).fetchone()
            assert row is not None
            schema = row[0]
        await original_pool.close()
        currencies.update(usd="GBP", eur="JPY")

        async with AsyncConnectionPool(
            original_pool.conninfo,
            kwargs={"options": f"-csearch_path={schema} -cstatement_timeout=15000"},
            open=False,
        ) as restarted_pool:
            await restarted_pool.wait(timeout=10)
            restarted_store = PgReportingLedgerStore(pool=restarted_pool, clock=lambda: NOW)
            await restarted_store.create_schema()
            source, staging = source_for(money_rows)

            def must_not_resolve(
                config: ReportingConfiguration, candidate: ReportingObligationRecord
            ) -> str:
                raise AssertionError("restart must use PostgreSQL's frozen currency")

            restarted = producer_for(
                restarted_store, source, staging, currency="GBP", resolver=must_not_resolve
            )
            for config in configs:
                assert await restarted.close_elapsed_periods(config) == []
                obligation = await restarted_store.find_obligation(
                    account_id=config.account_id,
                    delivery_config_id=config.delivery_config_id,
                    delivery_config_version=config.delivery_config_version,
                    period_start=START,
                    period_end=END,
                )
                assert obligation is not None
                expected = "EUR" if config.account_id == "eur" else "USD"
                assert obligation.currency == expected
                for restate in (False, True):
                    revision = await restarted.acquire_obligation(
                        config, obligation, restate=restate
                    )
                    assert revision is not None
                    content = await restarted_store.read_revision_rows(
                        account_id=config.account_id,
                        reporting_revision_id=revision.reporting_revision_id,
                    )
                    assert {row["currency"] for row in content.rows} == {expected}


@pytest.mark.parametrize(
    ("shape", "expected_error"),
    [
        ("response", "CURRENCY_MISMATCH"),
        ("media_buy", "CURRENCY_MISMATCH"),
        ("package", "CURRENCY_MISMATCH"),
        ("mixed_packages", "MIXED_CURRENCY_SCOPE"),
        ("reporting_rows", "CURRENCY_MISMATCH"),
        ("matching_eur", None),
    ],
)
async def test_delivery_response_currency_is_checked_before_flattening(
    store: ReportingLedgerStore,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    expected_error: str | None,
) -> None:
    def fetch(request: ReportingSourceSliceRequestV1) -> GetMediaBuyDeliveryResponse:
        delivery: dict[str, Any] = {
            "media_buy_id": "mb_eur",
            "status": "active",
            "totals": {"impressions": 15, "spend": 0.30},
            "by_package": [],
        }
        payload: dict[str, Any] = {
            "reporting_period": {"start": START, "end": END},
            "media_buy_deliveries": [delivery],
        }
        if shape in {"response", "reporting_rows", "matching_eur"}:
            payload["currency"] = "EUR" if shape == "matching_eur" else "USD"
        if shape in {"reporting_rows", "matching_eur"}:
            # Core's canonical row format uses exact decimal strings; the
            # ordinary delivery surface models its legacy metrics as floats.
            payload["reporting_rows"] = money_rows(request)
        if shape == "media_buy":
            delivery["currency"] = "USD"
        if shape in {"package", "mixed_packages"}:
            codes = ["USD", "EUR"] if shape == "mixed_packages" else ["USD"]
            delivery["totals"] = {"impressions": 15}
            delivery["by_package"] = [
                {
                    "package_id": f"pkg_{code}",
                    "pricing_model": "cpm",
                    "rate": 1.0,
                    "currency": code,
                    "impressions": 5,
                    "spend": 0.10,
                }
                for code in codes
            ]
        return GetMediaBuyDeliveryResponse.model_validate(payload)

    source, staging = source_for(fetch)
    producer = producer_for(store, source, staging, currency="EUR")
    config = configuration()
    (obligation,) = await producer.close_elapsed_periods(config)
    if expected_error is None:
        revision = await producer.acquire_obligation(config, obligation)
        assert revision is not None
        assert Decimal(dict(revision.control_totals)["spend"]) == Decimal("0.30")
        assert obligation.currency == "EUR"
    else:

        def must_not_aggregate(*args: Any) -> None:
            raise AssertionError("delivery-response currency was discarded before aggregation")

        monkeypatch.setattr("adcp.reporting.inline_source._control_totals", must_not_aggregate)
        request = producer._build_slice(config, obligation, SNAPSHOT_OFFERING_ID, now=NOW)
        result = await source.execute(request, cancel=asyncio.Event())
        assert result.error is not None and result.error.code == "INTEGRITY_FAILED"
        assert result.error.retry == "terminal"
        assert expected_error in result.error.safe_message


async def test_inline_money_is_frozen_before_staging_awaits(
    store: ReportingLedgerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared: list[dict[str, Any]] = []

    def fetch(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        shared.extend(money_rows(request))
        return InlineFetchResult(rows=shared, currency="EUR")

    source, staging = source_for(fetch)
    original_stage = staging.stage

    async def stage(**kwargs: Any) -> tuple[str, str]:
        shared[0]["currency"] = "USD"
        shared[0]["spend"] = "99.99"
        return await original_stage(**kwargs)

    monkeypatch.setattr(staging, "stage", stage)
    producer = producer_for(store, source, staging, currency="EUR")
    config = configuration(pinned_currency="EUR")
    (obligation,) = await producer.close_elapsed_periods(config)
    revision = await producer.acquire_obligation(config, obligation)
    assert revision is not None and dict(revision.control_totals)["spend"] == "0.30"
    content = await store.read_revision_rows(
        account_id="eur", reporting_revision_id=revision.reporting_revision_id
    )
    assert {row["currency"] for row in content.rows} == {"EUR"}
    assert content.rows[0]["spend"] == "0.10"
    assert shared[0]["currency"] == "USD" and shared[0]["spend"] == "99.99"


@pytest.mark.parametrize("monetary", [False, True])
async def test_control_total_units_use_trusted_semantics_instead_of_guessing_from_shape(
    store: ReportingLedgerStore,
    monetary: bool,
) -> None:
    def corrupt(payload: dict[str, Any]) -> None:
        payload["control_totals"].append(
            {
                "name": "custom_total",
                "value": "1",
                "value_type": "integer",
                "unit": "USD" if monetary else "GRP",
            }
        )

    source, staging = source_for(money_rows)
    adapter = CorruptingSource(source, corrupt)
    producer = producer_for(store, adapter, staging, currency="EUR")
    config = configuration(pinned_currency="EUR")
    if monetary:
        assert config.definition is not None
        config = replace(
            config,
            definition=replace(
                config.definition,
                monetary_control_total_units=(("spend", "EUR"), ("custom_total", "EUR")),
            ),
        )
    (obligation,) = await producer.close_elapsed_periods(config)
    if monetary:
        with pytest.raises(ReportingCurrencyError, match="CURRENCY_MISMATCH"):
            await producer.acquire_obligation(config, obligation)
    else:
        # Nonmonetary units remain valid even if they have three capital letters.
        revision = await producer.acquire_obligation(config, obligation)
        assert revision is not None and dict(revision.control_totals)["custom_total"] == "1"
        assert adapter.request is not None and adapter.result is not None
        manifest = await validate_reporting_source_execution(
            capabilities=adapter.capabilities,
            request=adapter.request.model_copy(
                update={"deadline_at": datetime.now(timezone.utc) + timedelta(hours=1)}
            ),
            result=adapter.result,
            object_reader=staging,
        )
        assert next(t for t in manifest.control_totals if t.name == "custom_total").unit == "GRP"
