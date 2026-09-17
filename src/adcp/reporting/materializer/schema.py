"""Materializer readiness is independent of every prior mandatory manifest."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.outbox._schema import REQUIRED_OBJECTS, schema_objects, validate_schema


async def validate_materializer_schema(connection: Any, *, notifications: bool = False) -> None:
    required = json.loads(
        files("adcp.reporting.materializer").joinpath("required_schema.json").read_text()
    )
    if not required:
        raise LedgerConflictError("MATERIALIZER_SCHEMA_UNREADY", "install the materializer schema")
    installed = None
    try:
        installed = await schema_objects(connection)
    except Exception:
        installed = None
    if installed is None:
        raise LedgerConflictError(
            "MATERIALIZER_SCHEMA_UNREADY", "materializer schema catalog is unavailable"
        )
    if any(installed.get(key) != expected for key, expected in required.items()):
        raise LedgerConflictError("MATERIALIZER_SCHEMA_UNREADY", "install the materializer schema")
    # Inherit the reviewed foundation prerequisites without appending B2 objects
    # to A's manifest or requiring optional notification delivery infrastructure.
    for key, expected in REQUIRED_OBJECTS.items():
        if any(
            word in key
            for word in ("notification_", "webhook_", "status_dirty", "status_checkpoints")
        ):
            continue
        if installed.get(key) != expected:
            raise LedgerConflictError(
                "MATERIALIZER_SCHEMA_UNREADY", "install the retained evidence schema"
            )
    if notifications:
        await validate_schema(connection)
