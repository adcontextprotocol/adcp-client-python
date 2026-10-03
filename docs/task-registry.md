# Decisioning task polling

`PlatformHandler` serves `get_task_status` for every `TaskRegistry`. It resolves
and authorizes the account before reading, passes `expected_account_id`, and
returns `REFERENCE_NOT_FOUND` with the same message for unknown, discarded,
expired, and cross-account references. Custom registries must enforce that scope
and return `None` for expired records; an optional epoch `expires_at` is also
recognized. SDK registries retain tasks until explicit discard; no TTL is
installed by this change.

Polls use `status`, ISO UTC timestamps and protocol metadata. Progress, failure
summary and the original task's context are preserved. Set `include_result=true`
to retrieve a terminal artifact. A failed task exposes the stored fatal error
in its convenience `error` summary, `result.adcp_error`, and the canonical
`result.errors` array. Successful results retain the adopter's original wire
response; the background callable must return a valid response for its task type.

`list_tasks` is advertised only for the separate optional `ListableTaskRegistry`
protocol. Existing third-party `TaskRegistry` implementations need no changes.
The SDK's memory and PostgreSQL implementations support canonical filters,
sort and pagination, with signed cursors bound to the resolved account and
query. Set `ADCP_PAGINATION_SECRET` consistently across workers if cursors must
survive restarts. Ordering uses task ID as a deterministic tie-breaker. Pages
reflect current state, rather than a frozen snapshot across requests.

```python
from adcp.decisioning import ListableTaskRegistry, TaskRegistry

async def reconcile(registry: TaskRegistry, account_id: str) -> None:
    if isinstance(registry, ListableTaskRegistry):
        page = await registry.list(
            account_id=account_id,
            filters={"statuses": ["submitted", "working"]},
            pagination={"max_results": 25},
        )
        print(page["tasks"])
```

The pinned legacy list response admits only `media-buy`, `signals`, and `creative`
in its `domain` field. SDK listing includes those domains; other tasks remain
pollable. SDK registries do not store conversation history, so `include_history`
does not fabricate history. Unknown filter extensions are rejected. The current
PostgreSQL implementation filters and pages an account-scoped snapshot in Python;
large task histories may need a custom SQL-native implementation of the optional
protocol. No schema migration is required.
