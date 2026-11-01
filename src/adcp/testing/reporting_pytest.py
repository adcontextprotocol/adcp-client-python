"""pytest plugin: the ``adcp_reporting_kit`` fixture (AdCP 3.3, experimental).

Enable it in a ``conftest.py``::

    pytest_plugins = ["adcp.testing.reporting_pytest"]

then take the fixture, configured by an optional marker::

    @pytest.mark.adcp_reporting_kit(accounts=[{"account_id": "acc_a", "timezone": "UTC"}])
    async def test_owed(adcp_reporting_kit):
        buyer = adcp_reporting_kit.buyer("agency-x")
        ...
        await adcp_reporting_kit.assert_agreement("agency-x")

The marker's keyword arguments are those of
:func:`adcp.testing.reporting.reporting_test_kit`. The fixture is async; run
it under pytest-asyncio (``asyncio_mode = "auto"``, or the plugin's strict
mode, which this fixture supports).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

try:  # pragma: no cover - which decorator depends on the installed plugin
    import pytest_asyncio

    _fixture: Any = pytest_asyncio.fixture
except ImportError:  # pragma: no cover
    _fixture = pytest.fixture


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "adcp_reporting_kit(**options): options for the adcp_reporting_kit fixture "
        "(the keyword arguments of adcp.testing.reporting.reporting_test_kit)",
    )


@_fixture  # type: ignore[untyped-decorator]
async def adcp_reporting_kit(request: Any) -> AsyncIterator[Any]:
    """A started :class:`~adcp.testing.reporting.ReportingTestKit`, closed after the test."""
    from adcp.testing.reporting import reporting_test_kit

    marker = request.node.get_closest_marker("adcp_reporting_kit")
    options = dict(marker.kwargs) if marker is not None else {}
    async with reporting_test_kit(**options) as kit:
        yield kit
