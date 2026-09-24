"""Receipt, feed, captured dirty input, and ordinal share one rollback boundary."""

from copy import deepcopy

import pytest

from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.receipts import ReportingReceiptError

from ._receipt_support import adjustment_for, batch_state, receipt_case, receipts, request_for

__all__ = ["receipts"]


def fail_after(monkeypatch, owner, name, *, predicate=None, asynchronous=False):
    original = getattr(owner, name)
    if asynchronous:

        async def injected(*args, **kwargs):
            result = await original(*args, **kwargs)
            if predicate is None or predicate(args, kwargs):
                raise RuntimeError("receipt fault")
            return result

    else:

        def injected(*args, **kwargs):
            result = original(*args, **kwargs)
            if predicate is None or predicate(args, kwargs):
                raise RuntimeError("receipt fault")
            return result

    monkeypatch.setattr(owner, name, injected)


@pytest.mark.parametrize(
    "point",
    [
        "header",
        "receipt",
        "receipt_head",
        "feed_head",
        "feed",
        "dirty",
        "capture_head",
        "account_capture_head",
        "capture",
        "ordinal",
    ],
)
async def test_every_insertion_rolls_back_first_ordinal_in_both_notification_modes(
    receipts, point, monkeypatch
):
    h = receipts
    s = await receipt_case(h)
    request = request_for(s)
    before = await h.image()
    store_class = type(h.store)
    if h.pool is None:
        if point in {"receipt", "receipt_head", "feed_head", "feed"}:
            # Memory publishes the evidence, caller feed and sequence in one
            # list assignment; there are no independently mutable receipt heads.
            method = "_append_reconciliation_change"
        elif point in {"capture_head", "account_capture_head"}:
            # Construction occurs after both newly allocated heads changed but
            # before the boundary is appended. This catches incomplete rollback
            # snapshots that forget first-use collections or sequence counters.
            import adcp.reporting.receipts.memory as memory

            fail_after(monkeypatch, memory, "ReportingReceiptBoundary")
            method = None
        else:
            method = {
                "header": "_receipt_batch",
                "dirty": "_capture_receipt",
                "capture": "_capture_receipt",
                "ordinal": "_append_receipt_result",
            }[point]
        if method is not None:
            fail_after(monkeypatch, store_class, method)
    else:
        if point == "receipt_head":
            async with h.pool.connection() as c:
                await c.execute(
                    "CREATE FUNCTION receipt_test_fault() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'receipt fault'; END $$"
                )
                await c.execute(
                    "CREATE TRIGGER receipt_test_fault AFTER INSERT OR UPDATE ON "
                    "reporting_receipt_heads FOR EACH ROW EXECUTE FUNCTION receipt_test_fault()"
                )
        elif point in {"feed_head", "capture_head", "account_capture_head"}:
            from psycopg import AsyncConnection

            needle = {
                "feed_head": "INSERT INTO reporting_reconciliation_heads",
                "capture_head": "INSERT INTO reporting_receipt_ingestion_heads",
                "account_capture_head": (
                    "UPDATE reporting_materializer_accounts SET captured_sequence"
                ),
            }[point]
            fail_after(
                monkeypatch,
                AsyncConnection,
                "execute",
                predicate=lambda args, _: isinstance(args[1], str) and needle in args[1],
                asynchronous=True,
            )
        else:
            method = {
                "header": "_receipt_batch_on",
                "receipt": "_insert",
                "feed": "_append_reconciliation_change",
                "dirty": "_capture_receipt_on",
                "capture": "_capture_receipt_on",
                "ordinal": "_insert_receipt_result_on",
            }[point]
            fail_after(monkeypatch, store_class, method, asynchronous=True)
    with pytest.raises((RuntimeError, ReportingReceiptError)):
        await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    monkeypatch.undo()
    assert await h.image() == before
    if h.pool is not None and point == "receipt_head":
        async with h.pool.connection() as c:
            await c.execute("DROP TRIGGER receipt_test_fault ON reporting_receipt_heads")
    result = await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    assert result["results"][0]["result"] == "recorded"


@pytest.mark.parametrize("point", ["persistence", "assembly"])
async def test_final_response_fault_preserves_prior_ordinals_and_original_recorded_results(
    receipts, point, monkeypatch
):
    h = receipts
    s = await receipt_case(h)
    adjustment = await adjustment_for(h, s)
    request = request_for(s, adjustment_receipts=[adjustment])
    method = (
        "_assemble_receipt_response"
        if point == "assembly"
        else ("_save_receipt_response" if h.pool is None else "_save_receipt_response_on")
    )
    fail_after(
        monkeypatch,
        type(h.store),
        method,
        asynchronous=h.pool is not None and point == "persistence",
    )
    with pytest.raises((RuntimeError, ReportingReceiptError)):
        await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    monkeypatch.undo()
    assert await batch_state(h) == ((2, False),)
    original = await h.store.get_receipt(s.receipt.key)
    before = await h.store.read_receipt_boundaries(caller=s.attempt.scope.principal)
    assert len(before) == 2
    result = await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    assert [r["result"] for r in result["results"]] == ["recorded", "recorded"]
    assert result["results"][0]["receipt"] == receipt_to_wire(original)
    assert await h.store.read_receipt_boundaries(caller=s.attempt.scope.principal) == before
    assert await batch_state(h) == ((2, True),)


async def test_second_ordinal_failure_preserves_successful_sibling_and_durable_failure(
    receipts, monkeypatch
):
    h = receipts
    s = await receipt_case(h)
    adjustment = await adjustment_for(h, s)
    request = request_for(
        s,
        receipts=[
            {
                **receipt_to_wire(s.receipt),
                "reporting_receipt_id": "missing-first-0001",
                "reporting_revision_id": "missing",
            },
            receipt_to_wire(s.receipt),
        ],
        adjustment_receipts=[adjustment],
    )
    if h.pool is None:
        # Fail after appending the second ordinal: its newly recorded receipt,
        # private capture and result must roll back; ordinal-zero failure stays.
        fail_after(
            monkeypatch,
            type(h.store),
            "_append_receipt_result",
            predicate=lambda args, _: len(args[1].results) == 2,
        )
    else:
        fail_after(
            monkeypatch,
            type(h.store),
            "_insert_receipt_result_on",
            predicate=lambda args, _: args[4] == 1,
            asynchronous=True,
        )
    with pytest.raises((RuntimeError, ReportingReceiptError)):
        await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    monkeypatch.undo()
    assert await batch_state(h) == ((1, False),)
    assert await h.store.get_receipt(s.receipt.key) is None
    assert await h.store.read_receipt_boundaries(caller=s.attempt.scope.principal) == ()
    changed = deepcopy(request)
    changed["receipts"][0]["reporting_revision_id"] = s.revision.reporting_revision_id
    before = await h.image()
    with pytest.raises(ReportingReceiptError) as error:
        await h.store.ingest_receipt_batch(changed, caller=s.attempt.scope.principal)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert await h.image() == before
    result = await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    assert [r["result"] for r in result["results"]] == ["failed", "recorded", "recorded"]


async def test_captured_inputs_remain_private_and_frozen_after_current_state_changes(receipts):
    h = receipts
    s = await receipt_case(h, consumer_id="https://buyer.example.test/one")
    sibling = await receipt_case(h, consumer_id="https://buyer.example.test/two")
    await h.store.ingest_receipt_batch(request_for(s), caller=s.attempt.scope.principal)
    frozen = await h.store.read_receipt_boundaries(caller=s.attempt.scope.principal)
    assert len(frozen) == 1
    assert not await h.store.read_receipt_boundaries(caller=sibling.attempt.scope.principal)
    await h.store.ingest_receipt_batch(request_for(sibling), caller=sibling.attempt.scope.principal)
    await h.store.set_revision_readable(
        account_id=s.obligation.account_id,
        reporting_revision_id=s.revision.reporting_revision_id,
        readable=False,
    )
    assert await h.store.read_receipt_boundaries(caller=s.attempt.scope.principal) == frozen
    assert all(
        (
            r.scope.consumer_id == s.binding.consumer_id
            if hasattr(r, "scope")
            else r.consumer_id == s.binding.consumer_id
        )
        for r in frozen[0].reconciliation
    )
    assert frozen[0].core.consumer_ids == (s.binding.consumer_id,)
    assert all(r.readable for r in frozen[0].core.revisions)
    other = await h.store.read_receipt_boundaries(caller=sibling.attempt.scope.principal)
    assert frozen[0].account_sequence < other[0].account_sequence


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_ordinary_status_dirty_insert_failure_rolls_back_receipt_and_ordinal(
    backend, monkeypatch
):
    from ._receipt_support import receipt_harness

    async with receipt_harness(backend, notifications=True) as h:
        s = await receipt_case(h)
        before = await h.image()
        if h.pool is None:
            fail_after(monkeypatch, type(h.store), "_dirty_status")
        else:
            from psycopg import AsyncConnection

            fail_after(
                monkeypatch,
                AsyncConnection,
                "execute",
                asynchronous=True,
                predicate=lambda args, _: isinstance(args[1], str)
                and args[1].startswith("INSERT INTO reporting_status_dirty"),
            )
        with pytest.raises((RuntimeError, ReportingReceiptError)):
            await h.store.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
        monkeypatch.undo()
        assert await h.image() == before
        receipt_operation_1 = await h.store.ingest_receipt_batch(
            request_for(s), caller=s.binding.principal
        )
        assert (receipt_operation_1)["results"][0]["result"] == "recorded"


@pytest.mark.parametrize("damage", ["id", "kind", "outcome", "received_at", "ordinal"])
async def test_corrupt_durable_prefix_blocks_resume_before_sibling_mutation(
    receipts, monkeypatch, damage
):
    from adcp.reporting.canonical_json import canonical_json_sha256_v1
    from adcp.reporting.receipts.wire import ReceiptBatch

    h = receipts
    s = await receipt_case(h)
    request = request_for(s, adjustment_receipts=[await adjustment_for(h, s)])
    # Retain the first committed ordinal, then corrupt only that immutable
    # prefix through a deliberately privileged fixture. Restore guards before
    # asking the actual store to resume.
    method = "_append_receipt_result" if h.pool is None else "_insert_receipt_result_on"
    fail_after(
        monkeypatch,
        type(h.store),
        method,
        asynchronous=h.pool is not None,
        predicate=(
            (lambda args, _: len(args[1].results) == 2)
            if h.pool is None
            else (lambda args, _: args[4] == 1)
        ),
    )
    with pytest.raises((RuntimeError, ReportingReceiptError)):
        await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    monkeypatch.undo()
    if h.pool is None:
        state = next(iter(h.store._receipt_batches.values()))
        value = deepcopy(state.results[0])
    else:
        async with h.pool.connection() as c:
            value = (
                await (
                    await c.execute("SELECT result FROM reporting_receipt_ingestion_results")
                ).fetchone()
            )[0]
    if damage == "id":
        value["receipt"]["reporting_receipt_id"] = "unrelated-receipt-0001"
    elif damage == "kind":
        value["adjustment_receipt"] = value.pop("receipt")
    elif damage == "outcome":
        value["receipt"]["status"] = "unrecognized"
    elif damage == "received_at":
        del value["receipt"]["received_at"]
    else:
        value["reporting_receipt_id"] = "extra-ordinal"
    if h.pool is None:
        state.results = (value,)
    else:
        from psycopg.types.json import Jsonb

        async with h.pool.connection() as c, c.transaction():
            await c.execute("SET LOCAL session_replication_role = replica")
            await c.execute(
                "UPDATE reporting_receipt_ingestion_results SET result=%s,content_sha256=%s",
                (Jsonb(value), canonical_json_sha256_v1(value)),
            )
    before = await h.image()
    with pytest.raises(ReportingReceiptError) as error:
        await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    assert error.value.code == "RECEIPT_HISTORY_CORRUPT"
    assert await h.image() == before
    assert len(ReceiptBatch.parse(request).items) == 2
