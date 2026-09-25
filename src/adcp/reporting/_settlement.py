"""Settlement of work Python cannot safely interrupt."""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

_T = TypeVar("_T")


async def settle_task(task: asyncio.Task[_T]) -> _T:
    """Defer caller cancellation until an owned task has actually finished.

    Shield alone only keeps the child alive: the parent still returns early on
    cancellation. Retain the child and consume its outcome, including under
    repeated cancellation, before propagating the caller's cancellation. This
    barrier deliberately has no timeout; a supervisor can time out its *wait*
    while retaining ownership of the still-running operation.
    """
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = error
        except Exception:
            break  # result() below retrieves the child's exception exactly once
    if cancelled is not None:
        if not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()


async def cancel_and_settle(task: asyncio.Task[Any]) -> None:
    """Request cancellation, then join without losing repeated caller cancels.

    Joining through a task that consumes the child's cancellation lets us
    distinguish its expected CancelledError from a new cancellation of the
    caller. The latter is propagated only after the child has settled.
    """
    task.cancel()

    async def join() -> None:
        await asyncio.gather(task, return_exceptions=True)

    await settle_task(asyncio.create_task(join()))
