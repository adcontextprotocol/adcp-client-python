"""Admitted production work preserves exact transactions and epoch-zero history."""

import pytest

from adcp.reporting.materializer import reference_digest
from adcp.reporting.materializer.work import ReportingMaterializerLease
from adcp.server.base import ToolContext
from adcp.validation.schema_loader import get_named_validator

from ._production_support import production_harness
from ._projection_support import drain
from .test_reporting_production_lock_order import source_turn


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("count", [0, 503])
async def test_actual_source_publication_builds_canonical_evidence(backend, count, tmp_path):
    async with production_harness(
        backend, tmp_path / "destination.sqlite", count=count, source_publication=True
    ) as h:
        support, item = h.production, h.item
        source = support.offerings[0].producer._source
        assert source.requests == []
        await support.activate(account_id=item.config.account_id)
        turn = await source_turn(support)
        assert not turn.slices_failed and len(turn.revisions_committed) == 1
        revisions = await h.store.list_revisions(
            account_id=item.config.account_id,
            reporting_obligation_id=item.obligation.reporting_obligation_id,
        )
        assert len(revisions) == 1
        item.revision = revisions[0]
        assert item.revision.canonical_content_digest == reference_digest(item.verifier, item.rows)
        assert item.revision.managed_control_totals == item.verifier.canonicalize(item.rows)[1]
        assert len(source.requests) == 1
        repeat = await source_turn(support)
        assert not repeat.revisions_committed and len(source.requests) == 1
        result = await support.materializer.run_once()
        assert result.state == "verified"
        assert item.writer.writes == 1
        await drain(h.projection, item.config.account_id)
        page = await support.handler.get_reporting_status(
            {"account": {"account_id": item.config.account_id}, "view": "summary"},
            ToolContext(caller_identity=item.binding.consumer_id),
        )
        assert page["health"] == "complete"


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_actual_admission_verified_finish_and_private_polling(
    backend, notifications, tmp_path
):
    async with production_harness(
        backend, tmp_path / "destination.sqlite", notifications=notifications
    ) as h:
        support, item = h.production, h.item
        before = await support.reporting_delivery()
        assert bool(before) == (backend == "postgres")
        if before:
            assert before["managed_delivery"] is True
            assert "reconciled_billing" not in before
            assert "receipt_task" not in before
            assert "readiness_notification" not in before
            validator = get_named_validator("core/reporting-delivery-capabilities.json")
            assert validator is not None
            assert not list(validator.iter_errors(before))
        assert "sync_reporting_receipts" not in support.handler.advertised_tools_for_instance()
        assert await support.activate(account_id=item.config.account_id)
        lease = await item.claim()
        assert isinstance(lease, ReportingMaterializerLease)
        assert lease.admission_epoch == 2
        prepared, verified = await item.verified(lease)
        result = await h.store.finish_materialization(lease, prepared=prepared, verified=verified)
        assert result.state == "verified"
        assert item.writer.writes == 1
        assert item.writer.opens == item.writer.closes == 2
        assert not (await h.queue())[0]
        assert await h.store.read_materializer_boundaries(caller=item.binding.principal) == ()
        assert len(await h.store.read_production_boundaries(caller=item.binding.principal)) == 1
        if h.pool is None:
            queue = h.store._production_outbox
            count = len(queue.events) if queue is not None else 0
        else:
            async with h.pool.connection() as c:
                count = (
                    await (
                        await c.execute(
                            "SELECT count(*) FROM reporting_production_notification_events"
                        )
                    ).fetchone()
                )[0]
        assert count == int(notifications)
        await drain(h.projection, item.config.account_id)
        context = ToolContext(caller_identity=item.binding.consumer_id)
        response = await support.handler.get_reporting_status(
            {"account": {"account_id": item.config.account_id}, "view": "summary"}, context
        )
        assert response["health"] == "complete"
        rows = []
        request = {
            "account": {"account_id": item.config.account_id},
            "reporting_revision_id": item.revision.reporting_revision_id,
            "pagination": {"max_results": 100},
        }
        for _ in range(10):
            page = await support.handler.get_media_buy_delivery(request, context)
            assert isinstance(page, dict)
            validator = get_named_validator("media-buy/get-media-buy-delivery-response.json")
            assert validator is not None
            assert not list(validator.iter_errors(page))
            rows.extend(page["reporting_rows"])
            if not page["pagination"]["has_more"]:
                break
            assert len(page["pagination"]["cursor"]) <= 2048
            request["pagination"]["cursor"] = page["pagination"]["cursor"]
        else:
            pytest.fail("exact revision walk did not terminate")
        assert rows == item.rows
        repeat = await h.store.finish_materialization(lease, prepared=prepared, verified=verified)
        assert repeat.state == "verified"
        assert item.writer.writes == 1
        await support.aclose()
        assert await support.reporting_delivery() == {}
