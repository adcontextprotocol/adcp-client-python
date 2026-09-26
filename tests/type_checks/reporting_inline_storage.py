"""Borrowed pool composition with the existing public source protocols."""

from psycopg_pool import AsyncConnectionPool

from adcp.reporting.inline_source import (
    InlineFetch,
    InlineReportingSource,
    ReportingSealStore,
    ReportingStagingStore,
)
from adcp.reporting.inline_storage import PgReportingSealStore, PgReportingStagingStore
from adcp.reporting.source import ReportingSourceCapabilitiesV1, ReportingSourceExecutor


async def configure(
    pool: AsyncConnectionPool,
    capabilities: ReportingSourceCapabilitiesV1,
    fetch: InlineFetch,
) -> ReportingSourceExecutor:
    staging = PgReportingStagingStore(pool=pool, max_payload_bytes=1_048_576)
    seals = PgReportingSealStore(pool=pool)
    await staging.create_schema()
    await seals.check_ready()
    objects: ReportingStagingStore = staging
    replay: ReportingSealStore = seals
    return InlineReportingSource(
        capabilities=capabilities, fetch=fetch, staging=objects, seals=replay
    )
