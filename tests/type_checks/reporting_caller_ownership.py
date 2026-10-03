"""The breaking owned-generation and operator maintenance APIs are public."""

from datetime import datetime
from typing import Any

from typing_extensions import assert_type

from adcp.reporting.ledger import (
    LeasedConfiguration,
    ReportingCaller,
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingLedgerStore,
    ReportingStatusCaller,
)
from adcp.reporting.migration import (
    ReportingOwnershipBackfill,
    backfill_legacy_reporting,
    legacy_generation_digest,
    migrate_legacy_reporting,
)


async def ownership(
    store: ReportingLedgerStore,
    connection: Any,
    configuration: ReportingConfiguration,
    now: datetime,
) -> None:
    caller: ReportingCaller = ReportingStatusCaller(
        configuration.account_id, configuration.consumer_id
    )
    assert_type(await store.list_configurations(caller=caller), tuple[ReportingConfiguration, ...])
    key = ReportingConfigurationGenerationKey(caller.account_id, caller.consumer_id, "daily", 1)
    assert_type(key.consumer_id, str)
    lease = LeasedConfiguration(
        key.account_id, key.consumer_id, key.delivery_config_id, key.delivery_config_version, now
    )
    assert_type(lease.generation_key, ReportingConfigurationGenerationKey)
    await migrate_legacy_reporting(
        connection, archive_schema="adcp_reporting_quarantine_release", workers_stopped=True
    )
    digest = await legacy_generation_digest(
        connection,
        archive_schema="adcp_reporting_quarantine_release",
        account_id=caller.account_id,
        delivery_config_id="daily",
        delivery_config_version=1,
    )
    assert_type(digest, str)
    mapping = ReportingOwnershipBackfill(
        caller.account_id,
        "daily",
        1,
        caller.consumer_id,
        "ownership-register-42",
        digest,
        "retain_without_replay",
    )
    await backfill_legacy_reporting(
        connection,
        archive_schema="adcp_reporting_quarantine_release",
        mapping=mapping,
        workers_stopped=True,
    )
