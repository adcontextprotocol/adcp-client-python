"""All public exports work without optional drivers; the PostgreSQL stores explain their extra."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "adcp.reporting.production",
        "adcp.reporting.projection",
        "adcp_reporting_storage",
        "adcp_reporting_storage.production",
    ],
)
@pytest.mark.parametrize("drivers", [False, True])
def test_public_exports_and_optional_database_drivers(module, drivers):
    script = """
import importlib, sys
drivers = sys.argv[2] == 'True'
if not drivers:
    class NoDrivers:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split('.')[0] in {'psycopg', 'psycopg_pool'}:
                raise ModuleNotFoundError(fullname)
    sys.meta_path.insert(0, NoDrivers())
module = importlib.import_module(sys.argv[1])
exports = getattr(module, '__all__', [])
assert len(exports) == len(set(exports))
assert module.__name__ + '.pg' not in sys.modules
for name in exports:
    assert getattr(module, name) is not None, name
if not drivers:
    assert 'psycopg' not in sys.modules
    assert 'psycopg_pool' not in sys.modules
    # The stores moved to adcp-reporting-storage; adcp no longer binds them.
    for name in ('PgReportingProductionStore', 'PgReportingProjectionStore'):
        assert not hasattr(module, name) or module.__name__.startswith('adcp_reporting_storage')
    if module.__name__ == 'adcp_reporting_storage':
        try:
            module.PgReportingProductionStore(pool=object())
        except ImportError as error:
            assert 'adcp-reporting-storage[pg]' in str(error), str(error)
        else:
            raise AssertionError('PG store constructed without its driver')
        assert 'psycopg' not in sys.modules
try:
    getattr(module, 'NoSuchReportingExport')
except AttributeError:
    pass
else:
    raise AssertionError('unknown public export was accepted')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, module, str(drivers)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
