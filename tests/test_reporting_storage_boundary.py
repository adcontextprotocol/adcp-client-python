"""The adcp / adcp-reporting-storage boundary.

``adcp`` defines the reporting store protocols and the in-memory stores;
``adcp-reporting-storage`` implements them over PostgreSQL and object storage.
The dependency points one way: the storage distribution imports ``adcp``, and
``adcp`` never imports the storage distribution or a database driver from its
reporting package. The storage package may reach a short, fixed list of private
``adcp.reporting`` helpers; that list can only shrink.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADCP_SRC = ROOT / "src" / "adcp"
STORAGE_SRC = ROOT / "packages" / "adcp-reporting-storage" / "src" / "adcp_reporting_storage"

#: Private ``adcp`` modules the storage distribution imports. Each is a shared
#: codec or helper that both sides must agree on byte for byte; promote one to a
#: public name before adding another.
ALLOWED_PRIVATE_ADCP_IMPORTS = {
    "adcp._deferred_adapters",
    "adcp.reporting._source_authorization",
    "adcp.reporting.feed._errors",
    "adcp.reporting.ledger._delivery_state",
    "adcp.reporting.ledger._record_rows",
    "adcp.reporting.materializer._errors",
    "adcp.reporting.receipts._diagnostics",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


def test_adcp_never_imports_the_storage_distribution_or_a_reporting_driver() -> None:
    offenders: dict[str, set[str]] = {}
    for path in ADCP_SRC.rglob("*.py"):
        modules = _imports(path)
        bad = {m for m in modules if m.split(".")[0] == "adcp_reporting_storage"}
        if path.is_relative_to(ADCP_SRC / "reporting"):
            bad |= {m for m in modules if m.split(".")[0] in {"psycopg", "psycopg_pool"}}
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, offenders


def test_storage_distribution_imports_adcp_through_public_names_or_the_allowlist() -> None:
    reaches: dict[str, set[str]] = {}
    for path in STORAGE_SRC.rglob("*.py"):
        private = {
            m
            for m in _imports(path)
            if m.split(".")[0] == "adcp"
            and any(part.startswith("_") for part in m.split("."))
            and m not in ALLOWED_PRIVATE_ADCP_IMPORTS
        }
        if private:
            reaches[str(path.relative_to(ROOT))] = private
    assert not reaches, reaches


def test_allowlist_names_only_modules_that_exist_and_are_used() -> None:
    used: set[str] = set()
    for path in STORAGE_SRC.rglob("*.py"):
        used |= {m for m in _imports(path) if m in ALLOWED_PRIVATE_ADCP_IMPORTS}
    assert used == ALLOWED_PRIVATE_ADCP_IMPORTS, {
        "unused": ALLOWED_PRIVATE_ADCP_IMPORTS - used,
    }
    for module in ALLOWED_PRIVATE_ADCP_IMPORTS:
        assert (ADCP_SRC.parent / module.replace(".", "/")).with_suffix(".py").is_file(), module


def test_importing_the_storage_facade_loads_no_store_driver_or_cloud_sdk() -> None:
    """The facade is lazy: a store loads on first use, a cloud SDK only with its provider."""
    script = f"""
import sys
sys.path[:0] = [{str(ADCP_SRC.parent)!r}, {str(STORAGE_SRC.parent)!r}]
import adcp_reporting_storage
assert not [m for m in sys.modules if m.startswith("adcp_reporting_storage.")]
assert "psycopg" not in sys.modules and "adcp.reporting" not in sys.modules
from adcp_reporting_storage import PgReportingLedgerStore, ReportingRowStorageOptionsV1
assert PgReportingLedgerStore.__module__ == "adcp_reporting_storage.ledger.pg"
# psycopg may now be loaded (the store probes for it); the cloud SDKs never are.
for name in ("boto3", "google.cloud.storage", "azure.storage.blob"):
    assert name not in sys.modules, name
assert "adcp.reporting.ledger.pg" not in sys.modules
missing = [n for n in adcp_reporting_storage.__all__ if getattr(adcp_reporting_storage, n) is None]
assert not missing, missing
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok"


def test_moved_names_are_gone_from_adcp() -> None:
    """A clean cut: the old addresses raise, with nothing shimming them."""
    import adcp.reporting.ledger as ledger
    import adcp.reporting.outbox as outbox
    import adcp.reporting.production as production
    import adcp.reporting.service as service

    for module, name in (
        (ledger, "PgReportingLedgerStore"),
        (ledger, "PgReportingReconciliationStore"),
        (ledger, "ReportingRowStorageOptionsV1"),
        (ledger, "ReportingChangesPageV1"),
        (outbox, "PgReportingOutbox"),
        (outbox, "PgStatusNotificationStore"),
        (production, "PgReportingProductionStore"),
    ):
        assert not hasattr(module, name), name
        assert name not in module.__all__, name
    assert not hasattr(service.ReliableReportingService, "postgres")
    for module in (
        "adcp.reporting.ledger.pg",
        "adcp.reporting.ledger.row_storage",
        "adcp.reporting.outbox.pg",
        "adcp.reporting.migration",
    ):
        assert not (ADCP_SRC.parent / module.replace(".", "/")).with_suffix(".py").exists(), module
