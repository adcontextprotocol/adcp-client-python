"""Public metrics hook works without changing the minimal registry protocol."""

from typing import Any

from adcp.decisioning import (
    InMemoryTaskRegistry,
    TaskLifecycleObserver,
    TaskRegistry,
    TaskTransition,
)
from adcp.decisioning.pg import PgTaskRegistry


class Metrics:
    def __call__(
        self,
        event: TaskTransition,
        *,
        task_id: str,
        account_id: str,
        task_type: str,
        created_at: float,
        updated_at: float,
    ) -> None:
        print(event, task_id, account_id, task_type, updated_at - created_at)


observer: TaskLifecycleObserver = Metrics()


async def instrument(memory: InMemoryTaskRegistry, postgres: PgTaskRegistry) -> None:
    for registry in (memory, postgres):
        registry.add_lifecycle_observer(observer)
        minimal: TaskRegistry = registry
        task = await minimal.issue(account_id="account", task_type="get_products")
        result: dict[str, Any] = {"products": []}
        await minimal.complete(task, result)
        removed: bool = registry.remove_lifecycle_observer(observer)
        assert removed
