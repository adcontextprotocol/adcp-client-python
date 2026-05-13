"""Lifespan-tied background work via ``serve(on_startup, on_shutdown)``.

Adopters running ``transport="both"`` who need lifecycle-bound background
work — schedulers, message-queue consumers, cache warmers, connection
pools — pass zero-arg async callables to ``on_startup`` / ``on_shutdown``.
Hooks fire inside the SDK's composed parent lifespan: startups run after
both MCP and A2A inner apps have initialized, shutdowns run before
either tears down.

This replaces the ASGI ``SchedulerLifespanMiddleware`` pattern adopters
wired by hand before issue #709 — the SDK now owns the composition so
ordering is unambiguous.

Run::

    uv run python examples/scheduler_lifespan.py

Then the process prints scheduler start / tick / stop lines and serves
the unified MCP+A2A binary on :3001 until you Ctrl-C.
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from adcp.server import ADCPHandler, ToolContext, serve
from adcp.server.responses import capabilities_response

logger = logging.getLogger("scheduler-demo")


class _Scheduler:
    """Minimal stand-in for a real scheduler.

    Production adopters drop in APScheduler, Celery beat, a job-queue
    consumer loop, etc. The shape is the same: ``start()`` kicks off a
    long-lived task, ``stop()`` cancels it. The ``serve()`` lifespan
    hooks just call these.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        logger.info("scheduler: starting")
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                logger.info("scheduler: tick")
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            logger.info("scheduler: cancelled")
            raise

    async def stop(self) -> None:
        logger.info("scheduler: stopping")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class Agent(ADCPHandler[ToolContext]):
    advertised_tools: ClassVar[set[str]] = {"get_adcp_capabilities"}

    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # Production wiring usually looks like:
    #   scheduler = APScheduler(...)
    #   db_pool = asyncpg.create_pool(...)
    #   await db_pool  # in on_startup
    # The hook captures (``scheduler``, ``db_pool``, etc.) become the
    # handles your tool methods reach through to do real work — stash
    # them on ``Agent`` or in ``request.state`` via a context_factory.
    scheduler = _Scheduler()
    serve(
        Agent(),
        name="scheduler-demo",
        transport="both",
        on_startup=[scheduler.start],
        on_shutdown=[scheduler.stop],
    )


if __name__ == "__main__":
    main()
