"""The reviewed evidence/currency roots composed with #1167A's retained records.

Essential memory and real-PostgreSQL cases deliberately have no integration
marker. The PostgreSQL parameter skips only when ADCP_PG_TEST_URL is absent.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sys
import threading
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.conformance import validate_reporting_source_execution
from adcp.reporting.fixtures import OFFICIAL_OFFERING_ID, SNAPSHOT_OFFERING_ID
from adcp.reporting.inline_source import InlineFetchResult, InlineReportingSource, MetricEvidence
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ReportingConfiguration,
    ReportingControlTotalRecord,
    ReportingDeliveryPrincipal,
    ReportingDestinationBinding,
    ReportingMaterializationKey,
    ReportingObligationDeliveryRecord,
    ReportingObligationRecord,
    ReportingProducer,
)
from adcp.reporting.source import (
    MediaBuyConstituentV1,
    ReportingSourceCoverageRequestV1,
    ReportingSourceSliceRequestV1,
    coverage_denominator_fingerprint_v1,
)

from ._generation_support import configuration as old_configuration
from ._generation_support import obligation_for, revision_for
from ._reliable_support import (
    METRICS,
    Barrier,
    PublishedRecords,
    ReliableHarness,
    ScriptedSource,
    complete_fetch,
    configuration,
    drain_until_idle,
    publication_records,
    reliable_factory,
    verified,
)
from .test_reporting_generation_migration import _retained_rows


async def frozen_slice(
    harness: ReliableHarness,
    account: str = "eur",
    *,
    config: ReportingConfiguration | None = None,
    partial: bool = True,
) -> tuple[ReportingProducer, ReportingObligationRecord, ReportingSourceSliceRequestV1]:
    config = config or configuration(account)
    await harness.store.put_configuration(config)
    producer = harness.producer(harness.source(lambda request: None))
    (obligation,) = await producer.close_elapsed_periods(config)
    request = producer._build_slice(config, obligation, SNAPSHOT_OFFERING_ID, now=harness.clock())
    if partial:
        request = request.model_copy(
            update={"coverage": request.coverage.model_copy(update={"expected": "partial"})}
        )
    return producer, obligation, request


def sparse_fetch(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
    return InlineFetchResult(
        rows=[
            {"media_buy_id": item.media_buy_id, "impressions": 5, "clicks": 0}
            for item in request.coverage.constituents
        ],
        currency=request.currency,
        cell_availability={
            item.constituent_id: {
                "impressions": MetricEvidence.present(request.period.end),
                "clicks": MetricEvidence.explicit_zero(data_through=request.period.end),
                "spend": MetricEvidence.unavailable("billing_pending"),
            }
            for item in request.coverage.constituents
        },
    )


async def commit_records(harness: ReliableHarness, records: PublishedRecords) -> None:
    assert await harness.store.commit_materialization_attempt(records.attempt) == (
        records.attempt,
        True,
    )
    assert await harness.store.commit_materialization(records.outcome) == (records.outcome, True)
    account = records.attempt.scope.principal.account_id
    revision_id = records.attempt.reporting_revision_id
    payload = await harness.destination.read(account, revision_id)
    assert payload is not None
    await harness.receiver.write(account, revision_id, payload)
    loaded = await harness.receiver.read(account, revision_id)
    assert loaded == payload
    rows = [json.loads(line) for line in loaded.splitlines()]
    assert len(rows) == records.receipt.observed_row_count
    assert records.receipt.observed_canonical_content_digest is not None
    assert hashlib.sha256(canonical_json_utf8_v1(rows)).hexdigest() == (
        records.receipt.observed_canonical_content_digest.value
    )
    retained, created = await harness.store.record_revision_receipt(records.receipt)
    assert created and retained.received_at == harness.clock()
    assert await harness.store.record_revision_receipt(records.receipt) == (retained, False)
    if harness.blobs.pool is not None:
        async with harness.blobs.pool.connection() as connection:
            committed = await (
                await connection.execute(
                    "SELECT committed_at FROM reporting_reconciliation_changes"
                    " WHERE account_id=%s AND consumer_id=%s ORDER BY seq DESC LIMIT 1",
                    (account, records.attempt.scope.consumer_id),
                )
            ).fetchone()
        assert committed == (harness.clock(),)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_colliding_accounts_keep_sparse_metric_evidence_and_staging_isolated(
    reliable: ReliableHarness, asynchronous: bool
) -> None:
    h = reliable
    script = ScriptedSource(eur=[sparse_fetch], usd=[sparse_fetch])
    source = h.source(script.async_fetch if asynchronous else script.sync)
    results = []
    for account in ("eur", "usd"):
        producer, obligation, request = await frozen_slice(h, account)
        # Config, media buy, execution, destination and receipt IDs deliberately collide.
        request = request.model_copy(
            update={
                "identity": request.identity.model_copy(
                    update={"source_execution_key": "shared-execution"}
                )
            }
        )
        result = await source.execute(request, cancel=asyncio.Event())
        manifest = await validate_reporting_source_execution(
            capabilities=source.capabilities,
            request=request,
            result=result,
            object_reader=h.staging,
            clock=h.clock,
        )
        assert manifest.currency == obligation.currency == h.currencies[account]
        assert manifest.row_count == 1 and manifest.coverage.status == "partial"
        cells = {cell.metric: cell for cell in manifest.metric_availability}
        assert {name: cell.status for name, cell in cells.items()} == {
            "impressions": "present",
            "clicks": "explicit_zero",
            "spend": "unsupported",
        }
        assert cells["spend"].reason == "billing_pending" and cells["spend"].data_through is None
        assert [total.model_dump(exclude_none=True) for total in manifest.control_totals] == [
            {"name": "impressions", "value": "5", "value_type": "integer"},
            {"name": "clicks", "value": "0", "value_type": "integer"},
        ]
        revision = await h.commit_slice(producer, obligation, request, result)
        records = await publication_records(h, obligation, revision)
        await commit_records(h, records)
        results.append((request, result, revision, records))

    # Same staged ref AND digest: the second account must not evict the first.
    assert verified(results[0][1]).objects == verified(results[1][1]).objects
    assert verified(results[0][1]).publication_id != verified(results[1][1]).publication_id
    for request, result, revision, records in results:
        account = request.identity.account_id
        other = "usd" if account == "eur" else "eur"
        manifest = verified(result)
        obj = manifest.objects[0]
        payload = await h.staging.read(
            object_ref=obj.object_ref,
            object_generation=obj.object_generation,
            account_id=account,
            source_scope={},
            cancel=asyncio.Event(),
        )
        assert json.loads(payload) == {
            "media_buy_id": "shared-media-buy",
            "impressions": 5,
            "clicks": 0,
        }
        with pytest.raises(PermissionError):
            await h.staging.read(
                object_ref=obj.object_ref,
                object_generation=obj.object_generation,
                account_id="outsider",
                source_scope={},
                cancel=asyncio.Event(),
            )
        assert (
            await h.store.get_revision(
                account_id=other, reporting_revision_id=revision.reporting_revision_id
            )
            is None
        )
        assert await h.receiver.read(other, revision.reporting_revision_id) is None
        snapshot = await h.store.read_reconciliation_snapshot(
            caller=records.attempt.scope.principal
        )
        assert len(snapshot.current_receipts) == 1
        assert (
            snapshot.current_receipts[0].observed_control_totals == revision.managed_control_totals
        )
        assert tuple(
            item for item in snapshot.records if isinstance(item, ReportingObligationDeliveryRecord)
        ) == (records.delivery,)
    assert len(script.requests) == 2
    if not asynchronous:
        assert threading.get_ident() not in script.thread_ids


@pytest.mark.parametrize("zero", [True, False], ids=["explicit-zero", "unavailable"])
async def test_zero_spend_and_unavailable_spend_have_different_retained_totals(
    reliable: ReliableHarness, zero: bool
) -> None:
    h = reliable
    producer, obligation, request = await frozen_slice(h)
    answer = sparse_fetch(request)
    rows = [dict(row) for row in answer.rows]
    if zero:
        rows[0]["spend"] = "0.00"
    evidence = deepcopy(answer.cell_availability)
    assert evidence is not None
    if zero:
        evidence[request.coverage.constituents[0].constituent_id][
            "spend"
        ] = MetricEvidence.explicit_zero()
    answer = replace(answer, rows=rows, cell_availability=evidence)
    result = await h.source(lambda req: answer).execute(request, cancel=asyncio.Event())
    manifest = verified(result)
    totals = {total.name: total for total in manifest.control_totals}
    if zero:
        assert totals["spend"].model_dump() == {
            "name": "spend",
            "value": "0.00",
            "value_type": "decimal",
            "unit": "EUR",
        }
    else:
        assert "spend" not in totals
    assert manifest.explicit_zero is False  # These are nonempty, measured rows.
    revision = await h.commit_slice(producer, obligation, request, result)
    assert dict(revision.control_totals).get("spend") == ("0.00" if zero else None)
    await commit_records(h, await publication_records(h, obligation, revision))


async def test_one_unavailable_spend_cell_suppresses_only_its_metric_total(
    reliable: ReliableHarness,
) -> None:
    h = reliable
    _, _, request = await frozen_slice(h)
    constituents = [
        *request.coverage.constituents,
        MediaBuyConstituentV1(
            constituent_id="second",
            media_buy_id="second-buy",
            product_id=request.contract.report_definition_id,
        ),
    ]
    request = request.model_copy(
        update={
            "coverage": ReportingSourceCoverageRequestV1(
                expected="partial",
                constituents=constituents,
                denominator_fingerprint=coverage_denominator_fingerprint_v1(constituents),
            )
        }
    )
    answer = sparse_fetch(request)
    rows = [dict(row) for row in answer.rows]
    rows[0]["spend"] = "0.00"
    evidence = deepcopy(answer.cell_availability)
    assert evidence is not None
    evidence[constituents[0].constituent_id]["spend"] = MetricEvidence.explicit_zero()
    result = await h.source(
        lambda req: replace(answer, rows=rows, cell_availability=evidence)
    ).execute(request, cancel=asyncio.Event())
    manifest = verified(result)
    assert {
        (cell.constituent_id, cell.status)
        for cell in manifest.metric_availability
        if cell.metric == "spend"
    } == {(constituents[0].constituent_id, "explicit_zero"), ("second", "unsupported")}
    assert [(t.name, t.value) for t in manifest.control_totals] == [
        ("impressions", "10"),
        ("clicks", "0"),
    ]
    assert [item.status for item in manifest.coverage.constituents] == ["present", "partial"]


@pytest.mark.parametrize("mismatch", ["result", "row", "mixed", "invalid"])
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_currency_failure_with_cell_evidence_precedes_staging_and_sealing(
    reliable: ReliableHarness, mismatch: str, asynchronous: bool
) -> None:
    h = reliable
    _, obligation, request = await frozen_slice(h)
    answer = complete_fetch(request)
    rows = [dict(row) for row in answer.rows]
    if mismatch == "result":
        answer = replace(answer, currency="USD")
    elif mismatch == "invalid":
        answer = replace(answer, currency="eur")
    else:
        rows[0]["currency"] = "USD"
        if mismatch == "row":
            rows[1]["currency"] = "USD"
        answer = replace(answer, rows=rows)
    script = ScriptedSource(eur=[answer])
    result = await h.source(script.async_fetch if asynchronous else script.sync).execute(
        request, cancel=asyncio.Event()
    )
    assert result.error is not None
    assert result.error.code == "INTEGRITY_FAILED" and result.error.retry == "terminal"
    assert "stage.before" not in h.failures.hits and "seal.before" not in h.failures.hits
    assert (
        await h.seals.get(
            account_id="eur", source_execution_key=request.identity.source_execution_key
        )
        is None
    )
    assert (
        await h.store.list_revisions(
            account_id="eur", reporting_obligation_id=obligation.reporting_obligation_id
        )
        == ()
    )


async def test_currency_validation_and_cell_evidence_share_the_immutable_row_snapshot(
    reliable: ReliableHarness,
) -> None:
    h = reliable
    producer, obligation, request = await frozen_slice(h, partial=False)
    answer = complete_fetch(request)
    rows = [dict(row, annotation={"labels": ["original"]}) for row in answer.rows]
    evidence = deepcopy(answer.cell_availability)
    original_rows = deepcopy(rows)
    barrier = Barrier()
    h.failures.at("stage.before", barrier)
    task = asyncio.create_task(
        h.source(lambda req: replace(answer, rows=rows, cell_availability=evidence)).execute(
            request, cancel=asyncio.Event()
        )
    )
    await barrier.wait()
    rows[0].update(currency="USD", spend="999.00", impressions=999)
    rows[0]["annotation"]["labels"].append("mutated")
    assert evidence is not None
    evidence[request.coverage.constituents[0].constituent_id]["spend"] = MetricEvidence.unavailable(
        "changed"
    )
    barrier.release()
    result = await asyncio.wait_for(task, timeout=10)
    manifest = verified(result)
    assert manifest.currency == "EUR"
    assert [(t.name, t.value, t.unit) for t in manifest.control_totals] == [
        ("impressions", "10", None),
        ("clicks", "0", None),
        ("spend", "0.30", "EUR"),
    ]
    assert (
        next(cell for cell in manifest.metric_availability if cell.metric == "spend").status
        == "present"
    )
    revision = await h.commit_slice(producer, obligation, request, result)
    page = await h.store.read_revision_rows(
        account_id="eur", reporting_revision_id=revision.reporting_revision_id
    )
    assert list(page.rows) == original_rows


@pytest.mark.parametrize(
    "evidence_error", [False, True], ids=["provider-value-error", "cell-evidence-error"]
)
async def test_adapter_value_error_classification_survives_currency_composition(
    reliable: ReliableHarness, evidence_error: bool
) -> None:
    h = reliable
    _, _, request = await frozen_slice(h)

    def fetch(req: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        if evidence_error:
            return replace(
                sparse_fetch(req),
                cell_availability={
                    req.coverage.constituents[0].constituent_id: {
                        "spend": MetricEvidence.unavailable("")
                    }
                },
            )
        raise ValueError("provider private diagnostic")

    source = h.source(fetch)
    if evidence_error:
        with pytest.raises(ValueError, match="reason"):
            await source.execute(request, cancel=asyncio.Event())
    else:
        result = await source.execute(request, cancel=asyncio.Event())
        assert result.error is not None and result.error.code == "PROVIDER_TRANSIENT"
        assert result.error.retry == "retryable"
        assert "private diagnostic" not in result.error.safe_message
    assert "stage.before" not in h.failures.hits


async def test_same_configuration_id_runs_worker_to_revision_to_exact_records_without_leakage(
    reliable: ReliableHarness,
) -> None:
    h = reliable
    script = ScriptedSource(eur=[complete_fetch], usd=[complete_fetch])
    source = h.source(script.async_fetch)
    producer = h.producer(source)
    for account in ("eur", "usd"):
        await h.store.put_configuration(configuration(account))
    turns = await drain_until_idle(producer, h.clock, idle_turns=2)
    assert sum(len(turn.obligations_committed) for turn in turns) == 2
    assert sum(len(turn.revisions_committed) for turn in turns) == 2
    assert not any(turn.slices_failed for turn in turns)
    assert {req.currency for req in script.requests} == {"EUR", "USD"}
    for request in script.requests:
        account = request.identity.account_id
        obligation = await h.store.get_obligation(
            account_id=account, reporting_obligation_id=request.identity.reporting_obligation_id
        )
        assert obligation is not None
        (revision,) = await h.store.list_revisions(
            account_id=account, reporting_obligation_id=obligation.reporting_obligation_id
        )
        assert revision.row_count == 2
        assert revision.managed_control_totals == (
            ReportingControlTotalRecord("impressions", "10", "integer"),
            ReportingControlTotalRecord("clicks", "0", "integer"),
            ReportingControlTotalRecord("spend", "0.30", "decimal", request.currency),
        )
        records = await publication_records(h, obligation, revision)
        await h.store.commit_materialization_attempt(records.attempt)
        await h.store.commit_materialization(records.outcome)
        before = await h.store.read_reconciliation_snapshot(caller=records.attempt.scope.principal)
        for wrong_total in (
            replace(revision.managed_control_totals[-1], unit="USD" if account == "eur" else "EUR"),
            replace(revision.managed_control_totals[-1], value="0.300"),
        ):
            with pytest.raises(LedgerConflictError):
                await h.store.record_revision_receipt(
                    replace(
                        records.receipt,
                        observed_control_totals=(
                            *revision.managed_control_totals[:-1],
                            wrong_total,
                        ),
                    )
                )
        with pytest.raises(LedgerConflictError):
            await h.store.record_revision_receipt(replace(records.receipt, observed_row_count=1))
        assert (
            await h.store.read_reconciliation_snapshot(caller=records.attempt.scope.principal)
            == before
        )
        accepted, created = await h.store.record_revision_receipt(records.receipt)
        assert created and accepted.observed_control_totals[-1].unit == request.currency
        snapshot = await h.store.read_reconciliation_snapshot(
            caller=ReportingDeliveryPrincipal(account, "buyer")
        )
        assert {item.scope.principal.account_id for item in snapshot.current_receipts} == {account}
        assert snapshot.terminal_acceptances == (accepted.key,)
        assert tuple(
            item for item in snapshot.records if isinstance(item, ReportingDestinationBinding)
        ) == (records.binding,)


@pytest.mark.parametrize("failure_point", ["seal.after", "revision.before"])
async def test_retry_and_store_restart_preserve_original_currency_and_exact_evidence(
    reliable: ReliableHarness, failure_point: str
) -> None:
    h = reliable
    script = ScriptedSource(eur=[complete_fetch])
    await h.store.put_configuration(configuration("eur"))
    h.failures.at(failure_point, OSError("simulated process loss"))
    with pytest.raises(OSError, match="process loss"):
        await h.producer(h.source(script.async_fetch)).run_worker()
    (request,) = script.requests
    first = await h.seals.get(
        account_id="eur", source_execution_key=request.identity.source_execution_key
    )
    assert first is not None
    h.currencies["eur"] = "GBP"
    h.clock.advance(timedelta(hours=1))
    await h.restart()

    def changed(req: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        return replace(sparse_fetch(req), currency="USD")

    replacement = ScriptedSource(eur=[changed])
    source = h.source(replacement.sync)
    turns = await drain_until_idle(h.producer(source), h.clock)
    assert sum(len(turn.revisions_committed) for turn in turns) == 1
    assert replacement.requests == []
    cancel = asyncio.Event()
    cancel.set()
    replay = await source.execute(request, cancel=cancel)
    assert replay.manifest_bytes == first.manifest_bytes
    assert replay.response is not None and replay.response.manifest == first.reference
    manifest = verified(replay)
    assert manifest.currency == "EUR" and manifest.row_count == 2
    assert [(t.name, t.value, t.unit) for t in manifest.control_totals][-1] == (
        "spend",
        "0.30",
        "EUR",
    )
    obligation = await h.store.get_obligation(
        account_id="eur", reporting_obligation_id=request.identity.reporting_obligation_id
    )
    assert obligation is not None and obligation.currency == "EUR"
    (revision,) = await h.store.list_revisions(
        account_id="eur", reporting_obligation_id=obligation.reporting_obligation_id
    )
    records = await publication_records(h, obligation, revision)
    await commit_records(h, records)
    snapshot = await h.store.read_reconciliation_snapshot(caller=records.attempt.scope.principal)
    rows = await h.store.read_revision_rows(
        account_id="eur", reporting_revision_id=revision.reporting_revision_id
    )
    await h.restart()
    assert (
        await h.store.get_revision(
            account_id="eur", reporting_revision_id=revision.reporting_revision_id
        )
        == revision
    )
    assert (
        await h.store.read_revision_rows(
            account_id="eur", reporting_revision_id=revision.reporting_revision_id
        )
        == rows
    )
    assert (
        await h.store.read_reconciliation_snapshot(caller=records.attempt.scope.principal)
        == snapshot
    )
    assert (
        await h.seals.get(
            account_id="eur", source_execution_key=request.identity.source_execution_key
        )
        == first
    )
    assert (
        await h.source(replacement.sync).execute(request, cancel=cancel)
    ).manifest_bytes == first.manifest_bytes
    assert replacement.requests == []


async def test_zero_row_snapshot_and_official_publications_coexist_in_the_record_model(
    reliable: ReliableHarness,
) -> None:
    h = reliable

    def zero(req: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        return InlineFetchResult(
            rows=[],
            currency=req.currency,
            cell_availability={
                item.constituent_id: {metric: MetricEvidence.explicit_zero() for metric in METRICS}
                for item in req.coverage.constituents
            },
        )

    script = ScriptedSource(eur=[zero, complete_fetch])
    source = h.source(script.async_fetch)
    producer = h.producer(source)
    config = configuration("eur")
    await h.store.put_configuration(config)
    await drain_until_idle(producer, h.clock)
    request = script.requests[0]
    obligation = await h.store.get_obligation(
        account_id="eur", reporting_obligation_id=request.identity.reporting_obligation_id
    )
    assert obligation is not None
    (snapshot_revision,) = await h.store.list_revisions(
        account_id="eur", reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert snapshot_revision.row_count == 0
    assert snapshot_revision.managed_control_totals[-1] == ReportingControlTotalRecord(
        "spend", "0", "integer", "EUR"
    )
    snapshot_records = await publication_records(
        h, obligation, snapshot_revision, suffix="snapshot"
    )
    await commit_records(h, snapshot_records)
    assert await h.destination.read("eur", snapshot_revision.reporting_revision_id) == b""
    h.clock.advance(timedelta(hours=1))
    official_request = producer._build_slice(
        config,
        replace(obligation, required_finality="official"),
        OFFICIAL_OFFERING_ID,
        now=h.clock(),
    )
    result = await source.execute(official_request, cancel=asyncio.Event())
    official = await h.commit_slice(
        producer, obligation, official_request, result, finality="official"
    )
    assert official.finality == "official" and official.supersedes_reporting_revision_id is None
    official_records = await publication_records(h, obligation, official, suffix="official")
    await commit_records(h, official_records)
    await h.restart()
    retained = await h.store.read_reconciliation_snapshot(
        caller=snapshot_records.attempt.scope.principal
    )
    revisions = await h.store.list_revisions(
        account_id="eur", reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert {revision.finality for revision in revisions} == {"snapshot", "official"}
    assert len(retained.terminal_acceptances) == 2
    for records in (snapshot_records, official_records):
        view = await h.store.get_materialization(
            ReportingMaterializationKey(
                records.attempt.scope.principal, records.attempt.reporting_materialization_id
            )
        )
        assert view is not None and view.attempt.attempt == 1 and view.outcome == records.outcome


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_populated_ledger_upgrade_then_granular_publication_and_exact_reconciliation(
    backend: str,
) -> None:
    async with reliable_factory(backend, initialize=False) as h:
        if h.blobs.pool is not None:
            fixture = Path(__file__).resolve().parents[2] / "fixtures"
            async with h.blobs.pool.connection() as connection:
                await connection.execute((fixture / "reporting_ledger_beta15.sql").read_text())
                await connection.execute((fixture / "reporting_ledger_beta15_data.sql").read_text())
            before = await _retained_rows(h.blobs.pool)
            await h.store.create_schema()
            assert await _retained_rows(h.blobs.pool) == before
            legacy_id = "rpr_acct_a_official"
        else:
            # Explicit image of an old Core memory store: unknown currency stays
            # unknown when its retained data is attached to the additive store.
            old = InMemoryReportingLedgerStore(clock=h.clock)
            config = replace(old_configuration(), required_finality="official")
            await old.put_configuration(config)
            obligation = obligation_for(config)
            await old.commit_obligation(obligation)
            revision, rows = revision_for(obligation)
            revision = replace(
                revision,
                finality="official",
                finality_basis="source_final",
                finality_policy_id="legacy-policy",
                finalized_at=obligation.period.end,
            )
            await old.commit_revision(revision, rows)
            old._obligations[obligation.reporting_obligation_id] = replace(
                obligation, currency=None
            )
            vars(h.store).update(
                deepcopy(
                    {
                        key: value
                        for key, value in vars(old).items()
                        if key not in {"_lock", "_clock"}
                    }
                )
            )
            legacy_id = revision.reporting_revision_id
        legacy = await h.store.get_revision(account_id="acct_a", reporting_revision_id=legacy_id)
        assert legacy is not None and legacy.managed_control_totals is None
        legacy_rows = await h.store.read_revision_rows(
            account_id="acct_a", reporting_revision_id=legacy_id
        )
        h.currencies.update(acct_a="EUR")
        for account in ("acct_a", "usd"):
            config = replace(
                configuration(account), delivery_config_id="daily", delivery_config_version=2
            )
            producer, obligation, request = await frozen_slice(h, account, config=config)
            result = await h.source(sparse_fetch).execute(request, cancel=asyncio.Event())
            revision = await h.commit_slice(producer, obligation, request, result)
            assert revision.managed_control_totals is not None
            assert {total.name for total in revision.managed_control_totals} == {
                "impressions",
                "clicks",
            }
            records = await publication_records(h, obligation, revision)
            await commit_records(h, records)
            retained = await h.store.read_reconciliation_snapshot(
                caller=records.attempt.scope.principal
            )
            assert tuple(
                item
                for item in retained.records
                if isinstance(item, ReportingObligationDeliveryRecord)
            ) == (records.delivery,)
            assert records.delivery.currency == h.currencies[account]
            assert all(
                item.scope.principal.account_id == account for item in retained.current_receipts
            )
        await h.restart()
        assert (
            await h.store.get_revision(account_id="acct_a", reporting_revision_id=legacy_id)
            == legacy
        )
        assert (
            await h.store.read_revision_rows(account_id="acct_a", reporting_revision_id=legacy_id)
            == legacy_rows
        )
        assert await h.store.get_revision(account_id="usd", reporting_revision_id=legacy_id) is None
        legacy_obligation = await h.store.get_obligation(
            account_id="acct_a", reporting_obligation_id=legacy.reporting_obligation_id
        )
        assert legacy_obligation is not None and legacy_obligation.currency is None
        for account in ("acct_a", "usd"):
            retained = await h.store.read_reconciliation_snapshot(
                caller=ReportingDeliveryPrincipal(account, "buyer")
            )
            assert len(retained.current_receipts) == 1
            (delivery,) = tuple(
                item
                for item in retained.records
                if isinstance(item, ReportingObligationDeliveryRecord)
            )
            assert delivery.currency == h.currencies[account]


async def test_postgres_fresh_process_reads_exact_seals_currency_rows_and_receipts() -> None:
    async with reliable_factory("postgres") as h:
        source = h.source(complete_fetch)
        await h.store.put_configuration(configuration("eur"))
        await drain_until_idle(h.producer(source), h.clock)
        (manifest,) = h.manifests.values()
        request = h.producer(source)._build_slice(
            configuration("eur"),
            await h.store.get_obligation(
                account_id="eur", reporting_obligation_id=manifest.identity.reporting_obligation_id
            ),
            SNAPSHOT_OFFERING_ID,
            now=h.clock(),
        )
        obligation = await h.store.get_obligation(
            account_id="eur", reporting_obligation_id=request.identity.reporting_obligation_id
        )
        assert obligation is not None
        (revision,) = await h.store.list_revisions(
            account_id="eur", reporting_obligation_id=obligation.reporting_obligation_id
        )
        records = await publication_records(h, obligation, revision)
        await commit_records(h, records)
        seal = await h.seals.get(
            account_id="eur", source_execution_key=request.identity.source_execution_key
        )
        assert seal is not None and h.blobs.pool is not None
        data = {
            "url": h.blobs.pool.conninfo,
            "kwargs": h.blobs.pool.kwargs,
            "now": h.clock().isoformat(),
            "request": request.model_dump(mode="json"),
            "revision_id": revision.reporting_revision_id,
            "receipt": records.receipt.reporting_receipt_id,
        }
        await h.blobs.pool.close()
        code = """
import asyncio
import json
import sys
from datetime import datetime
from psycopg_pool import AsyncConnectionPool
from adcp.reporting.inline_source import InlineFetchResult, MetricEvidence
from adcp.reporting.ledger import (
    PgReportingReconciliationStore, ReportingDeliveryPrincipal, ReportingReceiptKey,
)
from adcp.reporting.source import ReportingSourceSliceRequestV1
from tests.conformance.reporting._reliable_support import (
    ManualClock, ReliableHarness, _BytesStore, verified,
)

async def main(data):
    async with AsyncConnectionPool(data['url'], kwargs=data['kwargs'], open=False) as pool:
        clock = ManualClock(datetime.fromisoformat(data['now']))
        h = ReliableHarness(
            PgReportingReconciliationStore(pool=pool, clock=clock), clock, _BytesStore(pool)
        )
        request = ReportingSourceSliceRequestV1.model_validate(data['request'])
        calls = []
        def changed(req):
            calls.append(req.identity.account_id)
            return InlineFetchResult(rows=[], currency='USD', cell_availability={
                item.constituent_id: {
                    metric: MetricEvidence.unavailable('changed')
                    for metric in req.requested_metrics
                } for item in req.coverage.constituents
            })
        cancel = asyncio.Event()
        result = await h.source(changed).execute(request, cancel=cancel)
        cancel.set()
        assert await h.source(changed).execute(request, cancel=cancel) == result
        manifest = verified(result)
        revision = await h.store.get_revision(
            account_id='eur', reporting_revision_id=data['revision_id']
        )
        rows = await h.store.read_revision_rows(
            account_id='eur', reporting_revision_id=data['revision_id']
        )
        receipt = await h.store.get_receipt(
            ReportingReceiptKey(ReportingDeliveryPrincipal('eur','buyer'), data['receipt'])
        )
        print(json.dumps({'manifest': result.manifest_bytes.decode(), 'currency': manifest.currency,
            'evidence': [cell.model_dump(mode='json') for cell in manifest.metric_availability],
            'totals': [total.to_wire() for total in revision.managed_control_totals],
            'rows': rows.rows,
            'receipt_totals': [total.to_wire() for total in receipt.observed_control_totals],
            'revision_hash': revision.revision_content_sha256, 'calls': calls}))
asyncio.run(main(json.load(sys.stdin)))
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(data).encode()), timeout=30
            )
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        assert result["calls"] == [] and result["currency"] == "EUR"
        assert result["manifest"].encode() == seal.manifest_bytes
        assert result["evidence"] == [
            cell.model_dump(mode="json") for cell in manifest.metric_availability
        ]
        assert (
            result["totals"]
            == result["receipt_totals"]
            == [total.to_wire() for total in revision.managed_control_totals]
        )
        assert result["revision_hash"] == revision.revision_content_sha256
        assert {row["currency"] for row in result["rows"]} == {"EUR"}


def test_inline_result_retains_six_positional_arguments_and_both_additions_are_keyword_only() -> (
    None
):
    parameters = inspect.signature(InlineFetchResult).parameters
    assert [
        name
        for name, parameter in parameters.items()
        if parameter.kind == parameter.POSITIONAL_OR_KEYWORD
    ] == [
        "rows",
        "data_through",
        "covered_constituent_ids",
        "unavailable_constituents",
        "unavailable_status",
        "warnings",
    ]
    assert (
        parameters["currency"].kind
        == parameters["cell_availability"].kind
        == inspect.Parameter.KEYWORD_ONLY
    )
    with pytest.raises(TypeError):
        InlineFetchResult([], None, None, {}, "unsupported", (), "EUR")


@pytest.mark.parametrize("currency", ["EUR", "USD"])
async def test_no_opt_in_keeps_manifest_and_control_total_bytes_compatible(currency: str) -> None:
    async with reliable_factory("memory") as h:
        h.currencies["eur"] = currency
        _, _, request = await frozen_slice(h)
        answer = complete_fetch(request)
        # Separate executors/seal stores ensure replay cannot conceal a byte change.
        results = []
        for declared_currency, evidence in (
            (None, None),
            (currency, None),
            (currency, {}),
            (currency, {request.coverage.constituents[0].constituent_id: {}}),
        ):
            legacy = InlineFetchResult(answer.rows, request.period.end, None, {}, "unsupported", ())
            result = replace(legacy, currency=declared_currency, cell_availability=evidence)
            source = InlineReportingSource(
                capabilities=h.source(complete_fetch).capabilities,
                fetch=lambda req: result,
                clock=h.clock,
            )
            results.append(await source.execute(request, cancel=asyncio.Event()))
        assert {result.manifest_bytes for result in results} == {results[0].manifest_bytes}
        manifest = verified(results[0])
        assert canonical_json_utf8_v1(
            [total.model_dump(exclude_none=True) for total in manifest.control_totals]
        ) == (
            b'[{"name":"impressions","value":"10","value_type":"integer"},'
            b'{"name":"clicks","value":"0","value_type":"integer"},'
            b'{"name":"spend","unit":"'
            + currency.encode()
            + b'","value":"0.30","value_type":"decimal"}]'
        )
