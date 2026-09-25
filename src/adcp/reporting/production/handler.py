"""Authenticated production routes with private, revision-bound exact reads."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from adcp.decisioning.context import RequestContext
from adcp.exceptions import ADCPTaskError
from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.consumer_status import ConsumerStatusIngest
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.store import LedgerConflictError, decode_cursor, encode_cursor
from adcp.reporting.receipts.errors import ReportingReceiptError
from adcp.reporting.receipts.handler import (
    ReceiptAccountResolver,
    ReportingReceiptHandler,
    _consumer,
)
from adcp.server.base import NotImplementedResponse, ToolContext
from adcp.server.responses import capabilities_response
from adcp.types import (
    Error,
    GetAdcpCapabilitiesRequest,
    GetMediaBuyDeliveryRequest,
    GetReportingStatusRequest,
    SyncAccountsRequest,
    SyncReportingReceiptsRequest,
    SyncReportingStatusRequest,
)

if TYPE_CHECKING:
    from adcp.decisioning.registry import BuyerAgentRegistry
    from adcp.reporting.production.service import ReportingProductionSupport


def _request(value: Any) -> dict[str, Any]:
    return (
        dict(value)
        if isinstance(value, dict)
        else dict(value.model_dump(mode="json", exclude_unset=True))
    )


def _task_error(task: str, code: str, message: str) -> ADCPTaskError:
    return ADCPTaskError(operation=task, errors=[Error(code=code, message=message)])


class ReportingProductionHandler(ReportingReceiptHandler):
    """Mount the same instance on MCP/A2A; the support owns its lifecycle."""

    advertised_tools = {
        "get_adcp_capabilities",
        "get_reporting_status",
        "get_media_buy_delivery",
        "sync_accounts",
        "sync_reporting_receipts",
        "sync_reporting_status",
    }

    def __init__(
        self,
        production: ReportingProductionSupport,
        *,
        resolve_account: ReceiptAccountResolver,
        buyer_agents: BuyerAgentRegistry | None = None,
        adcp_version: str | None = None,
    ) -> None:
        self.production = production
        super().__init__(
            production.store,
            resolve_account=resolve_account,
            buyer_agents=buyer_agents,
            consumer_status_enabled=production.projection.consumer_status_enabled,
            adcp_version=adcp_version,
        )

    def advertised_tools_for_instance(self) -> set[str]:
        names = {
            "get_adcp_capabilities",
            "get_reporting_status",
            "get_media_buy_delivery",
            "sync_accounts",
        }
        if any(o.reconciled for o in self.production.offerings):
            names.add("sync_reporting_receipts")
        if self._feed_consumer_status_enabled:
            names.add("sync_reporting_status")
        return names

    async def get_reporting_status(
        self,
        params: GetReportingStatusRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any] | NotImplementedResponse:
        return await super().get_reporting_status(params, context)

    async def sync_reporting_receipts(
        self,
        params: SyncReportingReceiptsRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        if not any(o.reconciled for o in self.production.offerings):
            raise _task_error(
                "sync_reporting_receipts", "NOT_SUPPORTED", "receipt task is unavailable"
            )
        return await super().sync_reporting_receipts(params, context)

    async def _authorize(
        self, request: dict[str, Any], context: ToolContext | None
    ) -> ReportingDeliveryPrincipal:
        try:
            if context is None or not isinstance(request.get("account"), dict):
                raise ReportingReceiptError("UNAUTHORIZED")
            consumer = await _consumer(context, self._receipt_registry)
            account = await self._receipt_account_resolver(
                dict(request["account"]), context, consumer
            )
            if isinstance(context, RequestContext) and context.account.id != account:
                raise ReportingReceiptError("UNAUTHORIZED")
            return ReportingDeliveryPrincipal(account, consumer)
        except Exception:
            raise ReportingReceiptError("UNAUTHORIZED") from None

    async def get_adcp_capabilities(
        self,
        params: GetAdcpCapabilitiesRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        response = capabilities_response(
            ["media_buy"],
            sandbox=False,
            idempotency={"supported": False},
            adcp_version=self.production._protocol_version,
            supported_versions=[self.production._protocol_version],
        )
        response["account"] = self.production.configuration_task.account_capabilities()
        reporting = await self.production.reporting_delivery()
        if reporting:
            response["media_buy"] = {"reporting_delivery": reporting}
            response["experimental_features"] = ["media_buy.reporting_delivery"]
            if any(
                reporting.get(k)
                for k in ("ledger_notification", "status_notification", "readiness_notification")
            ):
                from adcp.reporting.production.notifications import (
                    signing_capabilities,
                    signing_identity,
                )

                response["webhook_signing"] = signing_capabilities(self.production)
                response["identity"] = signing_identity(self.production)
        return response

    async def sync_accounts(
        self,
        params: SyncAccountsRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        try:
            self.production._assert_components()
            return await self.production.configuration_task.execute(
                self.production, _request(params), context
            )
        except LedgerConflictError as error:
            code, message = error.code, str(error)
        except ReportingReceiptError as error:
            code, message = error.code, str(error)
        except Exception:
            code, message = (
                "REPORTING_CONFIGURATION_UNAVAILABLE",
                "reporting configuration is unavailable",
            )
        raise _task_error("sync_accounts", code, message)

    async def sync_reporting_status(
        self,
        params: SyncReportingStatusRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any] | NotImplementedResponse:
        if not self._feed_consumer_status_enabled:
            return self._not_supported("sync_reporting_status")
        try:
            request = _request(params)
            caller = await self._authorize(request, context)
            result = await ConsumerStatusIngest(self.production.store, enabled=True).handle(
                request,
                account_id=caller.account_id,
                consumer_id=caller.consumer_id,
            )
            if await self._authorize(request, context) != caller:
                raise ReportingReceiptError("UNAUTHORIZED")
            return result
        except (ReportingReceiptError, LedgerConflictError) as error:
            code, message = error.code, str(error)
        except Exception:
            code, message = "REPORTING_STATUS_UNAVAILABLE", "reporting status is unavailable"
        raise _task_error("sync_reporting_status", code, message)

    async def get_media_buy_delivery(
        self,
        params: GetMediaBuyDeliveryRequest | dict[str, Any],
        context: ToolContext | None = None,
    ) -> dict[str, Any] | NotImplementedResponse:
        request = _request(params)
        if "reporting_revision_id" not in request:
            return self._not_supported("get_media_buy_delivery")
        try:
            from adcp.validation.schema_loader import get_named_validator

            validator = get_named_validator("media-buy/get-media-buy-delivery-request.json")
            if validator is None or next(validator.iter_errors(request), None) is not None:
                raise LedgerConflictError("INVALID_REQUEST", "exact revision request is invalid")
            caller = await self._authorize(request, context)
            revision_id = request["reporting_revision_id"]
            status_request = {
                "account": request["account"],
                "view": "revision",
                "reporting_revision_id": revision_id,
            }
            # This is the actual private mounted status path, including exact
            # ownership/visibility and complete captured reconciliation checks.
            projected = await self.get_reporting_status(status_request, context)
            if not isinstance(projected, dict) or "revision" not in projected:
                raise LedgerConflictError("LOOKUP_UNAVAILABLE", "no such revision is available")
            store = self.production.store
            revision = await store.get_revision(
                account_id=caller.account_id, reporting_revision_id=revision_id
            )
            if revision is None or not revision.readable:
                raise LedgerConflictError("LOOKUP_UNAVAILABLE", "no such revision is available")
            pagination = request.get("pagination") or {}
            limit = pagination.get("max_results", 50)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise LedgerConflictError("INVALID_REQUEST", "exact revision page size is invalid")
            binding_hash = hashlib.sha256(
                canonical_json_utf8_v1(
                    {
                        "account": caller.account_id,
                        "consumer": caller.consumer_id,
                        "revision": revision_id,
                        "digest": revision.revision_content_sha256,
                        "count": revision.row_count,
                        "limit": limit,
                    }
                )
            ).hexdigest()
            token = pagination.get("cursor")
            offset = 0
            if token is not None:
                if not isinstance(token, str) or not token.startswith("rpr2.") or len(token) > 2048:
                    raise LedgerConflictError(
                        "INVALID_CURSOR", "exact revision cursor is unavailable"
                    )
                cursor = decode_cursor(token[5:])
                if (
                    set(cursor) != {"v", "h", "p"}
                    or cursor["v"] != 2
                    or cursor["h"] != binding_hash
                    or type(cursor["p"]) is not int
                    or not 0 <= cursor["p"] <= revision.row_count
                ):
                    raise LedgerConflictError(
                        "INVALID_CURSOR", "exact revision cursor is unavailable"
                    )
                offset = cursor["p"]
            page = await store.read_revision_rows(
                account_id=caller.account_id,
                reporting_revision_id=revision_id,
                limit=limit,
                cursor=(
                    encode_cursor({"revision": revision_id, "offset": offset}) if offset else None
                ),
            )
            if page.total_count != revision.row_count or page.reporting_revision_id != revision_id:
                raise ReportingNotificationError("reporting_revision_content_unavailable")
            if await self._authorize(request, context) != caller:
                raise ReportingReceiptError("UNAUTHORIZED")
            after = await store.get_revision(
                account_id=caller.account_id, reporting_revision_id=revision_id
            )
            if after != revision:
                raise LedgerConflictError("LOOKUP_UNAVAILABLE", "no such revision is available")
            wire = projected["revision"]
            totals = (
                [r.to_wire() for r in revision.managed_control_totals]
                if revision.managed_control_totals is not None
                else [{"name": n, "value": v} for n, v in revision.control_totals]
            )
            position: dict[str, Any] = {"total_count": page.total_count, "has_more": page.has_more}
            if page.has_more:
                position["cursor"] = "rpr2." + encode_cursor(
                    {"v": 2, "h": binding_hash, "p": offset + len(page.rows)}
                )
            result = {
                "status": "completed",
                "reporting_period": {
                    "start": wire["period"]["start"],
                    "end": wire["period"]["end"],
                },
                "media_buy_deliveries": [],
                "reporting_revision": wire,
                "reporting_revision_binding": {
                    "reporting_revision_id": revision_id,
                    "row_count": revision.row_count,
                    "control_totals": totals,
                    "content_sha256": revision.revision_content_sha256,
                },
                "reporting_rows": list(page.rows),
                "pagination": position,
            }
            if "ext" in projected:
                result["ext"] = projected["ext"]
            return result
        except ADCPTaskError:
            raise
        except (ReportingReceiptError, LedgerConflictError) as error:
            code, message = error.code, str(error)
        except Exception:
            code, message = "REPORTING_CONTENT_UNAVAILABLE", "reporting content is unavailable"
        raise _task_error("get_media_buy_delivery", code, message)
