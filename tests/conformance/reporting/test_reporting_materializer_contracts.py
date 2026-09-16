"""Public closed contracts, URL consumers, import safety and real capability gates."""

import asyncio
import importlib
import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import get_args, get_type_hints

import pytest
from pydantic import TypeAdapter

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    ProducerOfferings,
    ReportingDeliveryPrincipal,
    ReportingProducer,
)
from adcp.reporting.ledger.delivery_models import MaterializationFailure
from adcp.reporting.materializer import (
    ReferenceReportingDestinationWriter,
    ReportingDestinationPage,
    ReportingIOContext,
    ReportingWriterError,
    ReportingWriterFailure,
    ReportingWriterFailureCode,
    reference_verifier,
)

from ._generation_support import UncalledSource
from ._materializer_support import io_context, materializer_case
from ._reliable_support import NotificationHarness, reliable_factory


@pytest.mark.parametrize(
    "key",
    [
        "managed_delivery",
        "reconciled_billing",
        "reporting.delivery_ready",
        "readiness_notification",
        "status_notification",
        "ledger_notification",
        "supports_webhook_activity",
        "consumer_status_task",
        "receipt_task",
        "reconciliation_task",
        "status_task",
        "delivery_task",
    ],
)
def test_producer_extra_cannot_inject_sdk_owned_readiness(key):
    producer = ReportingProducer(
        source=UncalledSource(), offerings=ProducerOfferings(), store=InMemoryReportingLedgerStore()
    )
    with pytest.raises(ValueError, match="SDK-owned"):
        producer.advertised_reporting_delivery(
            consumer_status_task=False,
            offerings=(),
            automated_recovery_window=timedelta(hours=1),
            status_retention_days=30,
            extra={key: True},
        )


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_real_outbox_capability_path_never_promotes_a_verified_reference_writer(backend):
    async with reliable_factory(backend, notifications=True) as h:
        case = await materializer_case(store=h.store)
        locator = await case.io.write(case.prepared, context=io_context())
        await case.io.verify(case.prepared, locator, context=io_context())
        fields = (
            await NotificationHarness(h)
            .worker()
            .advertised_notifications(h.store, account_id="acct_a", ready_scope=case.delivery.scope)
        )
        assert fields == {
            "ledger_notification": "reporting.ledger_changed",
            "supports_webhook_activity": False,
        }
        assert not set(fields).intersection(
            {
                "managed_delivery",
                "reconciled_billing",
                "reporting.delivery_ready",
                "readiness_notification",
            }
        )
        assert case.writer.production_eligible is False


async def test_url_consumer_round_trips_all_binding_resolver_reconciliation_and_delivery_paths():
    case = await materializer_case()
    principal = case.binding.principal
    assert "://" in principal.consumer_id
    assert (
        TypeAdapter(ReportingDeliveryPrincipal).validate_json(
            TypeAdapter(ReportingDeliveryPrincipal).dump_json(principal)
        )
        == principal
    )
    frozen = await case.store.get_destination_binding(
        caller=principal, generation_key=case.binding.generation_key
    )
    assert frozen == case.binding
    assert case.prepared.request.principal == principal
    assert (await case.store.get_obligation_delivery(case.delivery.scope)) == case.delivery
    page = await case.store.read_reconciliation_changes(caller=principal)
    assert page.caller == principal and page.changes
    snapshot = await case.store.read_status_snapshot(account_id=principal.account_id)
    assert principal.consumer_id in snapshot.consumer_ids
    locator = await case.io.write(case.prepared, context=io_context())
    verified = await case.io.verify(case.prepared, locator, context=io_context())
    assert verified.request.principal == principal


def test_new_failures_do_not_change_reviewed_persisted_failure_enum():
    assert get_args(MaterializationFailure) == (
        "WRITE_FAILED",
        "VERIFICATION_FAILED",
        "CONTENT_CORRUPT",
        "RESOURCE_UNAVAILABLE",
    )
    assert "CURRENT_REVISION_CHANGED" in get_args(ReportingWriterFailureCode)
    assert "CURRENT_REVISION_CHANGED" not in get_args(MaterializationFailure)
    for code in get_args(ReportingWriterFailureCode):
        assert str(ReportingWriterError(ReportingWriterFailure(code))) == code
    for changes in (
        {"code": "provider prose"},
        {"retry": "eventually"},
        {"effect": "probably_written"},
        {"retry_after_seconds": True},
        {"retry_after_seconds": -1},
    ):
        with pytest.raises(ValueError):
            ReportingWriterFailure(**{"code": "WRITE_FAILED", **changes})


def test_reference_writer_production_flag_is_not_configurable_or_subclass_promotable():
    with pytest.raises(TypeError):
        ReferenceReportingDestinationWriter((), production_eligible=True)
    writer = ReferenceReportingDestinationWriter(())
    with pytest.raises(AttributeError):
        writer.production_eligible = True
    with pytest.raises(TypeError):
        type(
            "PromotedReference",
            (ReferenceReportingDestinationWriter,),
            {"production_eligible": True},
        )


def test_readback_page_repr_never_includes_unverified_provider_body():
    body = b"https://provider.example.test/private?token=credential-sentinel"
    page = ReportingDestinationPage("revision", (body,), 1, False, None, "jsonl", "producer")
    assert "credential-sentinel" not in str(page) + repr(page)
    assert "https://provider" not in str(page) + repr(page)


@pytest.mark.parametrize(
    "field", ["report_definition_uri", "schema_uri", "schema_dialect", "schema_version"]
)
def test_public_verification_key_rejects_credentials_in_every_definition_coordinate(field):
    verifier = reference_verifier()
    with pytest.raises(ValueError) as caught:
        replace(
            verifier.key,
            definition=replace(
                verifier.key.definition,
                **{field: "https://provider.example.test/data?token=do-not-expose"},
            ),
        )
    assert "do-not-expose" not in str(caught.value) + repr(caught.value)


async def test_service_heartbeat_is_a_checkpoint_only_and_fences_before_source_read():
    case = await materializer_case()
    calls = 0

    class Heartbeat:
        async def checkpoint(self):
            nonlocal calls
            calls += 1
            raise ReportingWriterError(
                ReportingWriterFailure("LEASE_LOST", "same_identity", "not_started")
            )

    context = ReportingIOContext(
        datetime.now(timezone.utc) + timedelta(seconds=10), asyncio.Event(), Heartbeat()
    )
    with pytest.raises(ReportingWriterError, match="LEASE_LOST"):
        await case.prepare(context=context)
    assert calls == 1 and case.writer.open_count == 0


def test_curated_all_exports_resolve_and_do_not_add_materializer_sql():
    for name in (
        "adcp.reporting.materializer",
        "adcp.reporting.revision_selection",
        "adcp.reporting.ledger",
        "adcp.reporting.outbox",
    ):
        module = importlib.import_module(name)
        assert len(module.__all__) == len(set(module.__all__))
        for public in module.__all__:
            assert getattr(module, public) is not None
    module = importlib.import_module("adcp.reporting.materializer")
    assert not any(
        name.endswith(("Coordinator", "Service", "Lease", "WorkQueue")) for name in module.__all__
    )
    assert inspect.isclass(module.ReportingDestinationSession)
    assert get_type_hints(module.ReportingDestinationSession.write)["content"] is (
        module.ReportingPreparedRevision
    )
