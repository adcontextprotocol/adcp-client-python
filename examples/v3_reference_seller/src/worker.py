"""Separately supervised durable task-webhook and workflow worker.

Run beside the web process with the same ``ADCP_TASK_*`` and
``ADCP_WEBHOOK_SIGNING_*`` environment variables::

    python -m src.worker

The stock entrypoint delivers the webhook outbox. Adopters with external
workflow work call :func:`run_with_signals` with their idempotent handler.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .durable_tasks import DurableTaskWiring
from .workflow_queue import WorkflowHandler

logger = logging.getLogger(__name__)


async def run(
    *,
    stop_event: asyncio.Event | None = None,
    workflow_handler: WorkflowHandler | None = None,
) -> None:
    """Run durable workers until a shutdown signal sets ``stop_event``."""
    wiring = DurableTaskWiring.from_env(required=True, include_idempotency=False)
    assert wiring is not None  # required=True makes None impossible
    await wiring.startup()
    stop = stop_event or asyncio.Event()
    workers = [
        asyncio.create_task(
            wiring.outbox.run_worker(),
            name="adcp-task-webhook-outbox",
        )
    ]
    if workflow_handler is not None:
        workers.append(
            asyncio.create_task(
                wiring.workflow_queue.run_worker(workflow_handler),
                name="adcp-workflow-queue",
            )
        )
    stop_waiter = asyncio.create_task(stop.wait(), name="adcp-worker-shutdown")
    logger.info("Durable workers started")
    try:
        done, _pending = await asyncio.wait(
            [*workers, stop_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
        completed_workers = done.intersection(workers)
        for task in completed_workers:
            exc = task.exception()
            if exc is not None:
                raise exc
        if completed_workers:
            raise RuntimeError("A durable worker exited unexpectedly")
        logger.info("Shutdown requested; stopping durable workers")
    finally:
        stop_waiter.cancel()
        for task in workers:
            task.cancel()
        await asyncio.gather(stop_waiter, *workers, return_exceptions=True)
        await wiring.shutdown()


async def run_with_signals(
    *,
    workflow_handler: WorkflowHandler | None = None,
) -> None:
    """Install SIGTERM/SIGINT handlers and run until either is received."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            continue
        installed.append(signum)
    try:
        await run(
            stop_event=stop_event,
            workflow_handler=workflow_handler,
        )
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_with_signals())


if __name__ == "__main__":
    main()


__all__ = ["main", "run", "run_with_signals"]
