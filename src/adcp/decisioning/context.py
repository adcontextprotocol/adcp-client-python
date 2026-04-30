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
    """

    # Default factories so ``RequestContext()`` works in tests; in
    # production the dispatch adapter populates every field.
    account: Account[TMeta] = field(default_factory=lambda: Account(id="<unset>"))
    auth_info: AuthInfo | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

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
