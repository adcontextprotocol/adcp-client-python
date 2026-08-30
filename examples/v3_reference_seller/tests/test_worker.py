"""Lifecycle tests for the separately supervised durable worker."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))


@pytest.mark.asyncio
async def test_stop_event_cancels_workers_before_resource_shutdown(monkeypatch) -> None:
    import src.worker as worker_module

    outbox_started = asyncio.Event()
    workflow_started = asyncio.Event()
    shutdown_order = []

    async def run_outbox() -> None:
        outbox_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            shutdown_order.append("outbox")

    async def run_workflow(_handler) -> None:
        workflow_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            shutdown_order.append("workflow")

    async def shutdown() -> None:
        shutdown_order.append("wiring")

    wiring = SimpleNamespace(
        startup=AsyncMock(),
        shutdown=AsyncMock(side_effect=shutdown),
        outbox=SimpleNamespace(run_worker=run_outbox),
        workflow_queue=SimpleNamespace(run_worker=run_workflow),
    )
    monkeypatch.setattr(
        worker_module.DurableTaskWiring,
        "from_env",
        lambda **_kwargs: wiring,
    )
    stop_event = asyncio.Event()

    async def request_stop() -> None:
        await asyncio.gather(outbox_started.wait(), workflow_started.wait())
        stop_event.set()

    stopper = asyncio.create_task(request_stop())
    await worker_module.run(
        stop_event=stop_event,
        workflow_handler=AsyncMock(),
    )
    await stopper

    assert set(shutdown_order[:2]) == {"outbox", "workflow"}
    assert shutdown_order[2] == "wiring"
    wiring.startup.assert_awaited_once()
    wiring.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_failure_wins_when_shutdown_is_also_ready(monkeypatch) -> None:
    import src.worker as worker_module

    stop_event = asyncio.Event()
    failure = RuntimeError("outbox failed")

    async def fail_outbox() -> None:
        stop_event.set()
        raise failure

    wiring = SimpleNamespace(
        startup=AsyncMock(),
        shutdown=AsyncMock(),
        outbox=SimpleNamespace(run_worker=fail_outbox),
        workflow_queue=SimpleNamespace(),
    )
    monkeypatch.setattr(
        worker_module.DurableTaskWiring,
        "from_env",
        lambda **_kwargs: wiring,
    )
    real_wait = asyncio.wait

    async def wait_for_all(awaitables, *, return_when):
        assert return_when is asyncio.FIRST_COMPLETED
        return await real_wait(awaitables, return_when=asyncio.ALL_COMPLETED)

    monkeypatch.setattr(worker_module.asyncio, "wait", wait_for_all)

    with pytest.raises(RuntimeError, match="outbox failed") as exc_info:
        await worker_module.run(stop_event=stop_event)

    assert exc_info.value is failure
    wiring.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_signal_runner_installs_and_removes_sigterm_handler(monkeypatch) -> None:
    import src.worker as worker_module

    loop = asyncio.get_running_loop()
    callbacks = {}
    removed = []

    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        lambda signum, callback: callbacks.__setitem__(signum, callback),
    )
    monkeypatch.setattr(
        loop,
        "remove_signal_handler",
        lambda signum: removed.append(signum) or True,
    )

    async def fake_run(*, stop_event, workflow_handler=None) -> None:
        assert workflow_handler is None
        callbacks[signal.SIGTERM]()
        assert stop_event.is_set()

    monkeypatch.setattr(worker_module, "run", fake_run)

    await worker_module.run_with_signals()

    assert set(callbacks) == {signal.SIGTERM, signal.SIGINT}
    assert removed == [signal.SIGTERM, signal.SIGINT]
