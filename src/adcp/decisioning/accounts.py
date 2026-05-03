"""Account resolution: ``AccountStore`` Protocol + three reference impls.

Adopters pick a resolution mode at registration time. The mode literal
mirrors the JS-side ``AccountStore.resolution`` field
(``src/lib/server/decisioning/account.ts``) for cross-language parity:

* :class:`SingletonAccounts` (``resolution='derived'``) — single-process
  / single-platform deployments (Innovid training-agent, single-publisher
  proof-of-concept). Synthesizes ``account.id`` per verified principal
  so idempotency scopes correctly across distinct callers.
* :class:`ExplicitAccounts` (``resolution='explicit'``) — multi-tenant
  where the URL or request body identifies the account
  (``/tenants/<id>``, ``account.account_id`` in body). Resolves by the
  wire reference.
* :class:`FromAuthAccounts` (``resolution='implicit'``) — multi-tenant
  or single-tenant where the verified auth principal identifies the
  account (signed-request bound, OAuth bearer bound). Resolves by
  ``ctx.auth_info.principal``.

Adopters with shapes that don't fit these three implement the
:class:`AccountStore` Protocol directly.

Spec-agent vs auth-layer principal:
   AdCP v3 introduced agent-to-agent flows where the calling principal
   IS an agent identified by a stable ``agent_url`` (the agent's
   well-known URL acting as a global identifier) and a key id (``kid``)
   on the request signature. The framework's ``AuthInfo.principal``
   is the verified-principal opaque string the auth layer surfaces —
   the documented convention is ``agent_url`` for signed-request
   agents, OAuth subject claim for bearer tokens, mTLS subject for
   client-cert flows. Adopters wiring ``FromAuthAccounts`` decide
   what string their auth middleware projects onto
   ``ctx.auth_info.principal``; the framework treats it opaquely.

   The SDK's signing primitives in :mod:`adcp.signing` verify the
   request signature against a JWKS provider; it's the adopter's
   middleware (today) or the framework's built-in
   ``SignedRequestAuth`` adapter wrapper (Tier 2, lands in 4.5.0)
   that takes the verified result and writes ``AuthInfo.principal =
   agent_url`` onto the dispatch context. Adopters wiring this
   manually before 4.5.0 ships should follow that convention to keep
   ``adcp.adagents`` (the spec validator that ties an ``agent_url``
   to a seller's published ``adagents.json`` whitelist) reading the
   right key.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, Protocol, runtime_checkable

from typing_extensions import TypeVar

from adcp.decisioning.context import AuthInfo
from adcp.decisioning.types import (
    Account,
    SyncAccountsResultRow,
    SyncGovernanceEntry,
    SyncGovernanceResultRow,
)

if TYPE_CHECKING:
    from adcp.decisioning.registry import BuyerAgent
    from adcp.types import AccountReference

#: Per-platform metadata generic.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@dataclass
class ResolveContext:
    """Per-request context threaded into :class:`AccountStore` methods
    that need the caller's principal but don't get a full
    :class:`RequestContext` (because the resolved account isn't yet
    available, or the surface operates on multiple accounts at once).

    Mirrors the JS-side ``ResolveContext`` shape: ``auth_info``,
    ``tool_name``, ``agent``. Adopters read these to implement
    principal-keyed gates on ``sync_accounts`` / ``list_accounts`` /
    ``sync_governance`` (e.g., the spec's
    ``BILLING_NOT_PERMITTED_FOR_AGENT`` per-buyer-agent gate from
    adcontextprotocol/adcp#3851) without re-deriving identity from
    the request.

    **Prefer ``agent`` over ``auth_info`` for commercial-relationship
    decisions.** ``agent`` is the registry-resolved durable identity
    (status, billing capabilities, default account terms);
    ``auth_info`` is the raw transport-level credential. For billing
    gates the registry-resolved identity is canonical.

    :param auth_info: Verified principal info. ``None`` for
        unauthenticated requests (dev / ``'derived'`` fixtures).
    :param tool_name: Wire verb that triggered the call
        (e.g. ``'sync_accounts'``, ``'list_accounts'``,
        ``'sync_governance'``). Adopters use it for audit logs; the
        framework doesn't dispatch on it.
    :param agent: Resolved :class:`BuyerAgent` when a
        :class:`BuyerAgentRegistry` is wired. ``None`` otherwise.
    """

    auth_info: AuthInfo | None = None
    tool_name: str | None = None
    agent: BuyerAgent | None = None
    #: Adopter passthrough for additional context the framework
    #: doesn't model. Reserved for forward compatibility.
    extra: dict[str, Any] = field(default_factory=dict)


def _call_with_optional_ctx(
    fn: Callable[..., Any],
    *args: Any,
    ctx: ResolveContext | None,
) -> Any:
    """Call ``fn`` with ``ctx`` as a trailing keyword arg when its
    signature accepts one; fall back to positional-only invocation
    when it doesn't.

    The framework probes via :func:`inspect.signature` so adopter
    impls written against the pre-ctx Protocol (no ``ctx`` parameter)
    keep working unchanged. Same posture as the JS-side optional-
    parameter pattern.

    Used by the framework's dispatch shims for
    :meth:`AccountStore.upsert`, :meth:`AccountStore.list`, and
    :meth:`AccountStore.sync_governance`.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # Built-in / C-extension callables don't expose a signature;
        # call positionally and let the adopter's surface handle the
        # mismatch.
        return fn(*args)
    params = sig.parameters
    if "ctx" in params:
        return fn(*args, ctx=ctx)
    # No ctx parameter — pre-ctx adopter impl. Drop ctx silently
    # (it's optional on every method that takes it).
    return fn(*args)


@runtime_checkable
class AccountStore(Protocol, Generic[TMeta]):
    """Resolves a wire reference + auth context to an :class:`Account`.

    The framework calls :meth:`resolve` for every tool dispatch
    (before the handler method runs). Adopters in ``'explicit'`` mode
    use ``ref.account_id`` from the wire; ``'implicit'`` mode reads
    ``ctx.auth_info`` to look up the principal-bound account;
    ``'derived'`` mode synthesizes a per-principal account from the
    one platform.

    The :attr:`resolution` literal is a structural attribute the
    framework reads at server boot — used by :func:`validate_platform`
    to fail fast on misconfigured deployments (e.g.
    ``'derived'`` registered into a multi-tenant ``TenantRegistry``).
    Mirrors the JS-side literal for cross-language parity:
    ``'explicit'`` (wire ref drives lookup), ``'implicit'`` (verified
    auth principal drives lookup), ``'derived'`` (single-platform with
    per-principal id synthesis).
    """

    resolution: Literal["explicit", "implicit", "derived"]

    def resolve(
        self,
        ref: dict[str, Any] | None,
        auth_info: AuthInfo | None = None,
    ) -> Awaitable[Account[TMeta]] | Account[TMeta]:
        """Return the resolved :class:`Account` or raise on miss.

        :param ref: The wire reference object (typically
            ``request.account`` carrying ``account_id`` /
            ``account_ref``). ``None`` for tools that don't carry an
            explicit account ref — adopters in ``'derived'`` /
            ``'implicit'`` modes ignore it.
        :param auth_info: Verified principal info. ``None`` for
            unauthenticated requests (dev / ``'derived'`` fixtures).
        :raises adcp.decisioning.AdcpError: ``code='ACCOUNT_NOT_FOUND'``
            when the resolution can't produce a valid account.

        Implementations may be sync or async; the dispatch adapter
        detects via :func:`inspect.iscoroutine` at call time.
        """
        ...

    # ----- Optional v6 surfaces -----
    #
    # The methods below are documented on the Protocol class for
    # discoverability but live on the SEPARATE :class:`AccountStoreUpsert`,
    # :class:`AccountStoreList`, and :class:`AccountStoreSyncGovernance`
    # Protocols below — keeping them off the runtime-checkable
    # :class:`AccountStore` Protocol so an adopter who only implements
    # ``resolve`` (the minimum viable AccountStore) still passes
    # ``isinstance(store, AccountStore)``. The framework's dispatch
    # shim probes via :func:`hasattr` for the optional methods at
    # call time and surfaces ``UNSUPPORTED_FEATURE`` when absent.
    #
    # See:
    #   * :meth:`AccountStoreUpsert.upsert` — ``sync_accounts``
    #   * :meth:`AccountStoreList.list` — ``list_accounts``
    #   * :meth:`AccountStoreSyncGovernance.sync_governance`


@runtime_checkable
class AccountStoreUpsert(Protocol):
    """``sync_accounts`` API surface. Optional adopter-side feature
    that complements :class:`AccountStore.resolve`.

    Not parameterized over ``TMeta`` — :meth:`upsert` returns
    :class:`SyncAccountsResultRow` (a wire-shaped row, no per-platform
    metadata) rather than ``Account[TMeta]``, so the type variable
    isn't needed here.

    Adopters implement this on the same object as :class:`AccountStore`
    (Protocols are structural — Python doesn't require explicit
    inheritance) and the framework's dispatch shim picks it up via
    :func:`hasattr`.

    **Backwards-compatible.** ``ctx`` is optional on the platform
    side, so adopter impls written before ctx threading landed (no
    ``ctx`` parameter) keep working — the framework's
    :func:`_call_with_optional_ctx` shim probes via
    :func:`inspect.signature` and drops ``ctx`` for pre-ctx impls.
    """

    def upsert(
        self,
        refs: list[AccountReference],
        ctx: ResolveContext | None = None,
    ) -> Awaitable[list[SyncAccountsResultRow]] | list[SyncAccountsResultRow]:
        """``sync_accounts`` API surface. Framework normalizes the
        wire request; platform upserts and returns per-account result
        rows. Raise :class:`adcp.decisioning.AdcpError` for
        buyer-facing rejection.

        ``ctx.auth_info`` carries the caller's authenticated
        principal; ``ctx.agent`` carries the resolved
        :class:`BuyerAgent` record (when a registry is configured).
        Adopters implementing principal-keyed gates (e.g.,
        per-buyer-agent ``BILLING_NOT_PERMITTED_FOR_AGENT`` on the
        spec's billing surfaces) read the principal here — same
        threading as :meth:`AccountStore.resolve`.

        **Prefer ``ctx.agent`` over ``ctx.auth_info`` for
        commercial-relationship decisions.** ``ctx.agent`` is the
        registry-resolved durable identity (status, billing
        capabilities, default account terms); ``ctx.auth_info``
        carries the raw transport-level credential. For billing gates
        the registry-resolved identity is canonical.
        """
        ...


@runtime_checkable
class AccountStoreList(Protocol, Generic[TMeta]):
    """``list_accounts`` API surface. Optional adopter-side feature.

    Framework wraps the returned account list with the cursor
    envelope and projects each account through
    :func:`to_wire_account` (stripping framework-internal fields and
    applying the write-only strips for ``billing_entity.bank`` and
    ``governance_agents[].authentication``).

    **Security migration note.** Pre-this-release, adopters had no
    way to scope ``list_accounts`` per-principal — impls either
    returned all accounts (over-disclosure) or rejected the
    operation. Post-this-release, scoping becomes possible via
    ``ctx.agent``. **This is opt-in, not automatic.** Multi-tenant
    adopters MUST add principal scoping in their impl; without it,
    every authenticated caller sees every account.
    """

    def list(
        self,
        filter: dict[str, Any] | None = None,
        ctx: ResolveContext | None = None,
    ) -> Awaitable[list[Account[TMeta]]] | list[Account[TMeta]]:
        """Return the accounts visible to the calling principal.

        :param filter: Wire-shape filter object — ``status`` /
            ``sandbox`` / pagination. Pass-through from the parsed
            wire request.
        :param ctx: Per-request context. ``ctx.auth_info`` and
            ``ctx.agent`` carry the caller's principal — adopters
            scope the listing per-principal (e.g., return only
            accounts visible to the calling buyer agent) without
            re-deriving identity from the request.
        """
        ...


@runtime_checkable
class AccountStoreSyncGovernance(Protocol):
    """``sync_governance`` API surface. Optional adopter-side feature.

    Buyers register governance agent endpoints per-account; the
    seller persists the binding and consults the agents during media
    buy lifecycle events via ``check_governance``. Adopters that
    don't model buyer-supplied governance agents (most direct
    sellers) leave this unimplemented and the framework returns
    ``UNSUPPORTED_FEATURE``.
    """

    def sync_governance(
        self,
        entries: list[SyncGovernanceEntry],
        ctx: ResolveContext | None = None,
    ) -> Awaitable[list[SyncGovernanceResultRow]] | list[SyncGovernanceResultRow]:
        """Persist the per-entry governance-agent bindings.

        ``entries`` is the wire request's ``accounts[]`` — each entry
        pairs an :class:`AccountReference` with its
        ``governance_agents[]``. The framework has already deduped on
        ``idempotency_key`` and stripped wire metadata
        (``adcp_major_version``, ``context``, ``ext``) before
        invoking this method.

        **Replace semantics, per spec.** Each call REPLACES the
        previously synced governance agents for the referenced
        account. An entry whose ``governance_agents`` is empty clears
        the binding for that account.

        **Write-only credentials.** Each
        ``governance_agents[i].authentication.credentials`` is the
        bearer the seller presents to that governance agent on
        outbound ``check_governance`` calls. Persist them — silently
        dropping ships unauthenticated requests once cross-agent
        calls are wired. The framework strips ``authentication`` from
        each ``governance_agents[i]`` of every row before
        serialization (:func:`to_wire_sync_governance_row`), so
        credentials never reach the response wire OR the idempotency
        replay cache, even if an adopter returns a loosely-typed row
        that spreads the input. Do not rely on Python type hints
        alone — the strip is enforced at the dispatcher.

        ``ctx.auth_info`` and ``ctx.agent`` carry the caller's
        principal. Adopters MUST gate per-entry persistence by the
        caller's tenant: each entry's ``account.operator`` (or
        ``account_id``) must map to the same tenant the auth
        principal authorizes; otherwise return a row with
        ``status='failed'`` carrying
        ``errors=[{code: 'PERMISSION_DENIED', ...}]`` for that entry.
        Operation-level rejection (``raise AdcpError(...)``) fails
        the whole batch, which is the wrong shape when a single
        entry fails the gate.
        """
        ...


# ---------------------------------------------------------------------------
# Reference implementations
# ---------------------------------------------------------------------------


class SingletonAccounts(Generic[TMeta]):
    """Single-platform deployment with per-principal idempotency scoping.

    Use for: Innovid training-agent class, single-publisher proof-of-
    concepts, dev/staging environments.

    Synthesizes ``account.id`` from the verified principal:
    ``f"{base_account_id}:{principal}"``. Without this, every caller
    across the entire deployment would share one idempotency cache —
    UUID collision (random or engineered) returns another caller's
    ``response_payload``, which is a buyer-to-buyer data leak.
    Per-principal synthesis closes this while keeping the "one platform,
    no per-tenant lookup" ergonomic.

    For unauthenticated dev fixtures (``ctx.auth_info is None``),
    the synthesized id is ``f"{base_account_id}:anonymous"`` — adopters
    relying on this MUST ensure their dev/CI pipeline authenticates
    before any cross-test isolation matters.

    Example::

        class TrainingAgentSeller(DecisioningPlatform):
            accounts = SingletonAccounts(account_id="training-agent")

    :param account_id: Base account id used in the synthesized
        per-principal id. Must be stable across process restarts so
        idempotency cache hits work across deploys.
    :param name: Human-readable name copied to ``Account.name``.
    :param metadata_factory: Optional factory for ``Account.metadata``
        — adopters with typed metadata pass a closure that returns the
        right TypedDict / dataclass instance.
    """

    resolution: Literal["derived"] = "derived"

    def __init__(
        self,
        account_id: str,
        *,
        name: str = "",
        metadata_factory: Callable[[], TMeta] | None = None,
    ) -> None:
        if not account_id or not isinstance(account_id, str):
            raise ValueError(
                f"SingletonAccounts requires a non-empty account_id; got {account_id!r}"
            )
        self._account_id = account_id
        self._name = name or account_id
        self._metadata_factory = metadata_factory

    def resolve(
        self,
        ref: dict[str, Any] | None = None,
        auth_info: AuthInfo | None = None,
    ) -> Account[TMeta]:
        del ref  # singleton ignores wire refs
        principal = auth_info.principal if auth_info and auth_info.principal else "anonymous"
        scoped_id = f"{self._account_id}:{principal}"
        metadata: TMeta = (
            self._metadata_factory() if self._metadata_factory else {}  # type: ignore[assignment]
        )
        return Account(
            id=scoped_id,
            name=f"{self._name} ({principal})" if principal != "anonymous" else self._name,
            status="active",
            metadata=metadata,
            auth_info=_auth_info_to_dict(auth_info),
        )


class ExplicitAccounts(Generic[TMeta]):
    """Multi-tenant where the wire ref identifies the account.

    Use for: salesagent (URL-pattern ``/tenants/<id>/...``), DSPs that
    expose multi-account-per-principal flows, agencies routing across
    publisher accounts via ``account.account_id`` in the body.

    The framework passes ``ref`` from the parsed request body
    (typically ``request.account``); ``resolve`` reads
    ``ref["account_id"]`` and routes through the adopter-supplied
    ``loader``. The wire ref is the source of truth for *which*
    account to resolve.

    Auth scope checks (does this principal have access to the
    requested account?) are NOT performed by ``ExplicitAccounts.resolve``
    — the default loader signature only takes ``account_id``. Adopters
    needing principal-vs-account scope enforcement implement the
    :class:`AccountStore` Protocol directly with a custom resolve that
    reads ``auth_info``, OR add a request middleware that runs before
    the handler. The framework does NOT silently bind ``auth_info`` to
    the lookup; if your loader returns an account a principal shouldn't
    see, you've shipped a cross-tenant data leak.

    Example::

        class SalesAgentSeller(DecisioningPlatform):
            accounts = ExplicitAccounts(loader=load_tenant_from_db)

    :param loader: Callable taking ``account_id: str`` and returning an
        :class:`Account` instance. Sync or async. Raises
        ``AdcpError(code='ACCOUNT_NOT_FOUND')`` on miss.
    """

    resolution: Literal["explicit"] = "explicit"

    def __init__(
        self,
        loader: Callable[[str], Awaitable[Account[TMeta]] | Account[TMeta]],
    ) -> None:
        self._loader = loader

    def resolve(
        self,
        ref: dict[str, Any] | None,
        auth_info: AuthInfo | None = None,
    ) -> Awaitable[Account[TMeta]] | Account[TMeta]:
        # Explicit mode resolves purely off the wire ref. Adopters
        # needing principal-vs-account scope checks implement
        # AccountStore directly (see class docstring). The loader
        # signature is account_id-only by contract, so auth_info isn't
        # threaded through here.
        del auth_info
        if not ref or not ref.get("account_id"):
            from adcp.decisioning.types import AdcpError

            raise AdcpError(
                "ACCOUNT_NOT_FOUND",
                message=(
                    "ExplicitAccounts.resolve requires ref with 'account_id'; "
                    "got missing/empty ref"
                ),
                recovery="terminal",
                field="account.account_id",
            )
        return self._loader(ref["account_id"])


class FromAuthAccounts(Generic[TMeta]):
    """Multi-tenant where the verified auth principal identifies the account.

    Use for: signed-request-bound integrations (one signing key per
    publisher account), OAuth-bearer integrations where the token
    binds to a specific account, MMP / measurement-vendor patterns
    where the principal IS the account holder.

    Reads ``auth_info.principal`` and routes through the adopter-
    supplied ``loader``. The wire ``ref`` is ignored — the auth
    principal is the source of truth.

    Example::

        class MeasurementVendor(DecisioningPlatform):
            accounts = FromAuthAccounts(loader=load_account_for_principal)

    :param loader: Callable taking ``principal: str`` and returning an
        :class:`Account` instance. Sync or async.
    """

    resolution: Literal["implicit"] = "implicit"

    def __init__(
        self,
        loader: Callable[[str], Awaitable[Account[TMeta]] | Account[TMeta]],
    ) -> None:
        self._loader = loader

    def resolve(
        self,
        ref: dict[str, Any] | None = None,
        auth_info: AuthInfo | None = None,
    ) -> Awaitable[Account[TMeta]] | Account[TMeta]:
        del ref  # from_auth ignores wire refs
        if auth_info is None or not auth_info.principal:
            from adcp.decisioning.types import AdcpError

            raise AdcpError(
                "AUTH_INVALID",
                message=(
                    "FromAuthAccounts.resolve requires auth_info with a "
                    "verified principal; got None / empty"
                ),
                recovery="terminal",
            )
        return self._loader(auth_info.principal)


def _auth_info_to_dict(auth_info: AuthInfo | None) -> dict[str, Any] | None:
    """Project an :class:`AuthInfo` to the dict shape ``Account.auth_info``
    carries. Returns ``None`` when auth_info is absent — keeps account
    serialization stable for unauthenticated requests."""
    if auth_info is None:
        return None
    return {
        "kind": auth_info.kind,
        "key_id": auth_info.key_id,
        "principal": auth_info.principal,
        "scopes": list(auth_info.scopes),
    }
