"""Separate production participants; no inherited mandatory manifest is widened."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp_reporting_storage.outbox._schema import schema_objects
from adcp_reporting_storage.projection.schema import validate_projection_schema


async def validate_production_schema(
    connection: Any, *, notifications: bool = False, service_context: bool = False
) -> None:
    try:
        required = json.loads(
            files("adcp_reporting_storage.production").joinpath("required_schema.json").read_text()
        )
        if service_context:
            required.update(
                json.loads(
                    files("adcp_reporting_storage.production")
                    .joinpath("service_context_schema.json")
                    .read_text()
                )
            )
        actual = await schema_objects(connection)
        if not required or any(actual.get(k) != v for k, v in required.items()):
            raise ValueError
        await validate_projection_schema(connection, notifications=notifications)
    except Exception:
        raise ReportingNotificationError("reporting_production_schema_unready") from None
