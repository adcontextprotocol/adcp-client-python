"""The reporting-subscription test kit and the reference seller (AdCP 3.3, experimental).

``reporting_test_kit(...)`` runs a seller in process under a virtual clock and
hands you real buyer clients bound to it::

    from adcp.testing.reporting import offerings, reporting_test_kit

    async with reporting_test_kit(
        start="2026-11-01T00:00:00-04:00",
        offerings=[offerings.daily_analytics(alignment="account_timezone")],
        accounts=[{"account_id": "acc_a", "timezone": "America/New_York"}],
        buys=[{"media_buy_id": "mb_1", "account_id": "acc_a",
               "confirmed_at": "2026-11-03T15:00:00Z",
               "start_time": "2026-11-03T05:00:00Z", "end_time": "2026-11-30T05:00:00Z"}],
    ) as kit:
        buyer = kit.buyer("agency-x")
        await buyer.reporting.subscribe(analytics_daily)
        await kit.clock.advance("P3D")          # runs the worker at each boundary crossed
        await kit.grants.revoke("agency-x", "acc_a")
        await kit.assert_agreement("agency-x")  # seller owed set == buyer computed set

Without ``platform=``, the kit runs :class:`ReferenceReportingSeller`, the
SDK's reference seller on the managed path. Pass your own installed
``platform``, its ``service`` and a ``controller`` (a
:class:`~adcp.server.test_controller.TestControllerStore` serving
``reporting_subscription_probe`` and ``seed_media_buy``) to test your wiring:
the kit drives grants, timezones, buys and the clock only through that
controller, the same scenario a remote storyboard uses.

:func:`create_reference_reporting_seller` builds the reference seller alone,
for a storyboard or a standalone server (``python -m adcp reference-seller
reporting``).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from adcp.reporting.subscriptions._clock import iso_z, parse_delivery_sla, utc_instant
from adcp.server import ADCPHandler
from adcp.server.base import AccountAwareToolContext, ToolContext
from adcp.server.test_controller import TestControllerError, TestControllerStore

#: The principal the reference seller's standalone server assumes without a token.
DEFAULT_PRINCIPAL = "storyboard-buyer"
#: The sandbox account every principal is granted on the reference seller.
REFERENCE_ACCOUNT = "acct-reference"
#: Every task a fresh grant allows on the reference seller.
GRANTED_TASKS = (
    "get_media_buys",
    "get_reporting_status",
    "sync_reporting_receipts",
    "sync_reporting_status",
    "list_accounts",
    "sync_principal",
    "get_principal",
)
#: Boundaries one ``advance`` may process before it refuses.
MAX_STEPS = 20_000


def _instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return utc_instant(value)
    from adcp.reporting._timestamp import aware_timestamp

    return utc_instant(aware_timestamp(str(value)))


# -- offerings -------------------------------------------------------------------------


#: The report contract the reference source declares (``adcp.reporting.fixtures``).
_DEFINITION_ID = "PAID_MEDIA_DAILY_V1"
_DEFINITION_URI = "https://contracts.example.test/reporting/paid-media-daily-v1"


def _offering(
    offering_id: str,
    *,
    feed_purpose: str,
    period_duration: str,
    alignment: str,
    delivery_sla: str,
    dimensions: Sequence[str],
    finality: Sequence[str],
) -> dict[str, Any]:
    return {
        "offering_id": offering_id,
        "feed_purpose": feed_purpose,
        "report_definition_id": _DEFINITION_ID,
        "report_definition_uri": _DEFINITION_URI,
        "report_definition_sha256": "a" * 64,
        "reporting_profile": {
            "id": "paid_media_delivery",
            "version": "1.0.0",
            "schema_uri": f"{_DEFINITION_URI}/schema",
            "schema_sha256": "b" * 64,
            "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
            "schema_ref_policy": "local_fragment_only",
            "grain": "one row per media buy per period",
            "primary_keys": ["media_buy_id"],
        },
        "schedule": {
            "period_duration": period_duration,
            "delivery_sla": delivery_sla,
            "alignment": alignment,
        },
        "supported_finality": list(finality),
        "reconciliation_mode": "delivery_only",
        "standard_dimensions": list(dimensions),
    }


class _Offerings:
    """Offering fixtures for the kit and the reference seller."""

    @staticmethod
    def daily_analytics(
        *,
        offering_id: str = "REFERENCE_DAILY_V1",
        alignment: str = "utc",
        dimensions: Sequence[str] = ("package",),
        delivery_sla: str = "PT6H",
        finality: Sequence[str] = ("snapshot",),
    ) -> dict[str, Any]:
        """A ``P1D`` analytics offering."""
        return _offering(
            offering_id,
            feed_purpose="analytics",
            period_duration="P1D",
            alignment=alignment,
            delivery_sla=delivery_sla,
            dimensions=dimensions,
            finality=finality,
        )

    @staticmethod
    def hourly_pacing(
        *,
        offering_id: str = "REFERENCE_HOURLY_V1",
        dimensions: Sequence[str] = ("package",),
        delivery_sla: str = "PT10M",
        finality: Sequence[str] = ("snapshot",),
    ) -> dict[str, Any]:
        """A ``PT1H`` UTC pacing offering."""
        return _offering(
            offering_id,
            feed_purpose="pacing",
            period_duration="PT1H",
            alignment="utc",
            delivery_sla=delivery_sla,
            dimensions=dimensions,
            finality=finality,
        )


offerings = _Offerings()


# -- the world -------------------------------------------------------------------------


@dataclass
class _Grant:
    active: bool = True
    allowed_tasks: tuple[str, ...] = GRANTED_TASKS


@dataclass
class ReportingWorld:
    """The reference seller's state: accounts, grants and buys, all in memory."""

    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    buys: dict[str, dict[str, Any]] = field(default_factory=dict)
    grants: dict[tuple[str, str], _Grant] = field(default_factory=dict)
    #: Accounts no principal may read: the others are granted on first sight.
    restricted: set[str] = field(default_factory=set)

    def grant(self, principal: str, account_id: str) -> _Grant:
        key = (principal, account_id)
        if key not in self.grants:
            self.grants[key] = _Grant(active=account_id not in self.restricted)
        return self.grants[key]

    def readable(self, principal: str, account_id: str) -> bool:
        return account_id in self.accounts and self.grant(principal, account_id).active


def _principal(context: Any) -> str:
    identity = getattr(context, "caller_identity", None)
    return str(identity) if identity else DEFAULT_PRINCIPAL


class ReferenceReportingSeller(ADCPHandler[ToolContext]):
    """The tasks a reporting-subscriptions seller already serves, over :class:`ReportingWorld`.

    ``list_accounts`` lists the accounts the caller holds a grant on, and
    ``get_media_buys`` the buys on an account it can read. The Reliable
    Reporting service installed on it serves everything else.
    """

    def __init__(self, world: ReportingWorld) -> None:
        self.world = world

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def list_accounts(self, params: Any, context: Any = None) -> Any:
        principal = _principal(context)
        accounts = []
        for account_id, account in sorted(self.world.accounts.items()):
            if not self.world.readable(principal, account_id):
                continue
            grant = self.world.grant(principal, account_id)
            accounts.append(
                {
                    **copy.deepcopy(account),
                    "authorization": {"allowed_tasks": list(grant.allowed_tasks)},
                }
            )
        return {"accounts": accounts}

    async def get_media_buys(self, params: Any, context: Any = None) -> Any:
        principal = _principal(context)
        payload = (
            params.model_dump(mode="json", exclude_none=True)
            if hasattr(params, "model_dump")
            else dict(params or {})
        )
        account_id = (payload.get("account") or {}).get("account_id")
        wanted = set(payload.get("media_buy_ids") or ())
        buys = [
            copy.deepcopy(buy)
            for buy in self.world.buys.values()
            if self.world.readable(principal, buy["account_id"])
            and (account_id is None or buy["account_id"] == account_id)
            and (not wanted or buy["media_buy_id"] in wanted)
        ]
        buys.sort(key=lambda buy: buy["media_buy_id"])
        return {"media_buys": buys}


class _Source:
    """Answers every slice with one row per requested media buy."""

    def __init__(self, capabilities: Any) -> None:
        self._capabilities = capabilities

    @property
    def capabilities(self) -> Any:
        return self._capabilities

    def fetch_slice(self, request: Any) -> list[dict[str, Any]]:
        return [
            {
                "media_buy_id": item.media_buy_id,
                "campaign_id": "campaign-reference",
                "impressions": 100,
                "spend": "1.00",
            }
            for item in request.coverage.constituents
        ]


@dataclass
class ReferenceSeller:
    """The reference seller: its world, service, installed platform, clock and controller."""

    world: ReportingWorld
    service: Any
    platform: Any
    clock: Any
    controller: ReportingTestController
    offerings: tuple[dict[str, Any], ...]

    def context(self, principal: str = DEFAULT_PRINCIPAL, account_id: str | None = None) -> Any:
        return AccountAwareToolContext(caller_identity=principal, account_id=account_id)

    def mcp_server(
        self, *, principal: str = DEFAULT_PRINCIPAL, name: str = "adcp-reference-seller"
    ) -> Any:
        """An in-process MCP server, with the caller fixed to ``principal``."""
        from adcp.server.serve import create_mcp_server
        from adcp.server.test_controller import register_test_controller

        def context_factory(_meta: Any) -> ToolContext:
            return ToolContext(caller_identity=principal)

        server = create_mcp_server(self.platform, name=name, context_factory=context_factory)
        register_test_controller(server, self.controller, context_factory=context_factory)
        return server


def create_reference_reporting_seller(
    *,
    start: Any = "2026-11-01T00:00:00Z",
    offerings: Sequence[Mapping[str, Any]] | None = None,
    accounts: Sequence[Mapping[str, Any]] | None = None,
    buys: Sequence[Mapping[str, Any]] = (),
    max_accounts: int = 100,
    backfill_on_regrant: bool = False,
    storage: Any = None,
) -> ReferenceSeller:
    """The SDK's reference seller for reporting subscriptions, on the managed path.

    Every principal is granted on every account (``acct-reference`` by
    default) the first time it is seen, unless the account is ``restricted``.
    ``storage`` defaults to process-local memory storage.
    """
    from adcp.reporting import SubscriptionOptions
    from adcp.reporting.fixtures import SNAPSHOT_OFFERING_ID, redacted_capabilities
    from adcp.reporting.ledger import ReportingDefinitionBinding
    from adcp.reporting.service import ReliableReportingService, ReportingAccountContext
    from adcp.reporting.testing import DeterministicReportingClock

    clock = DeterministicReportingClock(_instant(start))
    world = ReportingWorld()
    for account in accounts or (
        {
            "account_id": REFERENCE_ACCOUNT,
            "name": "Reference account",
            "status": "active",
            "timezone": "UTC",
            "operator": "agency.example",
            "sandbox": True,
        },
    ):
        record = {"name": account["account_id"], "status": "active", "timezone": "UTC", **account}
        world.accounts[str(record["account_id"])] = record
    for buy in buys:
        _add_buy(world, buy)
    advertised = tuple(dict(item) for item in (offerings or (_Offerings.daily_analytics(),)))
    definitions = {
        item["offering_id"]: ReportingDefinitionBinding(
            report_definition_uri=item["report_definition_uri"],
            report_definition_sha256=item["report_definition_sha256"],
            schema_version=item["reporting_profile"]["version"],
            schema_uri=item["reporting_profile"]["schema_uri"],
            schema_sha256=item["reporting_profile"]["schema_sha256"],
        )
        for item in advertised
    }
    capabilities = redacted_capabilities()
    by_id = {item["offering_id"]: item for item in advertised}

    def account_context(configuration: Any) -> Any:
        offering = by_id.get(getattr(configuration, "offering_id", None) or "") or advertised[0]
        account = world.accounts.get(configuration.account_id) or {}
        return ReportingAccountContext(
            account_id=configuration.account_id,
            adapter="reference",
            currency="USD",
            account_timezone=account.get("timezone", "UTC"),
            source_scope=capabilities.source_scope,
            snapshot_offering_id=SNAPSHOT_OFFERING_ID,
            requested_dimensions=("campaign_id",),
            capability_offering=offering,
            publication_namespace="reporting-source:reference",
        )

    def caller_resolver(request: Any, context: Any) -> Any:
        """The caller is the authenticated principal; the account is the one the request names."""
        from adcp.reporting.ledger import ReportingStatusCaller

        payload = (
            request.model_dump(mode="json", exclude_none=True)
            if hasattr(request, "model_dump")
            else dict(request or {})
        )
        account_id = (payload.get("account") or {}).get("account_id") or getattr(
            context, "account_id", None
        )
        if not account_id:
            raise TestControllerError("INVALID_PARAMS", "the request names no account")
        return ReportingStatusCaller(account_id=str(account_id), consumer_id=_principal(context))

    options = SubscriptionOptions(
        max_accounts=max_accounts,
        backfill_on_regrant=backfill_on_regrant,
        definitions=definitions,
    )
    if storage is None:
        service = ReliableReportingService.memory(
            account_context=account_context,
            caller_resolver=caller_resolver,
            clock=clock,
            read_jitter_window=timedelta(0),
            subscriptions=options,
        )
    else:
        service = ReliableReportingService(
            storage=storage,
            account_context=account_context,
            caller_resolver=caller_resolver,
            clock=clock,
            read_jitter_window=timedelta(0),
            subscriptions=options,
        )
    service.sources.register(
        "reference", _Source(capabilities), capability_offerings=list(advertised)
    )
    platform = service.install(ReferenceReportingSeller(world))
    seller = ReferenceSeller(world, service, platform, clock, None, advertised)  # type: ignore[arg-type]
    seller.controller = ReportingTestController(seller)
    return seller


def _add_buy(world: ReportingWorld, buy: Mapping[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {"status": "active", "currency": "USD", "total_budget": 1000.0, **buy}
    if "account_id" not in record:
        raise TestControllerError("INVALID_PARAMS", "a media buy needs account_id")
    if record["account_id"] not in world.accounts:
        raise TestControllerError("NOT_FOUND", f"unknown account {record['account_id']!r}")
    record.setdefault(
        "packages", [{"package_id": f"{record['media_buy_id']}-pkg", "product_id": "reference"}]
    )
    world.buys[str(record["media_buy_id"])] = record
    return record


# -- the controller --------------------------------------------------------------------


class ReportingTestController(TestControllerStore):
    """``seed_media_buy`` and ``reporting_subscription_probe`` for the reference seller.

    Every probe acts on the authenticated caller's own grant on a sandbox
    account, then processes the change as production would (a reconcile and,
    for ``advance_time``, a worker turn at every period boundary crossed).
    """

    def __init__(self, seller: ReferenceSeller) -> None:
        self._seller = seller
        self._lock = asyncio.Lock()

    async def seed_media_buy(
        self,
        fixture: dict[str, Any] | None = None,
        media_buy_id: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        payload = dict(fixture or {})
        if media_buy_id is not None:
            payload["media_buy_id"] = media_buy_id
        if "media_buy_id" not in payload:
            raise TestControllerError("INVALID_PARAMS", "seed_media_buy needs media_buy_id")
        record = _add_buy(self._seller.world, payload)
        await self._touch(record["account_id"], context)
        return {"media_buy_id": record["media_buy_id"]}

    async def reporting_subscription_probe(
        self,
        operation: str,
        account_id: str | None = None,
        allowed_tasks: list[str] | None = None,
        timezone: str | None = None,
        target_time: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        seller = self._seller
        principal = _principal(context)
        result: dict[str, Any] = {}
        async with self._lock:
            if operation == "advance_time":
                assert target_time is not None
                await self._advance_to(_instant(target_time))
            else:
                assert account_id is not None
                if account_id not in seller.world.accounts:
                    raise TestControllerError("NOT_FOUND", f"unknown account {account_id!r}")
                grant = seller.world.grant(principal, account_id)
                if operation == "revoke_grant":
                    grant.active = False
                    await self._touch(account_id, context, principal=principal)
                elif operation == "regrant":
                    grant.active = True
                    grant.allowed_tasks = GRANTED_TASKS
                    await self._touch(account_id, context, principal=principal)
                    result.update(await self._authorization(principal, account_id))
                elif operation == "reduce_grant":
                    grant.allowed_tasks = tuple(allowed_tasks or ())
                    await self._touch(account_id, context, principal=principal)
                elif operation == "set_account_timezone":
                    assert timezone is not None
                    account = seller.world.accounts[account_id]
                    result["previous_timezone"] = account.get("timezone")
                    account["timezone"] = timezone
                    await self._touch(account_id, context, principal=principal)
                    result["cutovers"] = await self._cutovers(principal, account_id)
                else:  # pragma: no cover - the dispatcher validated it
                    raise TestControllerError("INVALID_PARAMS", f"unknown operation {operation}")
        return {"success": True, "simulated_now": iso_z(seller.clock()), **result}

    async def _touch(self, account_id: str, context: Any, *, principal: str | None = None) -> None:
        service = self._seller.service
        ctx = self._seller.context(principal or _principal(context), account_id)
        await service.touch(account_id=account_id, context=ctx)
        await service.drain_subscription_triggers()

    async def _authorization(self, principal: str, account_id: str) -> dict[str, Any]:
        engine = self._seller.service._subscriptions.engine
        from adcp._principal_ref import PrincipalRef

        ref = PrincipalRef(None, principal)
        state = await engine._read(ref.subject)
        account = state.accounts.get(account_id)
        if account is None or not account.grants:
            return {}
        from adcp.reporting.subscriptions.coverage import (
            authorization_chain_start,
            authorization_regranted_at,
        )

        days = engine.status_retention_days
        start = authorization_chain_start(account.grants, status_retention_days=days)
        regranted = authorization_regranted_at(account.grants, status_retention_days=days)
        chain = (
            "continued" if regranted is not None else ("new" if len(account.grants) > 1 else "new")
        )
        result: dict[str, Any] = {"chain": chain}
        if start is not None:
            result["authorized_at"] = iso_z(start)
        if regranted is not None:
            result["regranted_at"] = iso_z(regranted)
        return result

    async def _cutovers(self, principal: str, account_id: str) -> list[dict[str, Any]]:
        from adcp._principal_ref import PrincipalRef

        engine = self._seller.service._subscriptions.engine
        result = []
        for entry in await engine.subscription_coverage(PrincipalRef(None, principal), account_id):
            for item in entry.timezone_cutovers or ():
                result.append(item.model_dump(mode="json", exclude_none=True))
        return result

    async def _advance_to(self, target: datetime) -> None:
        seller = self._seller
        now = seller.clock()
        if target < now:
            raise TestControllerError(
                "INVALID_PARAMS", "the sandbox clock moves forward only; target_time is earlier"
            )
        steps = 0
        for boundary in _boundaries(seller, now, target):
            steps += 1
            if steps > MAX_STEPS:
                raise TestControllerError(
                    "INVALID_PARAMS", f"advance_time would cross more than {MAX_STEPS} boundaries"
                )
            seller.clock.set(boundary)
            await seller.service.run_worker()
            await seller.service.drain_subscription_triggers()
        seller.clock.set(target)
        await seller.service.run_worker()
        await seller.service.drain_subscription_triggers()


def _boundaries(seller: ReferenceSeller, after: datetime, through: datetime) -> list[datetime]:
    """Every period boundary of every offering in ``(after, through]``."""
    from adcp.reporting.subscriptions.resolution import SubscriptionOffering

    zones = {str(account.get("timezone") or "UTC") for account in seller.world.accounts.values()}
    points: set[datetime] = set()
    for raw in seller.offerings:
        offering = SubscriptionOffering.from_offering(raw)
        for zone in zones:
            try:
                calendar = offering.calendar(zone)
            except Exception:  # noqa: BLE001 - a zone the offering cannot use
                continue
            for start, end in calendar.periods(
                starts_from=calendar.floor(after), starts_before=through
            ):
                for point in (start, end):
                    if after < point <= through:
                        points.add(point)
    return sorted(points)


# -- the kit ---------------------------------------------------------------------------


class KitBuyer:
    """A buyer bound to the kit's seller: ``.reporting`` plus the raw tasks it reads."""

    def __init__(self, platform: Any, principal: str, service: Any = None) -> None:
        from adcp.reporting.subscriptions.facade import ReportingClient

        self.principal = principal
        self._platform = platform
        self._service = service
        self.reporting = ReportingClient(self)

    def _context(self, account_id: str | None = None) -> Any:
        return AccountAwareToolContext(caller_identity=self.principal, account_id=account_id)

    async def _call(self, task: str, request: Any, model: Any) -> Any:
        from adcp.types.core import TaskResult, TaskStatus

        params = request.model_dump(mode="json", exclude_none=True)
        account_id = (params.get("account") or {}).get("account_id")
        value = await getattr(self._platform, task)(params, self._context(account_id))
        if self._service is not None:
            # A served write queues a reconcile; let it land before the next read.
            await self._service.drain_subscription_triggers()
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", exclude_none=True, by_alias=True)
        if task == "get_media_buys":
            # Fixture buys carry only what coverage reads; the buyer reads them
            # as plain data, so they need not be complete wire buys.
            try:
                data = model.model_validate(value)
            except ValueError:
                data = value
        else:
            # Every response reconcile() and assert_agreement rely on must be
            # valid: a malformed one fails here instead of being compared.
            data = model.model_validate(value)
        return TaskResult.model_construct(status=TaskStatus.COMPLETED, data=data, success=True)

    async def get_principal(self, request: Any) -> Any:
        from adcp.types import GetPrincipalResponse

        return await self._call("get_principal", request, GetPrincipalResponse)

    async def sync_principal(self, request: Any) -> Any:
        from adcp.types import SyncPrincipalResponse

        return await self._call("sync_principal", request, SyncPrincipalResponse)

    async def list_accounts(self, request: Any) -> Any:
        from adcp.types import ListAccountsResponse

        return await self._call("list_accounts", request, ListAccountsResponse)

    async def get_media_buys(self, request: Any) -> Any:
        from adcp.types import GetMediaBuysResponse

        return await self._call("get_media_buys", request, GetMediaBuysResponse)

    async def get_reporting_status(self, request: Any) -> Any:
        from adcp.types import GetReportingStatusResponse

        return await self._call("get_reporting_status", request, GetReportingStatusResponse)

    async def sync_reporting_receipts(self, request: Any) -> Any:
        from adcp.types import SyncReportingReceiptsResponse

        return await self._call("sync_reporting_receipts", request, SyncReportingReceiptsResponse)

    async def fetch_capabilities(self) -> Any:
        value = await self._platform.get_adcp_capabilities({}, self._context())
        return (
            value.model_dump(mode="json", exclude_none=True)
            if hasattr(value, "model_dump")
            else value
        )


class KitClock:
    """The kit's virtual clock: every move runs the seller's worker at each boundary."""

    def __init__(self, kit: ReportingTestKit) -> None:
        self._kit = kit

    def now(self) -> datetime:
        return utc_instant(self._kit.service._clock())

    def __call__(self) -> datetime:
        return self.now()

    async def advance(self, duration: str | timedelta) -> datetime:
        """Move forward by an ISO 8601 duration of days to seconds (``"P3D"``) or a timedelta."""
        delta = parse_delivery_sla(duration) if isinstance(duration, str) else duration
        return await self.advance_to(self.now() + delta)

    async def advance_to(self, instant: str | datetime) -> datetime:
        target = _instant(instant)
        await self._kit._probe("advance_time", target_time=iso_z(target))
        return self.now()


class KitGrants:
    """Grant changes, through the controller's ``reporting_subscription_probe``."""

    def __init__(self, kit: ReportingTestKit) -> None:
        self._kit = kit

    async def revoke(self, principal: str, account_id: str) -> dict[str, Any]:
        return await self._kit._probe("revoke_grant", principal=principal, account_id=account_id)

    async def regrant(self, principal: str, account_id: str) -> dict[str, Any]:
        return await self._kit._probe("regrant", principal=principal, account_id=account_id)

    async def reduce(
        self, principal: str, account_id: str, allowed_tasks: Sequence[str]
    ) -> dict[str, Any]:
        return await self._kit._probe(
            "reduce_grant",
            principal=principal,
            account_id=account_id,
            allowed_tasks=list(allowed_tasks),
        )

    async def wind_down(self, principal: str, account_id: str) -> dict[str, Any]:
        """Reduce to the wind-down tasks: coverage stays, everything else is refused."""
        from adcp.server.reporting_grants import REPORTING_WIND_DOWN_TASKS

        return await self.reduce(principal, account_id, sorted(REPORTING_WIND_DOWN_TASKS))


class KitAccounts:
    """Account changes, through the controller."""

    def __init__(self, kit: ReportingTestKit) -> None:
        self._kit = kit

    async def set_timezone(
        self, account_id: str, timezone_name: str, *, principal: str | None = None
    ) -> dict[str, Any]:
        return await self._kit._probe(
            "set_account_timezone",
            principal=principal or self._kit.default_principal,
            account_id=account_id,
            timezone=timezone_name,
        )


class KitBuys:
    """Buys, through the controller's ``seed_media_buy``."""

    def __init__(self, kit: ReportingTestKit) -> None:
        self._kit = kit

    async def add(self, buy: Mapping[str, Any], *, principal: str | None = None) -> str:
        result = await self._kit._controller_call(
            "seed_media_buy", principal or self._kit.default_principal, fixture=dict(buy)
        )
        return str(result["media_buy_id"])

    async def cancel(
        self, media_buy_id: str, *, at: Any = None, principal: str | None = None
    ) -> None:
        """Cancel a buy at ``at`` (default now): its stop time becomes that instant."""
        world = self._kit.world
        if world is None:
            raise NotImplementedError("cancel needs the reference seller; seed the change yourself")
        buy = world.buys[media_buy_id]
        when = self._kit.clock.now() if at is None else _instant(at)
        buy["status"] = "canceled"
        buy["cancellation"] = {"canceled_at": iso_z(when), "canceled_by": "buyer"}
        await self._kit._controller_call(
            "seed_media_buy", principal or self._kit.default_principal, fixture=buy
        )


class AgreementError(AssertionError):
    """The seller's owed set and the buyer's computed set differ."""


@dataclass
class ReportingTestKit:
    """A seller in process, a virtual clock and buyers bound to it. See the module doc."""

    service: Any
    platform: Any
    controller: TestControllerStore
    world: ReportingWorld | None = None
    default_principal: str = DEFAULT_PRINCIPAL

    def __post_init__(self) -> None:
        self.clock = KitClock(self)
        self.grants = KitGrants(self)
        self.accounts = KitAccounts(self)
        self.buys = KitBuys(self)
        self._buyers: dict[str, KitBuyer] = {}

    def buyer(self, principal: str) -> KitBuyer:
        """A real ``client.reporting`` for ``principal``, bound to the in-process seller."""
        if principal not in self._buyers:
            self._buyers[principal] = KitBuyer(self.platform, principal, self.service)
        return self._buyers[principal]

    async def _controller_call(
        self, scenario: str, principal: str, **params: Any
    ) -> dict[str, Any]:
        method = getattr(self.controller, scenario)
        context = AccountAwareToolContext(caller_identity=principal)
        try:
            return dict(await method(**params, context=context))
        except NotImplementedError as error:
            raise NotImplementedError(
                f"the controller does not implement {scenario}; serve it to drive the kit"
            ) from error

    async def _probe(
        self, operation: str, *, principal: str | None = None, **params: Any
    ) -> dict[str, Any]:
        return await self._controller_call(
            "reporting_subscription_probe",
            principal or self.default_principal,
            operation=operation,
            **params,
        )

    async def assert_agreement(self, principal: str) -> None:
        """The seller's owed set equals the buyer's, for every subscription and account.

        The seller side is the engine's owed periods that have ended; the
        buyer side is ``reconcile()`` over the wire. Also checks that the
        ``authorized_at`` served on ``list_accounts`` is the one coverage
        used. On a difference, the error carries both sides' traces and
        where they first differ.
        """
        from adcp._principal_ref import PrincipalRef
        from adcp.reporting.subscriptions import diff_coverage_traces

        now = self.clock.now()
        ref = PrincipalRef(None, principal)
        engine = self.service._subscriptions.engine
        buyer = self.buyer(principal)
        report = await buyer.reporting.reconcile(now=now)
        buyer_set = {
            (
                line.account_id,
                line.subscription_id,
                line.subscription_version,
                line.offering_id,
                line.period["start"],
                line.period["end"],
                tuple(sorted(line.media_buy_ids)),
            )
            for line in report.periods
            if line.status != "not_owed"
        }
        seller_set: set[tuple[Any, ...]] = set()
        accounts = await engine.account_ids(ref)
        unauthorized = set()
        readback = await self.service.principals.reporting_subscriptions(ref)
        for state in readback:
            for entry in state.accounts_unauthorized or ():
                unauthorized.add(entry.account_id)
        for state in readback:
            for account_id in sorted(set(accounts) | unauthorized):
                for period in await engine.owed_periods(
                    ref, state.subscription_id, account_id, now=now
                ):
                    seller_set.add(
                        (
                            account_id,
                            period.subscription_id,
                            period.delivery_config_version,
                            period.offering_id,
                            period.period_start,
                            period.period_end,
                            tuple(sorted(period.media_buy_ids)),
                        )
                    )
        problems: list[str] = []
        listed = await buyer.list_accounts(_list_accounts_request())
        for account in _plain_accounts(listed):
            served = (account.get("authorization") or {}).get("authorized_at")
            used = await engine.authorized_at(ref, account["account_id"])
            if served is not None and used is not None and _instant(served) != used:
                problems.append(
                    f"{account['account_id']}: list_accounts serves authorized_at {served}, "
                    f"coverage used {iso_z(used)}"
                )
        only_seller = sorted(seller_set - buyer_set)
        only_buyer = sorted(buyer_set - seller_set)
        for side, rows in (("seller", only_seller), ("buyer", only_buyer)):
            for row in rows[:10]:
                account_id, subscription_id, _version, offering_id, start, end, buys = row
                detail = f"only the {side} owes {row}"
                try:
                    period = {"start": start, "end": end}
                    seller_traces = await engine.explain_coverage(
                        ref,
                        account_id,
                        period=period,
                        subscription_id=subscription_id,
                        media_buy_id=buys[0],
                        offering_id=offering_id,
                        now=now,
                    )
                    buyer_traces = await buyer.reporting.explain(
                        account_id=account_id,
                        period=period,
                        subscription_id=subscription_id,
                        media_buy_id=buys[0],
                        offering_id=offering_id,
                        now=now,
                    )
                    if seller_traces and buyer_traces:
                        difference = diff_coverage_traces(buyer_traces[0], seller_traces[0])
                        if difference is not None:
                            detail += f"; {difference.describe()}"
                        detail += (
                            f"; seller: {seller_traces[0].explanation} "
                            f"buyer: {buyer_traces[0].explanation}"
                        )
                except Exception as error:  # noqa: BLE001 - the diff is best effort
                    detail += f"; explain failed: {error}"
                problems.append(detail)
        if problems:
            raise AgreementError(
                f"buyer and seller disagree for {principal!r} ({len(only_seller)} only the "
                f"seller owes, {len(only_buyer)} only the buyer owes):\n  " + "\n  ".join(problems)
            )


def _list_accounts_request() -> Any:
    from adcp.types import ListAccountsRequest

    return ListAccountsRequest.model_validate({})


def _plain_accounts(result: Any) -> list[dict[str, Any]]:
    data: Any = getattr(result, "data", None)
    if data is not None and hasattr(data, "model_dump"):
        data = data.model_dump(mode="json", exclude_none=True)
    return list((data or {}).get("accounts") or [])


@contextlib.asynccontextmanager
async def reporting_test_kit(
    *,
    start: Any = "2026-11-01T00:00:00Z",
    offerings: Sequence[Mapping[str, Any]] | None = None,
    accounts: Sequence[Mapping[str, Any]] | None = None,
    buys: Sequence[Mapping[str, Any]] = (),
    platform: Any = None,
    service: Any = None,
    controller: TestControllerStore | None = None,
    max_accounts: int = 100,
    backfill_on_regrant: bool = False,
    default_principal: str = DEFAULT_PRINCIPAL,
) -> AsyncIterator[ReportingTestKit]:
    """A reporting test kit (see the module doc). Closes the service on exit."""
    if platform is None:
        if service is not None or controller is not None:
            raise ValueError("service and controller come with platform=; omit all three")
        seller = create_reference_reporting_seller(
            start=start,
            offerings=offerings,
            accounts=accounts,
            buys=buys,
            max_accounts=max_accounts,
            backfill_on_regrant=backfill_on_regrant,
        )
        kit = ReportingTestKit(
            seller.service, seller.platform, seller.controller, seller.world, default_principal
        )
        await seller.service.run_worker()
    else:
        if service is None or controller is None:
            raise ValueError(
                "platform= needs service= (your ReliableReportingService) and controller= "
                "(a TestControllerStore serving reporting_subscription_probe)"
            )
        kit = ReportingTestKit(service, platform, controller, None, default_principal)
    try:
        yield kit
    finally:
        await kit.service.drain_subscription_triggers()
        close = getattr(kit.service, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


__all__ = [
    "DEFAULT_PRINCIPAL",
    "REFERENCE_ACCOUNT",
    "AgreementError",
    "KitBuyer",
    "ReferenceReportingSeller",
    "ReferenceSeller",
    "ReportingTestController",
    "ReportingTestKit",
    "ReportingWorld",
    "create_reference_reporting_seller",
    "offerings",
    "reporting_test_kit",
]
