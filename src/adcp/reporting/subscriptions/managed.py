"""The managed seller path for reporting subscriptions (AdCP 3.3, experimental).

``ReliableReportingService(subscriptions=SubscriptionOptions(...))`` enables
``media_buy.reporting_subscriptions``. The block's presence is the switch; the
SDK derives everything else from surfaces the seller already serves:

* **Principal runtime.** The service owns a
  :class:`~adcp.server.PrincipalService`, links it to the engine, and
  ``install()`` serves ``get_principal`` and ``sync_principal`` from it and
  advertises the principal section.
* **Roster.** The accounts a principal may read come from the seller's own
  ``list_accounts`` (a decisioning platform's ``accounts.list``, or an
  ``ADCPHandler.list_accounts``), invoked under the principal's identity: a
  service context carrying the principal's last authenticated binding, with
  no credential material.
* **Buys.** Each account's buys come from the seller's ``get_media_buys``
  under the same identity, so visibility matches what the principal can see.
* **Offerings.** From the service's registered capability offerings, each
  of which must publish ``standard_dimensions``.
* **Scheduling.** ``sync_principal``, ``sync_accounts``, ``create_media_buy``
  and ``update_media_buy`` served through the installed platform reconcile the
  principals concerned, and every worker turn reconciles each principal a
  period boundary has passed for before it plans obligations.

Each derivation has an override on :class:`SubscriptionOptions`. A seller
whose accounts are only reachable through a live upstream token supplies
``accounts``: a listing that fails under the service context raises
``SUBSCRIPTION_ROSTER_UNAVAILABLE`` and never reads as an empty roster.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from adcp._principal_ref import PrincipalRef
from adcp.reporting._errors import ReliableReportingConfigurationError
from adcp.reporting.subscriptions._clock import utc_instant
from adcp.reporting.subscriptions.coverage import SubscriptionBuy
from adcp.reporting.subscriptions.engine import (
    AccountConfigsEnder,
    AccountDeliveryConfig,
    AuthorizedAccount,
    AuthorizedAccountPage,
    ReportingSubscriptionStateStore,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.models import ReportingDefinitionBinding
    from adcp.reporting.service_subscriptions import (
        ManagedGenerationBinder,
        PlanningErrorHandler,
        ReportingSubscriptionServiceOptions,
        SubscriptionPlanningFailure,
    )
    from adcp.reporting.subscriptions.coverage import OwedSubscriptionPeriod
    from adcp.reporting.subscriptions.engine import ReportingSubscriptionEngine
    from adcp.reporting.subscriptions.resolution import SubscriptionOffering
    from adcp.server.principal import (
        PrincipalChangeEmitter,
        PrincipalRecordStore,
        PrincipalService,
    )

logger = logging.getLogger(__name__)

#: ``(principal, *, cursor)`` -> one page of the accounts the principal may read.
AccountsOverride = Callable[..., "Awaitable[AuthorizedAccountPage] | AuthorizedAccountPage"]
#: ``(principal, account_id)`` -> the buys the principal can see on the account.
BuysOverride = Callable[
    [PrincipalRef, str], "Awaitable[Sequence[SubscriptionBuy]] | Sequence[SubscriptionBuy]"
]
#: ``(media_buy)`` -> each package's ``reporting_delivery_offering_ids``, keyed by
#: ``package_id``; ``media_buy`` is the wire ``get_media_buys`` entry.
BuyOfferingsOverride = Callable[[Mapping[str, Any]], Mapping[str, Sequence[str]]]
#: ``(principal, account_id)`` -> the caller's own active account-level configurations.
AccountConfigsOverride = Callable[
    [PrincipalRef, str],
    "Awaitable[Sequence[AccountDeliveryConfig]] | Sequence[AccountDeliveryConfig]",
]
#: ``(principal, account_id, period, *, by)`` -> whether the period was delivered by ``by``.
PeriodDeliveredOverride = Callable[..., "Awaitable[bool] | bool"]
#: ``(request, context)`` -> the authenticated principal, never derived from request content.
ManagedPrincipalResolver = Callable[[Any, Any], Any]

#: The SDK's request-context key for the verified :class:`~adcp.decisioning.AuthInfo`.
_AUTH_INFO_KEY = "adcp.auth_info"
_ALL_BUY_STATUSES = (
    "pending_creatives",
    "pending_start",
    "active",
    "paused",
    "completed",
    "rejected",
    "canceled",
)
#: Hard cap on pages walked for one account's buys; a walk that does not end is
#: refused, never truncated. The engine caps the roster walk the same way.
_MAX_PAGES = 10_000


def _package_offering_ids(package: Mapping[str, Any], every: Sequence[str]) -> list[str]:
    """A package's ``reporting_delivery_offering_ids`` snapshot (3.3.0-beta.3).

    The seller fixes it when the package is confirmed and serves ``[]`` when
    no offering applies, so an empty snapshot is honoured. Only a package
    served without one falls back to every registered offering.
    """
    snapshot = package.get("reporting_delivery_offering_ids")
    if isinstance(snapshot, (list, tuple)):
        return [str(item) for item in snapshot]
    return list(every)


@dataclass(frozen=True)
class SubscriptionOptions:
    """Serve ``media_buy.reporting_subscriptions`` from ``ReliableReportingService``.

    Its presence enables the feature. ``max_accounts`` and
    ``backfill_on_regrant`` are advertised as ``subscriptions.max_accounts``
    and ``subscriptions.backfill_on_regrant``; ``dimensions`` defaults to the
    union of the offerings' ``standard_dimensions``.

    Every other field is an optional override of something the SDK derives:

    * ``accounts(principal, *, cursor)``: the roster, instead of the seller's
      ``list_accounts`` under the principal's identity. Required when account
      access depends on a live upstream token.
    * ``buys(principal, account_id)``: an account's buys, instead of
      ``get_media_buys``.
    * ``buy_offerings(media_buy)``: each package's offering ids, keyed by
      ``package_id``. Without it each package's own
      ``reporting_delivery_offering_ids`` snapshot from ``get_media_buys`` is
      used (3.3.0-beta.3, fixed at confirmation; ``[]`` means no offering), and
      only a package without one is reported by every offering the service
      registered.
    * ``configure(generation)``: the report content for a subscription
      generation, instead of the service's ``account_context``.
    * ``account_delivery_configs(principal, account_id)``: the caller's own
      active ``sync_accounts`` configurations, so they take precedence.
    * ``end_account_configs(principal, account_id, ids, *, at)``: ends them for
      ``replace_account_configs`` (needs ``account_delivery_configs``). Without
      it the flag is refused with ``UNSUPPORTED_FEATURE``.
    * ``is_period_delivered(principal, account_id, period, *, by)``: exact
      ``withheld_period_count``.
    * ``resolve_principal(request, context)``: the authenticated principal.
      By default it is the caller the reporting tasks resolve (the bearer
      principal, the verified signed-request principal, or
      ``ToolContext.caller_identity``), with ``ToolContext.tenant_id``.

    ``reconcile_on_write`` (default on) reconciles a principal when a served
    write changes what it is owed. The reconcile runs after the response, on
    a background drain, so a write's latency never depends on how many
    principals share the account; the worker's boundary sweep catches anything
    it misses. ``read_timeout`` bounds each read of the platform's
    ``list_accounts``/``get_media_buys`` (``None`` for no bound).

    ``principals``, ``principal_store`` and ``state_store`` supply the
    principal runtime and the stores; share durable stores across every worker
    in production. ``account_configuration`` keeps ``sync_accounts`` in
    ``configuration_tasks``. ``standard_dimensions`` maps an ``offering_id``
    to its standard dimensions for an offering whose declaration cannot carry
    them yet: production offerings validate against the served 3.3.0-beta.1
    contract, which lacks the field. It goes away once the SDK serves
    3.3.0-beta.3.
    """

    max_accounts: int | None = None
    backfill_on_regrant: bool = False
    dimensions: Sequence[str] | None = None
    accounts: AccountsOverride | None = None
    buys: BuysOverride | None = None
    buy_offerings: BuyOfferingsOverride | None = None
    configure: Callable[[Any], Any] | None = None
    account_delivery_configs: AccountConfigsOverride | None = None
    end_account_configs: AccountConfigsEnder | None = None
    is_period_delivered: PeriodDeliveredOverride | None = None
    resolve_principal: ManagedPrincipalResolver | None = None
    reconcile_on_write: bool = True
    read_timeout: timedelta | None = timedelta(seconds=30)
    standard_dimensions: Mapping[str, Sequence[str]] | None = None
    account_configuration: bool = True
    principal_kind: str = "buyer_agent"
    principals: PrincipalService | None = None
    principal_store: PrincipalRecordStore | None = None
    state_store: ReportingSubscriptionStateStore | None = None
    emit_change: PrincipalChangeEmitter | None = None
    definitions: Mapping[str, ReportingDefinitionBinding] | None = None
    bind_managed_generation: ManagedGenerationBinder | None = None
    planning_error_handler: PlanningErrorHandler | None = None

    def __post_init__(self) -> None:
        if self.max_accounts is not None and (
            type(self.max_accounts) is not int or self.max_accounts < 1
        ):
            raise ValueError("SubscriptionOptions.max_accounts must be a positive integer")
        if self.read_timeout is not None and self.read_timeout <= timedelta(0):
            raise ValueError("SubscriptionOptions.read_timeout must be positive")
        if self.principals is not None and self.principal_store is not None:
            raise ValueError("pass principals or principal_store, not both")
        if self.end_account_configs is not None and self.account_delivery_configs is None:
            raise ValueError(
                "SubscriptionOptions.end_account_configs needs account_delivery_configs, which "
                "names the configurations replace_account_configs ends"
            )
        for name in (
            "accounts",
            "buys",
            "buy_offerings",
            "configure",
            "account_delivery_configs",
            "end_account_configs",
            "is_period_delivered",
            "resolve_principal",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"SubscriptionOptions.{name} must be callable")


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _wire(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return utc_instant(value)
    from adcp.reporting._timestamp import aware_timestamp

    # Wire instants may carry 1-6 fractional digits, which Python 3.10's ISO
    # parser refuses.
    return aware_timestamp(str(value))


# -- principal identity --------------------------------------------------------


def request_principal_id(context: Any) -> str | None:
    """The authenticated caller a request context carries, as the reporting tasks read it.

    The same derivation as ``ReliableReportingService``'s default caller: an
    ``AccountAwareToolContext`` (or a plain ``ToolContext``) contributes its
    ``caller_identity``; otherwise the bearer middleware's current principal,
    then a verified signed-request principal. A decisioning ``RequestContext``
    never contributes its ``caller_identity``, which is a cache key.
    """
    from adcp.server.auth import current_principal

    if hasattr(context, "account_id"):
        value = getattr(context, "caller_identity", None)
        return value if isinstance(value, str) and value else None
    principal = current_principal.get()
    if principal:
        return principal
    auth_info = getattr(context, "auth_info", None)
    if auth_info is None:
        metadata = getattr(context, "metadata", None)
        if isinstance(metadata, Mapping):
            auth_info = metadata.get(_AUTH_INFO_KEY)
    auth_principal = getattr(context, "auth_principal", None)
    verified = getattr(auth_info, "principal", None)
    if auth_principal is not None and auth_principal == verified:
        return str(auth_principal)
    if type(context).__name__ != "RequestContext":
        # A transport ToolContext: the verified principal the decisioning
        # RequestContext would expose as auth_principal, else the identity the
        # context factory installed.
        if isinstance(verified, str) and verified:
            return verified
        value = getattr(context, "caller_identity", None)
        if isinstance(value, str) and value:
            return value
    return None


def _auth_snapshot(context: Any) -> dict[str, Any] | None:
    """A token-free copy of the request's verified ``AuthInfo``, as JSON."""
    metadata = getattr(context, "metadata", None)
    auth_info = metadata.get(_AUTH_INFO_KEY) if isinstance(metadata, Mapping) else None
    if auth_info is None:
        auth_info = getattr(context, "auth_info", None)
    if isinstance(auth_info, Mapping):
        # The legacy dict shape the decisioning handler also accepts.
        from adcp.decisioning.context import AuthInfo

        auth_info = AuthInfo._from_legacy_dict(dict(auth_info))
    if auth_info is None or not hasattr(auth_info, "kind"):
        return None
    snapshot: dict[str, Any] = {"kind": str(auth_info.kind)}
    for name in ("key_id", "principal", "agent_url", "operator"):
        value = getattr(auth_info, name, None)
        if isinstance(value, str) and value:
            snapshot[name] = value
    scopes = getattr(auth_info, "scopes", None)
    if scopes:
        snapshot["scopes"] = [str(item) for item in scopes]
    credential: Any = getattr(auth_info, "credential", None)
    kind = getattr(credential, "kind", None)
    # Credentials here are identities, not secrets: a key id, a client id, a
    # verified signer. Nothing else on AuthInfo (``extra``) is kept.
    if kind == "api_key":
        snapshot["credential"] = {"kind": kind, "key_id": credential.key_id}
    elif kind == "oauth":
        snapshot["credential"] = {
            "kind": kind,
            "client_id": credential.client_id,
            "scopes": list(credential.scopes),
        }
    elif kind == "http_sig":
        snapshot["credential"] = {
            "kind": kind,
            "keyid": credential.keyid,
            "agent_url": credential.agent_url,
            "verified_at": credential.verified_at,
        }
    return snapshot


def _restore_auth(snapshot: Mapping[str, Any] | None) -> Any:
    if not snapshot:
        return None
    from adcp.decisioning.context import AuthInfo
    from adcp.decisioning.registry import ApiKeyCredential, HttpSigCredential, OAuthCredential

    raw = dict(snapshot.get("credential") or {})
    credential: Any = None
    if raw.get("kind") == "api_key":
        credential = ApiKeyCredential(kind="api_key", key_id=raw["key_id"])
    elif raw.get("kind") == "oauth":
        credential = OAuthCredential(
            kind="oauth", client_id=raw["client_id"], scopes=tuple(raw.get("scopes") or ())
        )
    elif raw.get("kind") == "http_sig":
        credential = HttpSigCredential(
            kind="http_sig",
            keyid=raw["keyid"],
            agent_url=raw["agent_url"],
            verified_at=float(raw["verified_at"]),
        )
    return AuthInfo(
        kind=str(snapshot["kind"]),
        key_id=snapshot.get("key_id"),
        principal=snapshot.get("principal"),
        scopes=list(snapshot.get("scopes") or ()),
        credential=credential,
        agent_url=snapshot.get("agent_url"),
        operator=snapshot.get("operator"),
    )


def _roster_unavailable(principal: PrincipalRef, cause: str) -> ReliableReportingConfigurationError:
    return ReliableReportingConfigurationError(
        f"the accounts principal {principal.principal_id!r} may read could not be listed "
        f"under its identity ({cause}); reporting subscriptions never read that as an empty "
        "roster. Supply SubscriptionOptions.accounts to list them another way.",
        code="SUBSCRIPTION_ROSTER_UNAVAILABLE",
        config_field="SubscriptionOptions.accounts",
    )


# -- derived host --------------------------------------------------------------


class _ManagedHost:
    """The engine's host facts, derived from the installed platform or overridden."""

    def __init__(self, managed: ManagedSubscriptions) -> None:
        self._managed = managed
        options = managed.options
        if options.account_delivery_configs is not None:
            self.account_delivery_configs = self._account_delivery_configs
        if options.is_period_delivered is not None:
            self.is_period_delivered = self._is_period_delivered

    async def list_authorized_accounts(
        self, principal: Any, *, cursor: str | None
    ) -> AuthorizedAccountPage:
        ref = PrincipalRef.coerce(principal)
        override = self._managed.options.accounts
        if override is not None:
            page = await _resolve(override(ref, cursor=cursor))
            if not isinstance(page, AuthorizedAccountPage):
                raise TypeError("SubscriptionOptions.accounts must return AuthorizedAccountPage")
            return page
        return await self._managed._platform_accounts(ref, cursor)

    async def account_buys(self, principal: Any, account_id: str) -> Sequence[SubscriptionBuy]:
        ref = PrincipalRef.coerce(principal)
        override = self._managed.options.buys
        if override is not None:
            return tuple(await _resolve(override(ref, account_id)))
        return await self._managed._platform_buys(ref, account_id)

    async def _account_delivery_configs(
        self, principal: Any, account_id: str
    ) -> Sequence[AccountDeliveryConfig]:
        hook = self._managed.options.account_delivery_configs
        assert hook is not None
        return tuple(await _resolve(hook(PrincipalRef.coerce(principal), account_id)))

    async def _is_period_delivered(
        self,
        principal: Any,
        account_id: str,
        period: OwedSubscriptionPeriod,
        *,
        by: datetime,
    ) -> bool:
        hook = self._managed.options.is_period_delivered
        assert hook is not None
        return bool(await _resolve(hook(PrincipalRef.coerce(principal), account_id, period, by=by)))


@dataclass
class _Platform:
    """Where the derived host reads ``list_accounts`` and ``get_media_buys``."""

    target: Any = None
    #: A decisioning ``PlatformHandler`` once ``serve`` builds it.
    handler: Any = None
    decisioning: bool = False
    sync_accounts_seen: set[str] = field(default_factory=set)


class SubscriptionGrants:
    """Push grant changes into the grant journal: ``reporting.grants``.

    For sellers whose entitlement service emits events: a pushed change is
    dated exactly, where a listing can only observe it. The journal then
    re-evaluates the principal's exceptions without listing the host's
    accounts, so a listing that lags the push cannot undo it. Listings
    observed later must agree with what was pushed.
    """

    def __init__(self, managed: ManagedSubscriptions) -> None:
        self._managed = managed

    async def changed(
        self,
        principal: Any,
        account_id: str,
        *,
        at: datetime,
        authorized: bool,
        allowed_tasks: Sequence[str] | None = None,
        timezone: str | None = None,
        operator: str | None = None,
    ) -> None:
        """Record that ``principal``'s grant on ``account_id`` changed at ``at``.

        ``authorized=False`` is a revocation: no access at all from ``at``.
        ``allowed_tasks`` is the new grant's task list (``None`` restricts no
        task); a grant without ``get_reporting_status`` ends coverage exactly
        like a revocation, and :func:`~adcp.server.wind_down_grant`'s tasks
        keep it. A re-grant inside ``status_retention_days`` continues the
        chain, so the ``authorized_at`` served on ``list_accounts`` does not
        move.
        """
        managed = self._managed
        ref = PrincipalRef.coerce(principal)
        await managed.engine.authorization_changed(
            ref,
            account_id,
            authorized=authorized,
            at=at,
            timezone=timezone,
            operator=operator,
            allowed_tasks=allowed_tasks,
            source="pushed",
        )
        if await managed._subscribed(ref):
            await managed.engine.refresh(ref)


class ManagedSubscriptions:
    """The SDK-owned composition behind ``ReliableReportingService(subscriptions=...)``.

    Built by the service; sellers configure it through
    :class:`SubscriptionOptions` and never construct it.
    """

    def __init__(
        self,
        options: SubscriptionOptions,
        *,
        clock: Callable[[], datetime],
        status_retention_days: int,
        report: Callable[[SubscriptionPlanningFailure], Awaitable[None]],
        principal_store: PrincipalRecordStore | None = None,
        state_store: ReportingSubscriptionStateStore | None = None,
    ) -> None:
        if not isinstance(options, SubscriptionOptions):
            raise TypeError("subscriptions must be SubscriptionOptions")
        self.options = options
        #: The service's storage supplies these when the options do not.
        self._principal_store = options.principal_store or principal_store
        self._state_store = options.state_store or state_store
        self._clock = clock
        self._retention_days = status_retention_days
        self._report = report
        self._platform = _Platform()
        self._engine: ReportingSubscriptionEngine | None = None
        self._principals: PrincipalService | None = options.principals
        self._service_options: ReportingSubscriptionServiceOptions | None = None
        #: Served-write triggers waiting for the drain: (account_id, caller).
        self._queued: dict[tuple[str | None, str | None], PrincipalRef | None] = {}
        self._drain: asyncio.Task[None] | None = None
        self.grants = SubscriptionGrants(self)

    # -- composition -----------------------------------------------------------

    @property
    def composed(self) -> bool:
        return self._service_options is not None

    @property
    def engine(self) -> ReportingSubscriptionEngine:
        if self._engine is None:
            raise ReliableReportingConfigurationError(
                "reporting subscriptions are composed at install() or startup"
            )
        return self._engine

    @property
    def principals(self) -> PrincipalService:
        if self._principals is None:
            raise ReliableReportingConfigurationError(
                "reporting subscriptions are composed at install() or startup"
            )
        return self._principals

    @property
    def service_options(self) -> ReportingSubscriptionServiceOptions:
        assert self._service_options is not None
        return self._service_options

    def compose(
        self, offerings: Sequence[Mapping[str, Any]]
    ) -> ReportingSubscriptionServiceOptions:
        """Build the engine and the principal runtime from the registered offerings."""
        if self._service_options is not None:
            return self._service_options
        from adcp.reporting.service_subscriptions import ReportingSubscriptionServiceOptions
        from adcp.reporting.subscriptions.capabilities import ReportingSubscriptionSupport
        from adcp.reporting.subscriptions.engine import ReportingSubscriptionEngine
        from adcp.server.principal import PrincipalService

        options = self.options
        mapped = self._offerings(offerings, options.standard_dimensions or {})
        dimensions = (
            tuple(options.dimensions)
            if options.dimensions is not None
            else tuple(sorted({item for offering in mapped for item in offering.dimensions}))
        )
        try:
            support = ReportingSubscriptionSupport(
                max_accounts=options.max_accounts,
                backfill_on_regrant=options.backfill_on_regrant,
                dimensions=dimensions,
                account_configuration=options.account_configuration,
            )
        except ValueError as error:
            raise ReliableReportingConfigurationError(str(error)) from None
        if self._principals is None:
            self._principals = PrincipalService(
                self._principal_store,
                emit_change=options.emit_change,
                accepted_experimental_features=("protocol.principal",),
            )
        try:
            engine = ReportingSubscriptionEngine(
                principals=self._principals,
                host=_ManagedHost(self),
                offerings=mapped,
                support=support,
                status_retention_days=self._retention_days,
                store=self._state_store,
                clock=self._clock,
                end_account_configs=options.end_account_configs,
            )
        except ValueError as error:
            raise ReliableReportingConfigurationError(str(error)) from None
        self._principals.set_reporting_subscriptions(engine.principal_options())
        self._engine = engine
        self._service_options = ReportingSubscriptionServiceOptions(
            engine=engine,
            list_principals=self.list_subscription_principals,
            resolve_principal=self.resolve,
            planning_error_handler=options.planning_error_handler,
            definitions=options.definitions,
            bind_managed_generation=options.bind_managed_generation,
        )
        return self._service_options

    @staticmethod
    def _offerings(
        offerings: Sequence[Mapping[str, Any]], declared: Mapping[str, Sequence[str]]
    ) -> list[SubscriptionOffering]:
        from adcp.reporting.subscriptions._clock import CoverageInputError
        from adcp.reporting.subscriptions.resolution import SubscriptionOffering

        if not offerings:
            raise ReliableReportingConfigurationError(
                "reporting subscriptions resolve against the service's registered offerings, and "
                "none is registered. Register each source with capability_offerings, publishing "
                "standard_dimensions on every offering.",
                code="SUBSCRIPTIONS_REQUIRE_STANDARD_DIMENSIONS",
                config_field="capability_offerings",
            )
        mapped: list[SubscriptionOffering] = []
        for offering in offerings:
            offering_id = str(offering.get("offering_id"))
            if "standard_dimensions" not in offering and offering_id in declared:
                offering = {**offering, "standard_dimensions": list(declared[offering_id])}
            if "standard_dimensions" not in offering:
                raise ReliableReportingConfigurationError(
                    f"offering {offering.get('offering_id')!r} has no standard_dimensions, so "
                    "a buyer cannot resolve a subscription selector against it. Publish "
                    "standard_dimensions (empty for totals only) in its capability_offerings "
                    "entry.",
                    code="SUBSCRIPTIONS_REQUIRE_STANDARD_DIMENSIONS",
                    config_field="capability_offerings",
                )
            profile = offering.get("reporting_profile")
            keys = profile.get("primary_keys") if isinstance(profile, Mapping) else None
            if offering.get("method") is not None and "account_id" not in (keys or ()):
                # adcp#8619: consolidated destinations validate and never
                # collide only when every delivered row carries its account.
                # Warned, not refused, while existing row contracts migrate.
                logger.warning(
                    "offering %s is delivered to destinations but its reporting_profile."
                    "primary_keys omit account_id; AdCP 3.3.0-beta.3 requires its row schema "
                    "to declare account_id as a required string and list it in primary_keys",
                    offering_id,
                    extra={"event": "reporting.subscription.offering_primary_keys"},
                )
            try:
                mapped.append(SubscriptionOffering.from_offering(offering))
            except (CoverageInputError, KeyError, ValueError) as error:
                raise ReliableReportingConfigurationError(
                    f"offering {offering.get('offering_id')!r} cannot serve subscriptions: {error}"
                ) from None
        return mapped

    def check_roster(self) -> None:
        """Refuse a composition with no way to enumerate a principal's accounts."""
        if self.options.accounts is not None:
            return
        target = self._platform.target
        if target is None:
            return
        if self._platform.decisioning:
            listing = getattr(getattr(target, "accounts", None), "list", None)
            available = callable(listing)
        else:
            from adcp.server import ADCPHandler

            method = getattr(type(target), "list_accounts", None)
            available = method is not None and method is not ADCPHandler.list_accounts
        if not available:
            raise ReliableReportingConfigurationError(
                "all_authorized_accounts subscriptions follow the accounts a principal may read, "
                "and this platform does not serve list_accounts. Implement list_accounts (a "
                "decisioning platform's accounts.list), or supply SubscriptionOptions.accounts.",
                code="SUBSCRIPTION_ROSTER_UNAVAILABLE",
                config_field="SubscriptionOptions.accounts",
            )
        if self.options.buys is None and not self._platform.decisioning:
            from adcp.server import ADCPHandler

            method = getattr(type(target), "get_media_buys", None)
            if method is None or method is ADCPHandler.get_media_buys:
                raise ReliableReportingConfigurationError(
                    "reporting subscriptions read each account's buys through get_media_buys, "
                    "which this platform does not serve. Implement get_media_buys, or supply "
                    "SubscriptionOptions.buys."
                )

    def bind_platform(self, platform: Any, *, decisioning: bool) -> None:
        self._platform.target = platform
        self._platform.decisioning = decisioning
        self.check_roster()

    def bind_handler(self, handler: Any) -> None:
        """Record the decisioning ``PlatformHandler`` that serves the platform."""
        self._platform.handler = handler

    # -- principals ------------------------------------------------------------

    async def list_subscription_principals(self) -> list[PrincipalRef]:
        """Every principal with subscription state, from the engine store's index."""
        return await self.engine.list_subscription_principals()

    async def resolve(self, request: Any, context: Any) -> PrincipalRef:
        """The authenticated principal of a request; never derived from request content."""
        tenant_id = getattr(context, "tenant_id", None)
        override = self.options.resolve_principal
        if override is not None:
            value = await _resolve(override(request, context))
            return PrincipalRef.coerce(value, tenant_id=tenant_id)
        subject = request_principal_id(context)
        if not subject:
            raise ReliableReportingConfigurationError(
                "reporting subscriptions need an authenticated caller; supply "
                "SubscriptionOptions.resolve_principal for another trust model",
                kind="auth_required",
            )
        return PrincipalRef(
            tenant_id if isinstance(tenant_id, str) and tenant_id else None, subject
        )

    def _identity(self, principal: PrincipalRef) -> Any:
        from adcp.server.principal import PrincipalIdentity

        return PrincipalIdentity(principal.principal_id, self.options.principal_kind)

    async def _remember(self, principal: PrincipalRef, context: Any) -> None:
        binding: dict[str, Any] = {"tenant_id": principal.tenant_id}
        snapshot = _auth_snapshot(context)
        if snapshot is not None:
            binding["auth"] = snapshot
        await self.engine.record_binding(principal, binding)

    def _service_context(self, principal: PrincipalRef, binding: Mapping[str, Any] | None) -> Any:
        """A request context for reads made on the principal's behalf outside a request."""
        from adcp.server.base import ToolContext

        metadata: dict[str, Any] = {"adcp.reporting.service_context": True}
        auth = _restore_auth((binding or {}).get("auth"))
        if auth is not None:
            metadata[_AUTH_INFO_KEY] = auth
        return ToolContext(
            caller_identity=principal.principal_id,
            tenant_id=principal.tenant_id,
            metadata=metadata,
        )

    # -- served principal tasks ------------------------------------------------

    async def get_principal(self, params: Any, context: Any) -> Any:
        principal = await self.resolve(params, context)
        return await self.principals.get_principal(
            self._identity(principal), params, context=context
        )

    async def sync_principal(self, params: Any, context: Any) -> Any:
        principal = await self.resolve(params, context)
        await self._remember(principal, context)
        response = await self.principals.sync_principal(
            self._identity(principal), params, context=context
        )
        result = getattr(response, "result", None)
        if getattr(result, "kind", None) == "applied" and (
            str(getattr(result, "action", "")) != "unchanged"
        ):
            self._enqueue(None, principal)
        return response

    # -- scheduling ------------------------------------------------------------

    async def _subscribed(self, principal: PrincipalRef) -> bool:
        return bool(await self.principals.reporting_subscriptions(principal))

    async def reconcile(self, principal: PrincipalRef, *, now: datetime | None = None) -> bool:
        """Reconcile one principal, isolating and reporting any failure."""
        from adcp.reporting.service_subscriptions import SubscriptionPlanningFailure

        try:
            if not await self._subscribed(principal):
                return True
            await self.engine.reconcile(principal, now=now)
        except Exception as error:  # noqa: BLE001 - a trigger never fails the request
            await self._report(SubscriptionPlanningFailure(principal, error))
            return False
        return True

    async def touch(self, *, account_id: str | None, context: Any | None = None) -> None:
        """Reconcile now the principals a change on ``account_id`` concerns.

        The caller ``context`` authenticates, when it holds subscriptions, and
        every principal whose engine state includes the account. Awaited, for
        hosts that want immediacy after a change the SDK did not serve.
        """
        if not self.composed:
            return
        caller = await self._caller(context)
        await self._reconcile_concerned(account_id, caller)

    async def _caller(self, context: Any | None) -> PrincipalRef | None:
        if context is None:
            return None
        try:
            return await self.resolve(None, context)
        except ReliableReportingConfigurationError:
            return None

    async def trigger(self, *, account_id: str | None, context: Any | None = None) -> None:
        """Queue a reconcile after a served write; returns without waiting for it."""
        if not self.options.reconcile_on_write or not self.composed:
            return
        self._enqueue(account_id, await self._caller(context))

    def _enqueue(self, account_id: str | None, caller: PrincipalRef | None) -> None:
        if not self.options.reconcile_on_write:
            return
        self._queued[(account_id, None if caller is None else caller.subject)] = caller
        if self._drain is None or self._drain.done():
            self._drain = asyncio.get_running_loop().create_task(
                self._run_drain(), name="adcp-reporting-subscription-triggers"
            )

    async def _run_drain(self) -> None:
        while self._queued:
            (account_id, _subject), caller = next(iter(self._queued.items()))
            del self._queued[(account_id, _subject)]
            try:
                await self._reconcile_concerned(account_id, caller)
            except Exception:  # noqa: BLE001 - the drain never dies on one trigger
                logger.exception("Reliable Reporting subscription trigger failed")

    async def drain(self) -> None:
        """Wait until every queued served-write trigger has reconciled."""
        while self._drain is not None and not self._drain.done():
            await asyncio.shield(self._drain)

    async def aclose(self) -> None:
        """Stop the trigger drain; queued work is left to the boundary sweep."""
        self._queued.clear()
        task, self._drain = self._drain, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _reconcile_concerned(
        self, account_id: str | None, caller: PrincipalRef | None
    ) -> None:
        concerned: dict[str, PrincipalRef] = {}
        if caller is not None:
            concerned[caller.subject] = caller
        if account_id is not None:
            try:
                principals = await self.list_subscription_principals()
            except Exception as error:  # noqa: BLE001 - reported, never raised to the buyer
                logger.warning("reporting subscription principals could not be listed: %s", error)
                principals = []
            for principal in principals:
                if account_id in await self.engine.account_ids(principal):
                    concerned.setdefault(principal.subject, principal)
        for principal in concerned.values():
            await self.reconcile(principal)

    async def sweep(self, *, now: datetime) -> list[SubscriptionPlanningFailure]:
        """The worker's boundary sweep: reconcile each principal a boundary passed for."""
        from adcp.reporting.service_subscriptions import SubscriptionPlanningFailure

        failures: list[SubscriptionPlanningFailure] = []
        for principal in await self.list_subscription_principals():
            try:
                if not await self._subscribed(principal):
                    continue
                if not await self.engine.boundary_due(principal, now=now):
                    continue
                await self.engine.reconcile(principal, now=now)
            except Exception as error:  # noqa: BLE001 - isolate one principal
                failures.append(SubscriptionPlanningFailure(principal, error))
        return failures

    # -- derived roster and buys -----------------------------------------------

    async def _call(self, tool: str, request: Any, principal: PrincipalRef) -> dict[str, Any]:
        binding = await self.engine.binding(principal)
        context = self._service_context(principal, binding)
        platform = self._platform
        if platform.decisioning:
            if platform.handler is None:
                raise ReliableReportingConfigurationError(
                    "reporting subscriptions read the platform through the server that serves "
                    "it; pass the installed platform to adcp.decisioning.serve before reconciling"
                )
            target = platform.handler
        elif platform.target is not None:
            target = platform.target
        else:
            raise ReliableReportingConfigurationError(
                "reporting subscriptions derive accounts and buys from the installed platform; "
                "call service.install(platform) first"
            )
        timeout = self.options.read_timeout
        try:
            response = _wire(
                await asyncio.wait_for(
                    _resolve(getattr(target, tool)(request, context)),
                    None if timeout is None else timeout.total_seconds(),
                )
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"{tool} did not answer within SubscriptionOptions.read_timeout"
            ) from None
        if not isinstance(response, Mapping):
            raise TypeError(f"{tool} returned {type(response).__name__}")
        if response.get("supported") is False or response.get("errors"):
            errors = response.get("errors") or [response.get("error") or {}]
            codes = sorted({str((item or {}).get("code", "UNKNOWN")) for item in errors})
            raise RuntimeError(f"{tool} answered {', '.join(codes)}")
        return dict(response)

    async def _platform_accounts(
        self, principal: PrincipalRef, cursor: str | None
    ) -> AuthorizedAccountPage:
        from adcp.types import ListAccountsRequest

        request = ListAccountsRequest.model_validate(
            {"pagination": {"cursor": cursor}} if cursor else {}
        )
        try:
            response = await self._call("list_accounts", request, principal)
        except ReliableReportingConfigurationError:
            raise
        except Exception as error:  # noqa: BLE001 - becomes a coded configuration error
            raise _roster_unavailable(principal, type(error).__name__) from error
        now = self._clock()
        accounts: list[AuthorizedAccount] = []
        for raw in response.get("accounts") or ():
            account = dict(raw)
            account_id = account.get("account_id")
            if not account_id:
                continue
            zone = account.get("timezone")
            if not zone:
                raise ReliableReportingConfigurationError(
                    f"account {account_id!r} has no IANA timezone, and coverage never "
                    "substitutes one. Serve timezone on every account list_accounts returns.",
                    code="SUBSCRIPTION_ROSTER_UNAVAILABLE",
                    config_field="SubscriptionOptions.accounts",
                )
            authorization = account.get("authorization")
            tasks = None
            authorized_at = now
            regranted_at = None
            if isinstance(authorization, Mapping):
                if authorization.get("allowed_tasks") is not None:
                    tasks = frozenset(str(item) for item in authorization["allowed_tasks"])
                authorized_at = _instant(authorization.get("authorized_at")) or now
                regranted_at = _instant(authorization.get("regranted_at"))
            accounts.append(
                AuthorizedAccount(
                    account_id=str(account_id),
                    timezone=str(zone),
                    authorized_at=authorized_at,
                    operator=account.get("operator"),
                    allowed_tasks=tasks,
                    regranted_at=regranted_at,
                )
            )
        pagination = response.get("pagination") or {}
        next_cursor = pagination.get("cursor") if pagination.get("has_more") else None
        return AuthorizedAccountPage(tuple(accounts), next_cursor=next_cursor)

    async def _platform_buys(
        self, principal: PrincipalRef, account_id: str
    ) -> tuple[SubscriptionBuy, ...]:
        from adcp.types import GetMediaBuysRequest

        buys: list[SubscriptionBuy] = []
        cursor: str | None = None
        for _page in range(_MAX_PAGES):
            payload: dict[str, Any] = {
                "account": {"account_id": account_id},
                "status_filter": list(_ALL_BUY_STATUSES),
            }
            if cursor:
                payload["pagination"] = {"cursor": cursor}
            response = await self._call(
                "get_media_buys", GetMediaBuysRequest.model_validate(payload), principal
            )
            for raw in response.get("media_buys") or ():
                buy = self._buy(dict(raw))
                if buy is not None:
                    buys.append(buy)
            pagination = response.get("pagination") or {}
            cursor = pagination.get("cursor") if pagination.get("has_more") else None
            if not cursor:
                return tuple(buys)
        raise RuntimeError(f"get_media_buys for account {account_id!r} did not finish paging")

    def _buy(self, raw: Mapping[str, Any]) -> SubscriptionBuy | None:
        """One ``get_media_buys`` buy as coverage reads it (adcp#8617, #8618).

        The flight, ``cancellation_effective_at`` and ``flight_history`` come
        from the wire. Each package's offering set is ``buy_offerings`` when
        given, else the package's ``reporting_delivery_offering_ids`` snapshot
        (fixed at confirmation; an empty one means no offering reports the
        package), else, only when the seller omits the snapshot, every
        registered offering.
        """
        if raw.get("start_time") is None or raw.get("end_time") is None:
            return None  # not yet flighted: nothing can be owed for it
        every = tuple(item.offering_id for item in self.engine.offerings)
        media_buy = dict(raw)
        offerings = self.options.buy_offerings
        if offerings is not None:
            media_buy["packages"] = [
                {"package_id": str(key), "reporting_delivery_offering_ids": list(value)}
                for key, value in sorted(offerings(raw).items())
            ]
        else:
            media_buy["packages"] = [
                {
                    **package,
                    "reporting_delivery_offering_ids": _package_offering_ids(package, every),
                }
                for package in raw.get("packages") or ()
                if package.get("package_id")
            ]
        buy = SubscriptionBuy.from_media_buy(media_buy)
        if not buy.packages:
            buy = replace(buy, reporting_delivery_offering_ids=every)
        return buy

    # -- authorized_at projection ----------------------------------------------

    async def project_accounts(self, response: Any, context: Any) -> Any:
        """Serve the journal's ``authorized_at`` on a ``list_accounts``/``sync_accounts`` response.

        Each account shown with an authorization is first observed by the
        journal (a new grant, a re-grant, a reduction, or a regressed
        ``authorized_at``, which is rejected), so grant checks read what the
        caller was just shown. The served ``authorized_at`` is the value
        coverage uses. Only callers holding subscriptions are touched, and a
        failure here never fails the response.
        """
        if not self.composed:
            return response
        metadata = getattr(context, "metadata", None)
        if isinstance(metadata, Mapping) and metadata.get("adcp.reporting.service_context"):
            return response
        payload = _wire(response)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("accounts"), list):
            return response
        try:
            principal = await self.resolve(None, context)
            if not await self._subscribed(principal):
                return response
            accounts = await self._project_page(principal, list(payload["accounts"]))
        except Exception:  # noqa: BLE001 - the projection never fails the read
            logger.exception("reporting subscription authorized_at projection failed")
            return response
        return {**payload, "accounts": accounts}

    async def _project_page(self, principal: PrincipalRef, page: list[Any]) -> list[Any]:
        """Observe every shown grant in one journal write, then serve its ``authorized_at``."""
        from adcp.reporting.subscriptions._clock import iso_z
        from adcp.reporting.subscriptions.engine import GrantObservation

        observations: list[GrantObservation] = []
        for raw in page:
            authorization = raw.get("authorization") if isinstance(raw, Mapping) else None
            if (
                isinstance(raw, Mapping)
                and raw.get("account_id")
                and isinstance(authorization, Mapping)
            ):
                tasks = authorization.get("allowed_tasks")
                observations.append(
                    GrantObservation(
                        str(raw["account_id"]),
                        # An authorization without allowed_tasks restricts no
                        # task, as the reconcile reads it.
                        None if tasks is None else [str(item) for item in tasks],
                        _instant(authorization.get("authorized_at")),
                        raw.get("timezone") or None,
                        raw.get("operator"),
                        _instant(authorization.get("regranted_at")),
                    )
                )
        regrants: dict[str, datetime | None] = {}
        served = await self.engine.observe_grants(principal, observations, regrants=regrants)
        projected: list[Any] = []
        for raw in page:
            account_id = str(raw.get("account_id")) if isinstance(raw, Mapping) else ""
            start = served.get(account_id)
            if start is None:
                projected.append(raw)
                continue
            authorization = dict(raw["authorization"])
            authorization["authorized_at"] = iso_z(start)
            regranted = regrants.get(account_id)
            if regranted is not None:
                # adcp#8618: the latest re-grant within the chain, so a buyer
                # computes withheld deadlines and gap ends from the wire.
                authorization["regranted_at"] = iso_z(regranted)
            else:
                authorization.pop("regranted_at", None)
            projected.append({**raw, "authorization": authorization})
        return projected

    # -- capabilities ----------------------------------------------------------

    @staticmethod
    def advertise_principal(payload: dict[str, Any]) -> dict[str, Any]:
        """Advertise the principal layer the service serves, merged with the seller's."""
        adcp = dict(payload.get("adcp") or {})
        principal = dict(adcp.get("principal") or {})
        principal.setdefault("supported", True)
        principal.setdefault("sync_task", "sync_principal")
        principal.setdefault("read_task", "get_principal")
        sections = [str(item) for item in principal.get("supported_sections") or ()]
        for section in ("reporting_destinations", "reporting_subscriptions"):
            if section not in sections:
                sections.append(section)
        principal["supported_sections"] = sections
        adcp["principal"] = principal
        payload = {**payload, "adcp": adcp}
        experimental = [str(item) for item in payload.get("experimental_features") or ()]
        if "protocol.principal" not in experimental:
            experimental.append("protocol.principal")
        payload["experimental_features"] = experimental
        return payload


__all__ = [
    "SubscriptionGrants",
    "AccountConfigsOverride",
    "AccountsOverride",
    "BuyOfferingsOverride",
    "BuysOverride",
    "ManagedPrincipalResolver",
    "ManagedSubscriptions",
    "PeriodDeliveredOverride",
    "SubscriptionOptions",
    "request_principal_id",
]
