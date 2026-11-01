"""PostgreSQL and object-storage implementations of the AdCP Reliable Reporting stores.

``adcp`` defines the store protocols, the in-memory stores, the producer, the
handlers and the row encoding. This distribution implements those protocols
over PostgreSQL and, for revision rows, over a filesystem or an object store
(Google Cloud Storage, Amazon S3, Azure Blob Storage). It is versioned on its
own line so a storage fix never has to ship as a protocol-client release.

What you assemble::

    from psycopg_pool import AsyncConnectionPool
    from adcp.reporting.ledger import ProducerOfferings, ReportingProducer
    from adcp_reporting_storage import PgReportingLedgerStore

    store = PgReportingLedgerStore(pool=pool)
    await store.create_schema()
    producer = ReportingProducer(source=..., offerings=..., store=store)

or, for the adapter-first service::

    from adcp_reporting_storage import postgres_reliable_reporting_service

    reporting = postgres_reliable_reporting_service(pool=pool, account_context=...)

Names are resolved lazily (:pep:`562`): importing this package loads no driver
and no cloud SDK until the first store or provider is touched. The layout
mirrors ``adcp.reporting`` (``adcp_reporting_storage.ledger.pg`` is where
``PgReportingLedgerStore`` is defined), so a deep import is a supported
address too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    __version__: str
    from adcp_reporting_storage.contract import STORAGE_CONTRACT as STORAGE_CONTRACT
    from adcp_reporting_storage.contract import (
        require_adcp_contract as require_adcp_contract,
    )
    from adcp_reporting_storage.feed.pg import PgReportingFeedStore as PgReportingFeedStore
    from adcp_reporting_storage.inline_storage import (
        PgReportingSealStore as PgReportingSealStore,
    )
    from adcp_reporting_storage.inline_storage import (
        PgReportingStagingStore as PgReportingStagingStore,
    )
    from adcp_reporting_storage.ledger.change_feed import (
        ReportingChangeCursorExpiredError as ReportingChangeCursorExpiredError,
    )
    from adcp_reporting_storage.ledger.change_feed import (
        ReportingChangeKindV1 as ReportingChangeKindV1,
    )
    from adcp_reporting_storage.ledger.change_feed import (
        ReportingChangeRecordV1 as ReportingChangeRecordV1,
    )
    from adcp_reporting_storage.ledger.change_feed import (
        ReportingChangesPageV1 as ReportingChangesPageV1,
    )
    from adcp_reporting_storage.ledger.delivery_pg import (
        PgReportingReconciliationStore as PgReportingReconciliationStore,
    )
    from adcp_reporting_storage.ledger.period_retirement import (
        ReportingPeriodRetirementV1 as ReportingPeriodRetirementV1,
    )
    from adcp_reporting_storage.ledger.pg import (
        PgReportingLedgerStore as PgReportingLedgerStore,
    )
    from adcp_reporting_storage.ledger.row_migration import (
        ReportingRowMigrationBatchV1 as ReportingRowMigrationBatchV1,
    )
    from adcp_reporting_storage.ledger.row_migration import (
        ReportingRowQuarantineReason as ReportingRowQuarantineReason,
    )
    from adcp_reporting_storage.ledger.row_retention import (
        ReportingRowIntentSweepV1 as ReportingRowIntentSweepV1,
    )
    from adcp_reporting_storage.ledger.row_retention import (
        ReportingRowPruneV1 as ReportingRowPruneV1,
    )
    from adcp_reporting_storage.ledger.row_storage import (
        ReportingObjectRowBindingV1 as ReportingObjectRowBindingV1,
    )
    from adcp_reporting_storage.ledger.row_storage import (
        ReportingPostgresRowBindingV1 as ReportingPostgresRowBindingV1,
    )
    from adcp_reporting_storage.ledger.row_storage import (
        ReportingRowBindingSelectionInputV1 as ReportingRowBindingSelectionInputV1,
    )
    from adcp_reporting_storage.ledger.row_storage import (
        ReportingRowStorageOptionsV1 as ReportingRowStorageOptionsV1,
    )
    from adcp_reporting_storage.ledger.row_storage import (
        ReportingRowStorageV1 as ReportingRowStorageV1,
    )
    from adcp_reporting_storage.ledger.row_storage_azure import (
        create_azure_blob_reporting_row_object_provider_v1 as create_azure_blob_reporting_row_object_provider_v1,  # noqa: E501
    )
    from adcp_reporting_storage.ledger.row_storage_gcs import (
        assert_gcs_reporting_row_bucket_policy_v1 as assert_gcs_reporting_row_bucket_policy_v1,
    )
    from adcp_reporting_storage.ledger.row_storage_gcs import (
        create_gcs_reporting_row_object_provider_v1 as create_gcs_reporting_row_object_provider_v1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        ReportingRowObjectContextV1 as ReportingRowObjectContextV1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        ReportingRowObjectDeleteInputV1 as ReportingRowObjectDeleteInputV1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        ReportingRowObjectGetInputV1 as ReportingRowObjectGetInputV1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        ReportingRowObjectProviderV1 as ReportingRowObjectProviderV1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        ReportingRowObjectPutInputV1 as ReportingRowObjectPutInputV1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        ReportingRowObjectPutResultV1 as ReportingRowObjectPutResultV1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        ReportingRowObjectStatInputV1 as ReportingRowObjectStatInputV1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        ReportingRowObjectStatResultV1 as ReportingRowObjectStatResultV1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        create_filesystem_reporting_row_object_provider_v1 as create_filesystem_reporting_row_object_provider_v1,  # noqa: E501
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        render_reporting_row_object_key_v1 as render_reporting_row_object_key_v1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        reporting_period_date_v1 as reporting_period_date_v1,
    )
    from adcp_reporting_storage.ledger.row_storage_object import (
        run_reporting_row_object_provider_conformance_v1 as run_reporting_row_object_provider_conformance_v1,  # noqa: E501
    )
    from adcp_reporting_storage.ledger.row_storage_s3 import (
        create_s3_reporting_row_object_provider_v1 as create_s3_reporting_row_object_provider_v1,
    )
    from adcp_reporting_storage.materializer.pg import (
        PgReportingMaterializerStore as PgReportingMaterializerStore,
    )
    from adcp_reporting_storage.migration import (
        ReportingOwnershipBackfill as ReportingOwnershipBackfill,
    )
    from adcp_reporting_storage.migration import (
        ReportingOwnershipMigrationError as ReportingOwnershipMigrationError,
    )
    from adcp_reporting_storage.migration import (
        backfill_legacy_reporting as backfill_legacy_reporting,
    )
    from adcp_reporting_storage.migration import (
        legacy_generation_digest as legacy_generation_digest,
    )
    from adcp_reporting_storage.migration import (
        migrate_legacy_reporting as migrate_legacy_reporting,
    )
    from adcp_reporting_storage.migration import (
        replace_empty_legacy_reporting as replace_empty_legacy_reporting,
    )
    from adcp_reporting_storage.migration import (
        replace_empty_legacy_reporting_sync as replace_empty_legacy_reporting_sync,
    )
    from adcp_reporting_storage.migration import (
        replace_empty_owned_reporting as replace_empty_owned_reporting,
    )
    from adcp_reporting_storage.migration import (
        replace_empty_owned_reporting_sync as replace_empty_owned_reporting_sync,
    )
    from adcp_reporting_storage.migration import (
        require_owned_schema_or_empty as require_owned_schema_or_empty,
    )
    from adcp_reporting_storage.outbox.pg import PgReportingOutbox as PgReportingOutbox
    from adcp_reporting_storage.outbox.status_activity_pg import (
        PgReportingActivityUnionStore as PgReportingActivityUnionStore,
    )
    from adcp_reporting_storage.outbox.status_pg import (
        PgReportingStatusOutbox as PgReportingStatusOutbox,
    )
    from adcp_reporting_storage.outbox.status_pg import (
        PgStatusNotificationStore as PgStatusNotificationStore,
    )
    from adcp_reporting_storage.production.pg import (
        PgReportingProductionOutbox as PgReportingProductionOutbox,
    )
    from adcp_reporting_storage.production.pg import (
        PgReportingProductionStore as PgReportingProductionStore,
    )
    from adcp_reporting_storage.projection.notifications import (
        PgReportingProjectionOutbox as PgReportingProjectionOutbox,
    )
    from adcp_reporting_storage.projection.pg import (
        PgReportingProjectionStore as PgReportingProjectionStore,
    )
    from adcp_reporting_storage.projection.pg import (
        PgReportingStatusProjection as PgReportingStatusProjection,
    )
    from adcp_reporting_storage.receipts.pg import (
        PgReportingReceiptStore as PgReportingReceiptStore,
    )
    from adcp_reporting_storage.service import (
        postgres_reliable_reporting_service as postgres_reliable_reporting_service,
    )
    from adcp_reporting_storage.submissions.pg import (
        PgReportingSubmissionIntentStore as PgReportingSubmissionIntentStore,
    )
    from adcp_reporting_storage.subscriptions.pg import (
        PgPrincipalRecordStore as PgPrincipalRecordStore,
    )
    from adcp_reporting_storage.subscriptions.pg import (
        PgReportingSubscriptionStateStore as PgReportingSubscriptionStateStore,
    )

#: Public name -> (module under this package, attribute). Resolved on first use.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # The storage contract shared with adcp (see ``contract``)
    "STORAGE_CONTRACT": ("contract", "STORAGE_CONTRACT"),
    "require_adcp_contract": ("contract", "require_adcp_contract"),
    # Stores
    "PgReportingLedgerStore": ("ledger.pg", "PgReportingLedgerStore"),
    "PgReportingReconciliationStore": ("ledger.delivery_pg", "PgReportingReconciliationStore"),
    "PgReportingMaterializerStore": ("materializer.pg", "PgReportingMaterializerStore"),
    "PgReportingReceiptStore": ("receipts.pg", "PgReportingReceiptStore"),
    "PgReportingFeedStore": ("feed.pg", "PgReportingFeedStore"),
    "PgPrincipalRecordStore": ("subscriptions.pg", "PgPrincipalRecordStore"),
    "PgReportingSubscriptionStateStore": (
        "subscriptions.pg",
        "PgReportingSubscriptionStateStore",
    ),
    "PgReportingProjectionStore": ("projection.pg", "PgReportingProjectionStore"),
    "PgReportingStatusProjection": ("projection.pg", "PgReportingStatusProjection"),
    "PgReportingProjectionOutbox": ("projection.notifications", "PgReportingProjectionOutbox"),
    "PgReportingProductionStore": ("production.pg", "PgReportingProductionStore"),
    "PgReportingProductionOutbox": ("production.pg", "PgReportingProductionOutbox"),
    "PgReportingOutbox": ("outbox.pg", "PgReportingOutbox"),
    "PgReportingStatusOutbox": ("outbox.status_pg", "PgReportingStatusOutbox"),
    "PgStatusNotificationStore": ("outbox.status_pg", "PgStatusNotificationStore"),
    "PgReportingActivityUnionStore": ("outbox.status_activity_pg", "PgReportingActivityUnionStore"),
    "PgReportingSubmissionIntentStore": ("submissions.pg", "PgReportingSubmissionIntentStore"),
    "PgReportingStagingStore": ("inline_storage", "PgReportingStagingStore"),
    "PgReportingSealStore": ("inline_storage", "PgReportingSealStore"),
    # The adapter-first service over PostgreSQL
    "postgres_reliable_reporting_service": ("service", "postgres_reliable_reporting_service"),
    # Caller-ownership migration
    "ReportingOwnershipBackfill": ("migration", "ReportingOwnershipBackfill"),
    "ReportingOwnershipMigrationError": ("migration", "ReportingOwnershipMigrationError"),
    "backfill_legacy_reporting": ("migration", "backfill_legacy_reporting"),
    "legacy_generation_digest": ("migration", "legacy_generation_digest"),
    "migrate_legacy_reporting": ("migration", "migrate_legacy_reporting"),
    "replace_empty_legacy_reporting": ("migration", "replace_empty_legacy_reporting"),
    "replace_empty_legacy_reporting_sync": ("migration", "replace_empty_legacy_reporting_sync"),
    "replace_empty_owned_reporting": ("migration", "replace_empty_owned_reporting"),
    "replace_empty_owned_reporting_sync": ("migration", "replace_empty_owned_reporting_sync"),
    "require_owned_schema_or_empty": ("migration", "require_owned_schema_or_empty"),
    # Host change feed
    "ReportingChangeCursorExpiredError": (
        "ledger.change_feed",
        "ReportingChangeCursorExpiredError",
    ),
    "ReportingChangeKindV1": ("ledger.change_feed", "ReportingChangeKindV1"),
    "ReportingChangeRecordV1": ("ledger.change_feed", "ReportingChangeRecordV1"),
    "ReportingChangesPageV1": ("ledger.change_feed", "ReportingChangesPageV1"),
    # Row storage: options, bindings, maintenance results
    "ReportingRowStorageV1": ("ledger.row_storage", "ReportingRowStorageV1"),
    "ReportingRowStorageOptionsV1": ("ledger.row_storage", "ReportingRowStorageOptionsV1"),
    "ReportingObjectRowBindingV1": ("ledger.row_storage", "ReportingObjectRowBindingV1"),
    "ReportingPostgresRowBindingV1": ("ledger.row_storage", "ReportingPostgresRowBindingV1"),
    "ReportingRowBindingSelectionInputV1": (
        "ledger.row_storage",
        "ReportingRowBindingSelectionInputV1",
    ),
    "ReportingRowIntentSweepV1": ("ledger.row_retention", "ReportingRowIntentSweepV1"),
    "ReportingRowPruneV1": ("ledger.row_retention", "ReportingRowPruneV1"),
    "ReportingPeriodRetirementV1": ("ledger.period_retirement", "ReportingPeriodRetirementV1"),
    "ReportingRowMigrationBatchV1": ("ledger.row_migration", "ReportingRowMigrationBatchV1"),
    "ReportingRowQuarantineReason": ("ledger.row_migration", "ReportingRowQuarantineReason"),
    # Row object providers: the contract, the filesystem reference, the cloud factories
    "ReportingRowObjectProviderV1": ("ledger.row_storage_object", "ReportingRowObjectProviderV1"),
    "ReportingRowObjectContextV1": ("ledger.row_storage_object", "ReportingRowObjectContextV1"),
    "ReportingRowObjectPutInputV1": ("ledger.row_storage_object", "ReportingRowObjectPutInputV1"),
    "ReportingRowObjectPutResultV1": ("ledger.row_storage_object", "ReportingRowObjectPutResultV1"),
    "ReportingRowObjectGetInputV1": ("ledger.row_storage_object", "ReportingRowObjectGetInputV1"),
    "ReportingRowObjectDeleteInputV1": (
        "ledger.row_storage_object",
        "ReportingRowObjectDeleteInputV1",
    ),
    "ReportingRowObjectStatInputV1": ("ledger.row_storage_object", "ReportingRowObjectStatInputV1"),
    "ReportingRowObjectStatResultV1": (
        "ledger.row_storage_object",
        "ReportingRowObjectStatResultV1",
    ),
    "render_reporting_row_object_key_v1": (
        "ledger.row_storage_object",
        "render_reporting_row_object_key_v1",
    ),
    "reporting_period_date_v1": ("ledger.row_storage_object", "reporting_period_date_v1"),
    "create_filesystem_reporting_row_object_provider_v1": (
        "ledger.row_storage_object",
        "create_filesystem_reporting_row_object_provider_v1",
    ),
    "run_reporting_row_object_provider_conformance_v1": (
        "ledger.row_storage_object",
        "run_reporting_row_object_provider_conformance_v1",
    ),
    "create_gcs_reporting_row_object_provider_v1": (
        "ledger.row_storage_gcs",
        "create_gcs_reporting_row_object_provider_v1",
    ),
    "assert_gcs_reporting_row_bucket_policy_v1": (
        "ledger.row_storage_gcs",
        "assert_gcs_reporting_row_bucket_policy_v1",
    ),
    "create_s3_reporting_row_object_provider_v1": (
        "ledger.row_storage_s3",
        "create_s3_reporting_row_object_provider_v1",
    ),
    "create_azure_blob_reporting_row_object_provider_v1": (
        "ledger.row_storage_azure",
        "create_azure_blob_reporting_row_object_provider_v1",
    ),
}

_LAZY_SUBMODULES = frozenset(
    {
        "contract",
        "feed",
        "inline_storage",
        "ledger",
        "materializer",
        "migration",
        "outbox",
        "production",
        "projection",
        "receipts",
        "service",
        "submissions",
        "subscriptions",
    }
)

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    if name == "__version__":
        # Read the installed distribution's PEP 440 version rather than a
        # literal Release Please would rewrite in its own -beta.N spelling.
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("adcp-reporting-storage")
        except PackageNotFoundError:
            return "0.0.0"
    lazy_export = _LAZY_EXPORTS.get(name)
    if lazy_export is not None:
        import importlib

        module_name, attribute = lazy_export
        return getattr(importlib.import_module(f"{__name__}.{module_name}"), attribute)
    if name in _LAZY_SUBMODULES:
        import importlib

        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__) | _LAZY_SUBMODULES | {"__version__"})
