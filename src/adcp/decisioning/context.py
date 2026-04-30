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
from adcp.decisioning.types import Account, TaskHandoff
from adcp.server.base import ToolContext

if TYPE_CHECKING:
    pass

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

    :param kind: One of ``'signed_request'``, ``'bearer'``, ``'mtls'``,
        ``'derived'``. Adopters with custom auth schemes extend the
        type alias.
    :param key_id: The signing key id (``kid``) for signed-request auth.
    :param principal: The authenticated principal — typically the
        buyer's verified label or service-account id. Stable across
        sessions.
    :param scopes: Granted scopes / capabilities. Used by adopters
        gating tools per principal.
    """

    kind: str
    key_id: str | None = None
    principal: str | None = None
    scopes: list[str] = field(default_factory=list)


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
        the request is unauthenticated (dev / 'singleton' fixtures).
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

    +---------------------+-----------------------------+--------------------------------+
    | Field               | What it answers             | Read it for                    |
    +=====================+=============================+================================+
    | ``account.id``      | "Whose data is this?"       | Routing the request to the     |
    |                     | The resolved tenant /       | right adapter, scoping DB      |
    |                     | account that owns the call. | reads, audit logs.             |
    +---------------------+-----------------------------+--------------------------------+
    | ``auth_principal``  | "Who's calling?"            | Per-principal ACLs within an   |
    |                     | The verified caller's       | account ("can principal X      |
    |                     | identity label              | mutate this buy?").            |
    |                     | (``agent_url`` for AdCP v3  |                                |
    |                     | signed-request agents,      |                                |
    |                     | OAuth subject for bearer    |                                |
    |                     | flows, mTLS subject for     |                                |
    |                     | client-cert flows).         |                                |
    +---------------------+-----------------------------+--------------------------------+
    | ``caller_identity`` | "What's the cache scope?"   | NEVER read directly in adopter |
    |                     | Composite framework-set key | code. The framework's          |
    |                     | (``store.qualname:           | idempotency middleware reads   |
    |                     | account.id``) used by the   | this. Mutating it breaks       |
    |                     | idempotency middleware.     | replay-cache scoping.          |
    +---------------------+-----------------------------+--------------------------------+
    | ``tenant_id``       | "Which transport tenant?"   | Multi-tenant transport routing |
    |                     | Inherited from              | (host header, URL path).       |
    |                     | :class:`ToolContext`. Set   | Usually equals ``account.id``  |
    |                     | by the transport layer      | for explicit-resolution        |
    |                     | before dispatch.            | adopters; can diverge for      |
    |                     |                             | derived/implicit modes.        |
    +---------------------+-----------------------------+--------------------------------+

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
        """
        return TaskHandoff(fn)
