"""PREVIEW: unreleased ``sync_reporting_status`` wire types. Delete after rc.2.

``sync_reporting_status`` merged to ``adcontextprotocol/adcp`` main
(``388e78e63``) but is **not in a cut release tag**, so it is absent from this
SDK's pinned ``3.2.0-rc.1`` schema bundle and therefore from :mod:`adcp.types`.

Everything under this package is disposable scaffolding:

* :mod:`._generated_models` is ``datamodel-code-generator`` output, not
  hand-written types, produced by
  ``scripts/vendor_reporting_status_preview.py``.
* ``schemas/`` is the vendored ``$ref`` closure at the exact upstream commit
  recorded in ``schemas/UPSTREAM_COMMIT``.  It is also the *runtime* validator
  for the wire conditionals codegen cannot express (``received`` requires a
  revision id and digest; ``obligation_missing`` forbids them; and so on) --
  those rules stay in the schema rather than being restated in Python.

**When the SDK repins to a bundle containing these schemas**: delete this
package outright and re-point :mod:`adcp.reporting.ledger.consumer_status` at
``adcp.types``.  The public names it re-exports are chosen to match what the
generated surface will be called, so that change should be an import swap.

Nothing here is re-exported from :mod:`adcp.reporting`, and the ingest that
consumes it is off unless an adopter turns it on -- see
``adcp.reporting.ledger.consumer_status.CONSUMER_STATUS_PREVIEW_ENABLED``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from adcp.reporting._preview._generated_models.sync_reporting_status_request import (
    ConsumerStatus,
    FailureCode,
    ReportingConsumerStatus,
    SyncReportingStatusRequest,
)
from adcp.reporting._preview._generated_models.sync_reporting_status_response import (
    Results as RecordedConsumerStatusResult,
)
from adcp.reporting._preview._generated_models.sync_reporting_status_response import (
    Results1 as UnchangedConsumerStatusResult,
)
from adcp.reporting._preview._generated_models.sync_reporting_status_response import (
    Results2 as FailedConsumerStatusResult,
)
from adcp.reporting._preview._generated_models.sync_reporting_status_response import (
    SyncReportingStatusResponse,
)

__all__ = [
    "PREVIEW_UPSTREAM_COMMIT",
    "ConsumerStatus",
    "FailedConsumerStatusResult",
    "FailureCode",
    "RecordedConsumerStatusResult",
    "ReportingConsumerStatus",
    "SyncReportingStatusRequest",
    "SyncReportingStatusResponse",
    "UnchangedConsumerStatusResult",
    "consumer_status_schema",
    "validate_consumer_status_wire",
]

_SCHEMA_ROOT = Path(__file__).parent / "schemas"

#: The upstream commit these schemas were vendored from.
PREVIEW_UPSTREAM_COMMIT = (_SCHEMA_ROOT / "UPSTREAM_COMMIT").read_text().strip()


@lru_cache(maxsize=1)
def consumer_status_schema() -> dict[str, Any]:
    """The vendored ``reporting-consumer-status.json``, parsed."""
    import json

    payload: dict[str, Any] = json.loads(
        (_SCHEMA_ROOT / "core" / "reporting-consumer-status.json").read_text()
    )
    return payload


@lru_cache(maxsize=1)
def _consumer_status_validator() -> Any:
    from jsonschema import Draft7Validator

    return Draft7Validator(consumer_status_schema())


def validate_consumer_status_wire(payload: dict[str, Any]) -> list[str]:
    """Check a statement against the vendored schema's conditional rules.

    ``datamodel-code-generator`` flattens the ``allOf``/``if``/``then`` block
    that makes ``received`` require a revision id and digest, ``revision_missing``
    require an obligation id and forbid a revision id, and so on.  Restating
    those rules in Python would mean maintaining a second copy that drifts, so
    the schema enforces them directly.

    Returns human-readable messages, empty when the statement is valid.
    """
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or 'statement'}: {error.message}"
        for error in sorted(_consumer_status_validator().iter_errors(payload), key=str)
    ]
