"""Public adopter surface for the v6.0 DecisioningPlatform framework.

Two entry points:

* :func:`create_adcp_server_from_platform` — build the
  :class:`PlatformHandler` + supporting machinery (executor, registry)
  from a :class:`DecisioningPlatform` instance and return them as a
  3-tuple ``(handler, executor, registry)``. Adopters wanting to
  compose with their own MCP/A2A wiring use this seam.

* :func:`serve` — the one-call wrapper that builds the handler AND
  starts the MCP server. Most adopters call this. Mirrors
  :func:`adcp.server.serve` for parity with the existing handler
  workflow.

Stage-3 wiring per the dispatch design doc:

* D5 — explicit ``ThreadPoolExecutor`` for sync platform methods,
  with three configuration knobs (``executor=`` / ``thread_pool_size=``
  / default ``min(32, cpu+4)``). Mutually exclusive validation;
  framework owns lifecycle for default pools.
* Emma #8 — production-mode gate on :class:`InMemoryTaskRegistry`.
  Reads ``ADCP_ENV`` (case-insensitive ``{"prod", "production"}`` —
  same convention as
  :func:`adcp.validation.client_hooks._default_response_mode`). Refuses
  to start in production with the in-memory registry unless
  ``ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1`` is set.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from adcp.decisioning.dispatch import validate_platform
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.task_registry import InMemoryTaskRegistry
from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from adcp.decisioning.platform import DecisioningPlatform
    from adcp.decisioning.resolve import ResourceResolver
    from adcp.decisioning.state import StateReader
    from adcp.decisioning.task_registry import TaskRegistry


def _is_production_env() -> bool:
    """Detect production via ``ADCP_ENV`` env var.

    Case-insensitive ``{"prod", "production"}`` — matches the existing
    SDK convention at
    :func:`adcp.validation.client_hooks._default_response_mode` (the
    same env var the validation hook reads). Reused here so adopters
    don't manage two prod-detection mechanisms.
    """
    val = os.environ.get("ADCP_ENV", "").strip().lower()
    return val in {"prod", "production"}


def _default_thread_pool_size() -> int:
    """Default executor size — ``min(32, cpu+4)`` per Python stdlib's
    own ThreadPoolExecutor default. Adequate for hello-world / local
    dev; sellers running sync DB drivers under load bump via
    ``thread_pool_size=`` (or supply a custom ``executor=``).
    """
    return min(32, (os.cpu_count() or 1) + 4)


def create_adcp_server_from_platform(
    platform: DecisioningPlatform,
    *,
    executor: ThreadPoolExecutor | None = None,
    thread_pool_size: int | None = None,
    registry: TaskRegistry | None = None,
    state_reader: StateReader | None = None,
    resource_resolver: ResourceResolver | None = None,
) -> tuple[PlatformHandler, ThreadPoolExecutor, TaskRegistry]:
    """Build the :class:`PlatformHandler` + supporting wiring from a
    :class:`DecisioningPlatform`.

    Returns a 3-tuple ``(handler, executor, registry)``. The handler
    wraps the platform; the executor is wired into dispatch for sync
    platform methods; the registry handles
    :class:`adcp.decisioning.TaskHandoff` lifecycle.

    Adopters who need full control over the MCP server wiring use this
    seam — compose the returned handler with their own
    :func:`adcp.server.create_mcp_server` call. Most adopters use
    :func:`serve` instead.

    Validates the platform at server boot via
    :func:`validate_platform` — fails fast on missing specialism
    methods, missing ``accounts``, governance opt-in violations
    (D15 round-4), and unknown specialisms (UserWarning per round-3
    D14).

    :param platform: The adopter's :class:`DecisioningPlatform`
        subclass instance.
    :param executor: Bring-your-own :class:`ThreadPoolExecutor` —
        for operators with audit-instrumented thread pools or
        wrappers around stdlib's executor. Mutually exclusive with
        ``thread_pool_size``. Operator owns lifecycle (caller's
        ``shutdown(wait=True)`` responsibility).
    :param thread_pool_size: Size the default framework-allocated
        executor. Mutually exclusive with ``executor``. Default is
        :func:`_default_thread_pool_size`.
    :param registry: Bring-your-own :class:`TaskRegistry` — typically
        a v6.1 durable backing store. Default is
        :class:`InMemoryTaskRegistry`, which the production-mode
        gate refuses unless
        ``ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1`` is set.
    :param state_reader: Custom :class:`StateReader` impl
        (D15 — workflow-state reads). Default is the v6.0 stub
        (empty returns + one-time UserWarning per method).
    :param resource_resolver: Custom :class:`ResourceResolver` impl
        (D15 — async framework-mediated fetches). Default is the
        v6.0 stub (raises ``NotImplementedError`` with a pointer to
        v6.1).

    :raises ValueError: when ``executor`` and ``thread_pool_size`` are
        both supplied (D5 mutually-exclusive validation).
    :raises AdcpError: from :func:`validate_platform` when the
        platform fails server-boot validation, OR when the production
        gate refuses :class:`InMemoryTaskRegistry`.
    """
    # D5: executor / thread_pool_size mutually exclusive.
    if executor is not None and thread_pool_size is not None:
        raise ValueError(
            "Pass either executor= or thread_pool_size=, not both. "
            "thread_pool_size sizes the default executor; executor= is "
            "for operators wiring an audit-instrumented or otherwise "
            "vetted threadpool."
        )

    # Allocate executor.
    if executor is None:
        size = thread_pool_size if thread_pool_size is not None else _default_thread_pool_size()
        executor = ThreadPoolExecutor(
            max_workers=size,
            thread_name_prefix="adcp-decisioning-",
        )

    # Allocate registry, with production-mode gate (Emma #8).
    # Gate reads the registry's is_durable class-level marker rather
    # than `isinstance(registry, InMemoryTaskRegistry)`. Two reasons:
    #   1. Adopters subclassing InMemoryTaskRegistry for instrumentation
    #      inherit `is_durable=False` and correctly trip the gate.
    #   2. Adopters duck-typing a custom in-memory store would bypass
    #      the isinstance check; the marker is opt-in for durability,
    #      defaulting safe.
    if registry is None:
        registry = InMemoryTaskRegistry()
    # Round-5 Emma P1: an adopter duck-typing TaskRegistry without the
    # is_durable marker would treat the missing attribute as False and
    # silently trip the production gate — operator sees "non-durable
    # registry refused" with no clear cause. Distinguish "marker
    # absent" from "marker present and False" so the diagnostic
    # points at the real problem.
    has_marker = hasattr(type(registry), "is_durable") or hasattr(registry, "is_durable")
    is_durable = bool(getattr(registry, "is_durable", False))
    if not has_marker:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                f"TaskRegistry impl {type(registry).__name__!r} is missing "
                "the ``is_durable: ClassVar[bool]`` marker. The framework's "
                "production-mode gate requires every registry to declare "
                "durability explicitly — set ``is_durable = True`` (durable "
                "backing store like Postgres/Redis) or ``is_durable = False`` "
                "(in-memory / lossy). Without the marker, the gate would "
                "silent-deny the registry with a confusing 'non-durable' "
                "error."
            ),
            recovery="terminal",
            details={
                "registry": type(registry).__name__,
            },
        )
    if not is_durable and _is_production_env():
        opt_in = os.environ.get("ADCP_DECISIONING_ALLOW_INMEMORY_TASKS", "").strip()
        if opt_in != "1":
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    f"Non-durable TaskRegistry ({type(registry).__name__}) "
                    "refuses to start in production (ADCP_ENV is 'prod' "
                    "or 'production'). HITL flows depend on the registry "
                    "— silent in-memory fallback would lose tasks across "
                    "process restarts. Either wire a durable "
                    "TaskRegistry impl (set is_durable=True on the class; "
                    "v6.1 ships PostgresTaskRegistry) OR set "
                    "ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1 to "
                    "explicitly opt into in-memory tasks (e.g., for "
                    "single-process pilots)."
                ),
                recovery="terminal",
                details={
                    "registry": type(registry).__name__,
                    "is_durable": is_durable,
                    "ADCP_ENV": os.environ.get("ADCP_ENV", ""),
                },
            )

    # Validate the platform AFTER executor + registry exist so any
    # validation diagnostic includes the wiring context. Failure here
    # propagates to the caller.
    validate_platform(platform)

    handler = PlatformHandler(
        platform,
        executor=executor,
        registry=registry,
        state_reader=state_reader,
        resource_resolver=resource_resolver,
    )
    return handler, executor, registry


def serve(
    platform: DecisioningPlatform,
    *,
    name: str | None = None,
    executor: ThreadPoolExecutor | None = None,
    thread_pool_size: int | None = None,
    registry: TaskRegistry | None = None,
    state_reader: StateReader | None = None,
    resource_resolver: ResourceResolver | None = None,
    advertise_all: bool = False,
    **serve_kwargs: Any,
) -> None:
    """One-call wrapper — build the handler and serve over MCP.

    Most adopters use this. For full control, use
    :func:`create_adcp_server_from_platform` and compose with
    :func:`adcp.server.create_mcp_server` / ``serve()`` directly.

    :param platform: The :class:`DecisioningPlatform` subclass
        instance.
    :param name: Server name advertised on AdCP capabilities. Defaults
        to the platform class's ``__name__``.
    :param executor: BYO :class:`ThreadPoolExecutor` per
        :func:`create_adcp_server_from_platform` D5 contract.
    :param thread_pool_size: Default-executor size override.
    :param registry: BYO :class:`TaskRegistry`. Default is
        :class:`InMemoryTaskRegistry` (gated for production).
    :param state_reader: Custom :class:`StateReader` impl (D15).
    :param resource_resolver: Custom :class:`ResourceResolver` impl (D15).
    :param advertise_all: Forwarded to :func:`adcp.server.serve`. When
        ``True``, ``tools/list`` advertises every method on the
        handler regardless of override status. Default ``False`` —
        the override-detection filter trims unimplemented platform
        methods. Adopters with explicit-not-supported intent (e.g.,
        spec-compliance storyboards) pass ``True``.
    :param serve_kwargs: Forwarded to :func:`adcp.server.serve`. Use
        for ``host``, ``port``, ``transport``, ``test_controller``,
        ``context_factory``, ``middleware``, etc.
    """
    # Local import to avoid a circular at module-load time. Adopter
    # serves never run during foundation imports anyway.
    from adcp.server.serve import serve as _adcp_serve

    handler, _executor, _registry = create_adcp_server_from_platform(
        platform,
        executor=executor,
        thread_pool_size=thread_pool_size,
        registry=registry,
        state_reader=state_reader,
        resource_resolver=resource_resolver,
    )

    server_name = name or type(platform).__name__
    _adcp_serve(
        handler,
        name=server_name,
        advertise_all=advertise_all,
        **serve_kwargs,
    )


__all__ = [
    "create_adcp_server_from_platform",
    "serve",
]
