"""Isolated ingestion readiness; never widens an A/B/C/B1/B2.1 manifest."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from adcp.reporting.materializer.schema import validate_materializer_schema
from adcp.reporting.outbox._schema import schema_objects
from adcp.reporting.receipts.errors import ReportingReceiptError


async def validate_receipt_schema(connection: Any, *, notifications: bool = False) -> None:
    ready = False
    try:
        required = json.loads(
            files("adcp.reporting.receipts").joinpath("required_schema.json").read_text()
        )
        installed = await schema_objects(connection)
        if required and all(installed.get(key) == expected for key, expected in required.items()):
            await validate_materializer_schema(connection, notifications=notifications)
            ready = True
    except Exception:
        ready = False
    if not ready:
        raise ReportingReceiptError("RECEIPT_SCHEMA_UNREADY")
