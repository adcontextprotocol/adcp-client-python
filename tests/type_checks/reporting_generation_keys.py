"""Account-qualified reporting identity without changing low-level adopter calls."""

from __future__ import annotations

from datetime import datetime

from typing_extensions import assert_type

from adcp.reporting.ledger import (
    ConsumerStatusRecord,
    LeasedConfiguration,
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingLedgerStore,
    ReportingObligationRecord,
)


async def inspect_generation(
    store: ReportingLedgerStore,
    configuration: ReportingConfiguration,
    obligation: ReportingObligationRecord,
    status: ConsumerStatusRecord,
    lease: LeasedConfiguration,
    now: datetime,
) -> None:
    key = assert_type(configuration.generation_key, ReportingConfigurationGenerationKey)
    assert_type(obligation.generation_key, ReportingConfigurationGenerationKey)
    assert_type(status.generation_key, ReportingConfigurationGenerationKey)
    assert_type(lease.generation_key, ReportingConfigurationGenerationKey)
    assert_type(key.account_id, str)
    assert_type(key.delivery_config_id, str)
    assert_type(key.delivery_config_version, int)
    generations: dict[ReportingConfigurationGenerationKey, ReportingConfiguration] = {
        key: configuration
    }
    assert_type(generations.get(obligation.generation_key), ReportingConfiguration | None)
    assert_type(
        await store.find_obligation(
            account_id=key.account_id,
            delivery_config_id=key.delivery_config_id,
            delivery_config_version=key.delivery_config_version,
            period_start=obligation.period.start,
            period_end=obligation.period.end,
        ),
        ReportingObligationRecord | None,
    )
    assert_type(
        await store.lease_period_close(worker_id="worker", now=now, lease_seconds=60),
        LeasedConfiguration | None,
    )
    # The beta.15 lease constructor and release call remain valid.
    retained_handle = LeasedConfiguration(
        key.account_id, key.delivery_config_id, key.delivery_config_version, lease.lease_expires_at
    )
    await store.release_period_close(retained_handle, worker_id="worker")
