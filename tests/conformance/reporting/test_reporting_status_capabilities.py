"""Structural readiness, complete account surfaces and explicitly mounted wire claims."""

import asyncio
import json
from dataclasses import FrozenInstanceError, replace
from itertools import product

import pytest
from adcp_reporting_storage import PgReportingActivityUnionStore

from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform, SingletonAccounts
from adcp.decisioning.capabilities import Account, MediaBuy
from adcp.decisioning.serve import create_adcp_server_from_platform
from adcp.exceptions import ADCPTaskError
from adcp.reporting.ledger import InMemoryReportingLedgerStore, ReportingDeliveryEscalation
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_projection import mismatch_key
from adcp.reporting.ledger.status_server import ReportingStatusNotificationHandler
from adcp.reporting.outbox import (
    ReportingActivityProjector,
    ReportingActivitySupport,
    ReportingNotificationError,
    ReportingStatusAccountSurface,
    ReportingStatusProjector,
    ReportingStatusService,
    ReportingStatusSupport,
    ReportingStatusSweeper,
)
from adcp.reporting.outbox.status_support import validate_status_claims
from adcp.reporting.service import ReliableReportingService
from adcp.server.a2a_server import ADCPAgentExecutor
from adcp.server.auth import current_principal
from adcp.server.base import ToolContext
from adcp.server.mcp_tools import create_tool_caller, get_tools_for_handler
from adcp.types import NotificationConfig, ReportingDeliveryCapabilities
from adcp.validation.schema_loader import get_named_validator
from tests.test_reliable_reporting_service import _account_context
from tests.test_reporting_ledger import _OFFERING

from . import test_reporting_status_projection_contract as _contract
from ._generation_support import configuration
from ._reliable_support import NotificationHarness, notification_subscription
from .test_reporting_notification_outbox import statement
from .test_reporting_status_delivery import drain_http, status_worker
from .test_reporting_status_wire_contract import validate_read

status_harness = _contract.status_harness


class ReportingPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        supported_protocols=["media_buy"], account=Account(supported_billing=["agent"])
    )
    accounts = SingletonAccounts(account_id="acct_a")


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


async def test_static_lifecycle_default_iterates_without_advertising_status(
    status_harness, monkeypatch
):
    h = status_harness
    _, revision, _ = await h.seed(readable=True)
    notifications, static = await support_for(h)
    notifications.receiver.install(monkeypatch)
    support = ReportingStatusSupport(
        store=static.store,
        worker=static.worker,
        projector=static.projector,
        sweeper=static.sweeper,
        scheduled=True,
        account_ids=("acct_a",),
        handler=static.handler,
        readiness_subscriptions=static.readiness_subscriptions,
    )
    service = ReportingStatusService(support)
    await service.baseline()
    assert await h.status.baseline_ready(account_id="acct_a")
    assert not await service.ready()
    assert await support.advertised_notifications() == {}
    claim = {
        "media_buy": {
            "reporting_delivery": {
                "status_task": "get_reporting_status",
                "status_notification": "reporting.status_changed",
            }
        }
    }
    with pytest.raises(
        ReportingNotificationError, match="^status_capability_requires_durable_reporting$"
    ):
        await validate_status_claims(claim, support=support)
    await h.ledger.set_revision_readable(
        account_id="acct_a", reporting_revision_id=revision.reporting_revision_id, readable=False
    )
    await service.drain()
    assert [event.cause.health for event in await h.events()] == ["action_required"]
    assert len(notifications.receiver.received) == 2
    assert not await service.ready()


@pytest.mark.parametrize("mismatch", [None, "ledger", "escalation", "consumer_status"])
async def test_generated_handler_proves_installed_reporting_configuration(status_harness, mismatch):
    h = status_harness
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        pytest.skip("durability advertisement requires PostgreSQL")
    escalation = ReportingDeliveryEscalation()
    h.status = type(h.status)(h.ledger, escalation=escalation)
    await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    _, static = await support_for(h)
    support = replace(static, handler=None)
    service = ReliableReportingService(
        store=InMemoryReportingLedgerStore() if mismatch == "ledger" else h.ledger,
        account_context=_account_context,
        consumer_status_enabled=mismatch != "consumer_status",
        escalation=(
            ReportingDeliveryEscalation(operations_contact_email="ops@example.com")
            if mismatch == "escalation"
            else escalation
        ),
    )
    platform = service.install(ReportingPlatform())
    handler, executor, _ = create_adcp_server_from_platform(
        platform, reporting_status=support, validate_at_init=False
    )
    try:
        assert type(handler.reporting_status_handler) is ReportingStatusHandler
        assert await support.durable() is (mismatch is None)
    finally:
        executor.shutdown(wait=True)
        await service.close()


@pytest.mark.parametrize("status_support", [False, True])
@pytest.mark.parametrize("mounted_version", ["3.1", "3.2"])
async def test_generated_status_version_straddle_requires_explicit_support(
    status_harness, status_support, mounted_version
):
    h = status_harness
    escalation = ReportingDeliveryEscalation()
    h.status = type(h.status)(h.ledger, escalation=escalation)
    await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    _, static = await support_for(h)
    support = replace(static, handler=None)
    service = ReliableReportingService(
        store=h.ledger,
        account_context=_account_context,
        consumer_status_enabled=True,
        escalation=escalation,
    )
    platform = service.install(ReportingPlatform())
    handler, executor, _ = create_adcp_server_from_platform(
        platform,
        adcp_version=mounted_version,
        reporting_status=support if status_support else None,
        validate_at_init=False,
    )
    token = current_principal.set("buyer")
    try:
        await service.start()
        caller = create_tool_caller(
            handler,
            "get_reporting_status",
            validation=None,
            default_unnegotiated_adcp_version=handler.get_adcp_version(),
        )
        request = {
            "adcp_version": "3.2",
            "account": {"account_id": "acct_a"},
            "view": "summary",
        }
        if status_support:
            assert await support.durable() is (
                not isinstance(h.ledger, InMemoryReportingLedgerStore)
            )
        if status_support and mounted_version == "3.1":
            with pytest.raises(ADCPTaskError) as exc:
                await caller(request)
            assert exc.value.errors[0].code == "VERSION_UNSUPPORTED"
            assert exc.value.errors[0].details == {"mounted_version": "3.1"}
        else:
            result = await caller(request)
            assert result["health"] == "complete"
    finally:
        current_principal.reset(token)
        executor.shutdown(wait=True)
        await service.close()


@pytest.mark.parametrize("selector", ["body", "ext", "metadata", "combined"])
async def test_generated_capabilities_cannot_narrow_dynamic_account_surface(
    status_harness, selector
):
    h = status_harness
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        pytest.skip("durability advertisement requires PostgreSQL")
    escalation = ReportingDeliveryEscalation()
    h.status = type(h.status)(h.ledger, escalation=escalation)
    await h.seed(readable=True)
    await h.ledger.put_configuration(configuration("acct_b"))
    await h.status.baseline(account_id="acct_a")
    _, static = await support_for(h, ("acct_a", "acct_b"))
    surface = ReportingStatusAccountSurface(
        account_ids=("acct_a", "acct_b"),
        account_surface_complete=True,
        readiness_subscriptions=static.readiness_subscriptions,
    )

    async def resolve_surface() -> ReportingStatusAccountSurface:
        return surface

    support = replace(
        static,
        handler=None,
        account_ids=("acct_a",),
        readiness_subscriptions=static.readiness_subscriptions[:1],
        account_surface_resolver=resolve_surface,
    )
    platform = ReportingPlatform()
    platform.capabilities = replace(
        platform.capabilities,
        media_buy=MediaBuy(
            reporting_delivery=ReportingDeliveryCapabilities.model_validate(
                {
                    "supported": True,
                    "offerings": [_OFFERING],
                    "automated_recovery_window_seconds": 0,
                    "status_retention_days": 30,
                    "status_task": "get_reporting_status",
                    "status_notification": "reporting.status_changed",
                }
            )
        ),
    )
    service = ReliableReportingService(
        store=h.ledger,
        account_context=_account_context,
        consumer_status_enabled=True,
        escalation=escalation,
    )
    handler, executor, _ = create_adcp_server_from_platform(
        service.install(platform), reporting_status=support, validate_at_init=False
    )
    request = {}
    context = ToolContext()
    if selector in {"body", "combined"}:
        request.update({"account_id": "acct_a", "account": {"account_id": "acct_a"}})
    if selector in {"ext", "combined"}:
        request["ext"] = {"account_id": "acct_a"}
    if selector in {"metadata", "combined"}:
        context.metadata["account_id"] = "acct_a"
    try:
        with pytest.raises(
            ReportingNotificationError, match="^status_capability_requires_durable_reporting$"
        ):
            await handler.get_adcp_capabilities(request, context)
        await h.status.baseline(account_id="acct_b")
        capabilities = await handler.get_adcp_capabilities(request, context)
        assert (
            capabilities["media_buy"]["reporting_delivery"]["status_notification"]
            == "reporting.status_changed"
        )
    finally:
        executor.shutdown(wait=True)
        await service.close()


# Catches a stale constructor surface after an account is enrolled.
async def test_dynamic_surface_discovers_account_without_restart(status_harness):
    h = status_harness
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        pytest.skip("durability advertisement requires PostgreSQL")
    await h.seed(readable=True)
    await h.ledger.put_configuration(configuration("acct_b"))
    _, static = await support_for(h, ("acct_a", "acct_b"))
    surfaces = [
        ReportingStatusAccountSurface(
            account_ids=("acct_a",),
            account_surface_complete=True,
            readiness_subscriptions=(static.readiness_subscriptions[0],),
        )
    ]

    async def resolve_surface() -> ReportingStatusAccountSurface:
        return surfaces[0]

    support = replace(
        static,
        account_ids=(),
        account_surface_complete=False,
        readiness_subscriptions=(),
        account_surface_resolver=resolve_surface,
    )
    await h.status.baseline(account_id="acct_a")
    assert await support.durable()

    surfaces[0] = ReportingStatusAccountSurface(
        account_ids=("acct_a", "acct_b"),
        account_surface_complete=True,
        readiness_subscriptions=static.readiness_subscriptions,
    )
    assert not await support.durable()
    await h.status.baseline(account_id="acct_b")
    assert await support.durable()


# Catches stale constructor evidence being merged into an incomplete resolver surface.
@pytest.mark.parametrize("missing", ["complete", "accounts", "account_id", "probes"])
async def test_dynamic_surface_requires_complete_nonempty_evidence(status_harness, missing):
    h = status_harness
    await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    _, static = await support_for(h)
    surface = ReportingStatusAccountSurface(
        account_ids=(
            () if missing == "accounts" else (("",) if missing == "account_id" else ("acct_a",))
        ),
        account_surface_complete=missing != "complete",
        readiness_subscriptions=() if missing == "probes" else static.readiness_subscriptions,
    )

    async def resolve_surface() -> ReportingStatusAccountSurface:
        return surface

    support = replace(static, account_surface_resolver=resolve_surface)
    assert await support.advertised_notifications() == {}


# Catches resolver exception propagation through capability advertisement and validation.
async def test_dynamic_surface_failure_omits_claim_and_refuses_explicit_claim(status_harness):
    _, static = await support_for(status_harness)

    async def resolve_surface() -> ReportingStatusAccountSurface:
        raise RuntimeError("registry unavailable")

    support = replace(static, account_surface_resolver=resolve_surface)
    assert await support.advertised_notifications() == {}
    claim = {
        "media_buy": {
            "reporting_delivery": {
                "status_task": "get_reporting_status",
                "status_notification": "reporting.status_changed",
            }
        }
    }
    with pytest.raises(
        ReportingNotificationError, match="^status_capability_requires_durable_reporting$"
    ):
        await validate_status_claims(claim, support=support)
    with pytest.raises(
        ReportingNotificationError, match="^status_account_surface_unavailable$"
    ) as exc:
        await support.resolve_account_surface()
    assert isinstance(exc.value.__cause__, RuntimeError)


# Catches resolver exception details escaping into the readiness diagnostic.
async def test_dynamic_surface_failure_logging_omits_resolver_details(status_harness, caplog):
    _, static = await support_for(status_harness)
    failure = RuntimeError("account-private-sentinel subscription-private-sentinel")

    async def resolve_surface() -> ReportingStatusAccountSurface:
        raise failure

    support = replace(static, account_surface_resolver=resolve_surface)
    with pytest.raises(
        ReportingNotificationError, match="^status_account_surface_unavailable$"
    ) as exc:
        await support.resolve_account_surface()

    assert exc.value.__cause__ is failure
    assert "status_account_surface_unavailable" in caplog.text
    assert "account-private-sentinel" not in caplog.text
    assert "subscription-private-sentinel" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


# Catches invalid resolver return values being accepted as readiness evidence.
async def test_dynamic_surface_rejects_invalid_return_type(status_harness):
    _, static = await support_for(status_harness)

    async def resolve_surface():
        return {"account_ids": ("acct_a",), "account_surface_complete": True}

    support = replace(static, account_surface_resolver=resolve_surface)
    with pytest.raises(
        ReportingNotificationError, match="^status_account_surface_unavailable$"
    ) as exc:
        await support.resolve_account_surface()
    assert isinstance(exc.value.__cause__, TypeError)
    assert await support.advertised_notifications() == {}


# Catches resolver cancellation being swallowed as an unavailable capability.
async def test_dynamic_surface_cancellation_propagates(status_harness):
    _, static = await support_for(status_harness)

    async def resolve_surface() -> ReportingStatusAccountSurface:
        raise asyncio.CancelledError

    support = replace(static, account_surface_resolver=resolve_surface)
    with pytest.raises(asyncio.CancelledError):
        await support.durable()


# Catches partial signing evidence being combined with stale constructor probes.
async def test_dynamic_surface_requires_a_probe_for_every_account(status_harness):
    h = status_harness
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        pytest.skip("durability advertisement requires PostgreSQL")
    await h.seed(readable=True)
    await h.ledger.put_configuration(configuration("acct_b"))
    _, static = await support_for(h, ("acct_a", "acct_b"))
    for account_id in ("acct_a", "acct_b"):
        await h.status.baseline(account_id=account_id)
    surface = ReportingStatusAccountSurface(
        account_ids=("acct_a", "acct_b"),
        account_surface_complete=True,
        readiness_subscriptions=static.readiness_subscriptions[:1],
    )

    async def resolve_surface() -> ReportingStatusAccountSurface:
        return surface

    support = replace(static, account_surface_resolver=resolve_surface)
    assert await support.advertised_notifications() == {}


# Catches repeated resolver calls combining accounts and probes from different snapshots.
async def test_dynamic_surface_uses_one_snapshot_for_every_account(status_harness, monkeypatch):
    h = status_harness
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        pytest.skip("durability advertisement requires PostgreSQL")
    await h.seed(readable=True)
    await h.ledger.put_configuration(configuration("acct_b"))
    _, static = await support_for(h, ("acct_a", "acct_b"))
    for account_id in ("acct_a", "acct_b"):
        await h.status.baseline(account_id=account_id)
    surfaces = [
        ReportingStatusAccountSurface(
            account_ids=("acct_a", "acct_b"),
            account_surface_complete=True,
            readiness_subscriptions=static.readiness_subscriptions,
        )
    ]
    calls = 0
    checked_accounts = []
    baseline_ready = h.status.baseline_ready

    async def resolve_surface() -> ReportingStatusAccountSurface:
        nonlocal calls
        calls += 1
        return surfaces[0]

    async def check_baseline(*, account_id):
        checked_accounts.append(account_id)
        surfaces[0] = replace(
            surfaces[0],
            account_ids=("acct_a",),
            readiness_subscriptions=static.readiness_subscriptions[:1],
        )
        return await baseline_ready(account_id=account_id)

    monkeypatch.setattr(h.status, "baseline_ready", check_baseline)
    support = replace(
        static,
        account_ids=(),
        account_surface_complete=False,
        readiness_subscriptions=(),
        account_surface_resolver=resolve_surface,
    )
    assert await support.durable()
    assert calls == 1
    assert checked_accounts == ["acct_a", "acct_b"]


# Catches mutable account surfaces changing evidence within a readiness proof.
async def test_dynamic_surface_snapshot_is_immutable(status_harness):
    _, support = await support_for(status_harness)
    surface = ReportingStatusAccountSurface(
        account_ids=("acct_a",),
        account_surface_complete=True,
        readiness_subscriptions=support.readiness_subscriptions,
    )
    with pytest.raises(FrozenInstanceError):
        surface.account_surface_complete = False


# Catches a snapshot retaining the resolver's mutable account enumeration.
def test_dynamic_surface_copies_mutable_account_ids():
    account_ids = ["acct_a"]
    surface = ReportingStatusAccountSurface(
        account_ids=account_ids,
        account_surface_complete=True,
        readiness_subscriptions=(),
    )

    account_ids[:] = ["acct_b"]

    assert surface.account_ids == ("acct_a",)


# Catches a snapshot retaining the resolver's mutable signing evidence collection.
def test_dynamic_surface_copies_mutable_readiness_subscriptions():
    probe = replace(notification_subscription(), event_types=("reporting.status_changed",))
    subscriptions = [probe]
    surface = ReportingStatusAccountSurface(
        account_ids=("acct_a",),
        account_surface_complete=True,
        readiness_subscriptions=subscriptions,
    )

    subscriptions.clear()

    assert surface.readiness_subscriptions == (probe,)


# Catches resolver-owned list mutation replacing evidence during an awaited account check.
async def test_dynamic_surface_proof_retains_evidence_during_source_mutation(
    status_harness, monkeypatch
):
    h = status_harness
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        pytest.skip("durability advertisement requires PostgreSQL")
    await h.seed(readable=True)
    await h.ledger.put_configuration(configuration("acct_b"))
    _, static = await support_for(h, ("acct_a", "acct_b"))
    for account_id in ("acct_a", "acct_b"):
        await h.status.baseline(account_id=account_id)
    account_ids = ["acct_a", "acct_b"]
    subscriptions = list(static.readiness_subscriptions)
    surface = ReportingStatusAccountSurface(
        account_ids=account_ids,
        account_surface_complete=True,
        readiness_subscriptions=subscriptions,
    )
    calls = 0
    checked_accounts = []
    check_started = asyncio.Event()
    check_continue = asyncio.Event()
    baseline_ready = h.status.baseline_ready

    async def resolve_surface() -> ReportingStatusAccountSurface:
        nonlocal calls
        calls += 1
        return surface

    async def check_baseline(*, account_id):
        checked_accounts.append(account_id)
        if account_id == "acct_a":
            check_started.set()
            await check_continue.wait()
        return await baseline_ready(account_id=account_id)

    monkeypatch.setattr(h.status, "baseline_ready", check_baseline)
    support = replace(static, account_surface_resolver=resolve_surface)
    proof = asyncio.create_task(support.durable())
    try:
        await asyncio.wait_for(check_started.wait(), timeout=10)
        account_ids.clear()
        subscriptions.clear()
        check_continue.set()
        assert await asyncio.wait_for(proof, timeout=10)
    finally:
        proof.cancel()
        await asyncio.gather(proof, return_exceptions=True)

    assert calls == 1
    assert checked_accounts == ["acct_a", "acct_b"]


# Catches single-account readiness using constructor probes instead of the current surface.
async def test_account_ready_uses_dynamic_surface(status_harness):
    h = status_harness
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        pytest.skip("durability advertisement requires PostgreSQL")
    await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    _, static = await support_for(h)
    calls = 0

    async def resolve_surface() -> ReportingStatusAccountSurface:
        nonlocal calls
        calls += 1
        return ReportingStatusAccountSurface(
            account_ids=("acct_a",),
            account_surface_complete=True,
            readiness_subscriptions=static.readiness_subscriptions,
        )

    support = replace(static, readiness_subscriptions=(), account_surface_resolver=resolve_surface)
    assert await support.account_ready(account_id="acct_a")
    assert calls == 1


async def test_all_visible_accounts_must_be_baselined_and_body_context_cannot_select_one(
    status_harness,
):
    h = status_harness
    await h.seed(readable=True)
    await h.ledger.put_configuration(configuration("acct_b"))
    _, support = await support_for(h, ("acct_a", "acct_b"))
    await h.status.baseline(account_id="acct_a")
    status_operation_1 = await support.durable()
    assert not status_operation_1
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
    status_operation_2 = await support.durable()
    assert status_operation_2 is (not isinstance(h.ledger, InMemoryReportingLedgerStore))
    status_operation_3 = await replace(support, account_surface_complete=False).durable()
    assert not status_operation_3


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
    status_operation_4 = await support.advertised_notifications()
    assert status_operation_4 == advertised
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
    status_operation_5 = await support.advertised_notifications()
    assert status_operation_5 == advertised


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
        status_operation_19 = await support.durable()
        assert status_operation_19 is status_on
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
    status_operation_6 = await ReportingActivitySupport(
        b_worker, h.ledger, ReportingActivityProjector(b_worker.outbox)
    ).durable()
    assert status_operation_6
    async with h.reliable.blobs.pool.connection() as connection:
        await connection.execute(
            "ALTER TABLE reporting_webhook_attempts DISABLE TRIGGER reporting_webhook_attempt_guard"
        )
    status_operation_7 = await base.durable()
    assert status_operation_7  # C logging does not require B activity storage.
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
    status_operation_8 = await base.durable()
    assert not status_operation_8
    base.worker.activity = None
    status_operation_9 = await base.durable()
    assert status_operation_9
    status_operation_10 = await ReportingActivitySupport(
        b_worker, h.ledger, ReportingActivityProjector(b_worker.outbox)
    ).durable()
    assert status_operation_10
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
        create_tool_caller(
            handler,
            "get_reporting_status",
            default_unnegotiated_adcp_version=handler.get_adcp_version(),
        ),
        executor._tool_callers["get_reporting_status"],
    ):
        buyer = await caller(
            {
                "adcp_version": "3.2",
                "account": {"account_id": "acct_a"},
                "view": "summary",
                "context": {"consumer_id": "auditor"},
            },
            ToolContext(caller_identity="buyer"),
        )
        auditor = await caller(
            {
                "adcp_version": "3.2",
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
    status_operation_11 = await service.ready()
    assert status_operation_11 is (not isinstance(h.ledger, InMemoryReportingLedgerStore))
    status_operation_12 = await service.project_dirty_once(account_id="acct_a")
    assert not (status_operation_12).did_work
    status_operation_13 = await service.sweep_due_once(account_id="acct_a")
    assert not (status_operation_13).did_work
    status_operation_14 = await service.expand_once(account_id="acct_a")
    assert not status_operation_14
    status_operation_15 = await service.deliver_once(account_id="acct_a")
    assert not status_operation_15
    status_operation_16 = await service.drain()
    assert status_operation_16 == 0
    await service.aclose()
    status_operation_17 = await service.ready()
    assert not status_operation_17
    with pytest.raises(ReportingNotificationError, match="status_service_closed"):
        await service.project_dirty_once(account_id="acct_a")


async def test_managed_destination_scopes_wait_for_d_clock_semantics(status_harness):
    from ._reconciliation_support import scenario

    h = status_harness
    await scenario(h.ledger)
    await h.status.baseline(account_id="acct_a")
    _, support = await support_for(h)
    assert await h.status.baseline_ready(account_id="acct_a")
    status_operation_18 = await support.advertised_notifications()
    assert status_operation_18 == {}
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
