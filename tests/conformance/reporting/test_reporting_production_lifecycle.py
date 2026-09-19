"""Actual source/I/O, mounted financial receipts, private frozen reads and health cycles."""

import copy
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    ReportingAdjustmentRecord,
    ReportingControlTotalRecord,
    ReportingRevisionReceiptRecord,
    revision_content_sha256,
)
from adcp.reporting.ledger.delivery import adjustment_to_wire, receipt_to_wire
from adcp.reporting.materializer.work import ReportingMaterializerLease
from adcp.reporting.ownership import page_revision_ownership
from adcp.validation.schema_loader import get_validator

from ._generation_support import END
from ._production_support import production_harness
from ._production_transport import MountedProduction
from ._projection_support import drain
from ._receipt_transport import error_code
from .test_reporting_production_lock_order import source_turn
from .test_reporting_schedule_schema import assert_original_rejection


async def public_walk(mounted, client, request, *, first=None, transport="mcp"):
    request = copy.deepcopy(request)
    pages = []
    page = first
    for _ in range(40):
        if page is None:
            _, page = await mounted.call(
                client, "get_reporting_status", request, transport=transport
            )
        assert page.get("status") == "completed", page
        page_revision_ownership(page)
        pages.append(page)
        assert page["changes_checkpoint"] == pages[0]["changes_checkpoint"]
        if not page["pagination"]["has_more"]:
            return pages
        request["pagination"]["cursor"] = page["pagination"]["cursor"]
        page = None
    pytest.fail("production cursor walk exceeded its bound")


async def generation(h):
    account = h.item.config.account_id
    await drain(h.projection, account)
    values = [
        value.generation
        for value in await h.projection.checkpoints(account_id=account)
        if value.scope.consumer_id == h.item.binding.consumer_id
        and value.scope.reporting_obligation_id == h.item.obligation.reporting_obligation_id
    ]
    assert values
    return max(values)


async def observed_now(h):
    # PostgreSQL owns materialization/receipt commit time. The app fixture's
    # frozen clock predates I/O and is not a valid consumer observation time.
    if h.pool is None:
        return h.clock()
    async with h.pool.connection() as connection:
        return (await (await connection.execute("SELECT clock_timestamp()")).fetchone())[0]


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("feedback", [False, True])
async def test_production_official_receipts_adjustment_and_frozen_wire_cycle(
    backend, notifications, feedback, tmp_path
):
    async with production_harness(
        backend,
        tmp_path / "provider.sqlite",
        count=3,
        source_publication=True,
        reconciled=True,
        notifications=notifications,
        feedback=feedback,
    ) as h:
        support, item = h.production, h.item
        snapshot_id = "retained-unlinked-snapshot"
        old = replace(
            item.revision,
            reporting_revision_id=snapshot_id,
            finality="snapshot",
            finality_basis=None,
            finality_policy_id=None,
            finalized_at=None,
            revision_content_sha256=revision_content_sha256(
                reporting_revision_id=snapshot_id,
                row_count=len(item.rows),
                control_totals=item.revision.control_totals,
                reporting_rows=item.rows,
                control_total_evidence=item.revision.managed_control_totals,
            ),
        )
        await h.store.commit_revision(old, item.rows)
        mounted = MountedProduction(h)
        mounted.authorize(item)
        await support.activate(account_id=item.config.account_id)
        async with mounted.client() as client:
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, caps = await mounted.call(
                    client, "get_adcp_capabilities", {}, transport=transport
                )
                assert caps.get("status") == "completed", caps
                claims = caps.get("media_buy", {}).get("reporting_delivery", {})
                if backend == "postgres":
                    assert claims["managed_delivery"] and claims["reconciled_billing"]
                    assert claims["receipt_task"] == "sync_reporting_receipts"
                    # Complete polling requires no optional HTTP delivery worker.
                    assert "readiness_notification" not in claims
                else:
                    assert not claims
            selected = await item.claim()
            assert not isinstance(selected, ReportingMaterializerLease)
            assert item.writer.writes == 0  # never falls back to the retained snapshot
            source_result = await source_turn(support)
            assert len(source_result.revisions_committed) == 1 and not source_result.slices_failed
            revisions = await h.store.list_revisions(
                account_id=item.config.account_id,
                reporting_obligation_id=item.obligation.reporting_obligation_id,
            )
            official = next(r for r in revisions if r.finality == "official")
            assert {r.reporting_revision_id for r in revisions} == {
                snapshot_id,
                official.reporting_revision_id,
            }
            item.revision = official
            lease = await item.claim()
            assert isinstance(lease, ReportingMaterializerLease)
            assert lease.attempt.reporting_revision_id == official.reporting_revision_id
            prepared, verified = await item.verified(lease)
            assert (
                await h.store.finish_materialization(lease, prepared=prepared, verified=verified)
            ).state == "verified"
            before_receipts = await generation(h)
            receipt = ReportingRevisionReceiptRecord(
                item.scope,
                "production-rejected",
                official.reporting_revision_id,
                lease.attempt.reporting_materialization_id,
                "rejected",
                item.binding.verification_profile,
                official.row_count,
                official.managed_control_totals,
                await observed_now(h),
                observed_canonical_content_digest=official.canonical_content_digest,
                rejection_codes=("LOAD_FAILED",),
            )
            request = {
                "account": {"account_id": item.config.account_id},
                "idempotency_key": "production-rejected-0001",
                "receipts": [receipt_to_wire(receipt)],
            }
            _, rejected = await mounted.call(client, "sync_reporting_receipts", request)
            assert rejected["results"][0]["result"] == "recorded", rejected
            after_rejection = await generation(h)
            assert after_rejection > before_receipts
            assert not isinstance(await item.claim(), ReportingMaterializerLease)
            assert item.writer.writes == 1  # consumer rejection cannot schedule a retry
            feed_request = {
                "account": request["account"],
                "view": "periods",
                "pagination": {"max_results": 1},
            }
            frozen = await public_walk(mounted, client, feed_request)
            accepted = replace(
                receipt,
                reporting_receipt_id="production-accepted",
                status="accepted",
                supersedes_reporting_receipt_id=receipt.reporting_receipt_id,
                rejection_codes=(),
                observed_at=await observed_now(h),
            )
            request = {
                **request,
                "idempotency_key": "production-accepted-0001",
                "receipts": [receipt_to_wire(accepted)],
            }
            _, response = await mounted.call(
                client, "sync_reporting_receipts", request, transport="a2a-1.0"
            )
            assert response["results"][0]["result"] == "recorded", response
            accepted_generation = await generation(h)
            assert accepted_generation > after_rejection

            async def summary():
                _, value = await mounted.call(
                    client,
                    "get_reporting_status",
                    {"account": request["account"], "view": "summary"},
                )
                assert value.get("status") == "completed", value
                return value

            assert (await summary())["health"] == "complete"
            observed = await observed_now(h)
            adjustment = ReportingAdjustmentRecord(
                "production-adjustment",
                item.config.account_id,
                official.reporting_revision_id,
                "source_correction",
                END,
                END + timedelta(days=30),
                (("spend", "-0.50"),),
                observed,
                observed,
                managed_control_total_deltas=(
                    ReportingControlTotalRecord("spend", "-0.50", "decimal", "USD"),
                ),
            )
            await h.store.commit_adjustment(adjustment)
            pending_generation = await generation(h)
            assert pending_generation > accepted_generation
            assert (await summary())["health"] != "complete"
            replacement = replace(
                accepted,
                reporting_receipt_id="forbidden-accepted-replacement",
                supersedes_reporting_receipt_id=accepted.reporting_receipt_id,
            )
            mixed = {
                "account": request["account"],
                "idempotency_key": "production-mixed-final-0001",
                "receipts": [receipt_to_wire(replacement)],
                "adjustment_receipts": [
                    {
                        "reporting_receipt_id": "production-adjustment-accepted",
                        "reporting_adjustment_id": adjustment.reporting_adjustment_id,
                        "adjusts_reporting_revision_id": official.reporting_revision_id,
                        "status": "accepted",
                        "observed_adjustment_sha256": adjustment_to_wire(adjustment)[
                            "canonical_adjustment_sha256"
                        ],
                        "observed_at": (await observed_now(h)).isoformat(),
                    }
                ],
            }
            _, mixed_response = await mounted.call(
                client, "sync_reporting_receipts", mixed, transport="a2a-0.3"
            )
            assert mixed_response["results"][0]["reporting_receipt_id"] == (
                replacement.reporting_receipt_id
            )
            assert mixed_response["results"][0]["errors"][0]["code"] == (
                "ACCEPTED_RECEIPT_TERMINAL"
            )
            assert mixed_response["results"][1]["result"] == "recorded", mixed_response
            complete_generation = await generation(h)
            assert complete_generation > pending_generation
            assert (await summary())["health"] == "complete"
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, complete = await mounted.call(
                    client,
                    "get_reporting_status",
                    {"account": request["account"], "view": "summary"},
                    transport=transport,
                )
                assert complete["health"] == "complete"
                assert "next_expected_at" in complete
                assert_original_rejection(complete)
                get_validator("get_reporting_status", "sync").validate(complete)
                _, replay = await mounted.call(
                    client, "sync_reporting_receipts", mixed, transport=transport
                )
                assert replay == mixed_response
                assert await generation(h) == complete_generation
                assert (
                    await public_walk(
                        mounted, client, feed_request, first=frozen[0], transport=transport
                    )
                    == frozen
                )
                _, exact = await mounted.call(
                    client,
                    "get_media_buy_delivery",
                    {
                        "account": request["account"],
                        "reporting_revision_id": official.reporting_revision_id,
                    },
                    transport=transport,
                )
                assert exact["reporting_rows"] == item.rows, exact
                assert page_revision_ownership(exact) == {
                    official.reporting_revision_id: item.obligation.reporting_obligation_id
                }
            assert item.writer.writes == 1
            assert not (await h.queue())[0]  # epoch-zero readiness stays quarantined
            h.authorized_bindings.clear()
            _, denied = await mounted.call(client, "sync_reporting_receipts", mixed)
            assert error_code(denied) == "UNAUTHORIZED"
