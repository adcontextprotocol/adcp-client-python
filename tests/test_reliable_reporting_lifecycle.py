"""Lifecycle barriers through the public service and actual inline adapter."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import FileSystemStagingStore, InlineReportingSource
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    ReportingProducer,
    ReportingStatusCaller,
)
from adcp.reporting.service import (
    ReliableReportingService,
    ReliableReportingServiceError,
    ReliableReportingShutdownTimeoutError,
    ReliableReportingState,
    ReliableReportingTurn,
    ReliableReportingUnavailableError,
    ReportingServiceResource,
)
from adcp.reporting.testing import ScriptedReportingAdapter
from adcp.server import ADCPHandler
from tests.test_reliable_reporting_service import (
    NOW,
    _account_context,
    _configuration,
    _rows,
)


async def checkpoint() -> None:
    """Deliver scheduled task/cancellation callbacks, without advancing domain time."""
    loop = asyncio.get_running_loop()
    reached = loop.create_future()
    loop.call_soon(reached.set_result, None)
    await reached


class BlockingSchema(InMemoryReportingLedgerStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def create_schema(self) -> None:
        self.calls += 1
        self.entered.set()
        await self.release.wait()


async def test_injected_components_are_borrowed_even_across_repeated_close() -> None:
    class BorrowedWorker:
        closes = 0

        async def run_once(self) -> None:
            return None

        async def close(self) -> None:
            self.closes += 1

    worker = BorrowedWorker()
    service = ReliableReportingService.memory(
        account_context=_account_context, materialization_worker=worker
    )
    await service.start()
    await service.close()
    await service.close()
    assert worker.closes == 0


async def test_concurrent_initialization_has_one_schema_operation() -> None:
    store = BlockingSchema()
    service = ReliableReportingService(store=store, account_context=_account_context)
    first = asyncio.create_task(service.initialize())
    await asyncio.wait_for(store.entered.wait(), 2)
    second = asyncio.create_task(service.initialize())
    try:
        await checkpoint()
        assert store.calls == 1
    finally:
        store.release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await service.close()


async def test_close_waits_for_a_start_already_in_progress() -> None:
    store = BlockingSchema()
    service = ReliableReportingService(store=store, account_context=_account_context)
    starting = asyncio.create_task(service.start())
    await asyncio.wait_for(store.entered.wait(), 2)
    closing = asyncio.create_task(service.close())
    try:
        await checkpoint()
        assert not closing.done(), "schema work still owns the store"
    finally:
        store.release.set()
        await asyncio.gather(starting, closing, return_exceptions=True)


async def test_closed_service_rejects_reporting_rpc_admission() -> None:
    service = ReliableReportingService.memory(
        account_context=_account_context,
        caller_resolver=lambda _request, _context: ReportingStatusCaller("account", "buyer"),
    )
    await service.start()
    await service.close()
    with pytest.raises(RuntimeError, match="unavailable|closed"):
        await service.get_reporting_status({"view": "periods"})


async def test_cancelled_sync_source_remains_unsettled_until_its_thread_finishes() -> None:
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def fetch(_request: Any) -> list[dict[str, Any]]:
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(5), "test cleanup watchdog"
            return []
        finally:
            finished.set()

    source = InlineReportingSource(
        capabilities=redacted_capabilities(), fetch=fetch, clock=lambda: NOW
    )
    task = asyncio.create_task(source.execute(redacted_snapshot_request(), cancel=asyncio.Event()))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        task.cancel()
        await checkpoint()
        await checkpoint()
        assert not task.done(), "cancelled source returned while its sync fetch was still running"
        task.cancel()  # repeated cancellation must not bypass the settlement barrier
        await checkpoint()
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(finished.wait, 2)
    assert task.cancelled()
    assert finished.is_set()


async def test_cancelled_filesystem_staging_joins_its_owned_write(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    class Staging(FileSystemStagingStore):
        @staticmethod
        def _write(target: Path, payload: bytes) -> None:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test cleanup watchdog"
            FileSystemStagingStore._write(target, payload)

    staging = Staging(tmp_path)
    values = dict(account_id="account", source_execution_key="attempt", ordinal=0, payload=b"rows")
    task = asyncio.create_task(staging.stage(**values))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        task.cancel()
        await checkpoint()
        task.cancel()
        await checkpoint()
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    reference, generation = await staging.stage(**values)
    assert (
        await staging.read(
            object_ref=reference,
            object_generation=generation,
            account_id="account",
            source_scope={},
            cancel=asyncio.Event(),
        )
        == b"rows"
    )


async def test_startup_failure_unwinds_transferred_resources_in_reverse_order() -> None:
    events: list[str] = []

    def resource(name: str, *, fail: bool = False) -> ReportingServiceResource:
        async def opening() -> None:
            events.append(f"open:{name}")
            if fail:
                raise RuntimeError("secret-provider-response")

        async def closing() -> None:
            events.append(f"close:{name}")

        return ReportingServiceResource(open=opening, close=closing)

    service = ReliableReportingService.memory(
        account_context=_account_context,
        owned_resources=(resource("pool"), resource("writer", fail=True), resource("sender")),
    )
    with pytest.raises(ReliableReportingServiceError, match="startup") as caught:
        await service.start()
    assert "secret" not in str(caught.value)
    assert service.state is ReliableReportingState.FAILED
    assert service.capability_block() == {}
    await service.close()
    await service.close()
    assert events == ["open:pool", "open:writer", "close:sender", "close:writer", "close:pool"]


async def test_failed_closer_does_not_skip_other_resources_or_repeat_close() -> None:
    events: list[str] = []

    async def close_pool() -> None:
        events.append("pool")

    async def close_writer() -> None:
        events.append("writer")
        raise RuntimeError("secret-close-body")

    service = ReliableReportingService.memory(
        account_context=_account_context,
        owned_resources=(
            ReportingServiceResource(close=close_pool),
            ReportingServiceResource(close=close_writer),
        ),
    )
    await service.start()
    await asyncio.gather(service.close(), service.close(), service.close())
    assert events == ["writer", "pool"]
    assert service.state is ReliableReportingState.FAILED
    with pytest.raises(ReliableReportingServiceError, match="shutdown"):
        await service.wait()
    assert "secret" not in repr(service.failure)


async def test_cancelled_starter_keeps_resources_until_startup_settles() -> None:
    store = BlockingSchema()
    closed = asyncio.Event()

    async def close_owned() -> None:
        closed.set()

    service = ReliableReportingService(
        store=store,
        account_context=_account_context,
        owned_resources=(ReportingServiceResource(close=close_owned),),
    )
    starting = asyncio.create_task(service.start())
    await asyncio.wait_for(store.entered.wait(), 2)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    try:
        assert service.state is ReliableReportingState.STOPPING
        assert not closed.is_set()
        assert service.capability_block() == {}
        with pytest.raises(ReliableReportingUnavailableError):
            await service.start()
    finally:
        store.release.set()
        await asyncio.wait_for(service.close(), 2)
    assert closed.is_set()
    assert service.state is ReliableReportingState.CLOSED


@pytest.mark.parametrize("background", [False, True])
async def test_timed_out_and_cancelled_close_retain_live_sync_adapter(background: bool) -> None:
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    closed: list[str] = []
    loop = asyncio.get_running_loop()

    class Adapter:
        capabilities = redacted_capabilities()

        def fetch_slice(self, _request: Any) -> list[dict[str, Any]]:
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5), "test cleanup watchdog"
                return _rows(5)
            finally:
                finished.set()

        async def close(self) -> None:
            assert finished.is_set(), "resource was closed with a live provider thread"
            closed.append("adapter")

    adapter = Adapter()
    service = ReliableReportingService.memory(
        account_context=_account_context,
        clock=lambda: NOW,
        worker_interval=timedelta(days=1) if background else None,
        owned_resources=(ReportingServiceResource(close=adapter.close),),
    )
    service.sources.register("gam", adapter)
    await service.configure(_configuration())
    await service.start()
    turn = None if background else asyncio.create_task(service.run_worker())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        # Cancel the public producer caller as well as a close waiter. The
        # threaded adapter must remain owned through both cancellation paths.
        if turn is not None:
            turn.cancel()
        with pytest.raises(ReliableReportingShutdownTimeoutError):
            await service.close(timeout=0)
        assert service.state is ReliableReportingState.STOPPING
        if turn is not None:
            turn.cancel()
            await checkpoint()
            await checkpoint()
            assert not turn.done(), "repeated producer cancellation abandoned its source task"
        waiter = asyncio.create_task(service.close())
        await checkpoint()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert service.state is ReliableReportingState.STOPPING
        assert closed == []
        assert service.capability_block() == {}
        with pytest.raises(ReliableReportingUnavailableError):
            await service.run_worker()
    finally:
        release.set()
        if turn is not None:
            await asyncio.gather(turn, return_exceptions=True)
        await asyncio.wait_for(service.close(), 2)
    assert finished.is_set()
    assert closed == ["adapter"]
    assert service.state is ReliableReportingState.CLOSED
    await service.close()
    assert closed == ["adapter"]


@pytest.mark.parametrize(
    "method",
    [
        "get_reporting_status",
        "sync_reporting_status",
        "sync_reporting_receipts",
        "get_revision_content",
    ],
)
async def test_stopping_rejects_all_reporting_rpc_admission_before_authorization(
    method: str,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    resolutions: list[str] = []

    class Worker:
        async def run_once(self) -> None:
            entered.set()
            await release.wait()

    def caller(_request: Any, _context: Any) -> ReportingStatusCaller:
        resolutions.append("called")
        return ReportingStatusCaller("account", "buyer")

    service = ReliableReportingService.memory(
        account_context=_account_context, caller_resolver=caller, materialization_worker=Worker()
    )
    turn = asyncio.create_task(service.run_worker())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(ReliableReportingShutdownTimeoutError):
            await service.close(timeout=0)
        with pytest.raises(ReliableReportingUnavailableError):
            await getattr(service, method)({})
        with pytest.raises(ReliableReportingUnavailableError):
            await service.configure(_configuration())
        assert resolutions == []
    finally:
        release.set()
        await turn
        await service.close()


async def test_cancelled_async_adapter_settles_cleanup_before_owned_close() -> None:
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    events: list[str] = []

    class Adapter:
        capabilities = redacted_capabilities()

        async def fetch_slice(self, _request: Any) -> list[dict[str, Any]]:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                events.append("fetch-settled")
            return []

        async def aclose(self) -> None:
            events.append("adapter-closed")

    adapter = Adapter()
    service = ReliableReportingService.memory(
        account_context=_account_context,
        clock=lambda: NOW,
        owned_resources=(ReportingServiceResource(close=adapter.aclose),),
    )
    service.sources.register("gam", adapter)
    await service.configure(_configuration())
    turn = asyncio.create_task(service.run_worker())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        turn.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 2)
        turn.cancel()
        await checkpoint()
        with pytest.raises(ReliableReportingShutdownTimeoutError):
            await service.close(timeout=0)
        assert service.state is ReliableReportingState.STOPPING
        assert not turn.done()
        assert events == []
    finally:
        cleanup_release.set()
        await asyncio.gather(turn, return_exceptions=True)
        await service.close()
    assert events == ["fetch-settled", "adapter-closed"]


@pytest.mark.parametrize("fail_worker", [False, True])
async def test_installed_receipt_admission_drains_before_owned_cleanup(fail_worker: bool) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    failed = asyncio.Event()
    order: list[str] = []

    class Receipts:
        async def handle(self, request: dict[str, Any], **_caller: Any) -> dict[str, Any]:
            entered.set()
            await release.wait()
            order.append("receipt-settled")
            return request

    class Materializer:
        async def run_once(self) -> None:
            return None

    class BrokenScheduler(ReliableReportingService):
        async def run_worker(self, *, now: datetime | None = None) -> ReliableReportingTurn:
            await entered.wait()
            raise RuntimeError("secret-scheduler-body")

    async def close_owned() -> None:
        order.append("resource-closed")

    service = BrokenScheduler.memory(
        account_context=_account_context,
        caller_resolver=lambda _request, _context: ReportingStatusCaller("account", "buyer"),
        materialization_worker=Materializer(),
        receipt_handler=Receipts(),
        worker_interval=timedelta(days=1) if fail_worker else None,
        worker_error_handler=lambda _component, _error: failed.set(),
        owned_resources=(ReportingServiceResource(close=close_owned),),
    )
    handler = service.install(ADCPHandler())
    await service.start()
    receipt = asyncio.create_task(handler.sync_reporting_receipts({"request_id": "kept"}))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        if fail_worker:
            await asyncio.wait_for(failed.wait(), 2)
            assert service.state is ReliableReportingState.STOPPING
            assert service.failure is not None
        with pytest.raises(ReliableReportingShutdownTimeoutError):
            await service.close(timeout=0)
        assert order == []
        with pytest.raises(ReliableReportingUnavailableError):
            await handler.sync_reporting_receipts({})
    finally:
        release.set()
        result = await receipt
        await service.close()
    assert result == {"request_id": "kept"}
    assert order == ["receipt-settled", "resource-closed"]
    if fail_worker:
        with pytest.raises(ReliableReportingServiceError, match="service"):
            await service.wait()


async def test_repeated_rpc_cancellation_cannot_interrupt_transaction_cleanup() -> None:
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    settled = asyncio.Event()
    closed = asyncio.Event()

    class Receipts:
        async def handle(self, request: dict[str, Any], **_caller: Any) -> dict[str, Any]:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                settled.set()
            return request

    class Materializer:
        async def run_once(self) -> None:
            return None

    async def close_owned() -> None:
        closed.set()

    service = ReliableReportingService.memory(
        account_context=_account_context,
        caller_resolver=lambda _request, _context: ReportingStatusCaller("account", "buyer"),
        materialization_worker=Materializer(),
        receipt_handler=Receipts(),
        owned_resources=(ReportingServiceResource(close=close_owned),),
    )
    await service.start()
    call = asyncio.create_task(service.sync_reporting_receipts({}))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        call.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 2)
        call.cancel()
        await checkpoint()
        await checkpoint()
        with pytest.raises(ReliableReportingShutdownTimeoutError):
            await service.close(timeout=0)
        assert not call.done(), "repeated cancellation interrupted admitted cleanup"
        assert service.state is ReliableReportingState.STOPPING
        assert not closed.is_set()
    finally:
        cleanup_release.set()
        await asyncio.gather(call, return_exceptions=True)
        await service.close()
    assert settled.is_set()
    assert closed.is_set()


@pytest.mark.parametrize("fail_scheduler", [False, True])
async def test_only_scheduler_failure_withdraws_capabilities_and_sdk_logs_stay_redacted(
    caplog: Any,
    fail_scheduler: bool,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    reported = asyncio.Event()
    errors: list[tuple[str, BaseException]] = []
    failure = RuntimeError("secret-provider-body https://destination.test/?token=secret")

    class Worker:
        calls = 0

        async def run_once(self) -> None:
            self.calls += 1
            entered.set()
            await release.wait()
            raise failure

    class Service(ReliableReportingService):
        async def run_worker(self, *, now: datetime | None = None) -> ReliableReportingTurn:
            if fail_scheduler:
                entered.set()
                await release.wait()
                raise failure
            return await super().run_worker(now=now)

    class Handler(ADCPHandler):
        async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
            return {"supported_protocols": ["media_buy"]}

    worker = Worker()

    def report_error(component: str, error: BaseException) -> None:
        errors.append((component, error))
        reported.set()
        raise RuntimeError("secret-error-handler-body")

    service = Service.memory(
        account_context=_account_context,
        clock=lambda: NOW,
        materialization_worker=worker,
        worker_interval=timedelta(days=1),
        worker_error_handler=report_error,
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(1)]))
    await service.configure(_configuration())
    handler = service.install(Handler())
    assert "reporting_delivery" not in (await handler.get_adcp_capabilities({})).get(
        "media_buy", {}
    )
    await service.start()
    await asyncio.wait_for(entered.wait(), 2)
    assert (await handler.get_adcp_capabilities({}))["media_buy"]["reporting_delivery"]["supported"]
    try:
        release.set()
        await asyncio.wait_for(reported.wait(), 2)
        if fail_scheduler:
            with pytest.raises(ReliableReportingServiceError, match="service"):
                await asyncio.wait_for(service.wait(), 2)
            assert not service.ready
            assert "secret" not in repr(service.failure)
            assert "reporting_delivery" not in (await handler.get_adcp_capabilities({})).get(
                "media_buy", {}
            )
        else:
            assert service.ready
            assert service.failure is None
            assert (await handler.get_adcp_capabilities({}))["media_buy"]["reporting_delivery"][
                "supported"
            ]
    finally:
        release.set()
        await service.close()
    assert worker.calls == (0 if fail_scheduler else 1)
    assert "secret" not in caplog.text
    assert errors == [("service" if fail_scheduler else "materialization", failure)]


async def test_configure_during_startup_is_serialized_without_losing_an_accepted_generation() -> (
    None
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def context(configuration: Any) -> Any:
        entered.set()
        await release.wait()
        return _account_context(configuration)

    service = ReliableReportingService.memory(account_context=context)
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(1)]))
    configuration = _configuration()
    configuring = asyncio.create_task(service.configure(configuration))
    await asyncio.wait_for(entered.wait(), 2)
    starting = asyncio.create_task(service.start())
    try:
        await checkpoint()
        assert service.state is ReliableReportingState.STARTING
    finally:
        release.set()
        await asyncio.gather(configuring, starting)
    assert await service.store.list_configurations(account_id=configuration.account_id) == (
        configuration,
    )
    await service.close()


async def test_retryable_source_outcome_does_not_fail_supervision() -> None:
    service = ReliableReportingService.memory(account_context=_account_context, clock=lambda: NOW)
    adapter = ScriptedReportingAdapter(redacted_capabilities(), [None, _rows(8)])
    service.sources.register("gam", adapter)
    await service.configure(replace(_configuration(), deactivated_at=NOW))
    first = await service.run_worker()
    assert any(turn.slices_failed for turn in first.configurations.values())
    assert service.ready
    assert service.failure is None
    second = await service.run_worker()
    assert any(turn.revisions_committed for turn in second.configurations.values())
    await service.close()


async def test_low_level_producer_keeps_existing_lease_until_cancelled_sync_work_settles() -> None:
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def fetch(_request: Any) -> list[dict[str, Any]]:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test cleanup watchdog"
        return _rows(3)

    configuration = _configuration()
    store = InMemoryReportingLedgerStore(clock=lambda: NOW)
    await store.put_configuration(configuration)
    source = InlineReportingSource(
        capabilities=redacted_capabilities(), fetch=fetch, clock=lambda: NOW
    )
    producer = ReportingProducer(
        source=source,
        object_reader=source.staging,
        offerings=_account_context(configuration).producer_offerings(),
        store=store,
        clock=lambda: NOW,
        worker_id="first",
    )
    running = asyncio.create_task(producer.run_worker())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        running.cancel()
        await checkpoint()
        running.cancel()
        await checkpoint()
        await checkpoint()
        assert not running.done()
        assert await store.lease_period_close(worker_id="second", now=NOW, lease_seconds=60) is None
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
    lease = await store.lease_period_close(worker_id="second", now=NOW, lease_seconds=60)
    assert lease is not None
    await store.release_period_close(lease, worker_id="second")


async def test_mounted_mcp_status_call_drains_and_stopping_rejects_a_new_call() -> None:
    import httpx
    from asgi_lifespan import LifespanManager

    from adcp.server import create_mcp_server
    from tests.test_mcp_middleware_composition import (
        _call_tool,
        _initialize_session,
        _parse_event_stream,
    )

    entered = asyncio.Event()
    release = asyncio.Event()
    resolutions: list[str] = []

    class Handler(ADCPHandler):
        def get_adcp_version(self) -> str:
            return "3.2.0-rc.6"

    async def caller(_request: Any, _context: Any) -> ReportingStatusCaller:
        resolutions.append("authorized")
        entered.set()
        await release.wait()
        return ReportingStatusCaller("account-redacted", "buyer")

    service = ReliableReportingService.memory(
        account_context=_account_context, caller_resolver=caller, clock=lambda: NOW
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    await service.configure(_configuration())
    handler = service.install(Handler())
    mcp = create_mcp_server(handler, stateless_http=True, allowed_hosts=["localhost"])
    app = mcp.streamable_http_app()
    await service.start()
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            follow_redirects=True,
        ) as client,
    ):
        await _initialize_session(client)
        request = {"account": {"account_id": "account-redacted"}, "view": "periods"}
        first = asyncio.create_task(_call_tool(client, "get_reporting_status", request))
        started = asyncio.create_task(entered.wait())
        try:
            completed, _ = await asyncio.wait(
                {first, started}, timeout=5, return_when=asyncio.FIRST_COMPLETED
            )
            assert started in completed, (await first).text if first.done() else "no admission"
            with pytest.raises(ReliableReportingShutdownTimeoutError):
                await service.close(timeout=0)
            response = await _call_tool(client, "get_reporting_status", request)
            assert _parse_event_stream(response.text)["result"]["isError"] is True
            assert resolutions == ["authorized"]
        finally:
            release.set()
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
            completed = await first
            await service.close()
        assert not _parse_event_stream(completed.text)["result"].get("isError", False)
    assert service.state is ReliableReportingState.CLOSED
