"""C manifest is independent of the byte-identical A/B required-object manifest."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox._schema import schema_objects, validate_schema

REQUIRED_STATUS_OBJECTS: dict[str, dict[str, Any]] = json.loads(
    files("adcp.reporting.outbox").joinpath("required_status_schema.json").read_text()
)
REQUIRED_STATUS_SELECTOR_OBJECTS: dict[str, dict[str, Any]] = json.loads(
    files("adcp.reporting.outbox").joinpath("required_status_selector_schema.json").read_text()
)


async def validate_status_schema(
    connection: Any, *, activity: bool = False, status: bool = True
) -> None:
    # C workers need the A/B ledger/outbox foundation, independently of B
    # activity storage. The concrete B+C activity support validates B activity
    # separately; this manifest's activity switch validates only C objects.
    await validate_schema(connection)
    installed = await schema_objects(connection)
    _validate_status_objects(installed, activity=activity, status=status)


def _validate_status_objects(
    installed: dict[str, dict[str, Any]], *, activity: bool = False, status: bool = True
) -> None:
    """Apply only C's contract; the caller must also prove its B foundation."""
    if not REQUIRED_STATUS_OBJECTS:
        raise ReportingNotificationError("status_schema_unready:manifest_missing")
    for key, expected in REQUIRED_STATUS_OBJECTS.items():
        if not activity and "reporting_status_webhook_" in key:
            continue
        if not status and not any(
            name in key
            for name in (
                "reporting_status_webhook_",
                "reporting_status_notification_",
                "reporting_status_event_",
            )
        ):
            continue
        actual = installed.get(key)
        if actual is None:
            reason = "missing"
        elif not actual["enabled"]:
            reason = "disabled"
        elif actual["fingerprint"] != expected["fingerprint"]:
            reason = "changed"
        else:
            continue
        raise ReportingNotificationError(f"status_schema_unready:{reason}:{key}")
    if status:
        if not REQUIRED_STATUS_SELECTOR_OBJECTS:
            raise ReportingNotificationError("status_selector_schema_unready:manifest_missing")
        for key, expected in REQUIRED_STATUS_SELECTOR_OBJECTS.items():
            if installed.get(key) != expected:
                raise ReportingNotificationError(f"status_selector_schema_unready:{key}")
