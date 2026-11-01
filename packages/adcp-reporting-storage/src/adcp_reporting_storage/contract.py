"""The storage contract this distribution implements, checked against the installed ``adcp``.

``adcp`` reads and writes the reporting ledger through private column lists
(``adcp.reporting.ledger._record_rows``) and this distribution ships the DDL
those lists are read against. The two must agree exactly: a column one side
knows and the other does not fails every reporting turn with
``UndefinedColumn`` (adcp-client-python#1763). The agreement is named by one
integer on each side, bumped together whenever the shared layout changes:

* **1** — the SDK 9 layout and adcp 10.0.0b0–b3: a configuration generation is
  ``(account_id, consumer_id, delivery_config_id, delivery_config_version)``.
  Shipped by adcp-reporting-storage 0.1.0b4 and 0.2.0b0.
* **2** — adcp 10.0.0b4 and later: ``offering_id`` on configurations,
  obligations and consumer statuses (AdCP 3.3 reporting subscriptions; schema
  migration ``0002``). Shipped by adcp-reporting-storage 0.2.0b1 and later.

:func:`require_adcp_contract` runs when a PostgreSQL ledger store is built, so
an incompatible pair fails at startup with the pair named and the fix spelled
out, instead of on the first ledger read.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

__all__ = ["STORAGE_CONTRACT", "adcp_storage_contract", "require_adcp_contract"]

#: The reporting storage contract this distribution's DDL and stores implement.
STORAGE_CONTRACT = 2


def adcp_storage_contract() -> int:
    """The storage contract the installed ``adcp`` reads the ledger with.

    adcp declares it from 10.0.0b6 (``adcp.reporting.ledger.storage_contract``).
    Earlier releases are classified by the column list that distinguishes the
    contracts: 10.0.0b4 and b5 select ``offering_id`` (contract 2); everything
    before reads the four-column generation (contract 1).
    """
    try:
        declared = import_module("adcp.reporting.ledger.storage_contract")
    except ImportError:
        from adcp.reporting.ledger import _record_rows

        columns = [item.strip() for item in _record_rows._OBLIGATION_COLUMNS.split(",")]
        return 2 if "offering_id" in columns else 1
    return int(declared.REPORTING_STORAGE_CONTRACT)


def _installed(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unknown"


def require_adcp_contract() -> None:
    """Refuse an ``adcp`` whose storage contract differs from this distribution's.

    Raises ``adcp.reporting.ReliableReportingConfigurationError`` (code
    ``REPORTING_SCHEMA_BEHIND``) naming both installed versions, both contracts
    and the release to install.
    """
    expected = adcp_storage_contract()
    if expected == STORAGE_CONTRACT:
        return
    from adcp.reporting._errors import ReliableReportingConfigurationError

    adcp_version = _installed("adcp")
    storage_version = _installed("adcp-reporting-storage")
    if expected > STORAGE_CONTRACT:
        fix = (
            f"Install the adcp-reporting-storage release that implements contract {expected} "
            f"(newer than {storage_version}), or adcp 10.0.0b4–10.0.0b5 with this one."
        )
    else:
        fix = (
            f"Install adcp 10.0.0b4 or later (storage contract {STORAGE_CONTRACT}), or "
            "adcp-reporting-storage 0.2.0b0 with this adcp."
        )
    message = (
        f"adcp {adcp_version} reads the reporting ledger with storage contract {expected}, "
        f"but adcp-reporting-storage {storage_version} implements contract {STORAGE_CONTRACT}; "
        f"the pair would fail every reporting turn. {fix}"
    )
    try:
        error = ReliableReportingConfigurationError(
            message, code="REPORTING_SCHEMA_BEHIND", config_field="adcp-reporting-storage"
        )
    except (TypeError, ValueError):  # an adcp that predates the code or the keywords
        error = ReliableReportingConfigurationError(message)
    raise error
