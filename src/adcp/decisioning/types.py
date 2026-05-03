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
from typing import TYPE_CHECKING, Any, Generic, Literal

# PEP 696 TypeVar defaults + PEP 695 / PEP 718 generic TypeAlias both
# need ``typing_extensions`` backports for Python 3.10-3.12 (the package
# floor). ``TypeVar`` with ``default=`` lands in stdlib at 3.13;
# ``TypeAliasType`` (used to declare generic aliases like
# ``MaybeAsync[T]``) lands at 3.12. Importing both from
# ``typing_extensions`` keeps the same source compatible across the
# supported range.
from typing_extensions import TypeAliasType, TypeVar

# Wire-aligned optional Account fields use the codegen'd wire models so
# adopters set the same shapes that land on the wire. Imported under
# TYPE_CHECKING because the generated types pull in the heavy
# adcp.types dependency tree; the framework runs without forcing that
# import at decisioning module load time. Imports go through the public
# adcp.types surface (not adcp.types.generated_poc) per the type-import
# layering rule documented in CLAUDE.md.
if TYPE_CHECKING:
    from adcp.types import (
        AccountReference,
        AccountScope,
        BusinessEntity,
        CreditLimit,
        GovernanceAgent,
        PaymentTerms,
        ReportingBucket,
    )
    from adcp.types import (
        Setup as AccountSetup,
    )


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
            # Framework-async slow path — hand off to background work
            return ctx.handoff_to_task(self._review_async)

    **What TaskHandoff is for** — short, framework-mediated async work
    where the adopter awaits an external system (DSP API call,
    classifier inference, third-party brand-safety scan, generative
    creative render) inside a coroutine. The handoff fn runs in the
    same process, the framework awaits it, persists the terminal
    artifact, and emits a webhook on completion. Typical wall-clock:
    seconds to minutes.

    **What TaskHandoff is NOT for** — external workflows that complete
    on their own schedule (human queue review, nightly batch jobs,
    Airflow DAGs, ML pipelines that run hours later). The handoff fn
    would either block the framework's background runner indefinitely
    (until the external system acts), or poll an external queue (which
    doesn't fit the "fn returns terminal artifact" contract). Use
    :class:`WorkflowHandoff` instead — obtained via
    :meth:`RequestContext.handoff_to_workflow`. The framework allocates
    a ``task_id``, persists ``submitted`` state, and returns the wire
    envelope; the adopter's external system later calls
    ``registry.complete(task_id, result)`` or
    ``registry.fail(task_id, error)`` directly.

    Buyer experience is identical across the three paths — sync return,
    TaskHandoff, WorkflowHandoff — they all surface as polled-or-webhook
    completion against the same wire shape. The split is purely about
    where the work runs (in-process / framework-managed / adopter-owned).
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


class WorkflowHandoff:
    """Marker the framework recognizes as 'register this call as a task
    completed externally.'

    Adopters obtain instances via :meth:`RequestContext.handoff_to_workflow`;
    the framework dispatches based on type-identity (``type(obj) is
    WorkflowHandoff``) — same posture as :class:`TaskHandoff`.

    **Distinct from :class:`TaskHandoff`.** TaskHandoff is for
    framework-managed in-process async work — the adopter's coroutine
    runs in the background and returns a terminal artifact within
    seconds-to-minutes. WorkflowHandoff is for adopter-owned external
    workflows that complete on their own schedule (human queue review,
    nightly batch jobs, Airflow DAGs, ML pipelines, scheduled cron).
    The framework allocates a ``task_id``, calls the adopter's enqueue
    fn ONCE synchronously to register the work into the adopter's
    external system, persists ``submitted`` state, and returns the
    wire envelope. NO background coroutine runs.

    The adopter's external workflow later calls
    ``registry.complete(task_id, result)`` or
    ``registry.fail(task_id, error)`` directly — the registry handle
    is plumbed through the platform's own DI / app-level config.

    Example::

        class TraffickerSeller(DecisioningPlatform):
            def __init__(self, review_queue, task_registry):
                self.review_queue = review_queue
                # Stash the registry so the trafficker UI can call
                # registry.complete(task_id, result) when the human acts.
                self.task_registry = task_registry

            def create_media_buy(self, req, ctx):
                if self._needs_human_approval(req):
                    # Framework allocates task_id, calls _enqueue with
                    # task_ctx, persists 'submitted', returns Submitted.
                    # No background work runs in the framework.
                    return ctx.handoff_to_workflow(
                        lambda task_ctx: self._enqueue(task_ctx, req)
                    )
                return CreateMediaBuySuccess(media_buy_id="mb_1", ...)

            def _enqueue(self, task_ctx, req):
                # Persist for the trafficker UI. ``task_ctx.id`` is the
                # framework-allocated task_id; the buyer polls/webhooks
                # on this id.
                self.review_queue.add(
                    task_id=task_ctx.id,
                    request_snapshot=req.model_dump(),
                )
                # Return — no work done here. Trafficker UI completes
                # via self.task_registry.complete() when they decide.

    **Wire-shape parity.** The buyer cannot tell whether the seller
    used sync, TaskHandoff, or WorkflowHandoff. All three project to
    the same Submitted envelope (``{task_id, status: 'submitted'}``);
    completion (whenever it happens, by whatever path) flows via
    ``tasks/get`` or push-notification webhook with the same payload
    shape.

    **Rollback.** If the enqueue fn raises, the framework discards
    the just-allocated task_id from the registry and propagates the
    exception (wrapped to ``AdcpError`` per the dispatch contract).
    The buyer never sees an orphan task_id they can't reach. Adopter
    enqueue fns that need transactional persistence wrap their own DB
    write in their own transaction; the framework's rollback is
    registry-side only.
    """

    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[[Any], Awaitable[None] | None]) -> None:
        # ``fn`` is ``Callable[[TaskHandoffContext], Awaitable[None] |
        # None]`` — the framework calls it once synchronously (or
        # awaits it if a coroutine) at handoff time. Return value
        # unused; the adopter's external workflow completes via
        # ``registry.complete()`` later, NOT via fn return.
        # TaskHandoffContext lives in task_registry.py to avoid a cycle.
        self._fn = fn

    def __repr__(self) -> str:
        return "WorkflowHandoff(<sealed>)"


def is_workflow_handoff(obj: Any) -> bool:
    """Type-identity dispatch helper for :class:`WorkflowHandoff`.

    Same posture as :func:`is_task_handoff`: ``type(obj) is
    WorkflowHandoff``, not ``isinstance``. Adopter subclasses are not
    supported.
    """
    return type(obj) is WorkflowHandoff


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
    ``account.id``. Adopters in ``'derived'`` resolution mode MUST
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

    Wire-aligned optional fields (all default ``None``) carry the AdCP
    v3 commercial / lifecycle / reporting shape. Adopters who don't
    populate these see no behavior change; populated fields project
    through :func:`to_wire_account` onto the wire ``Account`` shape on
    every emit path that surfaces an :class:`Account`. The projection
    strips :attr:`BusinessEntity.bank` (write-only per spec) and
    :attr:`GovernanceAgent.authentication.credentials` (defense-in-depth
    — Python type hints aren't enforced at runtime, so the strip runs
    even if an adopter returns a loosely-typed governance-agent record
    that smuggles credentials through ``cast`` / ``Any``).

    :param billing_entity: Business entity invoiced on this account.
        Carries legal name, tax IDs, address, contacts, and (write-only)
        bank details. The framework's :func:`to_wire_account` strips
        ``bank`` on emit; adopters who load and return a full entity
        from their store no longer leak bank coordinates to buyers.
    :param setup: Setup payload for accounts in ``pending_approval``.
        Carries ``url`` / ``message`` / ``expires_at`` driving the
        ``pending_approval → active`` lifecycle.
    :param governance_agents: Governance agent endpoints registered on
        this account. The wire schema marks ``authentication.credentials``
        write-only — the framework strips ``authentication`` on emit
        regardless of what the adopter populates.
    :param account_scope: ``operator`` / ``brand`` / ``operator_brand``
        / ``agent``.
    :param payment_terms: ``net_15`` / ``net_30`` / ``net_45`` /
        ``net_60`` / ``net_90`` / ``prepay``.
    :param credit_limit: Maximum outstanding balance allowed
        (``{amount, currency}``).
    :param rate_card: Identifier for the rate card applied. Opaque
        seller-side string; emitted unchanged.
    :param reporting_bucket: Cloud storage bucket where the seller
        delivers offline reporting files for this account.
    """

    id: str
    name: str = ""
    status: str = "active"
    metadata: TMeta = field(default_factory=lambda: {})  # type: ignore[assignment]
    auth_info: dict[str, Any] | None = None

    # Wire-aligned optional fields. All default to ``None``; adopters
    # populate as their commercial / lifecycle / reporting model
    # requires. The framework projects through ``to_wire_account`` on
    # every emit path that surfaces an Account, applying the
    # write-only strips for ``billing_entity.bank`` and
    # ``governance_agents[].authentication``.
    billing_entity: BusinessEntity | None = None
    setup: AccountSetup | None = None
    governance_agents: list[GovernanceAgent] | None = None
    account_scope: AccountScope | None = None
    payment_terms: PaymentTerms | None = None
    credit_limit: CreditLimit | None = None
    rate_card: str | None = None
    reporting_bucket: ReportingBucket | None = None


# ---------------------------------------------------------------------------
# Sync_accounts / sync_governance row shapes
# ---------------------------------------------------------------------------


@dataclass
class SyncAccountsResultRow:
    """Per-account result row returned by an adopter's ``accounts.upsert``
    implementation. Maps to one element of the wire ``sync_accounts``
    response's ``accounts[]`` array.

    Carries the same optional commercial / lifecycle fields as the wire
    shape so adopters can echo ``setup`` (for ``pending_approval``
    accounts), ``billing_entity``, ``payment_terms``, etc. on creation.
    The framework projects through :func:`to_wire_sync_accounts_row`
    before emit, applying the same ``billing_entity.bank`` strip as
    :func:`to_wire_account` (write-only contract).

    **MUST NOT carry auth-derived fields.** This shape is emitted on
    the ``sync_accounts`` response wire. Adopters MUST NOT add an
    ``auth_info`` key on returned rows — same MUST-NOT-LEAK rule the
    framework enforces on :attr:`Account.auth_info`.

    :param brand: Required. Echoed from the request's ``account.brand``.
    :param operator: Required. Echoed from the request's
        ``account.operator``.
    :param action: Required. ``created`` / ``updated`` / ``unchanged``
        / ``failed``.
    :param status: Required. AdCP account-status enum value.
    :param account_id: Seller-assigned account identifier (when
        ``action`` is ``created``).
    :param name: Human-readable account name assigned by the seller.
    :param billing: Invoiced-to party
        (``operator`` / ``agent`` / ``advertiser``).
    :param billing_entity: Business entity invoiced.
        ``bank`` is stripped on emit (write-only).
    :param setup: Setup payload for ``pending_approval`` accounts.
    :param account_scope: Account scope.
    :param rate_card: Rate card applied to this account.
    :param payment_terms: Payment terms.
    :param credit_limit: Credit limit.
    :param errors: Per-account errors (only when action is ``failed``).
    :param warnings: Non-fatal warnings about this account.
    :param sandbox: Sandbox-account marker, echoed from the request.
    """

    brand: dict[str, Any]
    operator: str
    # The wire schema uses an Enum on the response side; adopters can
    # pass either the Enum value or the literal string. Typed as the
    # literal union (Pydantic enum-or-string coercion handles the
    # rest); the framework projects via ``_enum_value`` on emit.
    action: Literal["created", "updated", "unchanged", "failed"] | str
    status: str
    account_id: str | None = None
    name: str | None = None
    billing: Literal["operator", "agent", "advertiser"] | None = None
    billing_entity: BusinessEntity | None = None
    setup: AccountSetup | None = None
    account_scope: AccountScope | None = None
    rate_card: str | None = None
    payment_terms: PaymentTerms | None = None
    credit_limit: CreditLimit | None = None
    errors: list[dict[str, Any]] | None = None
    warnings: list[str] | None = None
    sandbox: bool | None = None


@dataclass
class SyncGovernanceEntry:
    """One entry from the wire ``sync_governance`` request's ``accounts[]``.

    The framework strips wire metadata (``idempotency_key``,
    ``adcp_major_version``, ``context``, ``ext``) before invoking
    :meth:`AccountStore.sync_governance`. Each entry pairs an
    :class:`AccountReference` with its ``governance_agents[]``.

    The ``governance_agents`` list carries ``authentication.credentials``
    on the input — adopters persist these for outbound
    ``check_governance`` calls. The framework strips ``authentication``
    on emit (see :func:`to_wire_sync_governance_row`); the input shape
    here keeps credentials present for the adopter's persistence step.

    :param account: AccountReference for the account being synced.
    :param governance_agents: Wire ``governance_agents[]``, including
        ``authentication`` (which carries the write-only credentials).
        Pass-through from the request — the framework does not strip
        credentials before this point so adopters can persist them.
    """

    account: AccountReference
    governance_agents: list[dict[str, Any]]


@dataclass
class SyncGovernanceResultRow:
    """Per-entry result row returned by ``AccountStore.sync_governance``.

    Maps to one element of the wire ``sync_governance`` response's
    ``accounts[]`` array. The framework projects through
    :func:`to_wire_sync_governance_row` before emit, stripping
    ``authentication`` (write-only) from every governance agent.

    **Replace semantics, per spec.** Each ``sync_governance`` call
    REPLACES the previously synced governance agents for the
    referenced account. An entry whose ``governance_agents`` is empty
    clears the binding for that account.

    Per-entry rejection (vs. operation-level throw) so a single bad
    entry doesn't fail the whole batch — return a row with
    ``status='failed'`` and ``errors=[{code: 'PERMISSION_DENIED', ...}]``
    for the rejected entry.

    :param account: AccountReference, echoed from the request.
    :param status: ``synced`` (governance agents persisted) or
        ``failed`` (could not complete; see ``errors``).
    :param governance_agents: Governance agents now synced on this
        account. Reflects the persisted state after sync.
    :param errors: Per-account errors (only when status is ``failed``).
    """

    account: AccountReference
    status: Literal["synced", "failed"] | str
    governance_agents: list[dict[str, Any]] | None = None
    errors: list[dict[str, Any]] | None = None
