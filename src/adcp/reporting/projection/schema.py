"""B2.4's additive manifest never widens an earlier feature's required objects."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from adcp.reporting.feed.schema import validate_feed_schema
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox._schema import schema_objects
from adcp.reporting.outbox.status_schema import validate_status_schema


async def validate_projection_schema(connection: Any, *, notifications: bool = False) -> None:
    try:
        required = json.loads(
            files("adcp.reporting.projection").joinpath("required_schema.json").read_text()
        )
        actual = await schema_objects(connection)
        if not required or any(actual.get(k) != v for k, v in required.items()):
            raise ValueError
        await validate_feed_schema(connection, notifications=notifications)
        await validate_status_schema(connection)
    except Exception:
        raise ReportingNotificationError("status_projection_schema_unready") from None
