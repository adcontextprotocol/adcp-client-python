"""Owned worker failures stop the composition and emit only closed diagnostics."""

import asyncio
import json
import logging

import pytest

from adcp.reporting.ledger.notification_models import ReportingNotificationError

from ._production_support import production_harness
from ._production_transport import MountedProduction


def _fault_target(harness, boundary):
    if boundary == "producer":
        return harness.production.offerings[0].producer, "run_worker"
    if boundary == "materializer":
        return type(harness.production.materializer), "run_once"
    if boundary == "projection":
        return harness.projection, "rebuild_one"
    if boundary == "sweeper":
        return harness.projection, "sweep_one"
    import adcp.reporting.production.notifications as notifications

    # The running loop binds notification_turn at startup; its live sampling
    # call is the actual owned boundary to fault after a healthy first turn.
    return notifications, "next_account"


_CASES = [
    (boundary, delivery)
    for boundary in ("producer", "materializer", "projection", "sweeper", "notifications")
    for delivery in (False, True)
    if delivery or boundary != "notifications"
]


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("boundary,delivery", _CASES)
async def test_unexpected_worker_failure_has_closed_operator_diagnostic(
    backend, boundary, delivery, tmp_path, monkeypatch, caplog
):
    entered, release = asyncio.Event(), asyncio.Event()
    private_marker = "private-worker-failure-canary"

    async def fail(*args, **kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError(private_marker)

    async with production_harness(
        backend,
        tmp_path / "destination.sqlite",
        notifications=delivery,
        notification_delivery=delivery,
        count=0,
        poll_seconds=0.02,
    ) as h:
        support = h.production
        mount = MountedProduction(h)
        mount.authorize(h.item)
        async with mount.client() as client:
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, before = await mount.call(
                    client, "get_adcp_capabilities", {}, transport=transport
                )
                if backend == "postgres":
                    assert before["media_buy"]["reporting_delivery"]["managed_delivery"]
                else:
                    assert "reporting_delivery" not in before.get("media_buy", {})
            target, method = _fault_target(h, boundary)
            with monkeypatch.context() as patch:
                patch.setattr(target, method, fail)
                original_factory = logging.getLogRecordFactory()

                def ambient_factory(*args, **kwargs):
                    record = original_factory(*args, **kwargs)
                    record.private_request_context = private_marker
                    return record

                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    caplog.clear()
                    logging.setLogRecordFactory(ambient_factory)
                    task = (
                        support._notification_task if boundary == "notifications" else support._task
                    )
                    task.set_name(private_marker)
                    release.set()
                    await asyncio.wait_for(asyncio.shield(task), 5)
                    records = [r for r in caplog.records if r.name == "adcp.reporting.production"]
                    assert len(records) == 1, "unexpected owned failure needs one operator signal"
                    record = records[0]
                    assert record.levelno == logging.ERROR
                    assert record.code == "REPORTING_PRODUCTION_WORKER_STOPPED"
                    assert record.boundary == boundary
                    assert record.getMessage() == "Reporting production worker stopped"
                    assert not record.args and record.exc_info is None and record.stack_info is None
                    assert record.pathname == ""
                    assert record.threadName is None and record.processName is None
                    assert getattr(record, "taskName", None) is None
                    assert private_marker not in json.dumps(record.__dict__, default=str)
                    assert support._failed and support._stop.is_set()
                    tasks = [
                        t for t in (support._task, support._notification_task) if t is not None
                    ]
                    await asyncio.wait_for(asyncio.gather(*tasks), 5)
                    assert all(t.done() for t in tasks)
                    for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                        _, after = await mount.call(
                            client, "get_adcp_capabilities", {}, transport=transport
                        )
                        assert "reporting_delivery" not in after.get("media_buy", {})
                        assert "webhook_signing" not in after
                    with pytest.raises(ReportingNotificationError, match="component_unready"):
                        await support.activate(account_id=h.item.config.account_id)
                finally:
                    logging.setLogRecordFactory(original_factory)
                    release.set()
                    support._stop.set()


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("boundary", ["producer", "notifications"])
@pytest.mark.parametrize("kind", ["domain", "cancellation"])
async def test_expected_worker_stop_is_silent_and_drains_sibling(
    backend, boundary, kind, tmp_path, monkeypatch, caplog
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(*args, **kwargs):
        entered.set()
        await release.wait()
        if kind == "cancellation":
            raise asyncio.CancelledError
        raise ReportingNotificationError("notification_chain_unready")

    async with production_harness(
        backend,
        tmp_path / "destination.sqlite",
        notifications=True,
        notification_delivery=True,
        count=0,
        poll_seconds=0.02,
    ) as h:
        support = h.production
        target, method = _fault_target(h, boundary)
        with monkeypatch.context() as patch:
            patch.setattr(target, method, fail)
            try:
                await asyncio.wait_for(entered.wait(), 5)
                caplog.clear()
                release.set()
                task = support._notification_task if boundary == "notifications" else support._task
                if kind == "cancellation":
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(task), 5)
                else:
                    await asyncio.wait_for(asyncio.shield(task), 5)
                assert support._stop.is_set(), "the owned sibling must be woken immediately"
                tasks = [t for t in (support._task, support._notification_task) if t is not None]
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                assert all(t.done() for t in tasks)
                assert not [r for r in caplog.records if r.name == "adcp.reporting.production"]
                production_operation_1 = await support.reporting_delivery()
                assert production_operation_1 == {}
            finally:
                release.set()
                support._stop.set()


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("delivery", [False, True])
async def test_failing_operator_sink_cannot_prevent_owned_shutdown(
    backend, delivery, tmp_path, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def fail():
        entered.set()
        await release.wait()
        raise RuntimeError("private-provider-diagnostic-canary")

    def broken_sink(record):
        observed.append(record)
        raise RuntimeError("private-operator-diagnostic-canary")

    async with production_harness(
        backend,
        tmp_path / "destination.sqlite",
        notifications=delivery,
        notification_delivery=delivery,
        count=0,
        poll_seconds=0.02,
    ) as h:
        support = h.production
        with monkeypatch.context() as patch:
            patch.setattr(support.offerings[0].producer, "run_worker", fail)
            patch.setattr(logging.getLogger("adcp.reporting.production"), "handle", broken_sink)
            try:
                await asyncio.wait_for(entered.wait(), 5)
                release.set()
                tasks = [t for t in (support._task, support._notification_task) if t is not None]
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
                assert support._failed and support._stop.is_set()
                assert len(observed) == 1
                assert "canary" not in json.dumps(observed[0].__dict__, default=str)
                production_operation_2 = await support.reporting_delivery()
                assert production_operation_2 == {}
                with pytest.raises(ReportingNotificationError, match="component_unready"):
                    await support.activate(account_id=h.item.config.account_id)
            finally:
                release.set()
                support._stop.set()


@pytest.mark.parametrize("invalid", ["private-boundary-canary", []], ids=["string", "unhashable"])
def test_operator_boundary_is_runtime_allowlisted(invalid, caplog):
    from typing import get_args

    from adcp.reporting.production._diagnostics import (
        _BOUNDARIES,
        _worker_stopped,
        _WorkerBoundary,
    )

    assert set(get_args(_WorkerBoundary)) == _BOUNDARIES
    _worker_stopped(boundary=invalid)
    records = [r for r in caplog.records if r.name == "adcp.reporting.production"]
    assert len(records) == 1 and records[0].boundary == "worker"
    assert "canary" not in json.dumps(records[0].__dict__, default=str)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("stop", ["cancellation", "close"])
async def test_stopped_composition_does_not_alert_on_late_inflight_failure(
    backend, stop, tmp_path, monkeypatch, caplog
):
    import adcp.reporting.production.notifications as notifications

    producer_entered, notification_entered = asyncio.Event(), asyncio.Event()
    producer_release, notification_release = asyncio.Event(), asyncio.Event()

    async def producer_wait():
        producer_entered.set()
        await producer_release.wait()
        raise RuntimeError("private-late-worker-canary")

    async def notification_wait(*args, **kwargs):
        notification_entered.set()
        await notification_release.wait()
        if stop == "cancellation":
            raise asyncio.CancelledError
        return None

    async with production_harness(
        backend,
        tmp_path / "destination.sqlite",
        notifications=True,
        notification_delivery=True,
        count=0,
        poll_seconds=0.02,
    ) as h:
        support = h.production
        closing = None
        with monkeypatch.context() as patch:
            patch.setattr(support.offerings[0].producer, "run_worker", producer_wait)
            patch.setattr(notifications, "next_account", notification_wait)
            try:
                await asyncio.wait_for(
                    asyncio.gather(producer_entered.wait(), notification_entered.wait()), 5
                )
                caplog.clear()
                if stop == "cancellation":
                    notification_release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(support._notification_task), 5)
                else:
                    closing = asyncio.create_task(support.aclose())
                    await asyncio.wait_for(support._stop.wait(), 5)
                assert support._stop.is_set()
                assert not support._task.done()
                producer_release.set()
                notification_release.set()
                tasks = [t for t in (support._task, support._notification_task) if t is not None]
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                if closing is not None:
                    await asyncio.wait_for(closing, 5)
                assert not [r for r in caplog.records if r.name == "adcp.reporting.production"]
                production_operation_3 = await support.reporting_delivery()
                assert production_operation_3 == {}
            finally:
                producer_release.set()
                notification_release.set()
                support._stop.set()
                if closing is not None:
                    await asyncio.gather(closing, return_exceptions=True)
