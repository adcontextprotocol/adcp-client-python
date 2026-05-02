"""Request context for DecisioningPlatform method dispatch.

:class:`RequestContext` extends :class:`adcp.server.ToolContext` so the
existing framework's idempotency middleware, observability hooks, and
A2A executor — all of which consume ``ToolContext`` — keep working
unchanged. Adopters' Protocol method signatures take
``RequestContext[TMeta]`` and get typed access to the resolved
``account`` plus a typed metadata bag.

The dispatch adapter (in ``adcp.decisioning.dispatch``) constructs a
``RequestContext`` per request from the underlying ``ToolContext`` and
the platform's ``AccountStore.resolve(...)`` result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Generic

from typing_extensions import TypeVar

from adcp.decisioning.resolve import ResourceResolver, _make_default_resolver
from adcp.decisioning.state import StateReader, _make_default_state_reader
from adcp.decisioning.types import Account, TaskHandoff, WorkflowHandoff
from adcp.server.base import ToolContext

if TYPE_CHECKING:
    from collections.abc import Mapping

    from adcp.decisioning.registry import (
        BuyerAgent,
        Credential,
    )

#: Per-platform metadata generic; mirrors ``adcp.decisioning.types.TMeta``
#: but redeclared here so ``RequestContext[TMeta]`` parameterization
#: works without importing the same TypeVar from another module (mypy
#: treats same-name TypeVars from different modules as distinct types,
#: which breaks downstream Protocol matching).
TMeta = TypeVar("TMeta", default=dict[str, Any])

T = TypeVar("T")


@dataclass
class AuthInfo:
    """The verified principal authenticated for a request.

    Populated by the framework's signed-request verifier
    (:func:`adcp.signing.signed_request_verifier`) or a custom
    ``authenticate=`` callable wired via :func:`adcp.decisioning.serve`.
    Threaded onto :attr:`RequestContext.auth_info` so platform methods
    can read scopes, key_id, principal, etc., without parsing
    transport headers.

    **Two field families.** The legacy fields (``kind`` / ``key_id`` /
    ``principal`` / ``scopes``) are the v6.0 surface — adopters built
    against the alpha pass these directly. The Tier 2 v3-identity
    fields (``credential`` / ``agent_url`` / ``operator`` / ``extra``)
    carry the typed AdCP v3 commercial identity context the
    :class:`adcp.decisioning.BuyerAgentRegistry` consumes. When an
    adopter constructs ``AuthInfo`` with only legacy fields,
    ``__post_init__`` synthesizes a typed
    :class:`adcp.decisioning.Credential` from them so the dispatch
    layer's registry call works without an adopter code change. One
    minor deprecation cycle — the legacy fields stay through 4.x.

    :param kind: One of ``'signed_request'``, ``'http_sig'``,
        ``'bearer'``, ``'api_key'``, ``'oauth'``, ``'mtls'``,
        ``'derived'``. Drives the legacy → ``credential`` synthesis.
    :param key_id: The signing key id (``kid``) for signed-request /
        http_sig auth, or the API-key id for bearer auth.
    :param principal: The authenticated principal label — for
        signed-request auth this is the verified ``agent_url`` (per
        AdCP v3 convention).
    :param scopes: Granted scopes / capabilities (OAuth or per-token).
    :param credential: Typed v3 :class:`adcp.decisioning.Credential` —
        the canonical surface the registry dispatches on. When
        unset, ``__post_init__`` synthesizes from the legacy fields.
        Adopters wiring v3 auth directly should construct the
        credential themselves and leave the legacy fields empty.
    :param agent_url: Verified buyer-agent URL — populated from
        ``credential.agent_url`` when ``credential`` is an
        :class:`adcp.decisioning.HttpSigCredential`, OR from
        ``principal`` when ``kind in {'signed_request', 'http_sig'}``.
        ``None`` for bearer / OAuth / unauthenticated traffic.
    :param operator: Operator / transport-tenant label — the AdCP v3
        operator binding (separate from the buyer agent). Distinct
        from ``ToolContext.tenant_id`` only for adopters running the
        AAO community proxy in front of a multi-operator deployment;
        most adopters leave this ``None``.
    :param extra: Adopter passthrough for auth-layer fields the SDK
        doesn't model (custom claims, MFA flags, internal session ids).
    """

    kind: str
    key_id: str | None = None
    principal: str | None = None
    scopes: list[str] = field(default_factory=list)

    # ----- Tier 2 v3-identity fields -----
    credential: Credential | None = None
    agent_url: str | None = None
    operator: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Synthesize ``credential`` + ``agent_url`` from legacy fields
        when not supplied directly.

        Adopter code constructing ``AuthInfo(kind="signed_request",
        principal="https://agent.example/", key_id="kid-1")`` (the v6.0
        alpha pattern) gets a typed
        :class:`adcp.decisioning.HttpSigCredential` populated
        automatically — the registry dispatch layer needs the typed
        credential, and synthesizing here keeps the migration
        zero-touch for existing adopter code.

        Legacy → credential mapping:

        * ``kind in {"signed_request", "http_sig"}`` →
          :class:`HttpSigCredential` (when ``key_id`` and ``principal``
          are both set; ``agent_url`` taken from ``principal`` per
          the documented v3 convention).
        * ``kind in {"api_key", "bearer"}`` →
          :class:`ApiKeyCredential` (when ``key_id`` set).
        * ``kind == "oauth"`` → :class:`OAuthCredential` (using
          ``key_id`` or ``principal`` as ``client_id``).
        * Other kinds (``"derived"``, ``"mtls"``, custom): no
          synthesis — ``credential`` stays ``None``.

        Synthesis is one-way: explicit ``credential=...`` always wins.
        """
        # Local imports to avoid the import-time cycle; registry.py
        # doesn't depend on context.py but the TYPE_CHECKING import
        # at the top is evaluated lazily.
        from adcp.decisioning.registry import (
            ApiKeyCredential,
            HttpSigCredential,
            OAuthCredential,
        )

        if self.credential is None:
            if self.kind in {"signed_request", "http_sig"}:
                if self.key_id and self.principal:
                    self.credential = HttpSigCredential(
                        kind="http_sig",
                        keyid=self.key_id,
                        agent_url=self.principal,
                        verified_at=0.0,
                    )
            elif self.kind in {"api_key", "bearer"}:
                if self.key_id:
                    self.credential = ApiKeyCredential(
                        kind="api_key",
                        key_id=self.key_id,
                    )
            elif self.kind == "oauth":
                client_id = self.key_id or self.principal
                if client_id:
                    self.credential = OAuthCredential(
                        kind="oauth",
                        client_id=client_id,
                        scopes=tuple(self.scopes),
                    )

        # Derive agent_url from the credential when present; fall back
        # to legacy principal for signed-request kinds so adopters
        # reading auth_info.agent_url get a consistent value regardless
        # of construction path.
        if self.agent_url is None:
            if isinstance(self.credential, HttpSigCredential):
                self.agent_url = self.credential.agent_url
            elif self.kind in {"signed_request", "http_sig"} and self.principal:
                self.agent_url = self.principal


@dataclass
class RequestContext(ToolContext, Generic[TMeta]):
    """Per-request context passed to every Protocol method.

    Subclasses :class:`adcp.server.ToolContext` so the existing
    framework primitives (idempotency middleware, observability,
    A2A executor) consume it as a ``ToolContext`` while adopter
    Protocol methods read the typed :attr:`account` directly.

    **Framework-only construction.** Adopter code receives a
    ``RequestContext`` from the framework on every dispatch via the
    hydration helper in ``adcp.decisioning.dispatch``. Direct
    construction is supported for tests only — production code that
    builds a ``RequestContext`` from outside the dispatch seam is a
    bug. Adopters who need to modify the context (custom middleware,
    test doubles for ``state`` / ``resolve``) should use
    :func:`dataclasses.replace`, not raw construction. Mirrors the
    TS-side ``to-context.ts:buildRequestContext`` contract.

    :param account: The resolved account, with typed ``metadata: TMeta``.
        The framework's idempotency middleware reads
        ``ctx.caller_identity`` for cache scoping; the dispatch adapter
        sets ``caller_identity = account.id`` so caching scopes per
        resolved account, not per raw auth principal.
    :param auth_info: Optional verified principal info. ``None`` when
        the request is unauthenticated (dev / ``'derived'`` fixtures).
    :param now: Monotonic timestamp for the request — adopters use
        this rather than ``datetime.now()`` directly so tests can
        inject deterministic clocks.

    Adopters call :meth:`handoff_to_task` to promote a method to the
    HITL background-task path. The framework dispatcher detects the
    returned :class:`TaskHandoff` via type-identity and projects it
    to the wire ``Submitted`` envelope.

    **Identifier disambiguation — when to use which:**

    The context carries four identifier-shaped fields. Each has a
    distinct role; mixing them up is the most common adopter bug.

    ``account.id`` — "whose data is this?"
        The resolved tenant / account that owns the call. Read it to
        route the request to the right adapter instance, scope your
        DB queries, and stamp audit logs.

    ``auth_principal`` — "who's calling?"
        The verified caller's identity label. The string varies by
        auth shape: ``agent_url`` for AdCP v3 signed-request agents
        (the documented convention; the SDK's signed-request adapter
        wrappers ship in 4.5.0), OAuth subject claim for bearer
        flows, mTLS subject for client-cert flows. Read it for
        per-principal ACLs *within* an account ("can principal X
        mutate this buy?").

    ``caller_identity`` — "what's the cache scope key?"
        Composite framework-set key
        (``<store_module>.<store_qualname>:<account_id>``) used by
        the idempotency middleware to scope the replay cache.
        Treat as opaque. Adopter code may log or forward it
        (rate-limiting, audit) but should not parse, compare, or
        rewrite it — the format is framework-internal and any
        adopter assumption about its shape will break when the
        scope-key composition changes.

    ``tenant_id`` — "which transport tenant?"
        Inherited from :class:`ToolContext`; set by the transport
        layer before dispatch (typically from the host header or URL
        path on multi-tenant deployments). Usually equals
        ``account.id`` for ``'explicit'``-resolution adopters; can
        diverge for ``'derived'`` / ``'implicit'`` modes.

    Common patterns:

    * Routing to the right adapter? → ``ctx.account.metadata.adapter``
      (typed via the ``TMeta`` generic).
    * Authorization check? → ``ctx.auth_principal`` (who's calling)
      against ``ctx.account.id`` (whose data they're touching).
    * Idempotency scope? → don't touch; the framework owns this.
    * Logging request provenance? → log all four; they're cheap.

    :param state: Sync reads of framework-owned in-flight workflow
        state. Default is :class:`adcp.decisioning.state._NotYetWiredStateReader`
        — returns empty values + emits one-time UserWarning per
        method on first call. v6.1 wires the backing store.
    :param resolve: Async framework-mediated fetches with cache +
        validation. Default is
        :class:`adcp.decisioning.resolve._NotYetWiredResolver` — raises
        ``NotImplementedError`` on every call. v6.1 wires the backing
        fetchers.
    :param auth_principal: Typed convenience field carrying the
        verified principal label (sourced from
        :class:`AuthInfo.principal` when present). Distinct from
        ``account.id`` (which the framework's idempotency middleware
        uses for cache scope) — middleware reading "who authenticated
        this request" gets a load-bearing field name.
    """

    # Default factories so ``RequestContext()`` works in tests; in
    # production the dispatch adapter populates every field.
    account: Account[TMeta] = field(default_factory=lambda: Account(id="<unset>"))
    auth_info: AuthInfo | None = None
    auth_principal: str | None = None
    buyer_agent: BuyerAgent | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state: StateReader = field(default_factory=_make_default_state_reader)
    resolve: ResourceResolver = field(default_factory=_make_default_resolver)

    def handoff_to_task(
        self,
        fn: Callable[[Any], Awaitable[T] | T],
    ) -> TaskHandoff[T]:
        """Promote this call to a background task.

        The buyer sees ``{status: 'submitted', task_id}`` on the
        immediate response; the framework runs ``fn`` after returning,
        persists ``fn``'s terminal artifact to the task registry, and
        emits a push-notification webhook on terminal state.

        ``fn`` receives a ``TaskHandoffContext`` (defined in
        :mod:`adcp.decisioning.dispatch`) carrying:

        * ``id`` — framework-issued task UUID
        * ``update(progress)`` — write progress payload, transition
          ``'submitted'`` → ``'working'``
        * ``heartbeat()`` — liveness signal (v6.1 stub)

        Adopter code passes either a coroutine function (``async def
        review_async(task_ctx): ...``) or a sync callable; the
        dispatcher detects which and runs it appropriately.

        For external workflows that complete on their own schedule
        (human queue review, batch jobs, Airflow DAGs, ML pipelines)
        — use :meth:`handoff_to_workflow` instead. The split is purely
        about where the work runs (in-process / framework-managed vs.
        adopter-owned external system).
        """
        return TaskHandoff(fn)

    def handoff_to_workflow(
        self,
        fn: Callable[[Any], Awaitable[None] | None],
    ) -> WorkflowHandoff:
        """Promote this call to an externally-completed task.

        For workflows that run OUTSIDE the framework's process —
        human queue review (trafficker UI), nightly batch jobs,
        Airflow DAGs, ML pipelines, scheduled cron. The framework
        allocates a ``task_id``, calls ``fn`` ONCE synchronously
        (or awaits it if a coroutine) to register the work into the
        adopter's external system, persists ``submitted`` state, and
        returns the wire envelope. NO background coroutine runs in
        the framework.

        ``fn`` receives a :class:`TaskHandoffContext` carrying
        ``id`` (framework-allocated task_id) and ``_registry``
        (adopter can stash a reference for later completion). The
        adopter's external workflow later calls
        ``registry.complete(task_id, result)`` or
        ``registry.fail(task_id, error)`` directly when the work
        finishes — minutes, hours, or days later.

        Buyer experience is identical to :meth:`handoff_to_task` —
        same ``{task_id, status: 'submitted'}`` wire envelope, same
        ``tasks/get`` polling, same push-notification webhook on
        terminal state.

        **Rollback.** If ``fn`` raises during enqueue, the framework
        discards the just-allocated task_id from the registry and
        propagates the exception (wrapped to ``AdcpError`` per the
        dispatch contract). Adopter enqueue fns that need
        transactional persistence wrap their own DB write in their
        own transaction; the framework's rollback is registry-side
        only.

        Example::

            class TraffickerSeller(DecisioningPlatform):
                def __init__(self, review_queue, task_registry):
                    self.review_queue = review_queue
                    # Stash for later completion when human acts
                    self.task_registry = task_registry

                def create_media_buy(self, req, ctx):
                    if self._needs_human_approval(req):
                        return ctx.handoff_to_workflow(
                            lambda task_ctx: self._enqueue(task_ctx, req)
                        )
                    return CreateMediaBuySuccess(media_buy_id="mb_1", ...)

                def _enqueue(self, task_ctx, req):
                    self.review_queue.add(
                        task_id=task_ctx.id,
                        request_snapshot=req.model_dump(),
                    )

                # Elsewhere — Flask handler for the trafficker UI:
                async def on_decision(self, task_id, decision):
                    if decision.approved:
                        await self.task_registry.complete(
                            task_id,
                            CreateMediaBuySuccess(...).model_dump(),
                        )
                    else:
                        await self.task_registry.fail(
                            task_id, AdcpError(...).to_wire(),
                        )

        See :class:`adcp.decisioning.WorkflowHandoff` for the full
        semantics.
        """
        return WorkflowHandoff(fn)
