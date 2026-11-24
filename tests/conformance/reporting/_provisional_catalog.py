"""Exact additive observation catalog; historical manifests remain unchanged."""

import importlib.util as _importlib_util


def _storage_split():
    """True when the adcp on sys.path predates the storage split (#1510).

    Its stores still live in adcp.reporting and the storage package must not
    be used. Evaluated at each import, after the script has set up sys.path.
    """
    return _importlib_util.find_spec("adcp.reporting.ledger.pg") is not None


if _storage_split():  # wheels before the storage split (#1510)
    from adcp.reporting.outbox._schema import PROVISIONAL_REQUIRED_OBJECTS as PROVISIONAL_OBJECTS
else:
    from adcp_reporting_storage.outbox._schema import (
        PROVISIONAL_REQUIRED_OBJECTS as PROVISIONAL_OBJECTS,
    )

assert len(PROVISIONAL_OBJECTS) == 27
