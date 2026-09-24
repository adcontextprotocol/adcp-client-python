"""Independent feed manifest; old ingestion and materializer guards stay intact."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from adcp.reporting.feed.errors import ReportingFeedError
from adcp.reporting.outbox._schema import schema_objects
from adcp.reporting.receipts.schema import validate_receipt_schema


async def validate_feed_schema(connection: Any, *, notifications: bool = False) -> None:
    ready = False
    try:
        required = json.loads(
            files("adcp.reporting.feed").joinpath("required_schema.json").read_text()
        )
        actual = await schema_objects(connection)
        if required and all(actual.get(k) == v for k, v in required.items()):
            await validate_receipt_schema(connection, notifications=notifications)
            ready = True
    except Exception:
        ready = False
    if not ready:
        raise ReportingFeedError("REPORTING_FEED_SCHEMA_UNREADY")
