"""Structural readiness, complete account surfaces and explicitly mounted wire claims."""

import json
from dataclasses import replace
from itertools import product

import pytest

from adcp.reporting.ledger import InMemoryReportingLedgerStore
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_projection import mismatch_key
from adcp.reporting.ledger.status_server import ReportingStatusNotificationHandler
from adcp.reporting.outbox import (
    PgReportingActivityUnionStore,
    ReportingActivityProjector,
    ReportingActivitySupport,
    ReportingNotificationError,
    ReportingStatusProjector,
    ReportingStatusService,
    ReportingStatusSupport,
    ReportingStatusSweeper,
)
from adcp.reporting.outbox.status_support import validate_status_claims
from adcp.server.a2a_server import ADCPAgentExecutor
from adcp.server.base import ToolContext
from adcp.server.mcp_tools import create_tool_caller, get_tools_for_handler
from adcp.types import NotificationConfig, ReportingDeliveryCapabilities
from adcp.validation.schema_loader import get_named_validator

from . import test_reporting_status_projection_contract as _contract
from ._generation_support import configuration
from ._reliable_support import NotificationHarness, notification_subscription
from .test_reporting_notification_outbox import statement
from .test_reporting_status_delivery import drain_http, status_worker
from .test_reporting_status_wire_contract import validate_read

status_harness = _contract.status_harness


async def support_for(h, accounts=("acct_a",)):
    n = NotificationHarness(h.reliable)
    probes = tuple(
        replace(
            notification_subscription(), account_id=a, event_types=("reporting.status_changed",)
        )
        for a in accounts
    )
    for subscription in probes:
        n.subscriptions.put(subscription)

    async def resolve(request, context):
        if context is None or context.caller_identity not in {"buyer", "auditor"}:
            raise ReportingNotificationError("status_identity_required")
        return ReportingStatusCaller("acct_a", context.caller_identity)

    handler = ReportingStatusNotificationHandler(
        ReportingStatusHandler(h.ledger, consumer_status_enabled=True), resolve_caller=resolve
    )
    return n, ReportingStatusSupport(
        h.status,
        status_worker(h, n),
        ReportingStatusProjector(h.status),
        ReportingStatusSweeper(h.status),
        True,
        accounts,
        True,
        handler,
        probes,
    )


async def test_all_visible_accounts_must_be_baselined_and_body_context_cannot_select_one(
    status_harness,
):
    h = status_harness
    await h.seed(readable=True)
    await h.ledger.put_configuration(configuration("acct_b"))
    _, support = await support_for(h, ("acct_a", "acct_b"))
    await h.status.baseline(account_id="acct_a")
    assert not await support.durable()
    claim = {
        "media_buy": {
            "reporting_delivery": {
                "status_task": "get_reporting_status",
                "status_notification": "reporting.status_changed",
            }
        },
        "context": {"account_id": "acct_a"},
    }
    with pytest.raises(ReportingNotificationError):
        await validate_status_claims(claim, support=support)
    await h.status.baseline(account_id="acct_b")
    assert await support.durable() is (not isinstance(h.ledger, InMemoryReportingLedgerStore))
    assert not await replace(support, account_surface_complete=False).durable()


async def test_waiver_recovers_without_withdrawing_capability_and_agreement_rearms(status_harness):
    h = status_harness
    obligation, revision, _ = await h.seed(readable=True)
    bad = replace(
        statement(obligation),
        consumer_status="unreadable",
        failure_code="transport_failed",
        reporting_revision_id=revision.reporting_revision_id,
    )
    await h.ledger.record_consumer_status(bad)
    await h.status.baseline(account_id="acct_a")
    _, support = await support_for(h)
    advertised = await support.advertised_notifications()
    assert bool(advertised) is (not isinstance(h.ledger, InMemoryReportingLedgerStore))
    issue = await h.ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(bad))
    await h.ledger.set_issue_state(
        account_id="acct_a", issue_key=issue.issue_key, state="waived", at=h.clock()
    )
    await h.drain()
    assert await support.advertised_notifications() == advertised
    context = ToolContext(caller_identity="buyer")
    response = await support.handler.get_reporting_status({"view": "summary"}, context)
    validate_read(response)
    assert response["health"] == "complete" and response["issues"] == []
    first = (await h.events(consumer="buyer"))[-1]
    assert first.cause.health == "complete" and first.cause.issue_ids == ()
    await h.ledger.record_consumer_status(
        replace(
            bad,
            reporting_status_id="consumer-status-agree",
            consumer_status="received",
            supersedes_reporting_status_id=bad.reporting_status_id,
            failure_code=None,
            observed_revision_content_sha256=revision.revision_content_sha256,
        )
    )
    await h.ledger.record_consumer_status(
        replace(
            bad,
            reporting_status_id="consumer-status-recur",
            supersedes_reporting_status_id="consumer-status-agree",
        )
    )
    await h.drain()
    response = await support.handler.get_reporting_status({}, context)
    validate_read(response)
    last = (await h.events(consumer="buyer"))[-1]
    assert last.notification_id != first.notification_id
    assert last.cause.issue_ids != (issue.issue_id,)
    assert last.cause.issue_ids == tuple(sorted(i["issue_id"] for i in response["issues"]))
    assert await support.advertised_notifications() == advertised


async def test_status_and_b_c_activity_cartesian_matrix_preserves_original_b_gate(
    status_harness, monkeypatch
):
    h = status_harness
    if h.reliable.blobs.pool is None:
        pytest.skip("concrete PostgreSQL composite readiness")
    _, revision, _ = await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    n, base = await support_for(h)
    b_worker = n.worker()
    union = PgReportingActivityUnionStore(b_worker.outbox, h.status.outbox)
    activity = ReportingActivityProjector(union)
    # Orthogonal structural components; no business health is used to toggle readiness.
    for status_on, b_activity, c_activity, listing in product((False, True), repeat=4):
        b_worker.activity = b_worker.outbox if b_activity else None
        base.worker.activity = base.worker.outbox if c_activity else None
        support = replace(base, projector=base.projector if status_on else None)
        assert await support.durable() is status_on
        composed = ReportingActivitySupport(b_worker, h.ledger, activity, support)
        try:
            flags = await composed.capability_flags(account_activity=activity if listing else None)
        except ReportingNotificationError:
            assert not (b_activity and c_activity)
            flags = {"reporting": False, "account_notifications": False}
        assert flags == {
            "reporting": b_activity and c_activity,
            "account_notifications": b_activity and c_activity and listing,
        }
    b_worker.activity = b_worker.outbox
    assert await ReportingActivitySupport(
        b_worker, h.ledger, ReportingActivityProjector(b_worker.outbox)
    ).durable()
    async with h.reliable.blobs.pool.connection() as connection:
        await connection.execute(
            "ALTER TABLE reporting_webhook_attempts DISABLE TRIGGER reporting_webhook_attempt_guard"
        )
    assert await base.durable()  # C logging does not require B activity storage.
    with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
        await ReportingActivitySupport(
            b_worker, h.ledger, ReportingActivityProjector(b_worker.outbox)
        ).durable()
    async with h.reliable.blobs.pool.connection() as connection:
        await connection.execute(
            "ALTER TABLE reporting_webhook_attempts ENABLE TRIGGER reporting_webhook_attempt_guard"
        )
    # A mounted attempt recorder is part of this worker's HTTP path. Status
    # remains independent of activity when that optional recorder is disabled.
    async with h.reliable.blobs.pool.connection() as connection:
        await connection.execute("DROP TABLE reporting_status_webhook_attempts")
    base.worker.activity = base.worker.outbox
    assert not await base.durable()
    base.worker.activity = None
    assert await base.durable()
    assert await ReportingActivitySupport(
        b_worker, h.ledger, ReportingActivityProjector(b_worker.outbox)
    ).durable()
    n.receiver.install(monkeypatch)
    await h.ledger.set_revision_readable(
        account_id="acct_a", reporting_revision_id=revision.reporting_revision_id, readable=False
    )
    await h.drain()
    await drain_http(h, base.worker)
    assert len(n.receiver.received) == 2


async def test_authoritative_task_is_callable_through_mcp_and_a2a_with_authenticated_isolation(
    status_harness,
):
    h = status_harness
    obligation, revision, _ = await h.seed(readable=True)
    await h.ledger.record_consumer_status(
        replace(
            statement(obligation),
            consumer_status="unreadable",
            reporting_revision_id=revision.reporting_revision_id,
            failure_code="transport_failed",
        )
    )
    _, support = await support_for(h)
    handler = support.handler
    assert "get_reporting_status" in {t["name"] for t in get_tools_for_handler(handler)}
    assert "sync_reporting_receipts" not in {t["name"] for t in get_tools_for_handler(handler)}
    executor = ADCPAgentExecutor(handler, validation=None)
    for caller in (
        create_tool_caller(handler, "get_reporting_status"),
        executor._tool_callers["get_reporting_status"],
    ):
        buyer = await caller(
            {
                "account": {"account_id": "acct_a"},
                "view": "summary",
                "context": {"consumer_id": "auditor"},
            },
            ToolContext(caller_identity="buyer"),
        )
        auditor = await caller(
            {
                "account": {"account_id": "acct_a"},
                "view": "summary",
                "context": {"consumer_id": "buyer"},
            },
            ToolContext(caller_identity="auditor"),
        )
        validate_read(buyer)
        validate_read(auditor)
        assert buyer["health"] == "action_required" and auditor["health"] == "complete"
        assert auditor["issues"] == []


@pytest.mark.parametrize("tier", ["core", "managed", "reconciled"])
def test_raw_generated_capability_dumps_omit_unset_task_notification_and_false_flags(tier):
    model = ReportingDeliveryCapabilities.model_validate(
        {
            "supported": True,
            "offerings": [
                {
                    "offering_id": "hourly",
                    "feed_purpose": "analytics",
                    "report_definition_id": "definition",
                    "report_definition_uri": "https://seller.example/definition",
                    "report_definition_sha256": "a" * 64,
                    "reporting_profile": {
                        "id": "profile",
                        "version": "1",
                        "schema_uri": "https://seller.example/schema",
                        "schema_sha256": "b" * 64,
                        "grain": "media_buy",
                        "primary_keys": ["media_buy_id"],
                    },
                    "schedule": {
                        "period_duration": "PT1H",
                        "alignment": "utc",
                        "delivery_sla": "PT1H",
                    },
                    "supported_finality": ["snapshot"],
                    "reconciliation_mode": "delivery_only",
                }
            ],
            "automated_recovery_window_seconds": 0,
            "status_retention_days": 7,
            **({"managed_delivery": True} if tier in {"managed", "reconciled"} else {}),
            **({"reconciled_billing": True} if tier == "reconciled" else {}),
        }
    )
    fields = set(model.model_fields_set)
    before = model.status_notification
    for raw in (model.model_dump(mode="json"), json.loads(model.model_dump_json())):
        assert not any(k.endswith(("_task", "_notification")) for k in raw)
        assert "supports_webhook_activity" not in raw and "reliable_reporting_version" not in raw
        assert ("managed_delivery" in raw) is (tier != "core")
        assert ("reconciled_billing" in raw) is (tier == "reconciled")
    assert model.model_fields_set == fields and model.status_notification == before


def test_reporting_only_notification_config_omits_product_default_without_mutation():
    config = NotificationConfig.model_validate(
        {
            "subscriber_id": "status",
            "url": "https://buyer.example/status",
            "event_types": ["reporting.status_changed"],
        }
    )
    validator = get_named_validator("core/notification-config.json")
    for raw in (config.model_dump(mode="json"), json.loads(config.model_dump_json())):
        assert "product_payload_view" not in raw
        assert not list(validator.iter_errors(raw))
    assert "product_payload_view" not in config.model_fields_set
    products = NotificationConfig.model_validate(
        {
            "subscriber_id": "products",
            "url": "https://buyer.example/products",
            "event_types": ["product.updated"],
        }
    )
    assert products.model_dump(mode="json")["product_payload_view"] == "legacy"


def test_adopter_subclasses_and_nested_wire_models_keep_the_explicit_field_allowlist():
    from pydantic import BaseModel

    from tests.test_reporting_ledger import _OFFERING

    class Registration(NotificationConfig):
        pass

    class Reporting(ReportingDeliveryCapabilities):
        pass

    class Envelope(BaseModel):
        registration: Registration
        reporting: Reporting

    value = Envelope.model_validate(
        {
            "registration": {
                "subscriber_id": "status",
                "url": "https://buyer.example/status",
                "event_types": ["reporting.status_changed"],
            },
            "reporting": {
                "supported": True,
                "offerings": [_OFFERING],
                "automated_recovery_window_seconds": 0,
                "status_retention_days": 30,
            },
        }
    )
    fields = set(value.reporting.model_fields_set)
    for wire in (value.model_dump(mode="json"), json.loads(value.model_dump_json())):
        assert "product_payload_view" not in wire["registration"]
        assert not any(k.endswith(("_task", "_notification")) for k in wire["reporting"])
    assert value.reporting.model_fields_set == fields


async def test_optional_service_turn_order_and_close(status_harness, monkeypatch):
    h = status_harness
    _, support = await support_for(h)
    service = ReportingStatusService(support)
    await service.migrate()
    await h.seed(readable=True)
    await service.baseline()
    assert await service.ready() is (not isinstance(h.ledger, InMemoryReportingLedgerStore))
    assert not (await service.project_dirty_once(account_id="acct_a")).did_work
    assert not (await service.sweep_due_once(account_id="acct_a")).did_work
    assert not await service.expand_once(account_id="acct_a")
    assert not await service.deliver_once(account_id="acct_a")
    assert await service.drain() == 0
    await service.aclose()
    assert not await service.ready()
    with pytest.raises(ReportingNotificationError, match="status_service_closed"):
        await service.project_dirty_once(account_id="acct_a")


async def test_managed_destination_scopes_wait_for_d_clock_semantics(status_harness):
    from ._reconciliation_support import scenario

    h = status_harness
    await scenario(h.ledger)
    await h.status.baseline(account_id="acct_a")
    _, support = await support_for(h)
    assert await h.status.baseline_ready(account_id="acct_a")
    assert await support.advertised_notifications() == {}
    with pytest.raises(
        ReportingNotificationError, match="status_capability_requires_durable_reporting"
    ):
        await validate_status_claims(
            {
                "media_buy": {
                    "reporting_delivery": {
                        "status_task": "get_reporting_status",
                        "status_notification": "reporting.status_changed",
                    }
                }
            },
            support=support,
        )
