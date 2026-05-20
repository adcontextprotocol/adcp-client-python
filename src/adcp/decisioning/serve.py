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
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from adcp.decisioning.dispatch import validate_platform
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.task_registry import InMemoryTaskRegistry
from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from adcp.decisioning.implementation_config import ProductConfigStore
    from adcp.decisioning.platform import DecisioningPlatform
    from adcp.decisioning.property_list import PropertyListFetcher
    from adcp.decisioning.registry import BuyerAgentRegistry
    from adcp.decisioning.resolve import ResourceResolver
    from adcp.decisioning.state import StateReader
    from adcp.decisioning.task_registry import TaskRegistry
    from adcp.webhook_sender import WebhookSender
    from adcp.webhook_supervisor import WebhookDeliverySupervisor


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
    webhook_sender: WebhookSender | None = None,
    webhook_supervisor: WebhookDeliverySupervisor | None = None,
    auto_emit_completion_webhooks: bool = True,
    buyer_agent_registry: BuyerAgentRegistry | None = None,
    config_store: ProductConfigStore | None = None,
    property_list_fetcher: PropertyListFetcher | None = None,
    advertise_all: bool = False,
    validate_at_init: bool = True,
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
    :param webhook_sender: Bring-your-own
        :class:`adcp.webhook_sender.WebhookSender` for sync-completion
        and HITL-completion webhook delivery. Default ``None``. The
        sender is the *transport* — one HTTP-Signatures POST per call,
        no retry, no breaker. Production sellers typically wrap the
        sender in a :class:`~adcp.webhook_supervisor.WebhookDeliverySupervisor`
        and pass that via ``webhook_supervisor=`` instead.
    :param webhook_supervisor: Bring-your-own
        :class:`~adcp.webhook_supervisor.WebhookDeliverySupervisor` for
        reliable delivery (retry, circuit breaker, attempt audit). When
        passed, the F12 auto-emit path routes through it instead of
        ``webhook_sender``. The reference
        :class:`~adcp.webhook_supervisor.InMemoryWebhookDeliverySupervisor`
        wraps a sender; adopters with infra-side retry (Celery, Kafka,
        durable outbox) implement the Protocol against their queue.
        Mutually optional with ``webhook_sender``; passing both is
        valid (supervisor wins for auto-emit, sender remains available
        for direct calls inside platform methods).
    :param buyer_agent_registry: BYO
        :class:`adcp.decisioning.BuyerAgentRegistry` — the v3 commercial
        identity layer. When wired, the framework calls the registry
        BEFORE :meth:`AccountStore.resolve` to gate every request on
        the seller's commercial allowlist. Suspended / blocked /
        unrecognized agents are rejected with structured errors:
        suspended → ``AGENT_SUSPENDED``, blocked → ``AGENT_BLOCKED``
        (both ``recovery="terminal"``, no ``details`` payload — the
        code itself is the discriminator per AdCP 3.1); unrecognized
        → ``PERMISSION_DENIED`` with no ``details.scope`` so the wire
        shape does not enumerate which ``agent_url``s are onboarded
        with this seller. The resolved
        :class:`adcp.decisioning.BuyerAgent` is threaded onto
        :attr:`RequestContext.buyer_agent` so platform methods can
        read commercial context (billing capabilities, default terms,
        adopter ext) without a second registry call. Default ``None``
        — pre-trust beta adopters running existing key-based auth
        without commercial gating omit this and the dispatch path
        falls through to ``AccountStore.resolve`` unchanged.
    :param auto_emit_completion_webhooks: F12 feature gate. When
        ``True`` (default), the framework auto-fires a completion
        webhook on the sync-success arm of mutating tools whenever the
        request supplied ``push_notification_config.url`` AND the tool
        is in :data:`adcp.decisioning.webhook_emit.SPEC_WEBHOOK_TASK_TYPES`.
        Buyers passing the URL expect notification regardless of
        whether the seller routed sync vs HITL. Set ``False`` for
        adopters who emit webhooks manually inside their handlers
        (avoid duplicate delivery; idempotency-key dedup at the
        receiver would handle it but explicit suppression matches the
        v5 manual-emit posture for adopters mid-migration).
    :param advertise_all: Mirror of the same flag on :func:`serve` —
        controls how :meth:`PlatformHandler.get_advertised_tools` and
        the eventual ``tools/list`` response filter the handler's tool
        universe. ``False`` (default, spec-aligned) drops tools whose
        method is still the SDK's ``not_supported`` shim; ``True``
        advertises every tool the platform's claimed specialisms cover
        regardless of override status. Stored on the returned handler
        so adopters can call ``handler.get_advertised_tools()`` to
        inspect the effective set without standing up a server.
    :param validate_at_init: When ``True`` (default), the framework
        runs :func:`validate_capabilities_response_shape` during
        construction — fail-fast boot validation for the projected
        capabilities response. The sync validator drives the async
        handler via :func:`asyncio.run`, so the call **fails** with
        ``RuntimeError: asyncio.run() cannot be called from a running
        event loop`` when the constructor is invoked from inside an
        async context (test fixtures, Starlette ``lifespan``,
        in-process A2A test clients). Pass ``False`` in those
        contexts and run the async validator yourself::

            handler, executor, registry = create_adcp_server_from_platform(
                platform, validate_at_init=False,
            )
            await validate_capabilities_response_shape_async(handler)

        The other boot validators (:func:`validate_platform`,
        :func:`validate_webhook_signing_for_capabilities`,
        :func:`validate_idempotency_wiring`) are synchronous-pure and
        always run; this flag only gates the capabilities-response
        check. See #700.

    To wire a :class:`ProposalManager` (v1 two-platform composition),
    pass it on a :class:`PlatformRouter` via
    ``proposal_managers={tenant_id: ProposalManager}``. The router is
    the per-tenant binding point — single-tenant adopters use a
    one-entry router (``platforms={"default": ...}``,
    ``proposal_managers={"default": ...}``). See
    ``docs/proposals/product-architecture.md`` § "Tenant binding model".

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
                    "v6.1 ships PgTaskRegistry) OR set "
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
        webhook_sender=webhook_sender,
        webhook_supervisor=webhook_supervisor,
        auto_emit_completion_webhooks=auto_emit_completion_webhooks,
        buyer_agent_registry=buyer_agent_registry,
        config_store=config_store,
        property_list_fetcher=property_list_fetcher,
        advertise_all=advertise_all,
    )

    # Boot-time fail-fast: property_list_filtering declared but no fetcher wired.
    from adcp.decisioning.property_list import (
        property_list_capability_enabled,
        validate_property_list_config,
    )

    validate_property_list_config(
        capability_enabled=property_list_capability_enabled(platform),
        fetcher=property_list_fetcher,
    )

    # F12 boot-time fail-fast (Emma sales-direct P0 root cause): if
    # the platform's claimed specialisms expose any spec-eligible
    # webhook task type (create_media_buy, activate_signal, etc.) AND
    # auto-emit is on AND no webhook_sender is wired, every buyer
    # ``push_notification_config.url`` would silently drop. Catch at
    # boot so adopters discover the misconfig before shipping. Same
    # posture as validate_platform's governance opt-in gate.
    #
    # Uses the per-instance advertised set (NOT the class-level
    # universe). A platform that doesn't claim any
    # webhook-eligible-tool-bearing specialism (test fixtures,
    # discovery-only agents) doesn't trigger the gate.
    from adcp.decisioning.webhook_emit import (
        validate_webhook_sender_for_platform,
        validate_webhook_signing_for_capabilities,
    )

    validate_webhook_sender_for_platform(
        advertised_tools=handler.advertised_tools_for_instance(),
        sender=webhook_sender,
        supervisor=webhook_supervisor,
        auto_emit=auto_emit_completion_webhooks,
    )

    # Issue #384: a platform advertising webhook_signing.supported=True
    # must wire a JWK-signing sender. The check is independent of the
    # auto-emit gate above — manually-emitted webhooks signed by the
    # platform handler also need to honor the capability advertisement.
    validate_webhook_signing_for_capabilities(
        capabilities=platform.capabilities,
        sender=webhook_sender,
        supervisor=webhook_supervisor,
    )

    # DX #422: boot-time fail-fast on a non-conformant capabilities
    # projection. Same posture as validate_platform / F12 — the
    # operator sees one structured AdcpError before the server starts
    # taking traffic, instead of buyers discovering a malformed
    # capabilities envelope on first contact.
    #
    # The sync validator drives the async handler via ``asyncio.run``,
    # which raises ``RuntimeError`` when called from inside an already-
    # running event loop. ``validate_at_init=False`` opts out so async
    # callers (test fixtures, ``lifespan`` handlers, in-process A2A
    # clients) can run the async sibling themselves — see #700.
    if validate_at_init:
        from adcp.decisioning.validate_capabilities import (
            validate_capabilities_response_shape,
        )

        validate_capabilities_response_shape(handler)

    # Boot-time fail-fast: idempotency advertised but no @wrap applied.
    # Buyers reading IdempotencySupported(supported=True) on the
    # capabilities envelope assume retries dedupe; without the
    # decorator, every retry re-executes side effects.
    from adcp.decisioning.validate_idempotency import (
        validate_idempotency_wiring,
    )

    validate_idempotency_wiring(platform)

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
    webhook_sender: WebhookSender | None = None,
    webhook_supervisor: WebhookDeliverySupervisor | None = None,
    auto_emit_completion_webhooks: bool = True,
    buyer_agent_registry: BuyerAgentRegistry | None = None,
    config_store: ProductConfigStore | None = None,
    property_list_fetcher: PropertyListFetcher | None = None,
    advertise_all: bool = False,
    mock_ad_server: Any | None = None,
    enable_debug_endpoints: bool = False,
    pre_validation_hooks: dict[str, Any] | None = None,
    validate_at_init: bool = True,
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
    :param webhook_sender: BYO :class:`adcp.webhook_sender.WebhookSender`
        for completion webhook delivery (sync auto-emit + HITL terminal).
        Transport only — one attempt, no retry. ``None`` disables
        auto-emit silently.
    :param webhook_supervisor: BYO
        :class:`~adcp.webhook_supervisor.WebhookDeliverySupervisor` for
        reliable delivery (retry, circuit breaker, attempt audit).
        Takes precedence over ``webhook_sender`` for F12 auto-emit
        when both are passed. Production sellers typically pass an
        :class:`~adcp.webhook_supervisor.InMemoryWebhookDeliverySupervisor`
        wrapping their sender.
    :param auto_emit_completion_webhooks: F12 — auto-fire a completion
        webhook on the sync-success arm of mutating tools when the
        request supplied ``push_notification_config.url``. Default
        ``True``. Set ``False`` for adopters who emit webhooks
        manually inside their handlers.
    :param mock_ad_server: Optional :class:`adcp.decisioning.MockAdServer`
        whose ``get_traffic()`` is wired into ``GET /_debug/traffic``
        when ``enable_debug_endpoints=True``. Default ``None`` —
        adopters with no anti-façade recorder leave this off.
    :param enable_debug_endpoints: When ``True``, mount
        ``GET /_debug/traffic`` exposing the JSON dict returned by
        ``mock_ad_server.get_traffic()``. Defaults to ``False``;
        production deployments stay closed. Reference / dev sellers
        flip on so storyboard runners can poll outbound call counts.
        Forwarded to :func:`adcp.server.serve`.
    :param advertise_all: Forwarded to :func:`adcp.server.serve`. When
        ``True``, ``tools/list`` advertises every method on the
        handler regardless of override status. Default ``False`` —
        the override-detection filter trims unimplemented platform
        methods. Adopters with explicit-not-supported intent (e.g.,
        spec-compliance storyboards) pass ``True``.
    :param serve_kwargs: Forwarded to :func:`adcp.server.serve`. Use
        for ``host``, ``port``, ``transport``, ``test_controller``,
        ``context_factory``, ``middleware``, ``validation``,
        ``config`` (:class:`adcp.server.ServeConfig` bundle), etc.
        Pass ``config=ServeConfig(transport="a2a", ...)`` to supply
        all server options as a single typed object rather than
        individual kwargs.
        Pass ``validation=ValidationHookConfig(requests="strict",
        responses="strict")`` to enable schema-driven request/response
        validation against the bundled AdCP JSON schemas — sellers who
        want their server to enforce wire conformance turn it on here.
    :param pre_validation_hooks: Optional dict mapping AdCP tool name to
        a ``(tool_name, raw_args) -> raw_args`` callable. The hook runs
        on the raw wire dict **before** schema + Pydantic validation —
        use it to apply spec-mandated defaults for pre-v3 buyers that
        omit required fields. Example::

            serve(
                router,
                pre_validation_hooks={
                    "get_products": lambda n, a: {
                        **a, "buying_mode": a.get("buying_mode", "brief")
                    },
                },
            )

        Hook exceptions surface as ``INVALID_REQUEST`` on the wire.
        The hook receives a shallow copy of the wire args, so it may
        mutate its argument freely or return a new dict — either style
        is safe. Context echo always reflects the original wire input.
    :param validate_at_init: Forwarded to
        :func:`create_adcp_server_from_platform`. Default ``True``
        runs the capabilities-shape boot validator in sync; pass
        ``False`` and run :func:`validate_capabilities_response_shape_async`
        yourself when invoking ``serve()`` from inside a running event
        loop (e.g. ``asyncio.run(your_main())`` that calls
        ``adcp.decisioning.serve`` for a sidecar binary). See #700.
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
        webhook_sender=webhook_sender,
        webhook_supervisor=webhook_supervisor,
        auto_emit_completion_webhooks=auto_emit_completion_webhooks,
        buyer_agent_registry=buyer_agent_registry,
        config_store=config_store,
        property_list_fetcher=property_list_fetcher,
        advertise_all=advertise_all,
        validate_at_init=validate_at_init,
    )

    # Phase 1 sandbox-authority — wire the comply controller's account
    # gate to the platform's AccountStore. When a test_controller is
    # present and the adopter hasn't supplied their own resolver, build
    # a closure over ``platform.accounts.resolve`` so the gate refuses
    # for live-mode accounts. Adopters supplying their own resolver
    # (``test_controller_account_resolver=``) take precedence.
    if (
        serve_kwargs.get("test_controller") is not None
        and "test_controller_account_resolver" not in serve_kwargs
    ):
        serve_kwargs["test_controller_account_resolver"] = _build_test_controller_account_resolver(
            platform
        )

    # Compliance-testing capability footgun — adopter declared
    # ``capabilities.compliance_testing`` but didn't wire a
    # ``test_controller=`` to ``serve()``. Buyers reading the projected
    # capabilities response will see ``compliance_testing`` advertised
    # and try to drive scenarios via ``comply_test_controller`` — which
    # then 404s because no controller is registered. Soft-warn rather
    # than fail-fast: adopters may legitimately declare the capability
    # while the controller is being wired in a follow-up PR, and a hard
    # boot error blocks that staged rollout.
    if (
        platform.capabilities.compliance_testing is not None
        and serve_kwargs.get("test_controller") is None
    ):
        warnings.warn(
            (
                "DecisioningCapabilities.compliance_testing is declared but "
                "no test_controller= was passed to serve(). Buyers reading "
                "this seller's capabilities will see compliance_testing "
                "advertised and try to drive scenarios via "
                "comply_test_controller — which will fail because no "
                "controller is registered. Either pass "
                "``test_controller=TestControllerStore(...)`` to ``serve()`` "
                "OR drop ``compliance_testing`` from "
                "``DecisioningCapabilities``. Capability declaration is a "
                "buyer-facing commitment; mismatched-vs-implemented "
                "advertisements are the kind of footgun the spec asks "
                "sellers to avoid."
            ),
            UserWarning,
            stacklevel=2,
        )

    server_name = name or type(platform).__name__
    debug_traffic_source = mock_ad_server.get_traffic if mock_ad_server is not None else None
    if pre_validation_hooks is not None:
        serve_kwargs["pre_validation_hooks"] = pre_validation_hooks
    _adcp_serve(
        handler,
        name=server_name,
        advertise_all=advertise_all,
        enable_debug_endpoints=enable_debug_endpoints,
        debug_traffic_source=debug_traffic_source,
        **serve_kwargs,
    )


def _build_test_controller_account_resolver(
    platform: DecisioningPlatform,
) -> Any:
    """Build a closure over ``platform.accounts.resolve`` for the
    comply controller's sandbox gate.

    The resolver takes a wire account ref dict (from the request's
    ``account`` or ``context.account``) plus the verified ``auth_info``
    threaded by the comply dispatch out of ``ToolContext.metadata``.
    ``FromAuthAccounts`` adopters (signed-request agents,
    OAuth-bearer-bound vendors) need auth_info to find the principal's
    account; without it the store would raise ``AUTH_INVALID``, which
    the gate now treats as DENY rather than fall-through. See
    :mod:`adcp.decisioning.account_mode`.
    """
    from adcp.decisioning.context import AuthInfo

    def _resolve(ref: dict[str, Any] | None, *, auth_info: AuthInfo | None = None) -> Any:
        return platform.accounts.resolve(ref, auth_info=auth_info)

    return _resolve


__all__ = [
    "create_adcp_server_from_platform",
    "serve",
]
