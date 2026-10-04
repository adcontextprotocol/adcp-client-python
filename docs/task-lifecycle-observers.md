# Task lifecycle metrics

`InMemoryTaskRegistry` and `PgTaskRegistry` offer additive lifecycle hooks. The
minimal `TaskRegistry` protocol and all existing write-method return values stay
unchanged. Register a synchronous callback with `add_lifecycle_observer` and
remove it with `remove_lifecycle_observer` (returns whether removal succeeded).
Registering the same callback twice is a no-op.

```python
from adcp.decisioning import InMemoryTaskRegistry, TaskTransition

registry = InMemoryTaskRegistry()

def record_transition(event: TaskTransition, *, task_id: str, account_id: str,
                      task_type: str, created_at: float, updated_at: float) -> None:
    print(event, task_type, updated_at - created_at)

registry.add_lifecycle_observer(record_transition)
```

Events are `submitted`, the first `working` transition, `completed`, `failed`,
and `discarded`. Repeated progress writes, idempotent terminal retries, unknown
progress/discard requests and failed transactions do not emit events. Terminal
states cannot be changed into another terminal state; memory and PostgreSQL
registries enforce the same rule, including concurrent complete/fail calls.

Callbacks run after the PostgreSQL transaction and connection contexts exit, or
after the memory registry lock is released. Each event contains a snapshot of
its transition metadata with epoch timestamps; discard uses deletion time for
`updated_at`. Registration is thread-safe, and delivery uses a snapshot of the
registered callbacks. Callback failures are logged and swallowed without
preventing remaining callbacks or changing the write result.

These hooks are best-effort metrics, not durable events: process exit can lose a
notification, and concurrent writers may notify out of order after commit. Keep
callbacks short and synchronous. Use the durable task webhook outbox for
buyer-facing delivery. No database migration is needed.
