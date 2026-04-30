"""Account resolution: ``AccountStore`` Protocol + three reference impls.

Adopters pick a resolution mode at registration time:

* :class:`SingletonAccounts` — single-process / single-platform
  deployments (Innovid training-agent, single-publisher proof-of-concept).
  Synthesizes ``account.id`` per verified principal so idempotency
  scopes correctly across distinct callers.
* :class:`ExplicitAccounts` — multi-tenant where the URL or request
  body identifies the account (``/tenants/<id>``, ``account.account_id``
  in body). Resolves by the wire reference.
* :class:`FromAuthAccounts` — multi-tenant or single-tenant where the
  verified auth principal identifies the account (signed-request bound,
  OAuth bearer bound). Resolves by ``ctx.auth_info.principal``.

Adopters with shapes that don't fit these three implement the
:class:`AccountStore` Protocol directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Generic, Literal, Protocol, runtime_checkable

from typing_extensions import TypeVar

from adcp.decisioning.context import AuthInfo
from adcp.decisioning.types import Account

#: Per-platform metadata generic.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class AccountStore(Protocol, Generic[TMeta]):
    """Resolves a wire reference + auth context to an :class:`Account`.

    The framework calls :meth:`resolve` for every tool dispatch
    (before the handler method runs). Adopters in ``'explicit'`` mode
    use ``ref.account_id`` from the wire; ``'from_auth'`` mode reads
    ``ctx.auth_info`` to look up the principal-bound account;
    ``'singleton'`` mode synthesizes a per-principal account from the
    one platform.

    The :attr:`resolution` literal is a structural attribute the
    framework reads at server boot — used by :func:`validate_platform`
    to fail fast on misconfigured deployments (e.g.
    ``'singleton'`` registered into a multi-tenant ``TenantRegistry``).
    """

    resolution: Literal["explicit", "from_auth", "singleton"]

    def resolve(
        self,
        ref: dict[str, Any] | None,
        auth_info: AuthInfo | None = None,
    ) -> Awaitable[Account[TMeta]] | Account[TMeta]:
        """Return the resolved :class:`Account` or raise on miss.

        :param ref: The wire reference object (typically
            ``request.account`` carrying ``account_id`` /
            ``account_ref``). ``None`` for tools that don't carry an
            explicit account ref — adopters in ``'singleton'`` /
            ``'from_auth'`` modes ignore it.
        :param auth_info: Verified principal info. ``None`` for
            unauthenticated requests (dev / ``'singleton'`` fixtures).
        :raises adcp.decisioning.AdcpError: ``code='ACCOUNT_NOT_FOUND'``
            when the resolution can't produce a valid account.

        Implementations may be sync or async; the dispatch adapter
        detects via :func:`inspect.iscoroutine` at call time.
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

    resolution: Literal["singleton"] = "singleton"

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

    resolution: Literal["from_auth"] = "from_auth"

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
