"""Real independent SQLite destination, mounted SDK composition, shared store vectors.

The destination is a test provider, not the development writer with a changed
eligibility flag. Its immutable rows and grants survive a new provider process.
"""

import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

from adcp.decisioning.capabilities import Account as AccountCapabilities
from adcp.reporting.fixtures import redacted_capabilities
from adcp.reporting.inline_source import (
    FileSystemStagingStore,
    InlineFetchResult,
    InlineReportingSource,
    SealedSlice,
)
from adcp.reporting.ledger import ReportingRevisionRecord, revision_content_sha256
from adcp.reporting.ledger.delivery_models import (
    ReportingDestinationBinding,
    ReportingResourceRecord,
)
from adcp.reporting.ledger.models import ReportingDeliveryEscalation
from adcp.reporting.ledger.producer import ProducerOfferings, ReportingProducer
from adcp.reporting.materializer import (
    ReportingDestinationIO,
    ReportingRevisionVerifierRegistry,
    reference_digest,
    reference_verifier,
)
from adcp.reporting.materializer.contracts import (
    ReportingDestinationLocator,
    ReportingDestinationPage,
    ReportingDestinationSession,
    ReportingWriterCapability,
    binding_fingerprint,
    failure,
)
from adcp.reporting.materializer.service import ReportingMaterializerService
from adcp.reporting.production.configuration import ReportingProductionConfigurationTask
from adcp.reporting.production.contracts import (
    ReportingProductionDestinationBinding,
    ReportingProductionMethod,
    ReportingProductionSourceBinding,
)
from adcp.reporting.production.memory import InMemoryReportingProductionStore
from adcp.reporting.production.offerings import ReportingProductionOffering
from adcp.reporting.production.service import ReportingProductionSupport
from adcp.reporting.projection.memory import InMemoryReportingStatusProjection
from adcp.reporting.receipts.errors import ReportingReceiptError
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    SourceBatchManifestReferenceV1,
    reporting_source_capabilities_sha256_v1,
)
from adcp.server.serve import create_mcp_server
from adcp.types import ReportingDeliveryOffering

from ._durable_materializer_support import DurableCase, DurableHarness
from ._generation_support import END, START, configuration, isolated_reporting_pool, obligation_for
from ._materializer_support import reference_rows
from ._reliable_support import ManualClock


class SQLiteDestination:
    production_eligible = True
    resource_retention_days = 400
    authorization_revocation_seconds = 0

    def __init__(self, path, key):
        self.path, self.key = path, key
        self.capabilities = (key.capability,)
        self.delivery_methods = (
            ReportingProductionMethod(
                key.capability,
                {
                    "pattern": key.capability.method,
                    "transport": key.capability.transport,
                    "format": key.capability.format,
                    "provider": {"domain": "fixture.example.test"},
                    "orchestration": "producer_managed",
                    "destination_modes": ["provision"],
                    "reader_compatibility": ["fixture-sql-v1"],
                },
            ),
        )
        self.opens = self.closes = self.writes = 0
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS grants (binding TEXT PRIMARY KEY)")
            c.execute(
                "CREATE TABLE IF NOT EXISTS methods"
                " (binding TEXT PRIMARY KEY, method TEXT NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, content TEXT NOT NULL)"
            )

    def grant(self, binding):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT OR IGNORE INTO grants VALUES (?)", (binding_fingerprint(binding),))
            c.execute(
                "INSERT OR IGNORE INTO methods VALUES (?,?)",
                (
                    binding_fingerprint(binding),
                    json.dumps(
                        {
                            "pattern": binding.method,
                            "transport": binding.transport,
                            "orchestration": "producer_managed",
                            "destination": {
                                "mode": "provision",
                                "provider": {"domain": "fixture.example.test"},
                                "location": "reporting/" + binding.destination_ref,
                            },
                        }
                    ),
                ),
            )

    def configuration_binding(self, binding):
        with sqlite3.connect(self.path) as c:
            row = c.execute(
                "SELECT method FROM methods JOIN grants USING(binding) WHERE binding=?",
                (binding_fingerprint(binding),),
            ).fetchone()
        if row is None:
            return None
        return ReportingProductionDestinationBinding(
            binding, self.delivery_methods[0], json.loads(row[0])
        )

    def revoke(self, binding):
        with sqlite3.connect(self.path) as c:
            c.execute("DELETE FROM grants WHERE binding=?", (binding_fingerprint(binding),))

    def resolve(self, request, *, phase, context):
        return _SQLiteSession(self, request, phase, context)


class _SQLiteSession(ReportingDestinationSession):
    def __init__(self, provider, request, phase, context):
        super().__init__(request, phase, context)
        self.provider, self.connection = provider, None

    async def _open(self):
        p = self.provider
        self.connection = sqlite3.connect(p.path)
        p.opens += 1
        if (
            self.request.verification_key != p.key
            or self.connection.execute(
                "SELECT 1 FROM grants WHERE binding=?", (self.request.binding_fingerprint,)
            ).fetchone()
            is None
        ):
            raise failure("AUTHORIZATION_DENIED")

    async def _close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.provider.closes += 1

    async def write(self, content):
        assert self.phase == "write" and self.connection is not None
        r = self.request
        path = "fixture/" + hashlib.sha256(r.external_id.encode()).hexdigest()
        row = self.connection.execute(
            "SELECT content FROM artifacts WHERE id=?", (r.external_id,)
        ).fetchone()
        if row:
            data = json.loads(row[0])
            if data["rows"] != [v.decode() for v in content.rows]:
                raise failure("WRITE_FAILED")
        else:
            resource = ReportingResourceRecord(
                "fixture_" + hashlib.sha256(r.external_id.encode()).hexdigest(),
                "warehouse_relation",
                path + "/table",
                "immutable_location",
                datetime.now(timezone.utc)
                + timedelta(days=self.provider.resource_retention_days + 1),
                reader_compatibility=content.binding.reader_compatibility,
            )
            data = {
                "rows": [v.decode() for v in content.rows],
                "resource": asdict(resource),
                "binding": r.binding_fingerprint,
                "revision": r.reporting_revision_id,
            }
            data["resource"]["expires_at"] = resource.expires_at.isoformat()
            self.connection.execute(
                "INSERT INTO artifacts VALUES (?,?)", (r.external_id, json.dumps(data))
            )
            self.connection.commit()
            self.provider.writes += 1
        resource = dict(data["resource"])
        resource["expires_at"] = datetime.fromisoformat(resource["expires_at"])
        resource["object_refs"] = tuple(resource["object_refs"])
        resource["reader_compatibility"] = tuple(resource["reader_compatibility"])
        return ReportingDestinationLocator(
            r.external_id, r.binding_fingerprint, ReportingResourceRecord(**resource)
        )

    async def read_rows(self, locator, *, cursor, limit):
        assert self.phase == "readback" and self.connection is not None
        raw = self.connection.execute(
            "SELECT content FROM artifacts WHERE id=?", (locator.external_id,)
        ).fetchone()
        if raw is None or locator.external_id != self.request.external_id:
            raise failure("RESOURCE_UNAVAILABLE")
        data = json.loads(raw[0])
        if data["binding"] != self.request.binding_fingerprint:
            raise failure("AUTHORIZATION_DENIED")
        offset = 0 if cursor is None else int(cursor)
        rows = tuple(v.encode() for v in data["rows"][offset : offset + limit])
        following = offset + len(rows)
        more = following < len(data["rows"])
        return ReportingDestinationPage(
            data["revision"],
            rows,
            len(data["rows"]),
            more,
            str(following) if more else None,
            self.request.verification_key.capability.format,
            "destination",
        )


class SQLiteSeals:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS seals (account TEXT, execution TEXT,"
                " reference TEXT NOT NULL, manifest BLOB NOT NULL, PRIMARY KEY(account,execution))"
            )

    async def get(self, *, account_id, source_execution_key):
        with sqlite3.connect(self.path) as c:
            row = c.execute(
                "SELECT reference,manifest FROM seals WHERE account=? AND execution=?",
                (account_id, source_execution_key),
            ).fetchone()
        return (
            None
            if row is None
            else SealedSlice(SourceBatchManifestReferenceV1.model_validate_json(row[0]), row[1])
        )

    async def put(self, *, account_id, source_execution_key, sealed):
        with sqlite3.connect(self.path) as c:
            c.execute(
                "INSERT OR IGNORE INTO seals VALUES (?,?,?,?)",
                (
                    account_id,
                    source_execution_key,
                    sealed.reference.model_dump_json(),
                    sealed.manifest_bytes,
                ),
            )
        return await self.get(account_id=account_id, source_execution_key=source_execution_key)


class Source:
    def __init__(
        self,
        key,
        path,
        rows=None,
        *,
        clock=None,
        product_ids=("catalog-7391", "catalog-5820"),
        official=False,
    ):
        raw = redacted_capabilities().model_dump(mode="json")
        raw["offerings"] = [raw["offerings"][int(official)]]
        self.source_id = raw["offerings"][0]["offering_id"]
        if official:
            raw["offerings"][0].update(
                grain="fixed_window",
                source_timezone="UTC",
                source_local_ready_time="00:00",
                days_after_period_end=0,
                expected_availability_lag="PT0S",
                windowing={
                    "kind": "fixed_closed_window",
                    "minimum_window": "PT1H",
                    "maximum_window": "P1D",
                    "overlapping_windows_supported": False,
                },
            )
        raw["offerings"][0]["product_ids"] = list(product_ids)
        raw["offerings"][0]["contract"] = {
            "report_definition_id": key.report_definition_id,
            "reporting_profile": key.reporting_profile,
            **{
                k: getattr(key.definition, k)
                for k in (
                    "report_definition_uri",
                    "report_definition_sha256",
                    "schema_version",
                    "schema_uri",
                    "schema_sha256",
                    "schema_dialect",
                    "schema_ref_policy",
                )
            },
        }
        raw["offerings"][0]["worst_case_availability_lag"] = "PT1H"
        raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
        self.capabilities = ReportingSourceCapabilitiesV1.model_validate(raw)
        self.bindings = {}
        self.rows, self.requests = rows, []
        self.inline = InlineReportingSource(
            capabilities=self.capabilities,
            fetch=self.fetch,
            staging=FileSystemStagingStore(path.with_suffix(".staging")),
            seals=SQLiteSeals(path.with_suffix(".seals")),
            constituent_of=lambda row, req: req.coverage.constituents[0].constituent_id,
            clock=clock or (lambda: END),
        )
        self.reader = self.inline.staging

    def bind_generation(self, configuration, *, product_id="catalog-7391"):
        binding = ReportingProductionSourceBinding.for_configuration(
            configuration,
            capabilities_sha256=self.capabilities.capabilities_sha256,
            media_buy_products=tuple(
                (media_buy_id, product_id) for media_buy_id in configuration.media_buy_ids
            ),
        )
        self.bindings[configuration.generation_key] = binding
        return binding

    def configuration_binding(self, configuration):
        return self.bindings.get(configuration.generation_key)

    async def fetch(self, request):
        assert self.rows is not None, "this seeded publication needs no new acquisition"
        self.requests.append(request)
        return InlineFetchResult(self.rows, data_through=request.period.end, currency="USD")

    async def execute(self, request, *, cancel, heartbeat=None):
        return await self.inline.execute(request, cancel=cancel, heartbeat=heartbeat)


async def account_task(request, context, admit):
    return {"accounts": []}


@asynccontextmanager
async def production_harness(
    backend,
    path,
    *,
    notifications=False,
    count=503,
    second_source=False,
    source_publication=False,
    notification_delivery=False,
    periods=None,
    existing_store=None,
    existing_pool=None,
    source_bindings=(),
    account_handler=None,
    reconciled=False,
    feedback=False,
    identity_prefix="",
    poll_seconds=60,
    source_factory=Source,
    adcp_version=None,
):
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        clock = ManualClock(datetime.now(timezone.utc))
        pool = existing_pool
        if existing_store is not None:
            store = existing_store
            clock = store._clock
        elif backend == "memory":
            store = InMemoryReportingProductionStore(clock=clock, notifications=notifications)
        else:
            from adcp.reporting.production.pg import PgReportingProductionStore

            if pool is None:
                pool = await stack.enter_async_context(isolated_reporting_pool(autocommit=True))
            store = PgReportingProductionStore(pool=pool, notifications=notifications)
            await store.create_schema()
        h = DurableHarness(store, clock, pool)
        cap = ReportingWriterCapability(
            "warehouse_materialization",
            "fixture-sql",
            "jsonl",
            "canonical_digest",
            "destination",
            "immutable_location",
            "sha256",
            "conditional_create",
        )
        verifier = reference_verifier(cap)
        key = verifier.key
        config = replace(
            configuration(),
            delivery_config_id=identity_prefix + configuration().delivery_config_id,
            deactivated_at=None if periods is None else START + timedelta(hours=periods),
            definition=key.definition,
            report_definition_id=key.report_definition_id,
            feed_purpose="billing" if reconciled else "analytics",
            required_finality="official" if reconciled else "snapshot",
        )
        await store.put_configuration(config)
        original_obligation = obligation_for(config)
        obligation = await store.commit_obligation(
            replace(
                original_obligation,
                reporting_obligation_id=identity_prefix
                + original_obligation.reporting_obligation_id,
            )
        )
        binding = ReportingDestinationBinding(
            config.generation_key,
            "https://buyer.example.test/agent",
            "destination",
            "trusted-provider-binding",
            cap.method,
            cap.transport,
            cap.verification_profile,
            "consumer_receipt" if reconciled else "delivery_only",
            config.feed_purpose,
            400,
            START,
            cap.format,
            ("fixture-sql-v1",),
            "delivered",
        )
        await store.put_destination_binding(binding)
        rows = reference_rows(count)
        _, totals = verifier.canonicalize(rows)
        pairs = tuple((t.name, t.value) for t in totals)
        revision = ReportingRevisionRecord(
            identity_prefix + "production-revision",
            config.account_id,
            obligation.reporting_obligation_id,
            config.required_finality,
            revision_content_sha256(
                reporting_revision_id=identity_prefix + "production-revision",
                row_count=count,
                control_totals=pairs,
                reporting_rows=rows,
                control_total_evidence=totals,
            ),
            count,
            pairs,
            END,
            END,
            END,
            finality_basis="source_final" if reconciled else None,
            finality_policy_id="fixture-official-v1" if reconciled else None,
            finalized_at=END if reconciled else None,
            canonical_content_digest=reference_digest(verifier, rows),
            managed_control_totals=totals,
        )
        if not source_publication:
            await store.commit_revision(revision, rows)
        writer = SQLiteDestination(path, key)
        writer.grant(binding)
        registry = ReportingRevisionVerifierRegistry((verifier,))
        io = ReportingDestinationIO(registry, writer)
        item = DurableCase(
            store,
            config,
            obligation,
            binding,
            revision,
            rows,
            verifier,
            registry,
            writer,
            writer,
            io,
        )
        source_clock = ManualClock(END)
        source = source_factory(
            key,
            path.with_name("source"),
            rows if source_publication or periods is not None else None,
            clock=source_clock,
            official=reconciled,
        )
        if not second_source:
            source.bind_generation(config)
        escalation = ReportingDeliveryEscalation()
        producer = ReportingProducer(
            source=source,
            offerings=ProducerOfferings(
                snapshot_offering_id=None if reconciled else source.source_id,
                official_offering_id=(
                    source.source_id if reconciled else getattr(source, "official_source_id", None)
                ),
                publication_namespace=source.capabilities.offerings[0].publication_namespace,
                source_scope=source.capabilities.source_scope,
            ),
            store=store,
            escalation=escalation,
            clock=source_clock,
            revision_verifier=verifier,
            object_reader=source.reader,
        )
        if pool is None:
            projection = InMemoryReportingStatusProjection(
                store,
                revision_ownership=True,
                escalation=escalation,
                consumer_status_enabled=feedback,
            )
        else:
            from adcp.reporting.projection.pg import PgReportingStatusProjection

            projection = PgReportingStatusProjection(
                store,
                revision_ownership=True,
                escalation=escalation,
                consumer_status_enabled=feedback,
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
        offering = ReportingDeliveryOffering.model_validate(
            {
                "offering_id": "reconciled-fixture" if reconciled else "managed-fixture",
                "feed_purpose": config.feed_purpose,
                "report_definition_id": key.report_definition_id,
                "report_definition_uri": key.definition.report_definition_uri,
                "report_definition_sha256": key.definition.report_definition_sha256,
                "reporting_profile": profile,
                "schedule": {
                    "period_duration": "PT1H",
                    "alignment": "utc",
                    "delivery_sla": "PT1H",
                },
                "supported_finality": [config.required_finality],
                "reconciliation_mode": binding.reconciliation_mode,
                "method": writer.delivery_methods[0].wire(),
            }
        )
        admitted = ReportingProductionOffering(offering, producer, key, source.source_id)
        offerings = (admitted,)
        if second_source:
            other = Source(key, path.with_name("other-source"))
            other_producer = ReportingProducer(
                source=other,
                offerings=producer._offerings,
                store=store,
                escalation=escalation,
                clock=lambda: END,
                revision_verifier=verifier,
                object_reader=other.reader,
            )
            offerings += (
                ReportingProductionOffering(
                    offering.model_copy(update={"offering_id": "managed-other"}),
                    other_producer,
                    key,
                    other.source_id,
                ),
            )

        for configured, offering_id, product_id in source_bindings:
            selected = next(o for o in offerings if o.offering_id == offering_id)
            selected.producer._source.bind_generation(configured, product_id=product_id)

        h.authorized_bindings = {(config.account_id, binding.consumer_id)}

        async def authorize(account, context, consumer):
            account_id = account.get("account_id")
            if (
                account != {"account_id": account_id}
                or (account_id, consumer) not in h.authorized_bindings
            ):
                raise ReportingReceiptError("UNAUTHORIZED")
            return account_id

        workers = ()
        if notification_delivery:
            from adcp.reporting.outbox.routing import ReportingEnvelopeCipher
            from adcp.reporting.production.notifications import (
                ReportingProductionSigning,
                production_notification_workers,
            )

            from ._reliable_support import FailurePlan, ScriptedSigning, ScriptedSubscriptions

            h.notification_failures = FailurePlan()
            h.subscriptions = ScriptedSubscriptions(h.notification_failures)
            h.signing = ScriptedSigning(h.notification_failures)
            workers = production_notification_workers(
                store,
                projection,
                subscriptions=h.subscriptions,
                signing=ReportingProductionSigning(
                    h.signing,
                    ("ed25519",),
                    brand_json_url="https://seller.example.test/brand.json",
                ),
                cipher=ReportingEnvelopeCipher(b"b" * 32),
            )
        support = ReportingProductionSupport(
            ReportingMaterializerService(store, io, writer),
            projection,
            offerings=offerings,
            configuration_task=ReportingProductionConfigurationTask(
                account_handler or account_task,
                AccountCapabilities(supported_billing=["operator"], require_operator_auth=True),
            ),
            resolve_account=authorize,
            notification_workers=workers,
            poll_seconds=poll_seconds,
            adcp_version=adcp_version,
        )
        h.production, h.projection, h.item = support, projection, item
        h.source_clock = source_clock
        h.mount = create_mcp_server(support.handler)
        try:
            await support.start()
            yield h
        finally:
            await support.aclose()
