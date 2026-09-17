"""Financial predicates are shared by ingress and literal PostgreSQL writes."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import ReportingMaterializationCheck
from adcp.reporting.ledger._delivery_state import current_receipt, receipt_chain
from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingReceiptKey,
)
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.receipts.records import receipt_record

from ._generation_support import END
from ._receipt_support import (
    adjustment_for,
    foreign_account,
    foreign_targets,
    receipt_case,
    receipt_harness,
    receipts,
    request_for,
)
from .test_reporting_reconciliation_sql import raw_insert

__all__ = ["receipts"]


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("rows", "RECEIPT_TOTALS_MISMATCH"),
        ("totals", "RECEIPT_TOTALS_MISMATCH"),
        ("currency", "RECEIPT_TOTALS_MISMATCH"),
        ("digest", "RECEIPT_EVIDENCE_MISMATCH"),
        ("manifest", "RECEIPT_EVIDENCE_MISMATCH"),
        ("profile", "RECEIPT_PROFILE_MISMATCH"),
        ("materialization", "REPORTING_RECORD_UNAVAILABLE"),
        ("revision", "REPORTING_RECORD_UNAVAILABLE"),
        ("obligation", "REPORTING_RECORD_UNAVAILABLE"),
        ("future", "REPORTING_TIME_INVALID"),
        ("before_outcome", "REPORTING_TIME_INVALID"),
        ("duplicate_totals", "INVALID_REPORTING_RECORD"),
    ],
)
async def test_exact_artifact_predicates_produce_durable_item_failures(receipts, fault, expected):
    h = receipts
    s = await receipt_case(h)
    request = request_for(s)
    item = request["receipts"][0]
    if fault == "rows":
        item["observed_row_count"] += 1
    elif fault == "totals":
        item["observed_control_totals"][0]["value"] = "1000"
    elif fault == "currency":
        next(t for t in item["observed_control_totals"] if t["name"] == "spend")["unit"] = "GBP"
    elif fault == "digest":
        item["observed_canonical_content_digest"]["value"] = "f" * 64
    elif fault == "manifest":
        item["observed_manifest_sha256"] = "f" * 64
    elif fault == "profile":
        item["verification_profile"] = "manifest_checksums"
        item["observed_manifest_sha256"] = "a" * 64
    elif fault == "materialization":
        item["reporting_materialization_id"] = "unknown"
    elif fault == "revision":
        item["reporting_revision_id"] = "unknown"
    elif fault == "obligation":
        item["reporting_obligation_id"] = "unknown"
    elif fault == "future":
        item["observed_at"] = "2099-01-01T00:00:00Z"
    elif fault == "before_outcome":
        item["observed_at"] = END.isoformat()
    else:
        item["observed_control_totals"].append({**item["observed_control_totals"][0], "value": "2"})
    result = await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    assert result["results"][0]["errors"][0]["code"] == expected
    assert not await h.store.get_receipt(s.receipt.key)
    assert not await h.store.read_receipt_boundaries(caller=s.binding.principal)
    assert await h.store.ingest_receipt_batch(request, caller=s.binding.principal) == result


@pytest.mark.parametrize(
    "fault",
    [
        "rows",
        "digest",
        "manifest",
        "profile",
        "materialization",
        "revision",
        "obligation",
        "readability",
    ],
)
async def test_literal_sql_enforces_exact_artifact_and_readability_without_python_validator(fault):
    async with receipt_harness("postgres") as h:
        from psycopg import IntegrityError

        s = await receipt_case(h)
        item = receipt_to_wire(s.receipt)
        if fault == "rows":
            item["observed_row_count"] += 1
        elif fault == "digest":
            item["observed_canonical_content_digest"]["value"] = "f" * 64
        elif fault == "manifest":
            item["observed_manifest_sha256"] = "f" * 64
        elif fault == "profile":
            item["verification_profile"] = "manifest_checksums"
            item["observed_manifest_sha256"] = "a" * 64
        elif fault == "materialization":
            item["reporting_materialization_id"] = "unknown"
        elif fault == "revision":
            item["reporting_revision_id"] = "unknown"
        elif fault == "obligation":
            item["reporting_obligation_id"] = "unknown"
        else:
            await h.store.record_materialization_check(
                ReportingMaterializationCheck(
                    s.attempt.scope,
                    s.attempt.reporting_materialization_id,
                    "corrupt-before-acceptance",
                    "corrupt",
                    s.receipt.observed_at - timedelta(microseconds=1),
                )
            )
        candidate = receipt_record("revision_receipt", item, s.binding.principal, s.obligation)
        if fault == "obligation":
            candidate = replace(
                candidate, scope=replace(candidate.scope, reporting_obligation_id="unknown")
            )
        before = await h.image()
        with pytest.raises(IntegrityError):
            await raw_insert(h.pool, candidate)
        assert await h.image() == before


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("digest", "ADJUSTMENT_DIGEST_MISMATCH"),
        ("revision", "REPORTING_RECORD_UNAVAILABLE"),
        ("unknown", "REPORTING_RECORD_UNAVAILABLE"),
        ("observed_before_adjustment", "ADJUSTMENT_ORDER_INVALID"),
        ("correction_before_finality", "ADJUSTMENT_ORDER_INVALID"),
    ],
)
async def test_official_adjustment_digest_ownership_and_order(receipts, fault, expected):
    h = receipts
    s = await receipt_case(h)
    changes = (
        {"correction_observed_at": END - timedelta(seconds=1)}
        if fault == "correction_before_finality"
        else {}
    )
    item = await adjustment_for(h, s, **changes)
    if fault == "digest":
        item["observed_adjustment_sha256"] = "f" * 64
    elif fault == "revision":
        item["adjusts_reporting_revision_id"] = "unknown"
    elif fault == "unknown":
        item["reporting_adjustment_id"] = "unknown"
    elif fault == "observed_before_adjustment":
        item["observed_at"] = END.isoformat()
    request = request_for(s, adjustment_receipts=[item])
    result = await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    assert result["results"][0]["result"] == "recorded"
    assert result["results"][1]["errors"][0]["code"] == expected
    assert result["results"][1]["reporting_receipt_id"] == item["reporting_receipt_id"]
    assert await h.store.ingest_receipt_batch(request, caller=s.binding.principal) == result


@pytest.mark.parametrize(
    "fault", ["digest", "observed_before_adjustment", "correction_before_finality"]
)
async def test_literal_sql_adjustment_finality_digest_and_order(fault):
    async with receipt_harness("postgres") as h:
        from psycopg import IntegrityError

        s = await receipt_case(h)
        changes = (
            {"correction_observed_at": END - timedelta(seconds=1)}
            if fault == "correction_before_finality"
            else {}
        )
        item = await adjustment_for(h, s, **changes)
        if fault == "digest":
            item["observed_adjustment_sha256"] = "f" * 64
        elif fault == "observed_before_adjustment":
            item["observed_at"] = END.isoformat()
        candidate = receipt_record("adjustment_receipt", item, s.binding.principal, s.obligation)
        before = await h.image()
        with pytest.raises(IntegrityError):
            await raw_insert(h.pool, candidate)
        assert await h.image() == before


async def test_adjustment_rejected_current_leaf_replacement_and_accepted_terminality(receipts):
    h = receipts
    s = await receipt_case(h)
    item = await adjustment_for(h, s)
    rejected = {**item, "status": "rejected", "rejection_codes": ["LOAD_FAILED"]}
    first = request_for(s, adjustment_receipts=[rejected])
    assert (await h.store.ingest_receipt_batch(first, caller=s.binding.principal))["results"][1][
        "result"
    ] == "recorded"
    accepted = {
        **item,
        "reporting_receipt_id": "accepted-adjustment-0002",
        "supersedes_reporting_receipt_id": item["reporting_receipt_id"],
    }
    second = request_for(s, key="receipt-batch-0002", adjustment_receipts=[accepted])
    assert (await h.store.ingest_receipt_batch(second, caller=s.binding.principal))["results"][1][
        "result"
    ] == "recorded"
    third = request_for(
        s,
        key="receipt-batch-0003",
        adjustment_receipts=[
            {
                **accepted,
                "reporting_receipt_id": "terminal-adjustment-0003",
                "supersedes_reporting_receipt_id": accepted["reporting_receipt_id"],
            }
        ],
    )
    result = await h.store.ingest_receipt_batch(third, caller=s.binding.principal)
    assert result["results"][1]["errors"][0]["code"] == "ACCEPTED_RECEIPT_TERMINAL"
    leaves = (
        await h.store.read_reconciliation_snapshot(caller=s.binding.principal)
    ).current_receipts
    assert {r.reporting_receipt_id for r in leaves} == {
        s.receipt.reporting_receipt_id,
        accepted["reporting_receipt_id"],
    }


def damaged_chain(s, fault, root=None):
    root = replace(
        root if root is not None else s.receipt, status="rejected", rejection_codes=("LOAD_FAILED",)
    )
    leaf = replace(
        root,
        reporting_receipt_id="leaf-receipt-0002",
        supersedes_reporting_receipt_id=root.reporting_receipt_id,
    )
    records = [root, leaf]
    if fault == "fork":
        records.append(replace(leaf, reporting_receipt_id="fork-receipt-0003"))
    elif fault == "cycle":
        records[0] = replace(root, supersedes_reporting_receipt_id=leaf.reporting_receipt_id)
    elif fault == "gap":
        records.pop(0)
    elif fault == "cross_target":
        # An adjustment chain key binds only reporting_adjustment_id, so a
        # divergent adjusts_reporting_revision_id stays inside the same chain
        # and only the dedicated official-target predicate can reject it.
        field = (
            "adjusts_reporting_revision_id"
            if isinstance(root, ReportingAdjustmentReceiptRecord)
            else "reporting_revision_id"
        )
        records[0] = replace(root, **{field: "other-revision"})
    elif fault == "accepted_predecessor":
        records[0] = replace(root, status="accepted", rejection_codes=())
    else:
        cycle = replace(
            root,
            reporting_receipt_id="cycle-receipt-0003",
            supersedes_reporting_receipt_id="cycle-receipt-0004",
        )
        records += [
            cycle,
            replace(
                cycle,
                reporting_receipt_id="cycle-receipt-0004",
                supersedes_reporting_receipt_id=cycle.reporting_receipt_id,
            ),
        ]
    return records, leaf


@pytest.mark.parametrize(
    "fault", ["fork", "cycle", "gap", "cross_target", "accepted_predecessor", "disconnected_cycle"]
)
async def test_complete_receipt_chain_rejects_damage_with_one_apparent_leaf(fault):
    async with receipt_harness("memory") as h:
        s = await receipt_case(h)
        records, leaf = damaged_chain(s, fault)
        with pytest.raises(LedgerConflictError) as error:
            current_receipt(tuple(records), leaf)
        assert error.value.code == "REPORTING_HISTORY_CORRUPT"


@pytest.mark.parametrize(
    "fault", ["fork", "cycle", "gap", "cross_target", "accepted_predecessor", "disconnected_cycle"]
)
async def test_literal_sql_blocks_forks_and_detects_damaged_existing_chains(fault):
    from adcp.reporting.ledger._delivery_state import (
        change_id,
        fingerprint,
        payload,
        storage_identity,
    )
    from adcp.reporting.ledger.delivery_pg import _IDENTITY_COLUMNS

    from ._generation_support import NOW

    async with receipt_harness("postgres") as h:
        from psycopg import IntegrityError, sql
        from psycopg.types.json import Jsonb

        s = await receipt_case(h)
        records, leaf = damaged_chain(s, fault)
        # Deliberate owner-level corruption bypasses triggers only while seeding
        # the invalid prior graph. The subsequent literal INSERT runs every
        # production predicate, without a Python validator or head API.
        from contextlib import nullcontext

        seed_before = await h.image()
        # A fork cannot even be seeded with replica triggers: the reviewed
        # unique successor index is an independent hard financial predicate.
        with pytest.raises(IntegrityError) if fault == "fork" else nullcontext():
            async with h.pool.connection() as c, c.transaction():
                await c.execute("SET LOCAL session_replication_role = replica")
                for record in records:
                    record = replace(record, received_at=NOW)
                    row = dict(zip(_IDENTITY_COLUMNS, storage_identity(record)))
                    row.update(
                        payload=Jsonb(payload(record)),
                        content_sha256=fingerprint(record),
                        change_id=change_id(record),
                    )
                    await c.execute(
                        sql.SQL(
                            "INSERT INTO reporting_reconciliation_records ({}) VALUES ({})"
                        ).format(
                            sql.SQL(", ").join(map(sql.Identifier, row)),
                            sql.SQL(", ").join(sql.Placeholder() for _ in row),
                        ),
                        tuple(row.values()),
                    )
        if fault == "fork":
            assert await h.image() == seed_before
            return
        new = replace(
            leaf,
            reporting_receipt_id="new-leaf-receipt-0099",
            supersedes_reporting_receipt_id=leaf.reporting_receipt_id,
        )
        before = await h.image()
        with pytest.raises(IntegrityError):
            await raw_insert(h.pool, new)
        assert await h.image() == before


@pytest.mark.parametrize(
    "method,profile,field,wrong",
    [
        ("file_transfer", "canonical_digest", "observed_canonical_content_digest", None),
        ("file_transfer", "manifest_checksums", "observed_manifest_sha256", "f" * 64),
        ("file_transfer", "native_commit", "observed_native_version_ref", "wrong-version"),
        ("dataset_share", "native_commit", "observed_native_version_ref", "wrong-version"),
        (
            "warehouse_materialization",
            "native_commit",
            "observed_native_version_ref",
            "wrong-version",
        ),
    ],
)
async def test_all_receipt_evidence_profiles_match_exact_immutable_artifact(
    receipts, method, profile, field, wrong
):
    from copy import deepcopy

    h = receipts
    s = await receipt_case(h, method=method, profile=profile, billing=profile == "canonical_digest")
    request = request_for(s)
    bad = deepcopy(request)
    if wrong is None:
        bad["receipts"][0][field][
            "canonicalization_uri"
        ] = "https://wrong.example.test/profile.json"
    else:
        bad["receipts"][0][field] = wrong
    result = await h.store.ingest_receipt_batch(bad, caller=s.binding.principal)
    assert result["results"][0]["errors"][0]["code"] == "RECEIPT_EVIDENCE_MISMATCH"
    request["idempotency_key"] = "valid-artifact-receipt"
    accepted = await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    assert accepted["results"][0]["result"] == "recorded"
    assert accepted["results"][0]["receipt"][field] == request["receipts"][0][field]
    assert await h.store.ingest_receipt_batch(request, caller=s.binding.principal) == accepted


async def test_snapshot_adjustment_is_never_admitted_as_official_receipt_evidence(receipts):
    h = receipts
    s = await receipt_case(h, finality="snapshot", billing=False)
    before = await h.image()
    with pytest.raises(LedgerConflictError) as error:
        await adjustment_for(h, s)
    assert error.value.code == "ADJUSTMENT_REQUIRES_OFFICIAL"
    assert await h.image() == before
    item = {
        "reporting_receipt_id": "snapshot-adjustment-0001",
        "reporting_adjustment_id": "adjustment-1",
        "adjusts_reporting_revision_id": s.revision.reporting_revision_id,
        "status": "accepted",
        "observed_at": s.receipt.observed_at.isoformat(),
        "observed_adjustment_sha256": "0" * 64,
    }
    result = await h.store.ingest_receipt_batch(
        request_for(s, adjustment_receipts=[item]), caller=s.binding.principal
    )
    assert result["results"][0]["result"] == "recorded"
    assert result["results"][1]["errors"][0]["code"] == "REPORTING_RECORD_UNAVAILABLE"


_FOREIGN_SQL_PREDICATE = {
    "materialization_other_obligation": "receipt artifact is unavailable",
    "materialization_other_consumer": "receipt artifact is unavailable",
    "revision_other_obligation": "receipt target is unavailable",
    "obligation_other_target": "receipt target is unavailable",
    "adjustment_other_revision": "receipt adjustment order is invalid",
    "adjustment_revision_other_target": "receipt target is unavailable",
}
_FOREIGN_TARGETS = list(_FOREIGN_SQL_PREDICATE)


def _foreign_case(s, f, fault):
    """One existing, valid but foreign reference, plus its truly absent twin.

    ``adjustment_other_revision`` deliberately observes *after* the foreign
    adjustment was created. Every ordering comparison then holds on its own, so
    within this trigger ``adjusts_reporting_revision_id`` is the remaining
    reason the adjustment tuple finds no row. A nearer observation would be
    refused by the ordering rule instead and could not attribute the ownership
    predicate. Inherited reconciliation guards still refuse the write if this
    predicate is weakened, so this is attribution, not exclusive necessity.
    """
    if fault == "materialization_other_obligation":
        return "revision", {"reporting_materialization_id": f.materialization_id}, {}
    if fault == "materialization_other_consumer":
        return "revision", {"reporting_materialization_id": f.consumer_materialization_id}, {}
    if fault == "revision_other_obligation":
        return "revision", {"reporting_revision_id": f.revision.reporting_revision_id}, {}
    if fault == "obligation_other_target":
        return "revision", {"reporting_obligation_id": f.obligation.reporting_obligation_id}, {}
    if fault == "adjustment_other_revision":
        late = (f.adjustment.created_at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        return (
            "adjustment",
            {"reporting_adjustment_id": f.adjustment.reporting_adjustment_id, "observed_at": late},
            {"observed_at": late},
        )
    return "adjustment", {"adjusts_reporting_revision_id": f.revision.reporting_revision_id}, {}


@pytest.mark.parametrize("fault", _FOREIGN_TARGETS)
async def test_existing_foreign_targets_fail_exactly_like_absent_records(receipts, fault):
    """A valid row owned by another target is not a usable receipt reference.

    Absence and misownership must be indistinguishable, so a caller cannot probe
    another account's, consumer's, obligation's or revision's artifact history.
    """
    h = receipts
    s = await receipt_case(h)
    f = await foreign_targets(h, s)
    kind, foreign, shared = _foreign_case(s, f, fault)
    base = await adjustment_for(h, s) if kind == "adjustment" else receipt_to_wire(s.receipt)
    array = "adjustment_receipts" if kind == "adjustment" else "receipts"
    # An adjustment batch keeps its ordinary revision receipt at ordinal zero.
    index = 1 if kind == "adjustment" else 0

    def build(overrides, key, receipt_id):
        item = {**base, **shared, **overrides, "reporting_receipt_id": receipt_id}
        return request_for(s, key=key, **{array: [item]})

    failed_id = base["reporting_receipt_id"]
    request = build(foreign, "receipt-batch-0001", failed_id)
    absent_of = {name: f"absent-{name}" for name in foreign if name != "observed_at"}
    control = build(absent_of, "receipt-batch-0002", "receipt-control-0001")
    result = await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    failure = result["results"][index]
    assert failure["result"] == "failed"
    assert failure["reporting_receipt_id"] == failed_id
    assert failure["errors"][0]["code"] == "REPORTING_RECORD_UNAVAILABLE"
    # The refused item is durably absent from evidence, the caller feed and capture.
    assert not await h.store.get_receipt(ReportingReceiptKey(s.binding.principal, failed_id))
    boundaries = await h.store.read_receipt_boundaries(caller=s.binding.principal)
    assert failed_id not in {b.reporting_receipt_id for b in boundaries}
    assert len(boundaries) == index
    # Identical durable replay, and an identical closed error for an absent row.
    assert await h.store.ingest_receipt_batch(request, caller=s.binding.principal) == result
    absent = await h.store.ingest_receipt_batch(control, caller=s.binding.principal)
    assert absent["results"][index]["result"] == "failed"
    assert absent["results"][index]["errors"] == failure["errors"]


@pytest.mark.parametrize("fault", _FOREIGN_TARGETS)
async def test_literal_sql_rejects_existing_foreign_targets(fault):
    """The exact tuple predicates run without any Python validator or head API."""
    async with receipt_harness("postgres") as h:
        from psycopg import IntegrityError

        s = await receipt_case(h)
        f = await foreign_targets(h, s)
        kind, foreign, shared = _foreign_case(s, f, fault)
        obligation = s.obligation
        if kind == "adjustment":
            item = {**await adjustment_for(h, s), **shared, **foreign}
        else:
            item = {**receipt_to_wire(s.receipt), **shared, **foreign}
            # The scope carries the obligation, so a foreign one replaces it.
            obligation = f.obligation if "reporting_obligation_id" in foreign else s.obligation
            item.pop("reporting_obligation_id", None)
            item["reporting_obligation_id"] = obligation.reporting_obligation_id
        candidate = receipt_record(f"{kind}_receipt", item, s.binding.principal, obligation)
        before = await h.image()
        with pytest.raises(IntegrityError) as error:
            await raw_insert(h.pool, candidate)
        # Attribution, not sole prevention: the inherited reconciliation guards
        # independently refuse these same tuples, so a bare IntegrityError
        # cannot show that this slice's predicates run at all. Asserting the
        # exact message proves they reject first. Predicate *drift* is refused
        # separately by the fingerprinted readiness manifest -- see
        # test_reporting_receipt_migration.py's function-body damage cases.
        assert error.value.diag.message_primary == _FOREIGN_SQL_PREDICATE[fault]
        assert await h.image() == before


@pytest.mark.parametrize(
    "fault", ["fork", "cycle", "gap", "cross_target", "accepted_predecessor", "disconnected_cycle"]
)
async def test_complete_adjustment_receipt_chain_rejects_damage(fault):
    """Adjustment chains need their own topology and official-target checks."""
    async with receipt_harness("memory") as h:
        s = await receipt_case(h)
        root = receipt_record(
            "adjustment_receipt", await adjustment_for(h, s), s.binding.principal, s.obligation
        )
        records, leaf = damaged_chain(s, fault, root)
        if fault == "cross_target":
            # The divergent predecessor is inside the requested chain, so no
            # fork/gap/leaf-count rule can stand in for the exact predicate.
            assert receipt_chain(records[0]) == receipt_chain(leaf)
        with pytest.raises(LedgerConflictError) as error:
            current_receipt(tuple(records), leaf)
        assert error.value.code == "REPORTING_HISTORY_CORRUPT"


@pytest.mark.parametrize(
    "fault", ["fork", "cycle", "gap", "cross_target", "accepted_predecessor", "disconnected_cycle"]
)
async def test_literal_sql_blocks_damaged_adjustment_chains(fault):
    from contextlib import nullcontext

    from adcp.reporting.ledger._delivery_state import (
        change_id,
        fingerprint,
        payload,
        storage_identity,
    )
    from adcp.reporting.ledger.delivery_pg import _IDENTITY_COLUMNS

    from ._generation_support import NOW

    async with receipt_harness("postgres") as h:
        from psycopg import IntegrityError, sql
        from psycopg.types.json import Jsonb

        s = await receipt_case(h)
        root = receipt_record(
            "adjustment_receipt", await adjustment_for(h, s), s.binding.principal, s.obligation
        )
        records, leaf = damaged_chain(s, fault, root)
        seed_before = await h.image()
        # Owner-level seeding only; the final literal INSERT runs every trigger.
        # "fork" is the exception: the unique successor index refuses it during
        # seeding, so that variant never reaches a trigger-enabled INSERT.
        with pytest.raises(IntegrityError) if fault == "fork" else nullcontext():
            async with h.pool.connection() as c, c.transaction():
                await c.execute("SET LOCAL session_replication_role = replica")
                for record in records:
                    record = replace(record, received_at=NOW)
                    row = dict(zip(_IDENTITY_COLUMNS, storage_identity(record)))
                    row.update(
                        payload=Jsonb(payload(record)),
                        content_sha256=fingerprint(record),
                        change_id=change_id(record),
                    )
                    await c.execute(
                        sql.SQL(
                            "INSERT INTO reporting_reconciliation_records ({}) VALUES ({})"
                        ).format(
                            sql.SQL(", ").join(map(sql.Identifier, row)),
                            sql.SQL(", ").join(sql.Placeholder() for _ in row),
                        ),
                        tuple(row.values()),
                    )
        if fault == "fork":
            assert await h.image() == seed_before
            return
        new = replace(
            leaf,
            reporting_receipt_id="new-adjustment-receipt-0099",
            supersedes_reporting_receipt_id=leaf.reporting_receipt_id,
        )
        before = await h.image()
        with pytest.raises(IntegrityError) as error:
            await raw_insert(h.pool, new)
        assert error.value.diag.message_primary in {
            "receipt replacement is unavailable",
            "receipt history is inconsistent",
        }
        assert await h.image() == before


_FOREIGN_ACCOUNT_SQL = {
    "revision_other_account": "receipt target is unavailable",
    "obligation_other_account": "receipt target is unavailable",
    "materialization_other_account": "receipt artifact is unavailable",
    "adjustment_other_account": "receipt adjustment order is invalid",
}


@pytest.mark.parametrize("fault", list(_FOREIGN_ACCOUNT_SQL))
async def test_literal_sql_rejects_valid_other_account_targets(fault):
    """The account column of each financial tuple, at the write predicate itself.

    Store-level and mounted authorization denials are a different boundary and
    cannot stand in for this: they never reach an INSERT. Obligations, revisions
    and adjustments are globally keyed, so a foreign-account reference is the
    only way to exercise the account column with a wholly valid target row.
    """
    async with receipt_harness("postgres") as h:
        from psycopg import IntegrityError

        s = await receipt_case(h)
        other, adjustment, materialization = await foreign_account(h)
        if fault == "adjustment_other_account":
            candidate = receipt_record(
                "adjustment_receipt", await adjustment_for(h, s), s.binding.principal, s.obligation
            )
            candidate = replace(
                candidate, reporting_adjustment_id=adjustment["reporting_adjustment_id"]
            )
        else:
            candidate = receipt_record(
                "revision_receipt", receipt_to_wire(s.receipt), s.binding.principal, s.obligation
            )
            if fault == "revision_other_account":
                candidate = replace(
                    candidate, reporting_revision_id=other.revision.reporting_revision_id
                )
            elif fault == "obligation_other_account":
                candidate = replace(
                    candidate,
                    scope=replace(
                        candidate.scope,
                        reporting_obligation_id=other.obligation.reporting_obligation_id,
                    ),
                )
            else:
                candidate = replace(candidate, reporting_materialization_id=materialization)
        before = await h.image()
        with pytest.raises(IntegrityError) as error:
            await raw_insert(h.pool, candidate)
        assert error.value.diag.message_primary == _FOREIGN_ACCOUNT_SQL[fault]
        assert await h.image() == before
