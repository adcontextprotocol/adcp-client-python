"""Adopter-facing type checks for per-call task options."""

from adcp import ADCPClient, TaskOptions
from adcp.types import GetProductsRequest


async def call_with_deadline(client: ADCPClient, request: GetProductsRequest) -> None:
    options = TaskOptions(timeout=10.0)
    await client.get_products(request, options=options)
    await client.execute_task("get_products", request, options=options)
