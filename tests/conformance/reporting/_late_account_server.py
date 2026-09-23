"""Public production support starts empty; all enrollment arrives over real HTTP."""

import argparse
import importlib.metadata
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

import adcp
from adcp.decisioning.capabilities import Account as AccountCapabilities
from adcp.reporting.ledger import (
    ProducerOfferings,
    ReportingConfiguration,
    ReportingDestinationBinding,
    ReportingProducer,
    ReportingScheduleSpec,
)
from adcp.reporting.materializer import (
    ReportingDestinationIO,
    ReportingMaterializerService,
    ReportingRevisionVerifierRegistry,
    ReportingWriterCapability,
)
from adcp.reporting.production import (
    PgReportingProductionStore,
    ReportingConfigurationAdmission,
    ReportingProductionConfigurationTask,
    ReportingProductionOffering,
    ReportingProductionSupport,
)
from adcp.reporting.projection import PgReportingStatusProjection
from adcp.reporting.receipts import ReportingReceiptError
from adcp.reporting.source import parse_verified_source_batch_manifest_v1
from adcp.server import serve
from adcp.server.auth import BearerTokenAuth, Principal, auth_context_factory
from adcp.types import ReportingDeliveryOffering

from ._generation_support import END, START
from ._late_account_support import ACCOUNTS, AccountSource, CurrencyDestination, verifier_for

CONSUMERS = {"usd": "urn:buyer:usd", "eur": "urn:buyer:eur"}


class LiveAccountSource(AccountSource):
    """Record the public source boundary, without changing its returned evidence."""

    async def execute(self, request, *, cancel, heartbeat=None):
        result = await super().execute(request, cancel=cancel, heartbeat=heartbeat)
        if result.ok:
            manifest = parse_verified_source_batch_manifest_v1(
                result.response.manifest, result.manifest_bytes
            )
            with self.observation_log.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "account": request.identity.account_id,
                            "period": {
                                "start": request.period.start.isoformat(),
                                "end": request.period.end.isoformat(),
                            },
                            "source_read_cutoff_at": (
                                request.period.source_read_cutoff_at.isoformat()
                            ),
                            "observed_at": manifest.observed_at.isoformat(),
                            "acquired_at": manifest.acquired_at.isoformat(),
                            "finalized_at": manifest.finality_evidence.observed_at.isoformat(),
                            "data_through": manifest.data_through.isoformat(),
                            "publication_id": manifest.publication_id,
                        }
                    )
                    + "\n"
                )
        return result


class WireCapture:
    def __init__(self, app, path):
        self.app, self.path = app, Path(path)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        request, response = bytearray(), bytearray()
        status = None

        async def incoming():
            message = await receive()
            if message["type"] == "http.request":
                request.extend(message.get("body", b""))
            return message

        async def outgoing(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                response.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, incoming, outgoing)
        finally:
            assert len(request) < 2_000_000 and len(response) < 4_000_000
            with self.path.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "path": scope["path"],
                            "status": status,
                            "request_utf8": request.decode(),
                            "response_utf8": response.decode(),
                        }
                    )
                    + "\n"
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--notifications", action="store_true")
    parser.add_argument("--live-source-observation", action="store_true")
    args = parser.parse_args()
    root = args.root
    pool = AsyncConnectionPool(
        os.environ["ADCP_PG_TEST_URL"],
        kwargs={
            "autocommit": True,
            "application_name": args.schema,
            "options": f"-csearch_path={args.schema} -cstatement_timeout=15000",
        },
        min_size=1,
        max_size=1,
        open=False,
    )
    store = PgReportingProductionStore(pool=pool, notifications=args.notifications)
    capability = ReportingWriterCapability(
        "warehouse_materialization",
        "fixture-sql",
        "jsonl",
        "canonical_digest",
        "destination",
        "immutable_location",
        "sha256",
        "conditional_create",
    )
    verifiers = {currency: verifier_for(currency, capability) for currency in ("USD", "EUR")}
    writer = CurrencyDestination(
        root / "destination.sqlite", tuple(v.key for v in verifiers.values())
    )
    registry = ReportingRevisionVerifierRegistry(tuple(verifiers.values()))
    sources, producers, offerings = {}, {}, {}
    # The runtime fairness test keeps a historical source observation. The
    # separate publication-time test selects a real UTC sample after fetch.
    # Production turns and PostgreSQL lease scheduling always use real clocks.
    source_observed_at = datetime.now(timezone.utc)
    for currency, verifier in verifiers.items():
        key = verifier.key
        source_class = LiveAccountSource if args.live_source_observation else AccountSource
        source = source_class(
            key,
            root / ("source-" + currency),
            clock=(
                (lambda: datetime.now(timezone.utc))
                if args.live_source_observation
                else (lambda: source_observed_at)
            ),
            official=True,
        )
        if args.live_source_observation:
            source.observation_log = root / "source-observations.jsonl"
        producer = ReportingProducer(
            source=source,
            store=store,
            offerings=ProducerOfferings(
                official_offering_id=source.source_id,
                publication_namespace=source.capabilities.offerings[0].publication_namespace,
                source_scope=source.capabilities.source_scope,
            ),
            object_reader=source.reader,
            revision_verifier=verifier,
            # Real clock, bounded catch-up: two actual publications per producer
            # turn keep pending work behind a materializer that serves one turn.
            # Public reads pin a delivered period while later periods provide
            # continuing production load; no scheduler call is injected.
            max_periods_per_turn=2,
            currency_resolver=lambda config, obligation: ACCOUNTS[config.account_id][0],
        )
        profile = {
            "id": key.reporting_profile,
            "version": key.definition.schema_version,
            "schema_uri": key.definition.schema_uri,
            "schema_sha256": key.definition.schema_sha256,
            "schema_dialect": key.definition.schema_dialect,
            "schema_ref_policy": key.definition.schema_ref_policy,
            "grain": "row",
            "primary_keys": ["row_id"],
            "canonicalization_id": key.canonicalization.canonicalization_id,
            "canonicalization_uri": key.canonicalization.canonicalization_uri,
            "canonicalization_sha256": key.canonicalization.canonicalization_sha256,
        }
        public_offering = ReportingDeliveryOffering.model_validate(
            {
                "offering_id": "official-" + currency,
                "feed_purpose": "billing",
                "report_definition_id": key.report_definition_id,
                "report_definition_uri": key.definition.report_definition_uri,
                "report_definition_sha256": key.definition.report_definition_sha256,
                "reporting_profile": profile,
                "schedule": {"period_duration": "PT1H", "alignment": "utc", "delivery_sla": "PT1H"},
                "supported_finality": ["official"],
                "reconciliation_mode": "consumer_receipt",
                "method": writer.delivery_methods[0].wire(),
            }
        )
        sources[currency], producers[currency] = source, producer
        offerings[currency] = ReportingProductionOffering(
            public_offering, producer, key, source.source_id
        )

    def config_for(account):
        key = verifiers[ACCOUNTS[account][0]].key
        return ReportingConfiguration(
            "shared-config",
            1,
            account,
            key.report_definition_id,
            key.reporting_profile,
            "billing",
            ReportingScheduleSpec("PT1H", "PT1H", period_anchor=START),
            "official",
            activated_at=START,
            media_buy_ids=("shared-media-buy",),
            definition=key.definition,
        )

    def binding_for(config):
        return ReportingDestinationBinding(
            config.generation_key,
            CONSUMERS[config.account_id],
            "shared-destination",
            "trusted-" + config.account_id,
            capability.method,
            capability.transport,
            capability.verification_profile,
            "consumer_receipt",
            "billing",
            400,
            START,
            capability.format,
            ("fixture-sql-v1",),
            "delivered",
        )

    def wire_for(config, binding):
        offering = offerings[ACCOUNTS[config.account_id][0]]
        key = verifiers[ACCOUNTS[config.account_id][0]].key
        return {
            "delivery_config_id": config.delivery_config_id,
            "delivery_config_version": 1,
            "offering_id": offering.offering_id,
            "active": True,
            "feed_purpose": "billing",
            "report_definition_id": key.report_definition_id,
            "reporting_profile": key.reporting_profile,
            "scope": {"media_buy_ids": list(config.media_buy_ids)},
            "coverage_requirement": "full",
            "required_finality": "official",
            "reconciliation_mode": "consumer_receipt",
            "schedule": offering.configuration_schedule(config),
            "method": writer.configuration_binding(binding).wire(),
        }

    async def resolve_account(reference, context, consumer):
        account = reference.get("account_id")
        if (
            account not in ACCOUNTS
            or reference != {"account_id": account}
            or consumer != CONSUMERS[account]
        ):
            raise ReportingReceiptError("UNAUTHORIZED")
        return account

    async def account_task(request, context, admit):
        records = []
        for entry in request["accounts"]:
            account = await resolve_account(entry["account"], context, context.caller_identity)
            config = config_for(account)
            binding = binding_for(config)
            currency = ACCOUNTS[account][0]
            sources[currency].bind_generation(config)
            writer.grant(binding)
            states = []
            for supplied in entry.get("reporting_delivery_configs", []):
                await admit(
                    ReportingConfigurationAdmission(
                        offerings[currency].offering_id,
                        config,
                        binding,
                        configuration_wire=supplied,
                    )
                )
                states.append(
                    {
                        "configuration": supplied,
                        "state": "ready",
                        "destination_ref": binding.destination_ref,
                        "validated_at": END.isoformat(),
                        "activated_at": START.isoformat(),
                        "current_coverage": {
                            "status": "full",
                            "evaluated_at": END.isoformat(),
                            "media_buy_ids": list(config.media_buy_ids),
                            "fully_covered_media_buy_ids": list(config.media_buy_ids),
                            "partially_covered_media_buy_ids": [],
                            "unsupported_media_buy_ids": [],
                            "unknown_media_buy_ids": [],
                            "package_ids": [],
                            "covered_package_ids": [],
                            "unsupported_package_ids": [],
                            "unknown_package_ids": [],
                            "limitations": [],
                        },
                    }
                )
            records.append(
                {
                    "account_id": account,
                    "brand": {"domain": account + ".advertiser.example.test"},
                    "operator": "buyer.example.test",
                    "action": "unchanged",
                    "status": "active",
                    "billing": "operator",
                    "timezone": "UTC",
                    "reporting_delivery_configs": states,
                }
            )
        return {"accounts": records}

    projection = PgReportingStatusProjection(
        store,
        consumer_status_enabled=True,
        revision_ownership=True,
        escalation=producers["USD"].escalation,
    )
    support = ReportingProductionSupport(
        ReportingMaterializerService(store, ReportingDestinationIO(registry, writer), writer),
        projection,
        offerings=tuple(offerings.values()),
        configuration_task=ReportingProductionConfigurationTask(
            account_task,
            AccountCapabilities(supported_billing=["operator"], require_operator_auth=True),
        ),
        resolve_account=resolve_account,
        poll_seconds=0.02,
    )
    # Only external source/provider configuration is restored after restart.
    # No revision, receipt, snapshot or successful work is seeded here.
    templates = {}
    for account in ACCOUNTS:
        config = config_for(account)
        sources[ACCOUNTS[account][0]].bind_generation(config)
        binding = binding_for(config)
        writer.grant(binding)
        templates[account] = wire_for(config, binding)

    async def startup():
        await pool.open(wait=True)
        await store.create_schema()
        async with pool.connection() as connection:
            initial = await (
                await connection.execute("SELECT count(*) FROM reporting_configurations")
            ).fetchone()
        await support.start()
        (root / "ready.json").write_text(
            json.dumps(
                {
                    "templates": templates,
                    "initial_configurations": initial[0],
                    "pool_size": 1,
                    "source_observed_at": (
                        None if args.live_source_observation else source_observed_at.isoformat()
                    ),
                    "live_source_observation": args.live_source_observation,
                    "python": __import__("sys").version,
                    "adcp_file": adcp.__file__,
                    "pydantic": importlib.metadata.version("pydantic"),
                    "mcp": importlib.metadata.version("mcp"),
                }
            )
        )

    async def shutdown():
        await support.aclose()
        await pool.close()
        (root / "stopped.json").write_text(
            json.dumps(
                {
                    "stopped": True,
                    "source_requests": {
                        currency: len(source.requests) for currency, source in sources.items()
                    },
                    "destination_sessions_closed": all(
                        p.opens == p.closes for p in writer.readers.values()
                    ),
                }
            )
        )

    tokens = {
        account + "-test-token": Principal(caller_identity=consumer, tenant_id="shared-tenant")
        for account, consumer in CONSUMERS.items()
    }
    serve(
        support.handler,
        name="late-account-production",
        transport="both",
        host="127.0.0.1",
        port=args.port,
        auth=BearerTokenAuth(validate_token=tokens.get),
        context_factory=auth_context_factory,
        allowed_hosts=["127.0.0.1", "localhost"],
        public_url=f"http://127.0.0.1:{args.port}",
        on_startup=[startup],
        on_shutdown=[shutdown],
        stateless_http=True,
        asgi_middleware=[(WireCapture, {"path": str(root / "wire.jsonl")})],
    )


if __name__ == "__main__":
    main()
