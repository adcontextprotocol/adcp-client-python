"""Core types for the DecisioningPlatform layer.

Defines:

* :class:`TaskHandoff` — marker the framework recognizes as "promote this
  call to a long-running task." Plain class with ``__slots__`` so adopters
  can't accidentally subclass it into framework dispatch.
* :class:`Account` — generic over per-platform metadata (``TMeta``) so
  adopter-defined fields (``adapter``, ``credentials``, ``network_id``,
  etc.) typecheck inside method bodies without ``cast``.
* :data:`MaybeAsync`, :data:`SalesResult` — named return-type aliases.
  Coding agents (Cursor, Claude Code, etc.) handle one named alias far
  better than a nested ``Awaitable[T | TaskHandoff[T]] | T | TaskHandoff[T]``.
* :class:`AdcpError` — re-exported from :mod:`adcp.exceptions` for
  one-stop import.

The :class:`RequestContext` lives in ``context.py`` to keep this module
free of ``adcp.server`` dependencies — pure types adopters can import
without dragging in the transport stack.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Literal

# PEP 696 TypeVar defaults + PEP 695 / PEP 718 generic TypeAlias both
# need ``typing_extensions`` backports for Python 3.10-3.12 (the package
# floor). ``TypeVar`` with ``default=`` lands in stdlib at 3.13;
# ``TypeAliasType`` (used to declare generic aliases like
# ``MaybeAsync[T]``) lands at 3.12. Importing both from
# ``typing_extensions`` keeps the same source compatible across the
# supported range.
from typing_extensions import TypeAliasType, TypeVar


class AdcpError(Exception):
    """Wire-shaped structured error raised by platform methods.

    Distinct from :class:`adcp.exceptions.ADCPError` (the client-side
    connection-failure exception). This is the *server-side* structured
    error the framework's dispatcher catches and projects to the wire
    ``adcp_error`` envelope:

    .. code-block:: json

        {
          "code": "BUDGET_TOO_LOW",
          "message": "total_budget below floor (0.50 CPM × 1000 imp)",
          "recovery": "correctable",
          "field": "total_budget",
          "suggestion": "Increase budget to at least $0.50",
          "retry_after": null,
          "details": {"errors": [...]}
        }

    Adopters raise this from inside Protocol method bodies for any
    buyer-fixable rejection. The framework catches at the dispatch
    seam, serializes to the structured-error envelope, and returns
    the wire response. Adopters do NOT serialize themselves.

    :param code: AdCP error code (e.g. ``BUDGET_TOO_LOW``,
        ``POLICY_VIOLATION``, ``INVALID_REQUEST``,
        ``ACCOUNT_NOT_FOUND``). The full enum is at
        ``schemas/cache/3.0.0/enums/error-code.json``; vendor codes
        outside the enum are accepted (``str``) but buyers won't have
        first-class handling for them.
    :param message: Human-readable error message. Always set.
    :param recovery: Buyer's retry strategy:

        * ``'retry_with_changes'`` — fix the indicated field and retry
        * ``'correctable'`` — same as retry_with_changes (legacy alias)
        * ``'transient'`` — retry as-is after a backoff
        * ``'terminal'`` — do not retry; the request is rejected

    :param field: The request field path that caused the error
        (e.g. ``'total_budget'``, ``'package[2].targeting'``). Buyers
        use this to highlight inputs in their UI.
    :param suggestion: Optional human-readable hint for fixing the
        error.
    :param retry_after: Seconds to wait before retrying. Only
        meaningful with ``recovery='transient'``.
    :param details: Free-form extras for codes that need them
        (e.g. ``{'errors': [...]}`` for multi-error preflight).
    """

    def __init__(
        self,
        code: str,
        *,
        message: str = "",
        recovery: Literal[
            "retry_with_changes", "correctable", "transient", "terminal"
        ] = "terminal",
        field: str | None = None,
        suggestion: str | None = None,
        retry_after: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.recovery = recovery
        self.field = field
        self.suggestion = suggestion
        self.retry_after = retry_after
        self.details = details or {}

    def __str__(self) -> str:
        return f"AdcpError[{self.code} / {self.recovery}]: {self.args[0]}"

    def to_wire(self) -> dict[str, Any]:
        """Project to the AdCP wire ``adcp_error`` envelope.

        Called by the framework dispatcher when serializing the
        rejection. Adopters don't typically call this directly; it's
        public for testing and for adopter middleware that wants to
        inspect the projection shape.
        """
        out: dict[str, Any] = {
            "code": self.code,
            "message": self.args[0] if self.args else "",
            "recovery": self.recovery,
        }
        if self.field is not None:
            out["field"] = self.field
        if self.suggestion is not None:
            out["suggestion"] = self.suggestion
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        if self.details:
            out["details"] = dict(self.details)
        return out


#: Per-platform metadata generic. Defaults to ``dict[str, Any]`` for
#: adopters who don't define a typed metadata shape; multi-tenant adopters
#: typically define a TypedDict and parameterize ``Account[TenantMeta]``,
#: ``RequestContext[TenantMeta]`` so ``ctx.account.metadata`` typechecks
#: without ``cast``.
TMeta = TypeVar("TMeta", default=dict[str, Any])

#: Generic return-type variable for hybrid handoff results.
T = TypeVar("T")


class TaskHandoff(Generic[T]):
    """Marker the framework recognizes as 'promote this call to a task.'

    Adopters obtain instances via :meth:`RequestContext.handoff_to_task`;
    the framework dispatches based on type-identity (``type(obj) is
    TaskHandoff``) so a buyer-supplied request body can never become a
    handoff (it would never have the right ``type``), and adopter
    subclasses don't accidentally trigger the handoff path.

    The Python implementation deliberately omits the JS-side
    ``Symbol.for(...)``-keyed brand. JS needs the brand to defend against
    untrusted code in the same realm forging markers; Python adopter code
    is trusted, and a buyer-supplied wire body cannot reach this type
    because :class:`TaskHandoff` is a return type — never deserialized
    from JSON. The adversary doesn't exist; the ceremony to defend
    against them shouldn't either.

    Example::

        def create_media_buy(self, req, ctx):
            if self._is_pre_approved(req, ctx.account):
                # Sync fast path — return Success directly
                return CreateMediaBuySuccess(media_buy_id="mb_1", ...)
            # HITL slow path — hand off to background trafficker review
            return ctx.handoff_to_task(self._review_async)
    """

    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[[Any], Awaitable[T] | T]) -> None:
        # ``fn`` is ``Callable[[TaskHandoffContext], Awaitable[T] | T]``
        # but TaskHandoffContext lives in dispatch.py to avoid a cycle.
        # The framework calls ``handoff._fn(task_ctx)`` at dispatch time;
        # adopters pass either a coroutine function or a sync callable
        # and the dispatcher detects via ``inspect.iscoroutine``.
        self._fn = fn

    def __repr__(self) -> str:
        return "TaskHandoff(<sealed>)"


def is_task_handoff(obj: Any) -> bool:
    """Type-identity dispatch helper.

    Uses ``type(obj) is TaskHandoff`` — NOT ``isinstance`` — so any
    adopter subclass of :class:`TaskHandoff` is rejected at dispatch.
    Subclassing is not supported; an adopter who tries gets the
    sync-return path and silently delivers their result as a normal
    response. Documented as a deliberate non-feature.
    """
    return type(obj) is TaskHandoff


# ---------------------------------------------------------------------------
# Result type aliases
# ---------------------------------------------------------------------------

#: Sync result OR async result. Use directly on tools whose response
#: schema does NOT include the ``Submitted`` arm (i.e. read-only +
#: synchronous mutations).
MaybeAsync = TypeAliasType("MaybeAsync", "Awaitable[T] | T", type_params=(T,))

#: Hybrid sync-or-handoff result. Read as: "return ``T`` directly for
#: the sync fast path, or ``TaskHandoff[T]`` for the HITL slow path,
#: in either a sync or async method body." Coding agents misread the
#: equivalent inline four-way union; the named alias is materially
#: more legible and matches the TS-side ``SalesResult<T>``.
SalesResult = TypeAliasType(
    "SalesResult",
    "Awaitable[T] | T | TaskHandoff[T] | Awaitable[TaskHandoff[T]]",
    type_params=(T,),
)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


@dataclass
class Account(Generic[TMeta]):
    """The resolved account a request operates on.

    Constructed by the platform's :class:`AccountStore` and threaded
    through every dispatch via :class:`RequestContext`. ``metadata``
    is the typed extension point — adopters define a TypedDict (or
    dataclass) carrying their per-account data (``adapter`` instance,
    OAuth credentials, network IDs, sandbox flags, etc.) and
    parameterize ``Account[TenantMeta]`` so ``ctx.account.metadata.adapter``
    typechecks inside method bodies.

    The framework's idempotency middleware scopes its cache by
    ``account.id``. Adopters in 'singleton' resolution mode MUST
    synthesize per-principal IDs (e.g. ``f"training-agent:{principal}"``)
    or buyer-to-buyer cache leakage is possible — see
    :class:`adcp.decisioning.SingletonAccounts`.

    :param id: Stable, globally-unique account identifier within the
        adopter's deployment. Used as the idempotency cache scope key
        and the ``caller_identity`` the framework's idempotency middleware
        reads.
    :param name: Human-readable account name for logging and admin
        UIs. Not used for routing or scoping.
    :param status: Account lifecycle state — ``'pending_approval'``,
        ``'active'``, ``'disabled'``, etc. Adopters consuming the
        ``account-status.json`` enum can use this directly.
    :param metadata: Adopter-defined typed metadata. Defaults to an
        untyped dict for adopters who don't care.
    :param auth_info: The verified principal that authenticated this
        request, if any. Distinct from ``id`` because one principal
        can act on multiple accounts in 'explicit' resolution mode.
    """

    id: str
    name: str = ""
    status: str = "active"
    metadata: TMeta = field(default_factory=lambda: {})  # type: ignore[assignment]
    auth_info: dict[str, Any] | None = None
