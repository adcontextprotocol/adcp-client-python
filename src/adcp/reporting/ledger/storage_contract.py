"""The reporting storage contract: the ledger layout adcp and adcp-reporting-storage share.

adcp reads and writes the PostgreSQL ledger through the column lists in
:mod:`adcp.reporting.ledger._record_rows`; ``adcp-reporting-storage`` ships
the DDL those lists are read against. The two must agree exactly: a column one
side knows and the other does not fails every reporting turn with
``UndefinedColumn``, logged only as a configuration failure
(adcp-client-python#1763). The agreement is named by one integer on each side,
bumped together whenever the shared layout changes:

* **1** — the SDK 9 layout and adcp 10.0.0b0–b3: a configuration generation is
  ``(account_id, consumer_id, delivery_config_id, delivery_config_version)``.
  Implemented by adcp-reporting-storage 0.1.0b4 and 0.2.0b0.
* **2** — adcp 10.0.0b4 and later: ``offering_id`` on configurations,
  obligations and consumer statuses (AdCP 3.3 reporting subscriptions).
  Implemented by adcp-reporting-storage 0.2.0b1 and later.

:func:`require_storage_contract` runs at service startup and when
``postgres_storage`` is built, so an incompatible pair is refused once, with
both versions named and the fix spelled out, instead of failing every turn.
``tests/test_reporting_storage_contract.py`` pins the column lists to the
contract number, so changing them without bumping it fails the build; the
companion release gate (``scripts/check_companion_releases.py``) refuses to
publish either package against a companion of another contract.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
from typing import Any

__all__ = [
    "REPORTING_STORAGE_CONTRACT",
    "declared_storage_contract",
    "require_storage_contract",
    "storage_contract_fingerprint",
]

#: The storage contract this adcp reads the reporting ledger with.
REPORTING_STORAGE_CONTRACT = 2

#: The distribution that implements the contract for PostgreSQL.
STORAGE_DISTRIBUTION = "adcp-reporting-storage"
#: Its first release of the current contract.
STORAGE_RELEASE = "0.2.0b1"


def storage_contract_fingerprint() -> str:
    """SHA-256 over the shared column lists; pinned per contract number in the tests."""
    from adcp.reporting.ledger import _record_rows

    lists = (
        _record_rows._CONFIGURATION_COLUMNS,
        _record_rows._OBLIGATION_COLUMNS,
        _record_rows._REVISION_COLUMNS,
        _record_rows._ADJUSTMENT_COLUMNS,
        _record_rows._STATUS_COLUMNS_BARE,
        _record_rows._ISSUE_COLUMNS,
    )
    normalized = "\n".join(
        ",".join(column.strip() for column in columns.split(",")) for columns in lists
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def declared_storage_contract(store: Any) -> int | None:
    """The contract ``store`` implements, or ``None`` for a store that declares none.

    A store declares it with a ``storage_contract`` attribute. A store from
    the ``adcp_reporting_storage`` distribution without one predates the
    declaration (0.1.0b4, 0.2.0b0) and implements contract 1. Any other
    store (in-memory, an adopter's own) is not subject to the check.
    """
    declared = getattr(store, "storage_contract", None)
    if declared is not None:
        return int(declared)
    module = getattr(type(store), "__module__", "") or ""
    if module == "adcp_reporting_storage" or module.startswith("adcp_reporting_storage."):
        return 1
    return None


def _installed(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unknown"


def require_storage_contract(store: Any) -> None:
    """Refuse a ledger store whose storage contract differs from this adcp's.

    Raises :class:`adcp.reporting.ReliableReportingConfigurationError` with
    code ``REPORTING_SCHEMA_BEHIND`` (recovery ``correctable``), naming both
    installed versions, both contracts, the release to install and the
    migration to run.
    """
    implemented = declared_storage_contract(store)
    if implemented is None or implemented == REPORTING_STORAGE_CONTRACT:
        return
    from adcp.reporting._errors import ReliableReportingConfigurationError

    adcp_version = _installed("adcp")
    storage_version = _installed(STORAGE_DISTRIBUTION)
    if implemented < REPORTING_STORAGE_CONTRACT:
        fix = (
            f"Install {STORAGE_DISTRIBUTION}>={STORAGE_RELEASE} (storage contract "
            f'{REPORTING_STORAGE_CONTRACT}), then run "python -m adcp migrate" or start the '
            "service, whose create_schema() upgrades the ledger in place."
        )
    else:
        fix = (
            f"Install the adcp release that reads storage contract {implemented}, or "
            f"{STORAGE_DISTRIBUTION} of contract {REPORTING_STORAGE_CONTRACT}."
        )
    raise ReliableReportingConfigurationError(
        f"adcp {adcp_version} reads the reporting ledger with storage contract "
        f"{REPORTING_STORAGE_CONTRACT}, but the installed {STORAGE_DISTRIBUTION} "
        f"{storage_version} implements contract {implemented}; the pair would fail every "
        f"reporting turn. {fix}",
        code="REPORTING_SCHEMA_BEHIND",
        config_field=STORAGE_DISTRIBUTION,
    )
