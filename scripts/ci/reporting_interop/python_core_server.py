#!/usr/bin/env python3
"""Installed-artifact Python Reliable Reporting Core MCP server.

This process deliberately composes only public SDK primitives.  It owns no
release-service lifecycle and makes no Managed Delivery or Reconciled Billing
claim.  A runner must give every invocation a fresh PostgreSQL database.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg_pool import AsyncConnectionPool

from adcp.reporting.ledger import (
    PgReportingLedgerStore,
    ReportingConfiguration,
    ReportingDefinitionBinding,
    ReportingObligationRecord,
    ReportingRevisionRecord,
    ReportingScheduleSpec,
    ReportingStatusCaller,
    ReportingStatusHandler,
    derive_period,
    revision_content_sha256,
)
from adcp.reporting.ledger.status_server import ReportingStatusNotificationHandler
from adcp.server import serve
from adcp.server.auth import BearerTokenAuth, Principal, auth_context_factory
from adcp.server.responses import capabilities_response, sync_accounts_response

ADCP_VERSION = "3.2.0-rc.4"
WIRE_ADCP_VERSION = "3.2-rc.4"
FIXED_NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)
PERIOD_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
PERIOD_END = PERIOD_START + timedelta(days=1)
ACCOUNTS = ("interop-account-a", "interop-account-b")
TOKENS = {
    "interop-token-a": (ACCOUNTS[0], "https://buyer-a.example.test/adcp"),
    "interop-token-b": (ACCOUNTS[1], "opaque-consumer-b"),
}


def _configuration(account_id: str) -> ReportingConfiguration:
    return ReportingConfiguration(
        delivery_config_id="shared-daily-key",
        delivery_config_version=1,
        account_id=account_id,
        report_definition_id="interop-core-v1",
        reporting_profile="interop-core-v1",
        feed_purpose="analytics",
        schedule=ReportingScheduleSpec("P1D", "PT1H", period_anchor=PERIOD_START),
        required_finality="snapshot",
        activated_at=PERIOD_START,
        deactivated_at=PERIOD_END,
        media_buy_ids=(f"media-buy-{account_id[-1]}",),
        definition=ReportingDefinitionBinding(
            report_definition_uri="https://contracts.example.test/interop-core-v1.json",
            report_definition_sha256="a" * 64,
            schema_version="1",
            schema_uri="https://contracts.example.test/interop-core-rows-v1.json",
            schema_sha256="b" * 64,
        ),
    )


def _obligation(configuration: ReportingConfiguration) -> ReportingObligationRecord:
    period = derive_period(configuration.schedule, account_timezone="UTC", ordinal=0)
    return ReportingObligationRecord(
        reporting_obligation_id=f"interop-obligation-{configuration.account_id[-1]}",
        account_id=configuration.account_id,
        delivery_config_id=configuration.delivery_config_id,
        delivery_config_version=configuration.delivery_config_version,
        report_definition_id=configuration.report_definition_id,
        reporting_profile=configuration.reporting_profile,
        feed_purpose=configuration.feed_purpose,
        period=period,
        scope_resolved_at=period.end,
        media_buy_ids=configuration.media_buy_ids,
        required_finality=configuration.required_finality,
        automated_recovery_deadline_at=period.expected_at + timedelta(days=1),
        schedule=configuration.schedule,
        definition=configuration.definition,
        created_at=period.end,
        currency="USD",
    )


def _revision(
    obligation: ReportingObligationRecord,
) -> tuple[ReportingRevisionRecord, list[dict[str, Any]]]:
    rows = [
        {
            "media_buy_id": obligation.media_buy_ids[0],
            "impressions": 5,
            "vendor": {"fraction": "1.25", "enabled": True, "ordinal": 1},
        }
    ]
    revision_id = f"interop-revision-{obligation.account_id[-1]}"
    totals = (("impressions", "5"),)
    return (
        ReportingRevisionRecord(
            reporting_revision_id=revision_id,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            finality="snapshot",
            revision_content_sha256=revision_content_sha256(
                reporting_revision_id=revision_id,
                row_count=len(rows),
                control_totals=totals,
                reporting_rows=rows,
            ),
            row_count=len(rows),
            control_totals=totals,
            observed_at=obligation.period.end,
            data_through=obligation.period.end,
            created_at=obligation.period.end,
        ),
        rows,
    )


class CoreHandler(ReportingStatusNotificationHandler):
    """Truthful Core-only mount over the durable public ledger."""

    advertised_tools = {"get_adcp_capabilities", "get_reporting_status", "sync_accounts"}

    async def sync_accounts(self, params: Any, context: Any = None) -> dict[str, Any]:
        """Return the already-provisioned Core fixture account configuration.

        This makes the capability's schema-required configuration task real,
        but the matrix does not credit this fixture method as typed production
        onboarding evidence.
        """
        request = (
            params.model_dump(mode="json", exclude_none=True)
            if hasattr(params, "model_dump")
            else dict(params)
        )
        accounts: list[dict[str, Any]] = []
        for item in request.get("accounts", []):
            account = item.get("account") if isinstance(item, dict) else None
            account_id = account.get("account_id") if isinstance(account, dict) else None
            if account_id in ACCOUNTS:
                accounts.append(
                    {
                        "account": {"account_id": account_id},
                        "action": "unchanged",
                        "status": "active",
                        "currency": "USD",
                        "timezone": "UTC",
                    }
                )
            else:
                accounts.append(
                    {
                        "account": {"account_id": account_id or "unknown"},
                        "action": "failed",
                        "errors": [
                            {
                                "code": "ACCOUNT_NOT_FOUND",
                                "message": "account is not part of this isolated fixture",
                            }
                        ],
                    }
                )
        return sync_accounts_response(accounts, sandbox=False)

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        response = capabilities_response(
            ["media_buy"],
            sandbox=False,
            idempotency={"supported": False},
            # capabilities_response normalizes the packaged three-component
            # SDK version to the protocol's release-precision wire spelling.
            adcp_version=ADCP_VERSION,
            supported_versions=[WIRE_ADCP_VERSION],
        )
        response["experimental_features"] = ["media_buy.reporting_delivery"]
        response["media_buy"] = {
            "reporting_delivery": {
                "supported": True,
                "configuration_task": "sync_accounts",
                "status_task": "get_reporting_status",
                "offerings": [
                    {
                        "offering_id": "interop-core-daily",
                        "feed_purpose": "analytics",
                        "report_definition_id": "interop-core-v1",
                        "report_definition_uri": (
                            "https://contracts.example.test/interop-core-v1.json"
                        ),
                        "report_definition_sha256": "a" * 64,
                        "reporting_profile": {
                            "id": "interop-core-v1",
                            "version": "1",
                            "schema_uri": (
                                "https://contracts.example.test/interop-core-rows-v1.json"
                            ),
                            "schema_sha256": "b" * 64,
                            "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
                            "schema_ref_policy": "local_fragment_only",
                            "grain": "media_buy",
                            "primary_keys": ["media_buy_id"],
                        },
                        "schedule": {
                            "period_duration": "P1D",
                            "alignment": "utc",
                            "delivery_sla": "PT1H",
                        },
                        "supported_finality": ["snapshot"],
                        "reconciliation_mode": "delivery_only",
                    }
                ],
                "automated_recovery_window_seconds": 86400,
                "status_retention_days": 30,
            }
        }
        return response


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    if not args.database_url:
        raise SystemExit("--database-url is required")
    pool = AsyncConnectionPool(
        args.database_url,
        min_size=1,
        max_size=4,
        open=False,
        kwargs={"autocommit": False},
    )
    store = PgReportingLedgerStore(pool=pool, clock=lambda: FIXED_NOW)

    async def startup() -> None:
        await pool.open()
        await pool.wait(timeout=15)
        await store.create_schema()
        for account_id in ACCOUNTS:
            configuration = _configuration(account_id)
            obligation = _obligation(configuration)
            revision, rows = _revision(obligation)
            await store.put_configuration(configuration)
            await store.commit_obligation(obligation)
            await store.commit_revision(revision, rows)

    async def shutdown() -> None:
        await pool.close()

    async def resolve_caller(request: dict[str, Any], context: Any) -> ReportingStatusCaller:
        if context is None or context.caller_identity is None:
            raise PermissionError("authenticated reporting caller required")
        requested = request.get("account")
        account_id = requested.get("account_id") if isinstance(requested, dict) else None
        authorized = context.metadata.get("account_id")
        consumer_id = context.metadata.get("consumer_id")
        if account_id != authorized or not isinstance(consumer_id, str):
            raise PermissionError("caller is not authorized for this account")
        return ReportingStatusCaller(account_id, consumer_id)

    def validate_token(token: str) -> Principal | None:
        binding = TOKENS.get(token)
        if binding is None:
            return None
        account_id, consumer_id = binding
        return Principal(
            caller_identity=f"principal-{account_id[-1]}",
            tenant_id=f"tenant-{account_id[-1]}",
            metadata={"account_id": account_id, "consumer_id": consumer_id},
        )

    handler = CoreHandler(
        ReportingStatusHandler(store), resolve_caller=resolve_caller, adcp_version=ADCP_VERSION
    )
    serve(
        handler,
        name="reporting-interop-python-core",
        host=args.host,
        port=args.port,
        transport="both",
        auth=BearerTokenAuth(validate_token=validate_token),
        context_factory=auth_context_factory,
        stateless_http=True,
        allowed_hosts=(args.host, "localhost", "127.0.0.1"),
        on_startup=(startup,),
        on_shutdown=(shutdown,),
    )


if __name__ == "__main__":
    main()
