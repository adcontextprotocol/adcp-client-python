"""Typed adopter usage of explicit PostgreSQL business/replay transactions."""

from typing import Any

from psycopg import AsyncConnection

from adcp.server import ToolContext
from adcp.server.idempotency import (
    IdempotencyReservationError,
    IdempotencyStore,
    PgBackend,
    PgReservation,
)
from adcp.types import CreateMediaBuyRequest


async def create_buy(
    store: IdempotencyStore,
    request: CreateMediaBuyRequest,
    context: ToolContext,
) -> dict[str, Any]:
    async with store.reserve(request, context, operation="create_media_buy") as slot:
        reservation: PgReservation = slot
        if reservation.replayed:
            replay = reservation.response
            assert replay is not None
            return replay
        connection: AsyncConnection[Any] = reservation.connection
        await connection.execute("INSERT INTO media_buys (id) VALUES (%s)", ("mb_1",))
        response: dict[str, Any] = {"media_buy_id": "mb_1"}
        await reservation.record(response)
    return response  # commit has completed before returning to the transport


async def with_caller_connection(
    backend: PgBackend, connection: AsyncConnection[Any]
) -> dict[str, Any] | None:
    try:
        async with backend.reserve("buyer", "key", "hash", connection=connection) as slot:
            if not slot.replayed:
                await slot.record({"ok": True})
            response = slot.response
        return response
    except IdempotencyReservationError:
        raise
