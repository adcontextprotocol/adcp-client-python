# Dynamic Reporting Status Account Surface Design

## Problem

`ReportingStatusSupport` currently captures `account_ids` and
`readiness_subscriptions` when the process starts. Reliable Reporting accounts can be
created and configured while that process remains alive, so the captured tuples cease
to represent the complete deployment account surface. Capability readiness can then
either omit `reporting.status_changed` after the new account is ready or continue to
advertise it without proving the new account.

The capability request has no account selector. Request body fields, `ext`, and arbitrary
`ToolContext.metadata` therefore cannot select or narrow the readiness surface. The ESA
integration needs a deployment-wide source that can return the current complete surface
without restarting the server.

The standard `create_adcp_server_from_platform()` and `serve()` composition also creates
the `PlatformHandler` after `ReportingStatusSupport` is constructed. The current support
object can validate only a preconstructed `ReportingStatusNotificationHandler`, so that
standard decisioning path cannot establish the handler identity proof.

## Goals

- Resolve the complete current account and signing-readiness surface asynchronously at
  each capability/readiness proof.
- Use one immutable surface snapshot consistently throughout one proof.
- Preserve the existing static `ReportingStatusSupport` constructor fields, positional
  order, and behavior.
- Fail closed for every incomplete, empty, inconsistent, or unavailable proof.
- Bind and validate the handler produced by the standard decisioning server factory.
- Let lifecycle and worker iteration discover accounts added after process startup.
- Keep the AdCP wire schema and consumer enrollment requirements unchanged.

## Non-goals

- No capability request-body account selector is added.
- No request-scoped account-surface resolver is added in this release. The resolver is a
  zero-argument deployment-wide source, so request parameters and context cannot affect
  the proof.
- No weakening of `webhook_signing.supported`, `reporting.ledger_changed`, or
  `reporting.status_changed` consumer gates.
- No SDK-owned scheduler, account registry, or cache is introduced.

## Public API

`adcp.reporting.outbox` will export two additional symbols:

```python
@dataclass(frozen=True, kw_only=True)
class ReportingStatusAccountSurface:
    account_ids: tuple[str, ...]
    account_surface_complete: bool
    readiness_subscriptions: tuple[ReportingNotificationSubscription, ...]


class ReportingStatusAccountSurfaceResolver(Protocol):
    async def __call__(self) -> ReportingStatusAccountSurface: ...
```

`ReportingStatusSupport` keeps every existing field unchanged and appends one optional
field:

```python
account_surface_resolver: ReportingStatusAccountSurfaceResolver | None = None
```

It also exposes the resolved snapshot for lifecycle composition:

```python
async def resolve_account_surface(self) -> ReportingStatusAccountSurface: ...
```

When `account_surface_resolver` is absent, `resolve_account_surface()` creates the snapshot
from the existing `account_ids`, `account_surface_complete`, and
`readiness_subscriptions` fields. When the resolver is present, its returned snapshot is
the complete source of proof; static fields are not merged into it. This prevents evidence
from different revisions from being combined.

The resolver must return the current complete deployment surface. A plain async function
conforms to the resolver protocol. Because it receives no request or context arguments,
capability body fields, `ext`, caller-controlled metadata, and arbitrary account IDs cannot
influence the result.

## Snapshot and proof semantics

`ReportingStatusSupport.durable()` resolves exactly one surface snapshot. It validates the
queue/store/projector/sweeper/worker chain and the bound authoritative handler, then checks
every unique account in that snapshot using only that snapshot's readiness subscriptions.
It does not call the resolver again while proving any account.

The global claim is omitted, and an explicit claim is refused, when any of these conditions
holds:

- the resolver raises or returns the wrong type;
- `account_surface_complete` is false;
- `account_ids` is empty or contains an empty ID;
- `readiness_subscriptions` is empty;
- any account has no matching signing-readiness subscription;
- any account is not baselined or has unsupported managed-delivery state;
- any active subscription or signing sender is invalid;
- the durable store, worker, cipher, projector, sweeper, or schema proof is invalid;
- the authoritative `get_reporting_status` handler is absent or is not the handler bound
  to this support object.

Resolver failures are logged without surface contents and translated into the existing
secret-free readiness refusal. Cancellation continues to propagate.

The existing `account_ready(account_id=...)` method resolves one snapshot and checks the
requested account against it. The internal proof accepts a snapshot explicitly so
`durable()` never re-resolves between accounts.

## Standard handler binding

`ReportingStatusSupport` gains a one-time handler-binding operation used by the decisioning
composition root. Binding is idempotent for the same handler and refuses a different
handler. Existing adopters that pass `handler=` retain that exact binding.

After `PlatformHandler` construction and before boot-time capability validation,
`create_adcp_server_from_platform()` binds the generated handler to the supplied support.
For a platform with an installed `ReliableReportingService`, the generated handler exposes
a `ReportingStatusHandler` configured with that service's ledger, escalation policy, and
consumer-status setting. The existing support proof can therefore validate the same
store/policy invariants for direct and generated handlers. A generated handler without the
installed authoritative reporting service remains unready.

`serve()` already delegates through `create_adcp_server_from_platform()`, so it receives
the same binding without an ESA-specific server wrapper.

## Lifecycle iteration

`ReportingStatusService` resolves a fresh complete surface for `baseline()` and for each
drain turn. One drain turn uses one snapshot for its account iteration. Explicit account
turns resolve the current surface and refuse an account not present in it.

This behavior lets a newly enrolled account participate in baseline, projection, sweep,
expansion, and delivery without a process restart. It also prevents an account removed
from the complete source from being accepted merely because it appeared in a constructor
tuple. Selector-epoch rebuild remains deployment-wide and independent of the account
surface, as it is today.

## ESA integration

ESA supplies an async deployment-wide resolver and passes the support object to the
ordinary `serve()` call:

```python
from adcp.decisioning import serve
from adcp.reporting.outbox import (
    ReportingStatusAccountSurface,
    ReportingStatusSupport,
)


async def resolve_status_surface() -> ReportingStatusAccountSurface:
    surface = await reporting_registry.complete_status_surface()
    return ReportingStatusAccountSurface(
        account_ids=tuple(surface.account_ids),
        account_surface_complete=surface.complete,
        readiness_subscriptions=tuple(surface.readiness_subscriptions),
    )


support = ReportingStatusSupport(
    store=status_store,
    worker=status_worker,
    projector=status_projector,
    sweeper=status_sweeper,
    scheduled=True,
    account_surface_resolver=resolve_status_surface,
)

serve(platform_with_reliable_reporting, reporting_status=support)
```

ESA does not pass `handler=` and does not restart when accounts change. Its resolver must
read account IDs and readiness subscriptions from one consistent registry transaction or
revision so the returned object is a truthful immutable snapshot.

## Tests

Tests follow red-green-refactor and cover these observable contracts:

1. A resolver-backed support object sees an account added after construction, suppresses
   readiness until that account is baselined, and becomes ready without restart.
2. One unbaselined or signing-unready account suppresses the global claim.
3. Resolver exceptions omit automatic advertisement and refuse an explicit claim.
4. Existing positional and keyword static construction continues to work unchanged.
5. Capability request body, `ext`, and arbitrary context account IDs cannot narrow a
   deployment-wide resolver snapshot.
6. `create_adcp_server_from_platform()` binds its generated handler before validation and
   rejects mismatched handler ownership.
7. A resolver that would return a different result on a second call is invoked once per
   proof, and every account check uses the first snapshot.
8. Service baseline and drain iteration resolve fresh surfaces and use one snapshot per
   iteration.
9. Public imports and strict type-check examples include the new symbols.

The focused reporting conformance suite, decisioning serve suite, formatting, lint, and
strict type checks for touched surfaces must pass before the pull request is opened.

## Packaging and rollout

This is an additive SDK API change with no storage-package or wire-schema change. The pull
request uses a conventional `fix(reporting): ...` title so release-please selects the next
patch release. The package target for ESA is `adcp>=9.6.3`; no
`adcp-reporting-storage` version change is required. This repository does not use changeset
files, so the conventional fix commit is the complete release input.
