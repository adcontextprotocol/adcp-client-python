"""``postgres_reliable_reporting_service`` builds an adopter's service subclass (#1588)."""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg_pool")

from adcp_reporting_storage import postgres_reliable_reporting_service  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from adcp.reporting import ReliableReportingService  # noqa: E402
from tests.test_reliable_reporting_service import _account_context  # noqa: E402


class HostReportingService(ReliableReportingService):
    """A host subclass overriding an installed tool method."""


def _pool() -> AsyncConnectionPool:
    return AsyncConnectionPool("postgresql://unused", open=False)


def test_default_builds_the_base_service() -> None:
    service = postgres_reliable_reporting_service(pool=_pool(), account_context=_account_context)
    assert type(service) is ReliableReportingService


def test_service_class_builds_the_subclass_like_memory() -> None:
    pg = postgres_reliable_reporting_service(
        pool=_pool(), account_context=_account_context, service_class=HostReportingService
    )
    memory = HostReportingService.memory(account_context=_account_context)
    assert type(pg) is HostReportingService
    assert type(memory) is HostReportingService
