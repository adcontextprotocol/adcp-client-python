"""Per-call execution options and deadline recovery metadata."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class TaskOptions:
    """Options for one complete SDK task call.

    ``timeout`` is a non-resetting wall-clock budget in seconds. It covers the
    full client lifecycle: discovery, capability/version and signing preflight,
    protocol dispatch, response validation, and postflight projection. It does
    not replace the transport timeout configured on :class:`~adcp.AgentConfig`.

    A timeout of ``None`` disables the task-level deadline.
    """

    timeout: float | None = None

    def __post_init__(self) -> None:
        timeout = self.timeout
        if timeout is None:
            return
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number of seconds")


@dataclass(frozen=True, slots=True)
class TaskRecoveryMetadata:
    """Safe retry identity for a timed-out mutating task.

    ``outcome_unknown`` is always true: once dispatch begins, a deadline cannot
    prove whether the seller committed the operation. Retry the exact original
    request with ``idempotency_key``; do not mint a new key.
    """

    task_name: str
    operation_id: str
    idempotency_key: str = field(repr=False)
    outcome_unknown: bool = True


@dataclass(slots=True)
class _TaskExecutionState:
    client_token: object
    operation_id: str
    timeout: float | None
    deadline: float | None
    task_name: str
    mutation_recovery: TaskRecoveryMetadata | None = None


_current_task_execution: ContextVar[_TaskExecutionState | None] = ContextVar(
    "adcp_current_task_execution", default=None
)


def _set_task_execution(state: _TaskExecutionState) -> Token[_TaskExecutionState | None]:
    return _current_task_execution.set(state)


def _reset_task_execution(token: Token[_TaskExecutionState | None]) -> None:
    _current_task_execution.reset(token)


def _get_task_execution(client_token: object) -> _TaskExecutionState | None:
    state = _current_task_execution.get()
    if state is None or state.client_token is not client_token:
        return None
    return state


def mark_task_dispatched(
    client_token: object | None,
    task_name: str,
    *,
    mutating: bool,
    idempotency_key: str | None,
) -> None:
    """Record that a protocol call may now have reached the seller."""

    state = _current_task_execution.get()
    if state is None or state.client_token is not client_token:
        return
    if state.deadline is not None and asyncio.get_running_loop().time() >= state.deadline:
        raise _TaskDeadlineExpiredError
    if mutating and idempotency_key is not None and state.mutation_recovery is None:
        state.mutation_recovery = TaskRecoveryMetadata(
            task_name=task_name,
            operation_id=state.operation_id,
            idempotency_key=idempotency_key,
        )


_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, asyncio.TimeoutError)


class _BodyTimeoutError(Exception):
    """Distinguish an inner transport timeout from the task deadline."""

    def __init__(self, cause: BaseException):
        self.cause = cause
        super().__init__(str(cause))


class _TaskDeadlineExpiredError(BaseException):
    """Raised only when the SDK-owned task deadline expires."""


async def run_with_timeout(awaitable_factory: Callable[[], Awaitable[T]], timeout: float) -> T:
    """Run under ``asyncio.wait_for`` without relabeling inner timeouts."""

    async def run() -> T:
        try:
            return await awaitable_factory()
        except _TIMEOUT_ERRORS as exc:
            raise _BodyTimeoutError(exc) from exc

    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except _BodyTimeoutError as exc:
        raise exc.cause from exc.cause.__cause__
    except _TIMEOUT_ERRORS as exc:
        raise _TaskDeadlineExpiredError from exc
