"""Production projection inputs preserve the exact-scoped rc.6 waiver contract."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger.status_projection import mismatch_key

from ._projection_support import drain, inputs, projection_harness
from ._receipt_support import receipt_case
from .test_reporting_notification_outbox import statement


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_projection_captures_exact_waiver_and_rearms_later_statement(backend, notifications):
    async with projection_harness(backend, notifications=notifications, feedback=True) as h:
        scenario = await receipt_case(
            h, finality="snapshot", billing=False, reconciliation_mode="delivery_only"
        )
        original = replace(
            statement(scenario.obligation, consumer=scenario.binding.consumer_id),
            consumer_status="unreadable",
            failure_code="access_denied",
        )
        await h.store.record_consumer_status(original)
        await h.projection.activate(account_id=scenario.obligation.account_id)
        request = {"view": "summary", "adcp_version": "3.2-rc.6"}
        before = await h.store.read_tier_status(
            request, caller=scenario.binding.principal, consumer_status_enabled=True
        )
        assert before["health"] == "action_required"

        waived = await h.store.set_issue_state(
            issue_key=mismatch_key(original),
            account_id=scenario.obligation.account_id,
            state="waived",
            at=h.clock(),
            external_ref="private-bilateral-audit",
        )
        await drain(h.projection, scenario.obligation.account_id)
        recovered = await h.store.read_tier_status(
            request, caller=scenario.binding.principal, consumer_status_enabled=True
        )
        assert recovered["health"] == "complete" and recovered["issues"] == []
        captured = await inputs(h, scenario.obligation.account_id)
        assert any(
            row.issue_id == waived.issue_id and row.waived_conflict_sha256
            for row in captured[-1].core.lifecycles
        )

        h.clock.now += timedelta(seconds=1)
        later = replace(
            original,
            reporting_status_id="next-mismatch",
            supersedes_reporting_status_id=original.reporting_status_id,
            recorded_at=h.clock(),
            status_as_of=h.clock(),
        )
        await h.store.record_consumer_status(later)
        await drain(h.projection, scenario.obligation.account_id)
        current = await h.store.read_tier_status(
            request, caller=scenario.binding.principal, consumer_status_enabled=True
        )
        assert current["health"] == "action_required"
        assert current["issues"][0]["reporting_status_id"] == later.reporting_status_id
        assert current["issues"][0]["issue_id"] != waived.issue_id
        retained = await h.store.get_issue(
            account_id=scenario.obligation.account_id, issue_key=waived.issue_key
        )
        assert retained == waived
