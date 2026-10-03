"""Adopter contract: optional reconciliation never enlarges TaskRegistry."""

from typing import Any

from adcp.decisioning import InMemoryTaskRegistry, ListableTaskRegistry, TaskRegistry
from adcp.decisioning.pg import PgTaskRegistry


async def reconcile(registry: TaskRegistry, account_id: str) -> dict[str, Any] | None:
    if isinstance(registry, ListableTaskRegistry):
        return await registry.list(
            account_id=account_id, filters={"status": "working"}, pagination={"max_results": 10}
        )
    return None


memory: TaskRegistry = InMemoryTaskRegistry()


def postgres_registry(registry: PgTaskRegistry) -> TaskRegistry:
    return registry
