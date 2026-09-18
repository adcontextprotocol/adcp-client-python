"""One authenticated composition path for direct MCP/A2A and hydrated contexts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from adcp.decisioning.context import AuthInfo, RequestContext
from adcp.decisioning.registry import BuyerAgent, BuyerAgentRegistry, HttpSigCredential
from adcp.exceptions import ADCPTaskError
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.identity import canonical_consumer, resolve_reporting_consumer
from adcp.reporting.receipts._diagnostics import _storage_failure
from adcp.reporting.receipts.errors import ReportingReceiptError
from adcp.reporting.receipts.store import ReportingReceiptBatchStore
from adcp.reporting.receipts.wire import TASK, validate_receipt_request
from adcp.server.base import ADCPHandler, NotImplementedResponse, ToolContext
from adcp.types import Error, GetReportingStatusRequest, SyncReportingReceiptsRequest

if TYPE_CHECKING:
    from adcp.reporting.feed.store import ReportingFeedStore

ReceiptAccountResolver = Callable[[dict[str, Any], ToolContext, str], Awaitable[str]]
"""Resolve AND reauthorize the exact account reference for this consumer on every call.

Return the canonical storage account ID, never a RequestContext cache key. A
natural-key account reference is resolved by this same application ACL boundary.
Raises ReportingReceiptError('UNAUTHORIZED') for unknown or denied accounts.
"""


async def _consumer(context: ToolContext, registry: BuyerAgentRegistry | None) -> str:
    identities: set[str] = set()
    auths: list[AuthInfo] = []
    agents: list[BuyerAgent] = []
    auth_value = getattr(context, "auth_info", None)
    if auth_value is not None:
        if not isinstance(auth_value, AuthInfo):
            raise ReportingReceiptError("UNAUTHORIZED")
        auths.append(auth_value)
    agent_value = getattr(context, "buyer_agent", None)
    if agent_value is not None:
        if not isinstance(agent_value, BuyerAgent):
            raise ReportingReceiptError("UNAUTHORIZED")
        agents.append(agent_value)
    auth_principal = getattr(context, "auth_principal", None)
    if auth_principal is not None:
        identities.add(canonical_consumer(auth_principal))
    # caller_identity is deliberately never inspected on RequestContext.
    if not isinstance(context, RequestContext) and context.caller_identity is not None:
        identities.add(canonical_consumer(context.caller_identity))
    for key in ("adcp.auth_info", "auth_info"):
        auth = context.metadata.get(key)
        if auth is not None:
            if not isinstance(auth, AuthInfo):
                raise ReportingReceiptError("UNAUTHORIZED")
            auths.append(auth)
    agent = context.metadata.get("adcp.buyer_agent")
    if agent is not None:
        if not isinstance(agent, BuyerAgent):
            raise ReportingReceiptError("UNAUTHORIZED")
        agents.append(agent)
    if registry is not None:
        if not auths:
            raise ReportingReceiptError("UNAUTHORIZED")
        for auth in auths:
            credential = auth.credential
            if credential is None:
                raise ReportingReceiptError("UNAUTHORIZED")
            resolved = (
                await registry.resolve_by_agent_url(credential.agent_url)
                if isinstance(credential, HttpSigCredential)
                else await registry.resolve_by_credential(credential)
            )
            if resolved is None:
                raise ReportingReceiptError("UNAUTHORIZED")
            agents.append(resolved)
    for agent in agents:
        if agent.status != "active":
            raise ReportingReceiptError("UNAUTHORIZED")
        identities.add(resolve_reporting_consumer(auth_info=None, agent=agent))
    for auth in auths:
        # A credential-only API/OAuth AuthInfo is allowed when a registry or
        # trusted hydrated agent supplied the consumer. It is never an alias.
        if (
            auth.principal is not None
            or auth.agent_url is not None
            or isinstance(auth.credential, HttpSigCredential)
        ):
            identities.add(resolve_reporting_consumer(auth_info=auth))
    if len(identities) != 1:
        raise ReportingReceiptError("UNAUTHORIZED")
    return identities.pop()


class ReportingReceiptHandler(ADCPHandler[ToolContext]):
    """Mount receipts and the optional frozen feed; tier activation is separately gated.

    ``resolve_account`` is an application ACL, called even for a completed
    batch. ``buyer_agents`` optionally re-resolves API/OAuth/signed commercial
    identity on every call. Neither transport tenancy nor body fields supply
    the consumer. Authentication middleware must populate trusted context.
    """

    advertised_tools = {TASK, "get_reporting_status"}

    def __init__(
        self,
        store: ReportingReceiptBatchStore,
        *,
        resolve_account: ReceiptAccountResolver,
        buyer_agents: BuyerAgentRegistry | None = None,
        consumer_status_enabled: bool = False,
    ) -> None:
        super().__init__()
        if not isinstance(store, ReportingReceiptBatchStore):
            raise TypeError("receipt ingress requires an atomic ReportingReceiptBatchStore")
        self.receipt_store = store
        self._receipt_account_resolver = resolve_account
        self._receipt_registry = buyer_agents
        from adcp.reporting.feed.store import ReportingFeedStore

        self.reporting_feed_store: ReportingFeedStore | None = (
            store if isinstance(store, ReportingFeedStore) else None
        )
        self._feed_consumer_status_enabled = consumer_status_enabled

    def advertised_tools_for_instance(self) -> set[str]:
        return {TASK, "get_reporting_status"} if self.reporting_feed_store is not None else {TASK}

    async def get_reporting_status(
        self,
        params: GetReportingStatusRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any] | NotImplementedResponse:
        """One authenticated mount; the optional store freezes periods walks."""
        from adcp.reporting.feed.errors import ReportingFeedError
        from adcp.reporting.feed.request import FeedRequest
        from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
        from adcp.reporting.ledger.store import LedgerConflictError, ReportingLedgerStore

        if self.reporting_feed_store is None:
            return self._not_supported("get_reporting_status")
        request = (
            params
            if isinstance(params, dict)
            else params.model_dump(mode="json", exclude_unset=True)
        )
        try:
            if request.get("view") == "periods":
                FeedRequest.parse(request)

            async def authorize() -> ReportingDeliveryPrincipal:
                if context is None:
                    raise ReportingFeedError("UNAUTHORIZED")
                try:
                    consumer = await _consumer(context, self._receipt_registry)
                    account = await self._receipt_account_resolver(
                        dict(request["account"]), context, consumer
                    )
                    if isinstance(context, RequestContext) and context.account.id != account:
                        raise ReportingFeedError("UNAUTHORIZED")
                    return ReportingDeliveryPrincipal(account, consumer)
                except (ReportingReceiptError, ReportingNotificationError, ValueError, TypeError):
                    raise ReportingFeedError("UNAUTHORIZED") from None

            caller = await authorize()
            if request.get("view") == "periods":

                async def reauthorize() -> None:
                    # A still-authorized alias must not change which account or
                    # canonical consumer owns the already captured boundary.
                    if await authorize() != caller:
                        raise ReportingFeedError("UNAUTHORIZED")

                response = await self.reporting_feed_store.read_reporting_feed(
                    request,
                    caller=caller,
                    consumer_status_enabled=self._feed_consumer_status_enabled,
                    reauthorize=reauthorize,
                )
                await reauthorize()
                return response
            pagination = request.get("pagination")
            positions = (
                request.get("changes_after"),
                pagination.get("cursor") if isinstance(pagination, dict) else None,
            )
            if any(isinstance(p, str) and p.startswith("rpf1.") for p in positions):
                # A view change cannot send a frozen position to the mutable
                # legacy projector. Leave ordinary Core requests compatible.
                raise ReportingFeedError("INVALID_CHECKPOINT")
            if isinstance(self.receipt_store, ReportingLedgerStore):
                try:
                    return await ReportingStatusHandler(
                        self.receipt_store,
                        consumer_status_enabled=self._feed_consumer_status_enabled,
                    ).handle(
                        request,
                        caller=ReportingStatusCaller(caller.account_id, caller.consumer_id),
                    )
                except LedgerConflictError as error:
                    # Only the legacy Core projector exposes its established
                    # domain errors. ACL/provider/feed failures stay redacted.
                    code, message = error.code, str(error)
            else:
                return self._not_supported("get_reporting_status")
        except ReportingFeedError as error:
            code, message = error.code, str(error)
        except Exception:
            unavailable = ReportingFeedError("REPORTING_FEED_STORAGE_UNAVAILABLE")
            code, message = unavailable.code, str(unavailable)
        raise ADCPTaskError(
            operation="get_reporting_status", errors=[Error(code=code, message=message)]
        )

    async def sync_reporting_receipts(
        self,
        params: SyncReportingReceiptsRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        request = (
            params
            if isinstance(params, dict)
            else params.model_dump(mode="json", exclude_unset=True)
        )
        try:
            validate_receipt_request(request)
            if context is None:
                raise ReportingReceiptError("UNAUTHORIZED")
            try:
                consumer = await _consumer(context, self._receipt_registry)
            except ReportingNotificationError:
                raise ReportingReceiptError("UNAUTHORIZED") from None
            account = await self._receipt_account_resolver(
                dict(request["account"]), context, consumer
            )
            if isinstance(context, RequestContext) and context.account.id != account:
                raise ReportingReceiptError("UNAUTHORIZED")
            try:
                caller = ReportingDeliveryPrincipal(account, consumer)
            except (ValueError, TypeError):
                raise ReportingReceiptError("UNAUTHORIZED") from None
            return await self.receipt_store.ingest_receipt_batch(request, caller=caller)
        except ReportingReceiptError as error:
            code, message = error.code, str(error)
        except Exception as error:
            _storage_failure(error, boundary="handler")
            unavailable = ReportingReceiptError("RECEIPT_STORAGE_UNAVAILABLE")
            code, message = unavailable.code, str(unavailable)
        # Leave the exception scope before translating. Credential/ACL adapters
        # may raise provider errors; only safe origin coordinates were logged.
        raise ADCPTaskError(operation=TASK, errors=[Error(code=code, message=message)])
