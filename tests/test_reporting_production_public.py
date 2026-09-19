"""All public exports work without optional drivers; concrete PG classes are lazy."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["adcp.reporting.production", "adcp.reporting.projection"])
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
assert len(module.__all__) == len(set(module.__all__))
assert module.__name__ + '.pg' not in sys.modules
for name in module.__all__:
    assert getattr(module, name) is not None, name
if not drivers:
    assert 'psycopg' not in sys.modules
    assert 'psycopg_pool' not in sys.modules
    name = ('PgReportingProductionStore' if module.__name__.endswith('production')
            else 'PgReportingProjectionStore')
    try:
        getattr(module, name)(pool=object())
    except ImportError as error:
        assert 'pg' in str(error)
    else:
        raise AssertionError('PG store constructed without its driver')
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
