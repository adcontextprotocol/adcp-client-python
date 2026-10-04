"""Typed adopter factories preserve concrete store, listing and observer APIs."""

from typing import Any

from psycopg_pool import AsyncConnectionPool

from adcp.decisioning import ListableTaskRegistry, TaskRegistry, TaskTransition
from adcp.decisioning.pg import (
    LazyProposalStore,
    LazyProposalStoreFactory,
    LazyTaskRegistry,
    LazyTaskRegistryFactory,
    LazyTaskWebhookOutbox,
    LazyTaskWebhookOutboxFactory,
    PgProposalStore,
    PgTaskRegistry,
    PgTaskWebhookOutbox,
)
from adcp.decisioning.proposal_store import ProposalStore


def example(pool: AsyncConnectionPool[Any], outbox: PgTaskWebhookOutbox) -> None:
    async def task_factory() -> tuple[PgTaskRegistry, PgTaskWebhookOutbox]:
        return PgTaskRegistry(pool=pool, task_webhook_outbox=outbox), outbox

    factory: LazyTaskRegistryFactory = task_factory
    registry = LazyTaskRegistry(factory)
    minimal: TaskRegistry = registry
    listable: ListableTaskRegistry = registry

    def metrics(
        event: TaskTransition,
        *,
        task_id: str,
        account_id: str,
        task_type: str,
        created_at: float,
        updated_at: float,
    ) -> None:
        print(event, task_id, account_id, task_type, updated_at - created_at)

    registry.add_lifecycle_observer(metrics)
    registry.remove_lifecycle_observer(metrics)
    facade: LazyTaskWebhookOutbox = LazyTaskWebhookOutbox.from_registry(registry)

    def proposal_store() -> PgProposalStore:
        return PgProposalStore(pool=pool)

    proposal_factory: LazyProposalStoreFactory = proposal_store
    proposals: ProposalStore = LazyProposalStore(proposal_factory)

    def task_outbox() -> PgTaskWebhookOutbox:
        return outbox

    outbox_factory: LazyTaskWebhookOutboxFactory = task_outbox
    other_facade = LazyTaskWebhookOutbox(outbox_factory)
    print(minimal, listable, facade, proposals, other_facade)


async def consume(registry: LazyTaskRegistry, facade: LazyTaskWebhookOutbox) -> None:
    concrete: PgTaskRegistry = await registry.resolve()
    box: PgTaskWebhookOutbox = await facade.resolve()
    original: PgTaskRegistry | None = registry.resolved
    tasks: dict[str, Any] = await registry.list(account_id="acct")
    assert concrete.task_webhook_outbox is box
    print(original, tasks)
