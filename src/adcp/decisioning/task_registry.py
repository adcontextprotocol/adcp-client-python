"""Task registry for the DecisioningPlatform handoff path.

Defines:

* :class:`TaskRegistry` Protocol — the seam adopters substitute when
  they need a durable backing store (PostgreSQL, Redis, etc.). The
  Protocol shape is pinned with per-method contract docstrings; D7 of
  the dispatch design names every invariant.
* :class:`InMemoryTaskRegistry` — the v6.0 reference implementation.
  Process-local, lossy on restart. Suitable for local dev, CI, and
  test fixtures; production deployments running ``sales-broadcast-tv``
  or any HITL flow refuse to start without an explicit opt-in (see
  :func:`adcp.decisioning.serve.serve` Stage 3 wiring).
* :class:`TaskHandoffContext` — what the framework passes into the
  adopter's handoff callable when ``ctx.handoff_to_task(fn)`` fires.
  Carries the framework-issued task id plus ``update(progress)`` and
  ``heartbeat()`` affordances.

The registry's storage shape is intentionally minimal:
``{task_id → TaskRecord}`` keyed by the framework-allocated UUID.
Cross-tenant access control is enforced via the optional
``expected_account_id`` argument on :meth:`TaskRegistry.get` — sellers
threading ``ctx.account.id`` through to ``tasks/get`` get a None
return on mismatch (no principal-enumeration via task_id probing).

Production-mode gate (Emma #8 / round-4):
:func:`adcp.decisioning.serve.serve` reads ``ADCP_ENV`` (case-insensitive
``{"prod", "production"}`` — same as
:func:`adcp.validation.client_hooks._default_response_mode`) and
refuses to wire :class:`InMemoryTaskRegistry` in production unless
``ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1`` is set. Sales-broadcast-tv
adopters are structurally forced into the HITL path which depends on
the registry — silent in-memory fallback is a real prod foot-gun.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Terminal task states per AdCP 3.0 spec (``enums/task-status.json``).
#: ``submitted`` = task created but not yet started; ``working`` = adopter
#: callback running; ``completed`` / ``failed`` = terminal.
TaskState = Literal["submitted", "working", "completed", "failed"]


@dataclass
class TaskRecord:
    """The framework's per-task storage row.

    Internal to the registry impl — adopters don't construct these.
    The Protocol surface returns dicts on :meth:`TaskRegistry.get`
    rather than the dataclass directly so the storage shape stays
    swappable (a Postgres impl might return a different row class).

    :param task_id: Framework-allocated UUID. Stable across the
        task's lifetime.
    :param account_id: Account that owns the task. Used for the
        cross-tenant access-control check in :meth:`TaskRegistry.get`.
    :param state: Terminal state lifecycle. Transitions are
        framework-driven; adopters drive completion via
        :meth:`TaskHandoffContext.update` and the dispatcher calls
        :meth:`TaskRegistry.complete` / :meth:`TaskRegistry.fail` at
        the end of the handoff fn.
    :param task_type: Wire-spec task type (``'create_media_buy'``,
        ``'sync_creatives'``, etc.). Stored on the registry record so
        ``tasks/get`` can return it on the response payload; NOT part
        of the synchronous Submitted envelope (per
        ``schemas/cache/core/protocol-envelope.json``).
    :param progress: Latest progress payload written by
        :meth:`TaskHandoffContext.update`. Buyers see this on
        ``tasks/get`` while the task is in the ``working`` state.
    :param result: Terminal artifact set by :meth:`TaskRegistry.complete`.
        MUST be the JSON-serialized spec response shape (e.g. a
        ``CreateMediaBuySuccessResponse`` projected through
        ``model_dump()``). v6.1 adds size enforcement; for now the
        registry trusts adopters.
    :param error: Terminal failure payload set by
        :meth:`TaskRegistry.fail`. MUST be the
        :meth:`AdcpError.to_wire` shape so ``tasks/get`` returns the
        spec ``adcp_error`` envelope verbatim.
    :param created_at: Monotonic creation timestamp (Unix epoch
        seconds). Adopters get the exact value the framework stored;
        useful for SLA dashboards.
    :param updated_at: Last-touched timestamp. Updated on every state
        transition AND every :meth:`TaskHandoffContext.update` call.
    """

    task_id: str
    account_id: str
    state: TaskState
    task_type: str
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for buyer consumption via ``tasks/get``.

        Adopters or middleware reading the dict shape get the exact
        wire-relevant fields. ``created_at`` / ``updated_at`` are
        included so admin tooling can build SLA reports.
        """
        return {
            "task_id": self.task_id,
            "account_id": self.account_id,
            "state": self.state,
            "task_type": self.task_type,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@runtime_checkable
class TaskRegistry(Protocol):
    """Per-account task store — the seam adopters substitute for a
    durable backing implementation.

    **Durability marker** (``is_durable: ClassVar[bool]``):

    Production deployments running ``sales-broadcast-tv`` or any HITL
    flow refuse to start with a non-durable registry unless the
    operator explicitly opts in via
    ``ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1``. The framework reads
    ``registry.is_durable`` to make this decision; subclassing
    :class:`InMemoryTaskRegistry` for instrumentation does NOT bypass
    the gate (the subclass inherits ``is_durable = False``). Custom
    durable impls MUST set ``is_durable = True`` explicitly. The
    Protocol declares this as a class-level ``bool``.

    Lifecycle (framework-driven; adopters call only :meth:`TaskHandoffContext`
    methods, not these directly):

    1. Dispatch detects ``ctx.handoff_to_task(fn)`` returned from a
       platform method. Allocates a task_id and calls :meth:`issue` to
       persist the ``submitted`` row.
    2. Dispatch projects the wire ``Submitted`` envelope to the buyer.
    3. Dispatch runs ``fn(task_handoff_ctx)`` in the background. The
       adopter calls ``task_handoff_ctx.update(progress)`` zero or
       more times; the framework routes each to :meth:`update_progress`
       (also transitions ``submitted`` → ``working`` on first update).
    4. When ``fn`` returns, dispatch calls :meth:`complete` with the
       terminal artifact (a JSON-serialized spec response).
    5. When ``fn`` raises :class:`adcp.decisioning.AdcpError` (or any
       exception, wrapped to ``INTERNAL_ERROR``), dispatch calls
       :meth:`fail` with the wire-shaped error payload.

    All write paths set ``updated_at = now``. The registry is
    expected to be safe for concurrent reads; concurrent writes to
    the same task are serialized by the dispatcher (one ``fn`` per
    handoff, no concurrent `update_progress`/`complete` against the
    same task_id).

    Cross-tenant safety: every read MUST be account-scoped. The
    :meth:`get` method takes an optional ``expected_account_id`` —
    when supplied (the wire ``tasks/get`` path always supplies it),
    a mismatch returns ``None``, NOT the raw record. Adopters
    implementing custom registries MUST honor this: returning a
    cross-tenant record on probe enables principal-enumeration via
    task_id guessing. See
    ``tests/test_decisioning_task_registry_cross_tenant.py`` for
    the regression suite.
    """

    #: Whether this registry persists tasks across process restarts.
    #: ``False`` for in-memory / lossy impls; ``True`` for durable
    #: backings (PostgreSQL, Redis, etc.). The framework's
    #: production-mode gate refuses non-durable registries unless
    #: the operator explicitly opts in via
    #: ``ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1``.
    is_durable: ClassVar[bool]

    async def issue(
        self,
        *,
        account_id: str,
        task_type: str,
    ) -> str:
        """Allocate a fresh task_id, persist a ``submitted`` row, and
        return the id.

        :param account_id: Account that owns the task. Drives the
            cross-tenant access check on subsequent reads.
        :param task_type: Wire-spec task type (``'create_media_buy'``,
            etc.). Persisted on the row and surfaced on ``tasks/get``
            reads; NOT included in the synchronous Submitted envelope
            (per ``schemas/cache/core/protocol-envelope.json``).
        :returns: The framework-allocated task_id (string UUID).
        """
        ...

    async def update_progress(
        self,
        task_id: str,
        progress: dict[str, Any],
    ) -> None:
        """Write a progress payload and transition ``submitted`` →
        ``working`` on first call. No-op transition on subsequent
        calls (already in ``working``).

        Errors here are swallowed by the dispatch wrapper — a transient
        registry write failure must NOT abort the adopter's background
        handoff. Buyer-facing impact is a missed progress event, not a
        failed task. Adopter impls of this method that need durability
        guarantees should buffer + retry internally.
        """
        ...

    async def complete(
        self,
        task_id: str,
        result: dict[str, Any],
    ) -> None:
        """Mark the task ``completed`` with ``result`` as the terminal
        artifact.

        ``result`` MUST be the JSON-serialized spec response shape
        (e.g. ``CreateMediaBuySuccessResponse`` via ``model_dump()``).
        Idempotent on repeated calls with equal ``result``;
        non-idempotent re-completion with different result raises
        ``ValueError``.
        """
        ...

    async def fail(
        self,
        task_id: str,
        error: dict[str, Any],
    ) -> None:
        """Mark the task ``failed`` with ``error`` as the terminal
        wire-shaped error payload.

        ``error`` MUST be the :meth:`AdcpError.to_wire` shape so
        ``tasks/get`` round-trips the spec ``adcp_error`` envelope
        verbatim. Idempotent on repeated calls with equal ``error``.
        """
        ...

    async def get(
        self,
        task_id: str,
        *,
        expected_account_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up a task record. Cross-tenant probes return ``None``.

        :param task_id: Framework-allocated id from a prior :meth:`issue`.
        :param expected_account_id: When supplied, the registry MUST
            return ``None`` if the stored record's ``account_id`` does
            not match. The wire ``tasks/get`` path always supplies the
            authenticated principal's account_id so adopters can't
            probe across tenants.
        :returns: The record dict (per :meth:`TaskRecord.to_dict`) or
            ``None`` if the id is unknown OR a cross-tenant mismatch.
        """
        ...

    async def discard(self, task_id: str) -> None:
        """Remove a task_id from the registry — rollback path.

        Used by the WorkflowHandoff dispatch projection
        (:func:`adcp.decisioning.dispatch._project_workflow_handoff`)
        when the adopter's enqueue fn raises after the task_id has
        been allocated. Without rollback, the buyer would receive a
        Submitted envelope referencing an orphan task_id their
        external workflow never registered.

        Idempotent: discarding an unknown task_id is a no-op (no
        raise). The discard window is tightly scoped — between
        ``issue()`` and the framework's projection step, with the
        adopter's enqueue fn in between. In practice this is a few
        milliseconds.

        Adopters MUST NOT call ``discard`` on a task that has
        progressed past ``submitted`` — that's the wrong recovery
        path; use ``fail()`` instead.
        """
        ...


# ---------------------------------------------------------------------------
# In-memory reference implementation — v6.0 ships this; v6.1 lands a
# durable Postgres-backed counterpart that implements the same Protocol.
# ---------------------------------------------------------------------------


class InMemoryTaskRegistry:
    """Process-local task registry — v6.0 reference implementation.

    Storage is a plain ``dict[str, TaskRecord]`` guarded by an
    :class:`asyncio.Lock`. Adequate for local dev, CI, and test
    fixtures; production deployments wire a durable counterpart
    (PostgreSQL, Redis, etc.) implementing the same :class:`TaskRegistry`
    Protocol.

    Production-mode gate: :func:`adcp.decisioning.serve.serve` refuses
    to wire this when ``ADCP_ENV`` indicates production unless
    ``ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1`` is set. The gate
    reads ``registry.is_durable``; subclassing this class for
    instrumentation does NOT bypass the gate (the ``False`` is
    inherited). Custom durable impls set ``is_durable = True``
    explicitly. Production sellers running ``sales-broadcast-tv``
    or any HITL flow get the explicit refusal so silent in-memory
    fallback can't bite oncall.
    """

    is_durable: ClassVar[bool] = False

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()

    async def issue(
        self,
        *,
        account_id: str,
        task_type: str,
    ) -> str:
        # Reject empty/unset account_id at issue-time. Without this,
        # two tenants whose AccountStore returns Account(id="") or the
        # default Account(id="<unset>") share a cache scope class and
        # can read each other's tasks via cross-tenant probe (the
        # equality check passes when both are empty). See
        # tests/test_decisioning_task_registry_cross_tenant.py for the
        # regression suite.
        if not account_id or not account_id.strip() or account_id == "<unset>":
            raise ValueError(
                f"account_id must be a non-empty, non-default string; "
                f"got {account_id!r}. AccountStore.resolve must always "
                "return Account(id=<non-empty>) so cross-tenant cache "
                "scoping works correctly."
            )
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        async with self._lock:
            self._records[task_id] = TaskRecord(
                task_id=task_id,
                account_id=account_id,
                state="submitted",
                task_type=task_type,
            )
        return task_id

    async def update_progress(
        self,
        task_id: str,
        progress: dict[str, Any],
    ) -> None:
        async with self._lock:
            record = self._records.get(task_id)
            if record is None:
                # Silent no-op — the dispatch wrapper expects this method
                # to never raise on transient lookup failure (see Protocol
                # docstring).
                return
            if record.state in ("completed", "failed"):
                # Terminal-state guard: a late progress update from a
                # straggler coroutine MUST NOT mutate a finalized record
                # — it would resurrect "working" appearance against
                # ``tasks/get`` reads that already saw the terminal
                # state. Log + drop is the safe choice (the dispatch
                # wrapper is expected to swallow update failures
                # anyway).
                logger.warning(
                    "InMemoryTaskRegistry.update_progress(task_id=%s) "
                    "dropped: task is already in terminal state %r",
                    task_id,
                    record.state,
                )
                return
            record.progress = dict(progress)
            if record.state == "submitted":
                record.state = "working"
            record.updated_at = time.time()

    async def complete(
        self,
        task_id: str,
        result: dict[str, Any],
    ) -> None:
        async with self._lock:
            record = self._records.get(task_id)
            if record is None:
                raise ValueError(f"Task {task_id!r} not found")
            if record.state == "completed":
                if record.result == result:
                    return  # idempotent
                raise ValueError(f"Task {task_id!r} already completed with a different result")
            record.state = "completed"
            record.result = dict(result)
            record.updated_at = time.time()

    async def fail(
        self,
        task_id: str,
        error: dict[str, Any],
    ) -> None:
        async with self._lock:
            record = self._records.get(task_id)
            if record is None:
                raise ValueError(f"Task {task_id!r} not found")
            if record.state == "failed":
                if record.error == error:
                    return  # idempotent
                raise ValueError(f"Task {task_id!r} already failed with a different error")
            record.state = "failed"
            record.error = dict(error)
            record.updated_at = time.time()

    async def get(
        self,
        task_id: str,
        *,
        expected_account_id: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return None
            if expected_account_id is not None and record.account_id != expected_account_id:
                # Cross-tenant probe — return None, NOT raw record.
                # Critical security boundary: returning the record
                # here enables principal-enumeration via task_id
                # probing. The dispatch path that calls this method
                # always passes the authenticated principal's
                # account_id; adopter impls implementing this Protocol
                # MUST preserve this behavior.
                return None
            return record.to_dict()

    async def discard(self, task_id: str) -> None:
        async with self._lock:
            # Idempotent: pop with default. The Protocol contract
            # tolerates discarding an unknown id (no raise) so the
            # WorkflowHandoff projection's rollback can be unconditional.
            self._records.pop(task_id, None)


# ---------------------------------------------------------------------------
# TaskHandoffContext — what the framework passes into adopter handoff fns
# ---------------------------------------------------------------------------


@dataclass
class TaskHandoffContext:
    """Per-task context passed to the handoff fn registered via
    :meth:`adcp.decisioning.RequestContext.handoff_to_task`.

    Adopter pattern::

        def create_media_buy(self, req, ctx):
            if self._needs_review(req):
                return ctx.handoff_to_task(self._async_review)

            return CreateMediaBuySuccess(media_buy_id="mb_1", ...)

        async def _async_review(self, task_ctx: TaskHandoffContext):
            await task_ctx.update({"message": "Trafficker reviewing"})
            decision = await self._wait_for_trafficker(task_ctx.id)
            return CreateMediaBuySuccess(media_buy_id=decision.id, ...)

    The framework allocates ``task_ctx.id`` BEFORE invoking the
    handoff fn so the adopter can persist the id to its own backend
    (storyboard runner row, Slack thread reference, etc.) before
    kicking off slow work. This fixes a documented v1 ergonomics bug
    where adopters could only learn the task_id AFTER returning.

    Constructed by :func:`adcp.decisioning.dispatch._build_handoff_context`;
    never instantiated by adopter code.
    """

    id: str
    _registry: TaskRegistry
    _heartbeat_impl: Callable[[], Awaitable[None]] = field(default_factory=lambda: _noop_heartbeat)

    async def update(self, progress: dict[str, Any]) -> None:
        """Write a progress payload. Transitions ``submitted`` →
        ``working`` on first call.

        Errors are swallowed (logged at WARNING with traceback):
        a transient registry write failure must not abort the handoff.
        Buyer-facing impact is a missed progress event, not a failed
        task. Adopters who need delivery guarantees plug a durable
        registry; the warning surfaces the transient via existing
        observability hooks so silent loss isn't truly invisible.
        """
        try:
            await self._registry.update_progress(self.id, progress)
        except Exception:
            logger.warning(
                "TaskHandoffContext.update(task_id=%s) suppressed "
                "registry transient — progress event lost; handoff "
                "continues",
                self.id,
                exc_info=True,
            )
            return

    async def heartbeat(self) -> None:
        """Liveness signal for operator infrastructure. v6.1 stub.

        v6.0 ships as a no-op so adopter code calling
        ``await task_ctx.heartbeat()`` future-proofs against the
        eventual implementation. Operator-side TTL-reset wiring lands
        with the durable registry impl.
        """
        await self._heartbeat_impl()


async def _noop_heartbeat() -> None:
    """Default no-op heartbeat — adequate for v6.0."""
    await asyncio.sleep(0)


__all__ = [
    "InMemoryTaskRegistry",
    "TaskHandoffContext",
    "TaskRecord",
    "TaskRegistry",
    "TaskState",
]
