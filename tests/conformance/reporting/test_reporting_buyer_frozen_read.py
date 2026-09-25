"""Buyer full reads against reviewed, authenticated in-memory seller mounts."""

from dataclasses import replace

import pytest

from adcp.reporting import (
    ExpectedReportingPeriod,
    ReportingReconciliationError,
    evaluate_reporting_ledger,
    load_reporting_ledger,
)
from adcp.types import GetReportingStatusRequest

from ._durable_materializer_support import durable_case
from ._feed_support import MountedFeed, feed_request, mixed_case, second_consumer
from ._projection_support import projection_harness
from .test_reporting_notification_outbox import statement


@pytest.fixture(autouse=True)
def _a2a_compat_send_and_aggregate():
    # These mounts require the real async-generator transport, not the unit mock shim.
    pass


@pytest.mark.parametrize("feedback", [False, True])
@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
async def test_public_loader_keeps_exact_consumer_evidence_with_maximum_url_principals(
    feedback, protocol
):
    prefix = "https://buyer.example/"
    consumer = prefix + "a" * (2048 - len(prefix))
    async with projection_harness("memory", feedback=feedback) as h:
        s, _, _ = await mixed_case(h, consumer_id=consumer)
        other = await second_consumer(h, s, consumer[:-1] + "b")
        # Current wire status includes its exact owner; the pure loader also
        # covers historical typed status with an omitted owner separately.
        own_status = replace(
            statement(s.obligation),
            consumer_id=consumer,
            consumer_status="received",
            reporting_revision_id=s.revision.reporting_revision_id,
            observed_revision_content_sha256=s.revision.revision_content_sha256,
        )
        await h.store.record_consumer_status_with_lifecycle(own_status)
        await h.projection.activate(account_id=s.obligation.account_id)
        mounted = MountedFeed(h, feedback=feedback)
        mounted.authorize(s)
        mounted.authorize(other, token="token-two")
        request = GetReportingStatusRequest.model_validate(feed_request(s, limit=1))
        async with mounted.sdk_clients("1.0") as (clients, observed):
            ledger = await load_reporting_ledger(clients[protocol], request)
            assert len(ledger.adjustment_receipts) == 1
            assert len(ledger.consumer_statuses) == 1
            assert ledger.revision_ownership == {
                s.revision.reporting_revision_id: s.obligation.reporting_obligation_id
            }
            assert all(params["pagination"]["max_results"] == 1 for _, _, params in observed)
            obligation = ledger.obligations[0]
            expected = ExpectedReportingPeriod(
                obligation.delivery_config_id,
                obligation.delivery_config_version,
                obligation.report_definition_id,
                obligation.feed_purpose.value,
                obligation.reporting_profile,
                tuple(b.root for b in obligation.media_buy_ids),
                obligation.period.start.isoformat(),
                obligation.period.end.isoformat(),
                obligation.period.source_timezone,
            )
            result = evaluate_reporting_ledger(ledger, expected_periods=[expected], now=h.clock())
            assert result.definitive, result.obligations
        async with mounted.sdk_clients("1.0", token="token-two") as (clients, _):
            other_ledger = await load_reporting_ledger(clients[protocol], request)
            assert other_ledger.adjustment_receipts == []
            assert other_ledger.consumer_statuses == []
            result = evaluate_reporting_ledger(
                other_ledger, expected_periods=[expected], now=h.clock()
            )
            assert not result.definitive
            assert "MISSING_MATCHING_ADJUSTMENT_RECEIPT" in result.obligations[0].reasons
        assert {consumer, other.binding.consumer_id} <= {
            principal for _, principal in mounted.auth_calls
        }


@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
async def test_revocation_between_pages_and_on_replay_never_produces_a_completed_ledger(protocol):
    async with projection_harness("memory") as h:
        s, _, _ = await mixed_case(h, consumer_id="https://buyer.example/authorized")
        await h.projection.activate(account_id=s.obligation.account_id)
        mounted = MountedFeed(h)
        mounted.authorize(s)
        request = GetReportingStatusRequest.model_validate(feed_request(s, limit=1))
        async with mounted.sdk_clients("1.0") as (clients, _):

            class RevokeAfterFirstPage:
                calls = 0

                async def get_reporting_status(self, request):
                    response = await clients[protocol].get_reporting_status(request)
                    self.calls += 1
                    if self.calls == 1:
                        mounted.grants.remove((s.obligation.account_id, s.binding.consumer_id))
                    return response

            client = RevokeAfterFirstPage()
            for _ in range(2):
                with pytest.raises(ReportingReconciliationError) as error:
                    await load_reporting_ledger(client, request)
                assert error.value.code == "STATUS_READ_FAILED"
                assert s.binding.consumer_id not in str(error.value)
                assert error.value.__context__ is None


@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
async def test_official_configuration_scope_keeps_a_current_snapshot_obligation(protocol):
    """The actual producer's scope describes required, not current, finality."""
    async with projection_harness("memory") as h:
        s = await durable_case(h.store, required="official", finality="snapshot", active=False)
        await h.projection.activate(account_id=s.obligation.account_id)
        mounted = MountedFeed(h)
        mounted.authorize(s)
        request = GetReportingStatusRequest.model_validate(
            feed_request(s, limit=1, finality=["official"])
        )
        async with mounted.sdk_clients("1.0") as (clients, observed):
            ledger = await load_reporting_ledger(clients[protocol], request)
            assert [value.value for value in ledger.scope.finality] == ["official"]
            assert len(ledger.obligations) == len(ledger.revisions) == 1
            assert ledger.obligations[0].required_finality.value == "official"
            assert ledger.revisions[0].finality.value == "snapshot"
            assert all("finality" not in params for _, _, params in observed)
            obligation = ledger.obligations[0]
            expected = ExpectedReportingPeriod(
                obligation.delivery_config_id,
                obligation.delivery_config_version,
                obligation.report_definition_id,
                obligation.feed_purpose.value,
                obligation.reporting_profile,
                tuple(b.root for b in obligation.media_buy_ids),
                obligation.period.start.isoformat(),
                obligation.period.end.isoformat(),
                obligation.period.source_timezone,
            )
            result = evaluate_reporting_ledger(ledger, expected_periods=[expected], now=h.clock())
            assert not result.definitive
            assert result.missing_expected_periods == []
            assert "FINALITY_NOT_MET" in result.obligations[0].reasons
            assert "UNVERIFIED_LEDGER_SNAPSHOT" not in result.obligations[0].reasons
