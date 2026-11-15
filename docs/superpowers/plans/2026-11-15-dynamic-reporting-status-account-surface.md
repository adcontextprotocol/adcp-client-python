# Dynamic Reporting Status Account Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Reliable Reporting status-notification readiness and lifecycle iteration use a current, complete, immutable account-surface snapshot without restarting the server.

**Architecture:** `ReportingStatusSupport` accepts an optional zero-argument async resolver that returns account IDs, completeness, and signing probes in one frozen value. Each readiness proof and service iteration resolves once and threads that snapshot through all account checks; the existing static fields become the fallback source. The decisioning composition root binds its generated `PlatformHandler` to support before capability validation and exposes the installed reporting service's authoritative status handler for the existing structural proof.

**Tech Stack:** Python 3.10+, frozen dataclasses, async protocols, pytest/pytest-asyncio, Ruff, mypy, uv.

**Spec:** `docs/superpowers/specs/2026-11-15-dynamic-reporting-status-account-surface-design.md`

## Global Constraints

- Do not change the AdCP wire schema.
- Do not accept account IDs from capability request bodies, `ext`, or arbitrary context metadata.
- Preserve every existing `ReportingStatusSupport` field and its positional order; append only the optional resolver field.
- One immutable snapshot supplies account IDs, completeness, and readiness subscriptions for one proof.
- Resolver failure, incomplete or empty evidence, unready accounts, invalid components, and handler mismatch fail closed.
- Preserve the agentic-api requirement for webhook signing plus both Reliable Reporting notification declarations.
- Use TDD: observe each new test fail for the intended missing behavior before production edits.
- Never stage ignored files or use `git add -f`, `git add .`, `git add -u`, or `git commit -a`.

---

## File Structure

- `src/adcp/reporting/outbox/status_support.py`: define the public snapshot/resolver API, resolve static or dynamic surfaces, perform snapshot-consistent readiness, and bind one handler.
- `src/adcp/reporting/outbox/status_service.py`: resolve fresh surfaces for lifecycle membership and account iteration.
- `src/adcp/reporting/outbox/__init__.py`: export the new public types.
- `src/adcp/reporting/service.py`: provide the authoritative configured `ReportingStatusHandler` used internally by an installed Reliable Reporting service.
- `src/adcp/decisioning/handler.py`: expose that authoritative handler on generated decisioning handlers.
- `src/adcp/decisioning/serve.py`: bind generated handlers to status support before boot-time capability validation.
- `tests/conformance/reporting/test_reporting_status_capabilities.py`: exercise dynamic readiness, fail-closed behavior, request isolation, and one-snapshot proofs.
- `tests/conformance/reporting/test_reporting_status_delivery.py`: retain the static positional API and component matrix.
- `tests/conformance/reporting/test_reporting_status_service_surface.py`: exercise fresh lifecycle account iteration with focused fakes and real surface validation.
- `tests/test_decisioning_serve.py`: exercise generated-handler binding and mismatch refusal.
- `tests/type_checks/reporting_status_notifications.py`: type-check the resolver API.
- `tests/fixtures/public_api_snapshot.json`: record the two exported outbox symbols.
- `examples/reporting_status_notifications.py`: demonstrate deployment-wide dynamic account discovery in the lifecycle loop.
- `docs/reporting-status-notifications.md`: document the dynamic complete-surface contract and ESA usage.

---

### Task 1: Public immutable surface and snapshot-consistent readiness

**Files:**
- Modify: `tests/conformance/reporting/test_reporting_status_capabilities.py`
- Modify: `tests/conformance/reporting/test_reporting_status_delivery.py`
- Modify: `src/adcp/reporting/outbox/status_support.py`
- Modify: `src/adcp/reporting/outbox/__init__.py`

**Interfaces:**
- Produces: `ReportingStatusAccountSurface(account_ids, account_surface_complete, readiness_subscriptions)`.
- Produces: async callable protocol `ReportingStatusAccountSurfaceResolver`.
- Produces: `ReportingStatusSupport.resolve_account_surface() -> ReportingStatusAccountSurface`.
- Preserves: all existing `ReportingStatusSupport` positional constructor arguments.

- [ ] **Step 1: Write failing dynamic-surface tests**

Add imports for `ReportingStatusAccountSurface` and tests shaped as follows. Use the real PostgreSQL harness for the positive readiness transitions; skip the positive assertion for the in-memory conformance store because it intentionally never advertises durability.

```python
async def test_dynamic_surface_discovers_account_without_restart(status_harness):
    h = status_harness
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        pytest.skip("durability advertisement requires PostgreSQL")
    await h.seed(readable=True)
    await h.ledger.put_configuration(configuration("acct_b"))
    n, static = await support_for(h, ("acct_a", "acct_b"))
    surfaces = [
        ReportingStatusAccountSurface(
            account_ids=("acct_a",),
            account_surface_complete=True,
            readiness_subscriptions=(static.readiness_subscriptions[0],),
        )
    ]

    async def resolve_surface() -> ReportingStatusAccountSurface:
        return surfaces[0]

    support = replace(
        static,
        account_ids=(),
        account_surface_complete=False,
        readiness_subscriptions=(),
        account_surface_resolver=resolve_surface,
    )
    await h.status.baseline(account_id="acct_a")
    assert await support.durable()

    surfaces[0] = ReportingStatusAccountSurface(
        account_ids=("acct_a", "acct_b"),
        account_surface_complete=True,
        readiness_subscriptions=static.readiness_subscriptions,
    )
    assert not await support.durable()
    await h.status.baseline(account_id="acct_b")
    assert await support.durable()
```

Add separate tests that:

- return `account_surface_complete=False` and assert `advertised_notifications() == {}`;
- raise `RuntimeError("registry unavailable")` and assert automatic advertisement omits the claim while `validate_status_claims()` raises only `status_capability_requires_durable_reporting`;
- provide two accounts with one probe removed and assert the global claim is omitted;
- mutate the resolver's next return value during the first account check, assert the resolver was called once, and assert the proof used only the first snapshot;
- instantiate `ReportingStatusSupport` using the existing nine positional values and assert `resolve_account_surface()` reproduces the static tuples.

Before each test body, record the production mutation it catches: stale constructor surface, exception propagation, partial signing evidence, repeated resolver calls, or positional API breakage.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run -m pytest \
  tests/conformance/reporting/test_reporting_status_capabilities.py \
  tests/conformance/reporting/test_reporting_status_delivery.py -q
```

Expected: collection/import failures for the missing public snapshot symbols or constructor field. Existing tests must remain green around those failures.

- [ ] **Step 3: Implement the public snapshot and resolver fallback**

In `status_support.py`, add:

```python
from typing import Any, Protocol


@dataclass(frozen=True, kw_only=True)
class ReportingStatusAccountSurface:
    account_ids: tuple[str, ...]
    account_surface_complete: bool
    readiness_subscriptions: tuple[ReportingNotificationSubscription, ...]


class ReportingStatusAccountSurfaceResolver(Protocol):
    async def __call__(self) -> ReportingStatusAccountSurface: ...
```

Append this field to `ReportingStatusSupport` after `readiness_subscriptions`:

```python
account_surface_resolver: ReportingStatusAccountSurfaceResolver | None = None
```

Implement `resolve_account_surface()` so the resolver is authoritative when present and the existing static fields are copied into a snapshot otherwise. Translate resolver exceptions and invalid return types into `ReportingNotificationError("status_account_surface_unavailable")` with exception chaining. Log only the fixed failure message with `exc_info=True`, never account IDs or subscriptions.

Add a private readiness method that accepts a snapshot. Make `account_ready()` resolve once for its public single-account check. Make `durable()` resolve once, require complete/non-empty account and subscription evidence, and pass that same snapshot to every account check. Catch only the secret-free surface error in `durable()` and return `False`; do not catch cancellation.

- [ ] **Step 4: Export the public symbols**

Add both names to the imports and `__all__` list in `adcp.reporting.outbox`:

```python
from adcp.reporting.outbox.status_support import (
    ReportingStatusAccountSurface,
    ReportingStatusAccountSurfaceResolver,
    ReportingStatusSupport,
)
```

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run the command from Step 2. Expected: all available tests pass; PostgreSQL-only tests skip only when the local test database is unavailable.

- [ ] **Step 6: Commit Task 1**

```bash
git add \
  src/adcp/reporting/outbox/status_support.py \
  src/adcp/reporting/outbox/__init__.py \
  tests/conformance/reporting/test_reporting_status_capabilities.py \
  tests/conformance/reporting/test_reporting_status_delivery.py
git commit -m "feat(reporting): resolve dynamic status account surfaces"
```

---

### Task 2: Fresh lifecycle account iteration

**Files:**
- Create: `tests/conformance/reporting/test_reporting_status_service_surface.py`
- Modify: `src/adcp/reporting/outbox/status_service.py`

**Interfaces:**
- Consumes: `ReportingStatusSupport.resolve_account_surface()` from Task 1.
- Preserves: `ReportingStatusNotificationLifecycle` public method signatures.
- Produces: one fresh immutable surface per `baseline()` call and per drain turn.

- [ ] **Step 1: Write failing lifecycle tests**

Build a focused fake durable support graph whose store records baselines and whose projector, sweeper, and worker record account turns. The fakes return real `StatusTurn(False)` values and do not mock assertions; the observable result is the ordered account history retained by the fake components.

```python
async def test_service_baseline_resolves_new_accounts_each_call(dynamic_service):
    service, state = dynamic_service
    state.surface = surface("acct_a")
    await service.baseline()
    state.surface = surface("acct_a", "acct_b")
    await service.baseline()
    assert state.baselined == ["acct_a", "acct_a", "acct_b"]


async def test_service_explicit_turn_rejects_account_removed_from_current_surface(
    dynamic_service,
):
    service, state = dynamic_service
    state.surface = surface("acct_a")
    with pytest.raises(ReportingNotificationError, match="status_account_unavailable"):
        await service.project_dirty_once(account_id="acct_b")


async def test_drain_uses_one_surface_snapshot_per_turn(dynamic_service):
    service, state = dynamic_service
    state.surface = surface("acct_a", "acct_b")
    assert await service.drain(max_turns=1) == 0
    assert state.resolver_calls == 1
    assert state.turn_accounts == ["acct_a", "acct_b"]
```

The drain fixture must return no work so the first turn exits normally. Its resolver changes the next available surface after the first component call; the asserted account history must still reflect the first snapshot.

- [ ] **Step 2: Run the lifecycle tests and verify RED**

Run:

```bash
uv run -m pytest tests/conformance/reporting/test_reporting_status_service_surface.py -q
```

Expected: the new account is absent from the second baseline, explicit turns still use the stale tuple, or the resolver is called more than once during a drain turn.

- [ ] **Step 3: Thread snapshots through service operations**

Make `_open()` check only closed state. Add async helpers that resolve a surface, require completeness/non-empty accounts, and validate explicit membership. `baseline()` resolves once and iterates the snapshot. Explicit project/sweep/expand/deliver calls resolve the current snapshot before dispatch.

Refactor `drain()` to resolve once at the beginning of each loop turn and invoke private component helpers with that same snapshot rather than calling the public methods, which would re-resolve. Preserve rebuild ordering and `status_drain_limit` behavior.

- [ ] **Step 4: Run lifecycle and existing capability tests**

```bash
uv run -m pytest \
  tests/conformance/reporting/test_reporting_status_service_surface.py \
  tests/conformance/reporting/test_reporting_status_capabilities.py -q
```

Expected: PASS with only environment-dependent PostgreSQL skips.

- [ ] **Step 5: Commit Task 2**

```bash
git add \
  src/adcp/reporting/outbox/status_service.py \
  tests/conformance/reporting/test_reporting_status_service_surface.py
git commit -m "fix(reporting): refresh status lifecycle account iteration"
```

---

### Task 3: Bind and prove the standard decisioning handler

**Files:**
- Modify: `tests/test_decisioning_serve.py`
- Modify: `tests/conformance/reporting/test_reporting_status_capabilities.py`
- Modify: `src/adcp/reporting/service.py`
- Modify: `src/adcp/decisioning/handler.py`
- Modify: `src/adcp/decisioning/serve.py`

**Interfaces:**
- Consumes: `ReportingStatusSupport` from Task 1.
- Produces: internal `ReliableReportingService._reporting_status_handler_for_support()` with the service's exact ledger, escalation, and consumer-status configuration.
- Produces: one-time `ReportingStatusSupport._bind_handler(handler)` used by the composition root.
- Preserves: `create_adcp_server_from_platform()` and `serve()` signatures.

- [ ] **Step 1: Write failing generated-handler binding tests**

In `test_decisioning_serve.py`, construct a support object with `handler=None`, call `create_adcp_server_from_platform(..., reporting_status=support, validate_at_init=False)`, and assert:

```python
assert support.handler is handler
assert handler._reporting_status is support
```

Add a mismatch test that pre-binds support to a different handler and expects
`ReportingNotificationError("status_handler_mismatch")` during construction.

In the reporting conformance test, install a `ReliableReportingService` backed by the same ledger as `PgStatusNotificationStore`, build the generated handler, and assert `await support.durable()` after baseline. Assert that a service with a different ledger or escalation policy remains unready. This exercises the real structural proof rather than a mock call.

- [ ] **Step 2: Run binding tests and verify RED**

```bash
uv run -m pytest \
  tests/test_decisioning_serve.py \
  tests/conformance/reporting/test_reporting_status_capabilities.py -q
```

Expected: support remains unbound and the generated handler lacks `reporting_status_handler`.

- [ ] **Step 3: Expose the installed authoritative status handler**

Add this internal method to `ReliableReportingService` and reuse it in `_get_reporting_status()`:

```python
def _reporting_status_handler_for_support(self) -> ReportingStatusHandler:
    return ReportingStatusHandler(
        self.store,
        escalation=self._escalation,
        consumer_status_enabled=self._consumer_status_enabled,
    )
```

During `PlatformHandler` initialization, when `_reliable_reporting_service` is installed,
assign `_reporting_status_handler_for_support()` to the generated handler's
`reporting_status_handler` attribute. The existing support proof
then validates direct and decisioning handlers through the same concrete type/store/policy
checks.

- [ ] **Step 4: Add one-time binding and composition-root wiring**

Implement:

```python
def _bind_handler(self, handler: ADCPHandler[Any]) -> None:
    if self.handler is not None and self.handler is not handler:
        raise ReportingNotificationError("status_handler_mismatch")
    object.__setattr__(self, "handler", handler)
```

Call `reporting_status._bind_handler(handler)` immediately after constructing
`PlatformHandler` and before any capability validator. Keep `validate_status_claims()`'s
identity check so a support object cannot validate another handler.

- [ ] **Step 5: Prove request fields cannot narrow the dynamic surface**

Add an async generated-handler test that invokes `get_adcp_capabilities()` with raw body
account IDs, an `ext.account_id`, and `ToolContext.metadata["account_id"]`. Configure the
deployment resolver to return two accounts with the second unbaselined. Assert the explicit
status notification claim is refused. Baseline the second account and assert the same
request succeeds without changing the resolver result.

- [ ] **Step 6: Run binding and request-isolation tests**

Run the Step 2 command. Expected: PASS with environment-dependent PostgreSQL skips only.

- [ ] **Step 7: Commit Task 3**

```bash
git add \
  src/adcp/reporting/service.py \
  src/adcp/decisioning/handler.py \
  src/adcp/decisioning/serve.py \
  tests/test_decisioning_serve.py \
  tests/conformance/reporting/test_reporting_status_capabilities.py
git commit -m "fix(reporting): bind generated handlers to status support"
```

---

### Task 4: Adopter documentation, type contract, and example

**Files:**
- Modify: `tests/type_checks/reporting_status_notifications.py`
- Modify: `tests/fixtures/public_api_snapshot.json`
- Modify: `examples/reporting_status_notifications.py`
- Modify: `docs/reporting-status-notifications.md`

**Interfaces:**
- Consumes: public symbols from Task 1.
- Documents: ESA's exact zero-argument resolver API and ordinary `serve(..., reporting_status=support)` call.
- Documents: package target `adcp>=9.6.3`; no storage-package bump and no changeset file.

- [ ] **Step 1: Add strict type-check usage**

Add a typed resolver and construction to the type-check fixture:

```python
async def account_surface() -> ReportingStatusAccountSurface:
    return ReportingStatusAccountSurface(
        account_ids=("acct_a",),
        account_surface_complete=True,
        readiness_subscriptions=(),
    )


def dynamic_support(support: ReportingStatusSupport) -> ReportingStatusSupport:
    return replace(support, account_surface_resolver=account_surface)
```

The empty tuple is valid at type-check time but fails readiness at runtime, which the docs
must state explicitly.

- [ ] **Step 2: Run the strict type check before documentation edits**

```bash
uv run mypy --strict tests/type_checks/reporting_status_notifications.py
```

Expected before imports/exports are complete: missing-name or incompatible-call failure.

- [ ] **Step 3: Update the lifecycle example**

Change the example composition to accept a `ReportingStatusAccountSurfaceResolver`, pass it
to support, and have the work loop resolve a fresh snapshot rather than close over the
startup `account_ids` sequence. Keep the static example path available by adapting its
existing inputs into a constant async snapshot.

- [ ] **Step 4: Update the operational documentation**

Replace the instruction to update/restart the startup enumeration with the dynamic
contract. Document:

- static tuples remain supported;
- dynamic snapshots are authoritative and are never merged with static fields;
- resolver data must come from one complete deployment-wide transaction/revision;
- request body, `ext`, and arbitrary context are never inputs;
- one unready account suppresses the global claim;
- `serve()` binds the generated handler automatically;
- service baseline/drain calls discover fresh accounts;
- ESA imports `ReportingStatusAccountSurface` and `ReportingStatusSupport`, supplies an
  async zero-argument resolver, and passes the support to ordinary decisioning `serve()`;
- ESA upgrades to `adcp>=9.6.3`; `adcp-reporting-storage` and wire schemas do not change.

- [ ] **Step 5: Run type checks and doc/example tests**

```bash
uv run mypy --strict tests/type_checks/reporting_status_notifications.py
uv run mypy --strict examples/reporting_status_notifications.py
uv run -m pytest tests/test_public_api.py -q
```

If `tests/test_public_api.py` reports snapshot drift, run
`uv run python scripts/regenerate_public_api_snapshot.py`, inspect the fixture diff to ensure
it contains only the intended outbox exports/signatures, and rerun the test.

- [ ] **Step 6: Commit Task 4**

```bash
git add \
  tests/type_checks/reporting_status_notifications.py \
  tests/fixtures/public_api_snapshot.json \
  examples/reporting_status_notifications.py \
  docs/reporting-status-notifications.md
git commit -m "docs(reporting): describe dynamic status account surfaces"
```

---

### Task 5: Full verification, review, and pull request

**Files:**
- Review: every file changed relative to `origin/main`.
- Modify only if verification exposes a defect in the approved scope.

**Interfaces:**
- Produces: a reviewable PR against `main` linked to AI-21766.
- Produces: PR description with exact ESA API, `adcp>=9.6.3`, no changeset/storage bump, and minimal usage.

- [ ] **Step 1: Format and lint only touched Python files**

```bash
uv run ruff format \
  src/adcp/reporting/outbox/status_support.py \
  src/adcp/reporting/outbox/status_service.py \
  src/adcp/reporting/outbox/__init__.py \
  src/adcp/reporting/service.py \
  src/adcp/decisioning/handler.py \
  src/adcp/decisioning/serve.py \
  tests/conformance/reporting/test_reporting_status_capabilities.py \
  tests/conformance/reporting/test_reporting_status_delivery.py \
  tests/conformance/reporting/test_reporting_status_service_surface.py \
  tests/test_decisioning_serve.py \
  tests/type_checks/reporting_status_notifications.py \
  examples/reporting_status_notifications.py
uv run ruff check --select I --fix \
  src/adcp/reporting/outbox/status_support.py \
  src/adcp/reporting/outbox/status_service.py \
  src/adcp/reporting/outbox/__init__.py \
  src/adcp/reporting/service.py \
  src/adcp/decisioning/handler.py \
  src/adcp/decisioning/serve.py \
  tests/conformance/reporting/test_reporting_status_capabilities.py \
  tests/conformance/reporting/test_reporting_status_delivery.py \
  tests/conformance/reporting/test_reporting_status_service_surface.py \
  tests/test_decisioning_serve.py \
  tests/type_checks/reporting_status_notifications.py \
  examples/reporting_status_notifications.py
uv run ruff check \
  src/adcp/reporting/outbox/status_support.py \
  src/adcp/reporting/outbox/status_service.py \
  src/adcp/reporting/outbox/__init__.py \
  src/adcp/reporting/service.py \
  src/adcp/decisioning/handler.py \
  src/adcp/decisioning/serve.py \
  tests/conformance/reporting/test_reporting_status_capabilities.py \
  tests/conformance/reporting/test_reporting_status_delivery.py \
  tests/conformance/reporting/test_reporting_status_service_surface.py \
  tests/test_decisioning_serve.py \
  tests/type_checks/reporting_status_notifications.py \
  examples/reporting_status_notifications.py
```

- [ ] **Step 2: Run focused verification**

```bash
uv run -m pytest \
  tests/conformance/reporting/test_reporting_status_capabilities.py \
  tests/conformance/reporting/test_reporting_status_delivery.py \
  tests/conformance/reporting/test_reporting_status_service_surface.py \
  tests/test_decisioning_serve.py -q
uv run mypy --strict tests/type_checks/reporting_status_notifications.py
```

- [ ] **Step 3: Run the broader reporting and decisioning suites**

```bash
uv run -m pytest tests/conformance/reporting tests/test_reliable_reporting_decisioning.py -q
uv run -m pytest tests/test_decisioning_serve.py tests/test_public_api.py -q
```

- [ ] **Step 4: Review the complete diff**

```bash
git diff --check origin/main...
git diff --stat origin/main...
git diff origin/main... -- \
  src/adcp/reporting \
  src/adcp/decisioning \
  tests \
  examples/reporting_status_notifications.py \
  docs/reporting-status-notifications.md \
  docs/superpowers
```

Confirm no wire model/generated-schema files changed, no request field reaches the resolver,
the resolver is called once per proof, and static positional construction remains covered.

- [ ] **Step 5: Invoke verification and code-review skills**

Use `superpowers:verification-before-completion` with the command outputs above, then
`superpowers:requesting-code-review`. Resolve every correctness finding and rerun affected
checks before pushing.

- [ ] **Step 6: Push and open the PR**

Use `scope3-developer:push-pr`. The PR title is:

```text
fix(reporting): resolve dynamic status account readiness
```

The body must link AI-21766 and include this exact adopter contract:

```python
from adcp.decisioning import serve
from adcp.reporting.outbox import ReportingStatusAccountSurface, ReportingStatusSupport

async def resolve_status_surface() -> ReportingStatusAccountSurface:
    current = await registry.complete_status_surface()
    return ReportingStatusAccountSurface(
        account_ids=tuple(current.account_ids),
        account_surface_complete=current.complete,
        readiness_subscriptions=tuple(current.readiness_subscriptions),
    )

support = ReportingStatusSupport(
    store=status_store,
    worker=status_worker,
    projector=status_projector,
    sweeper=status_sweeper,
    scheduled=True,
    account_surface_resolver=resolve_status_surface,
)
serve(platform, reporting_status=support)
```

State that ESA requires `adcp>=9.6.3`, no `adcp-reporting-storage` bump, no changeset file,
and no wire-schema change.

- [ ] **Step 7: Monitor the PR without merging**

Use `scope3-developer:pr-feedback-loop` until required checks are green and review feedback
is addressed. Never merge. Ensure the current branch remains checked out so Conductor shows
the PR.
