"""Bypass Python transition checks: PostgreSQL must enforce the complete evidence graph."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import pytest

from adcp.reporting.ledger import (
    LedgerConflictError,
    PgReportingReconciliationStore,
    ReportingAdjustmentReceiptRecord,
    ReportingAdjustmentRecord,
    ReportingControlTotalRecord,
    ReportingDeliveryRecord,
    ReportingMaterializationCheck,
    ReportingReconciliationFilter,
    ReportingRevisionReceiptRecord,
    adjustment_to_wire,
    revision_content_sha256,
)
from adcp.reporting.ledger._delivery_state import change_id, fingerprint, payload, storage_identity
from adcp.reporting.ledger.delivery_pg import _IDENTITY_COLUMNS

from ._generation_support import END, NOW, configuration, isolated_reporting_pool
from ._reconciliation_support import Scenario, scenario


async def raw_insert(
    pool: Any,
    record: ReportingDeliveryRecord,
    *,
    write_change: bool = True,
    legacy: bool = False,
    **overrides: Any,
) -> None:
    """Literal INSERTs; never call a store validator, _insert, or receipt-head operation."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    if isinstance(record, (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord)):
        record = replace(record, received_at=NOW)
    row = dict(zip(_IDENTITY_COLUMNS, storage_identity(record)))
    row.update(
        payload=Jsonb(payload(record)),
        content_sha256=fingerprint(record),
        change_id=change_id(record),
    )
    row.update(overrides)
    async with pool.connection() as connection, connection.transaction():
        await connection.execute(
            sql.SQL("INSERT INTO reporting_reconciliation_records ({}) VALUES ({})").format(
                sql.SQL(", ").join(map(sql.Identifier, row)),
                sql.SQL(", ").join(sql.Placeholder() for _ in row),
            ),
            tuple(row.values()),
        )
        if write_change and legacy:
            await connection.execute(
                "INSERT INTO reporting_ledger_changes (account_id, record_kind, record_id)"
                " VALUES (%s, %s, %s)",
                (row["account_id"], row["record_kind"], row["change_id"]),
            )
        elif write_change:
            sequence = await (
                await connection.execute(
                    "INSERT INTO reporting_reconciliation_heads"
                    " (account_id, consumer_id, max_sequence)"
                    " VALUES (%s, %s, 1) ON CONFLICT (account_id, consumer_id) DO UPDATE"
                    " SET max_sequence = reporting_reconciliation_heads.max_sequence + 1"
                    " RETURNING max_sequence",
                    (row["account_id"], row["consumer_id"]),
                )
            ).fetchone()
            await connection.execute(
                "INSERT INTO reporting_reconciliation_changes"
                " (account_id, consumer_id, seq, namespace, record_id, record_kind,"
                " change_id, content_sha256)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row["account_id"],
                    row["consumer_id"],
                    sequence[0],
                    row["namespace"],
                    row["record_id"],
                    row["record_kind"],
                    row["change_id"],
                    row["content_sha256"],
                ),
            )


@dataclass
class Graph:
    pool: Any
    store: PgReportingReconciliationStore
    first: Scenario
    second: Scenario
    adjustment: ReportingAdjustmentReceiptRecord
    other_adjustment: ReportingAdjustmentReceiptRecord


@pytest.fixture
async def graph():
    async with isolated_reporting_pool() as pool:
        store = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        s = await scenario(store)
        await store.commit_materialization(s.outcome)
        config = replace(
            configuration(),
            delivery_config_id="second-config",
            feed_purpose="billing",
            required_finality="official",
        )
        await store.put_configuration(config)
        obligation = replace(
            s.obligation,
            reporting_obligation_id="second-obligation",
            delivery_config_id=config.delivery_config_id,
        )
        await store.commit_obligation(obligation)
        binding = replace(s.binding, generation_key=config.generation_key)
        await store.put_destination_binding(binding)
        scope = replace(
            s.attempt.scope,
            generation_key=config.generation_key,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        delivery = replace(s.delivery, scope=scope)
        await store.bind_obligation_delivery(delivery)
        rows = (
            await store.read_revision_rows(
                account_id="acct_a", reporting_revision_id=s.revision.reporting_revision_id
            )
        ).rows
        revision = replace(
            s.revision,
            reporting_revision_id="second-revision",
            reporting_obligation_id=obligation.reporting_obligation_id,
            revision_content_sha256=revision_content_sha256(
                reporting_revision_id="second-revision",
                row_count=s.revision.row_count,
                control_totals=s.revision.control_totals,
                reporting_rows=rows,
                control_total_evidence=s.revision.managed_control_totals,
            ),
        )
        await store.commit_revision(revision, rows)
        attempt = replace(
            s.attempt,
            scope=scope,
            reporting_revision_id=revision.reporting_revision_id,
            reporting_materialization_id="second-materialization",
        )
        await store.commit_materialization_attempt(attempt)
        outcome = replace(
            s.outcome,
            scope=scope,
            reporting_revision_id=revision.reporting_revision_id,
            reporting_materialization_id=attempt.reporting_materialization_id,
        )
        await store.commit_materialization(outcome)
        receipt = replace(
            s.receipt,
            scope=scope,
            reporting_revision_id=revision.reporting_revision_id,
            reporting_materialization_id=attempt.reporting_materialization_id,
            reporting_receipt_id="second-receipt-0001",
        )
        second = Scenario(binding, delivery, obligation, revision, attempt, outcome, receipt)
        # This principal has a real binding/delivery but no attempt or outcome.
        await store.put_destination_binding(replace(s.binding, consumer_id="other-buyer"))
        await store.bind_obligation_delivery(
            replace(s.delivery, scope=replace(s.delivery.scope, consumer_id="other-buyer"))
        )
        await store.commit_materialization_attempt(
            replace(s.attempt, reporting_materialization_id="pending-attempt", attempt=2)
        )
        adjustments = []
        for index, candidate in enumerate((s, second)):
            adjustment = ReportingAdjustmentRecord(
                f"adjustment-{index}",
                "acct_a",
                candidate.revision.reporting_revision_id,
                "source_correction",
                END,
                END + timedelta(days=30),
                (("spend", "-1"),),
                END + timedelta(seconds=5),
                END + timedelta(seconds=6),
                managed_control_total_deltas=(
                    ReportingControlTotalRecord("spend", "-1", "decimal", "EUR"),
                ),
            )
            await store.commit_adjustment(adjustment)
            adjustments.append(
                ReportingAdjustmentReceiptRecord(
                    candidate.attempt.scope,
                    f"adjustment-receipt-{index}",
                    adjustment.reporting_adjustment_id,
                    candidate.revision.reporting_revision_id,
                    "accepted",
                    adjustment_to_wire(adjustment)["canonical_adjustment_sha256"],
                    END + timedelta(seconds=7),
                )
            )
        yield Graph(pool, store, s, second, *adjustments)


@pytest.mark.parametrize(
    "edge",
    [
        "revision_obligation",
        "obligation_generation",
        "outcome_attempt",
        "outcome_revision",
        "outcome_principal",
        "check_materialization",
        "check_principal",
        "receipt_materialization",
        "receipt_revision",
        "receipt_principal",
        "adjustment_revision",
        "adjustment_obligation",
        "receipt_predecessor_chain",
        "accepted_predecessor",
        "competing_root",
        "rejected_fork",
    ],
)
async def test_sql_rejects_inexact_graph_edges(graph: Graph, edge: str) -> None:
    from psycopg import IntegrityError

    s, t, store = graph.first, graph.second, graph.store
    candidate: ReportingDeliveryRecord = replace(
        s.attempt, reporting_materialization_id="raw-attempt", attempt=3
    )
    if edge == "revision_obligation":
        candidate = replace(candidate, scope=t.attempt.scope)
    if edge == "obligation_generation":
        candidate = replace(
            candidate, scope=replace(s.attempt.scope, generation_key=t.binding.generation_key)
        )
    if edge.startswith("outcome_"):
        candidate = replace(s.outcome, reporting_materialization_id="pending-attempt")
        if edge == "outcome_attempt":
            candidate = replace(candidate, reporting_materialization_id="no-attempt")
        if edge == "outcome_revision":
            candidate = replace(candidate, reporting_revision_id=t.revision.reporting_revision_id)
        if edge == "outcome_principal":
            candidate = replace(
                candidate, scope=replace(candidate.scope, consumer_id="other-buyer")
            )
    if edge.startswith("check_"):
        candidate = ReportingMaterializationCheck(
            s.attempt.scope, s.attempt.reporting_materialization_id, "sql-check", "readable", NOW
        )
        if edge == "check_materialization":
            candidate = replace(
                candidate, reporting_materialization_id=t.attempt.reporting_materialization_id
            )
        if edge == "check_principal":
            candidate = replace(
                candidate, scope=replace(candidate.scope, consumer_id="other-buyer")
            )
    if edge in {"receipt_materialization", "receipt_revision", "receipt_principal"}:
        candidate = s.receipt
        if edge == "receipt_materialization":
            candidate = replace(
                candidate, reporting_materialization_id=t.attempt.reporting_materialization_id
            )
        if edge == "receipt_revision":
            candidate = replace(
                candidate,
                reporting_revision_id=t.revision.reporting_revision_id,
                scope=t.attempt.scope,
            )
        if edge == "receipt_principal":
            candidate = replace(
                candidate, scope=replace(candidate.scope, consumer_id="other-buyer")
            )
    if edge == "adjustment_revision":
        candidate = replace(
            graph.adjustment, reporting_adjustment_id=graph.other_adjustment.reporting_adjustment_id
        )
    if edge == "adjustment_obligation":
        candidate = replace(graph.adjustment, scope=t.attempt.scope)
    if edge in {
        "receipt_predecessor_chain",
        "accepted_predecessor",
        "competing_root",
        "rejected_fork",
    }:
        predecessor = (
            s.receipt
            if edge == "accepted_predecessor"
            else replace(s.receipt, status="rejected", rejection_codes=("LOAD_FAILED",))
        )
        await raw_insert(graph.pool, predecessor)
        candidate = replace(
            t.receipt if edge == "receipt_predecessor_chain" else s.receipt,
            reporting_receipt_id="raw-successor-0001",
            supersedes_reporting_receipt_id=predecessor.reporting_receipt_id,
        )
        if edge == "competing_root":
            candidate = replace(candidate, supersedes_reporting_receipt_id=None)
        if edge == "rejected_fork":
            await raw_insert(
                graph.pool,
                replace(
                    predecessor,
                    reporting_receipt_id="already-replaced-receipt",
                    supersedes_reporting_receipt_id=predecessor.reporting_receipt_id,
                ),
            )
    before = await store.read_reconciliation_snapshot(caller=s.binding.principal)
    with pytest.raises(IntegrityError):
        await raw_insert(graph.pool, candidate)
    assert await store.read_reconciliation_snapshot(caller=s.binding.principal) == before


_COLUMN_MUTATIONS = [
    ("account_id", "other-account"),
    ("consumer_id", "other-buyer"),
    ("namespace", "wrong-namespace"),
    ("record_id", "wrong-record"),
    ("record_kind", "materialization"),
    ("delivery_config_id", "second-config"),
    ("delivery_config_version", 2),
    ("reporting_obligation_id", "second-obligation"),
    ("reporting_revision_id", "second-revision"),
    ("reporting_materialization_id", "second-materialization"),
    ("reporting_adjustment_id", "adjustment-1"),
    ("attempt_number", 99),
    ("receipt_chain_key", "f" * 64),
    ("receipt_status", "rejected"),
    ("supersedes_receipt_id", "nonexistent-receipt"),
    ("change_id", "e" * 64),
]


@pytest.mark.parametrize("column,value", _COLUMN_MUTATIONS)
async def test_sql_rejects_denormalized_payload_identity_disagreement(
    graph: Graph, column: str, value: Any
) -> None:
    from psycopg import IntegrityError

    candidate: ReportingDeliveryRecord = graph.first.receipt
    if column == "attempt_number":
        candidate = replace(
            graph.first.attempt, reporting_materialization_id="raw-attempt", attempt=3
        )
    if column == "reporting_adjustment_id":
        candidate = graph.adjustment
    with pytest.raises(IntegrityError):
        await raw_insert(graph.pool, candidate, **{column: value})


@pytest.mark.parametrize("column,value", _COLUMN_MUTATIONS)
async def test_records_fail_closed_after_owner_bypasses_database_guards(
    graph: Graph, column: str, value: Any
) -> None:
    from psycopg import sql

    candidate: ReportingDeliveryRecord = graph.first.receipt
    if column == "attempt_number":
        candidate = replace(
            graph.first.attempt, reporting_materialization_id="raw-attempt", attempt=3
        )
    if column == "reporting_adjustment_id":
        candidate = graph.adjustment
    await raw_insert(graph.pool, candidate)
    async with graph.pool.connection() as connection:
        # A database-owner damage probe. Ordinary SQL above cannot do this.
        await connection.execute("ALTER TABLE reporting_reconciliation_records DISABLE TRIGGER ALL")
        constraints = await (
            await connection.execute(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = 'reporting_reconciliation_records'::regclass AND contype = 'c'"
            )
        ).fetchall()
        for (name,) in constraints:
            await connection.execute(
                sql.SQL("ALTER TABLE reporting_reconciliation_records DROP CONSTRAINT {}").format(
                    sql.Identifier(name)
                )
            )
        await connection.execute(
            sql.SQL(
                "UPDATE reporting_reconciliation_records SET {} = %s WHERE change_id = %s"
            ).format(sql.Identifier(column)),
            (value, change_id(candidate)),
        )
        await connection.execute("ALTER TABLE reporting_reconciliation_records ENABLE TRIGGER ALL")
    caller = graph.first.binding.principal
    if column == "account_id":
        caller = replace(caller, account_id=value)
    if column == "consumer_id":
        caller = replace(caller, consumer_id=value)
    for read in [
        graph.store.read_reconciliation_snapshot(caller=caller),
        graph.store.read_reconciliation_changes(caller=caller),
    ]:
        with pytest.raises(LedgerConflictError) as error:
            await read
        assert error.value.code == "REPORTING_HISTORY_CORRUPT"
        assert error.value.__cause__ is None and error.value.__context__ is None


@pytest.mark.parametrize("has_predecessor", [False, True])
@pytest.mark.parametrize("status", ["accepted", "rejected"])
async def test_direct_sql_receipt_races_have_one_current_leaf(
    graph: Graph, has_predecessor: bool, status: str
) -> None:
    from psycopg import IntegrityError

    s = graph.first
    predecessor = None
    if has_predecessor:
        predecessor = s.receipt.reporting_receipt_id
        await raw_insert(
            graph.pool, replace(s.receipt, status="rejected", rejection_codes=("LOAD_FAILED",))
        )
    results = await asyncio.gather(
        *(
            raw_insert(
                graph.pool,
                replace(
                    s.receipt,
                    reporting_receipt_id=f"direct-sql-race-{i:04}",
                    status=status,
                    rejection_codes=("LOAD_FAILED",) if status == "rejected" else (),
                    supersedes_reporting_receipt_id=predecessor,
                ),
            )
            for i in range(8)
        ),
        return_exceptions=True,
    )
    assert sum(item is None for item in results) == 1
    assert sum(isinstance(item, IntegrityError) for item in results) == 7
    snapshot = await graph.store.read_reconciliation_snapshot(caller=s.binding.principal)
    assert len(snapshot.current_receipts) == 1
    assert snapshot.current_receipts[0].status == status


async def test_sql_cannot_commit_evidence_without_its_change(graph: Graph) -> None:
    from psycopg import IntegrityError

    before = await graph.store.read_reconciliation_snapshot(caller=graph.first.binding.principal)
    with pytest.raises(IntegrityError):
        await raw_insert(graph.pool, graph.first.receipt, write_change=False)
    assert (
        await graph.store.read_reconciliation_snapshot(caller=graph.first.binding.principal)
        == before
    )
    async with graph.pool.connection() as connection:
        assert not await (
            await connection.execute("SELECT 1 FROM reporting_receipt_heads")
        ).fetchone()
    with pytest.raises(IntegrityError):
        async with graph.pool.connection() as connection, connection.transaction():
            # Also prove the deferred FK prevents removing an existing change.
            await connection.execute(
                "DELETE FROM reporting_reconciliation_changes WHERE record_kind = 'materialization'"
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "foreign-account"),
        ("consumer_id", "other-buyer"),
        ("chain_key", "f" * 64),
        ("receipt_id", "nonexistent-receipt"),
        ("receipt_status", "accepted"),
        ("supersedes_receipt_id", "nonexistent-predecessor"),
    ],
)
async def test_sql_rejects_forged_receipt_heads(graph: Graph, field: str, value: str) -> None:
    from psycopg import IntegrityError, sql

    await raw_insert(
        graph.pool,
        replace(graph.first.receipt, status="rejected", rejection_codes=("LOAD_FAILED",)),
    )
    async with graph.pool.connection() as connection:
        before = await (
            await connection.execute("SELECT * FROM reporting_receipt_heads")
        ).fetchall()
    with pytest.raises(IntegrityError):
        async with graph.pool.connection() as connection:
            await connection.execute(
                sql.SQL("UPDATE reporting_receipt_heads SET {} = %s").format(sql.Identifier(field)),
                (value,),
            )
    async with graph.pool.connection() as connection:
        assert (
            await (await connection.execute("SELECT * FROM reporting_receipt_heads")).fetchall()
            == before
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE reporting_configurations SET feed_purpose = 'pacing'",
        "UPDATE reporting_obligations SET definition = NULL",
        "UPDATE reporting_revisions SET finality = 'snapshot'",
        "UPDATE reporting_revisions SET reporting_obligation_id = 'second-obligation'"
        " WHERE reporting_revision_id = 'revision-acct_a'",
        "UPDATE reporting_adjustments SET reason_detail = 'changed'"
        " WHERE reporting_adjustment_id = 'adjustment-0'",
    ],
)
async def test_sql_cannot_rewrite_roots_referenced_by_frozen_evidence(
    graph: Graph, statement: str
) -> None:
    from psycopg import IntegrityError

    await raw_insert(graph.pool, graph.adjustment)
    with pytest.raises(IntegrityError):
        async with graph.pool.connection() as connection:
            await connection.execute(statement)
    # Operational readability is still mutable through the supported Core API.
    await graph.store.set_revision_readable(
        account_id="acct_a",
        reporting_revision_id=graph.first.revision.reporting_revision_id,
        readable=False,
    )


@pytest.mark.parametrize("method", ["file_transfer", "dataset_share", "warehouse_materialization"])
async def test_sql_native_commit_cannot_claim_immutable_location(method: str) -> None:
    from psycopg import IntegrityError

    async with isolated_reporting_pool() as pool:
        store = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        s = await scenario(store, method=method, profile="native_commit", billing=False)
        with pytest.raises(IntegrityError):
            await raw_insert(
                pool,
                replace(
                    s.outcome,
                    resource=replace(s.outcome.resource, immutability="immutable_location"),
                ),
            )
        await raw_insert(pool, s.outcome)
        assert (await store.get_materialization(s.attempt.key)).outcome == s.outcome


@pytest.mark.parametrize(
    "field,value",
    [
        ("seq", 99),
        ("account_id", "other-account"),
        ("consumer_id", "other-buyer"),
        ("namespace", "wrong-namespace"),
        ("record_id", "wrong-record"),
        ("record_kind", "materialization_attempt"),
        ("change_id", "f" * 64),
        ("content_sha256", "0" * 64),
    ],
)
async def test_feed_identity_damage_cannot_disappear_through_a_join(
    graph: Graph, field: str, value: Any
) -> None:
    from psycopg import sql

    async with graph.pool.connection() as connection:
        await connection.execute("ALTER TABLE reporting_reconciliation_changes DISABLE TRIGGER ALL")
        await connection.execute(
            sql.SQL(
                "UPDATE reporting_reconciliation_changes SET {} = %s"
                " WHERE account_id = 'acct_a' AND consumer_id = 'buyer'"
                " AND record_kind = 'materialization' AND record_id = 'materialization-1'"
            ).format(sql.Identifier(field)),
            (value,),
        )
        await connection.execute("ALTER TABLE reporting_reconciliation_changes ENABLE TRIGGER ALL")
    caller = graph.first.binding.principal
    for read in (
        graph.store.read_reconciliation_snapshot(caller=caller),
        graph.store.read_reconciliation_changes(caller=caller, limit=1),
        graph.store.read_reconciliation_changes(
            caller=caller, filters=ReportingReconciliationFilter(record_kinds=("revision_receipt",))
        ),
    ):
        with pytest.raises(LedgerConflictError) as error:
            await read
        assert error.value.code == "REPORTING_HISTORY_CORRUPT"
        assert error.value.__cause__ is None and error.value.__context__ is None


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE reporting_reconciliation_heads SET max_sequence = max_sequence + 2",
        "UPDATE reporting_reconciliation_heads SET max_sequence = max_sequence + 1",
        "UPDATE reporting_reconciliation_heads SET consumer_id = 'forged-principal'",
        "DELETE FROM reporting_reconciliation_heads",
        "DELETE FROM reporting_reconciliation_changes",
        "UPDATE reporting_reconciliation_changes SET seq = seq + 100",
        "INSERT INTO reporting_reconciliation_heads (account_id, consumer_id, max_sequence)"
        " VALUES ('new-account', 'new-consumer', 1)",
    ],
)
async def test_sql_cannot_advance_move_or_erase_the_feed_without_exact_evidence(
    graph: Graph, statement: str
) -> None:
    from psycopg import IntegrityError

    caller = graph.first.binding.principal
    before = await graph.store.read_reconciliation_changes(caller=caller)
    with pytest.raises(IntegrityError):
        async with graph.pool.connection() as connection, connection.transaction():
            await connection.execute(statement)
    assert await graph.store.read_reconciliation_changes(caller=caller) == before
