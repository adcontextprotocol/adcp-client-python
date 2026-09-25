# Reliable Reporting upgrade and release notes

These notes describe the integrated changes prepared for the next SDK 8
prerelease. The release version, published distributions, complete service
acceptance and Python/TypeScript interoperability qualification are still
pending. Include these boundaries in the reviewed release proposal; merging
this document does not publish or qualify a package.

## Account-qualified configuration identity

`ReportingConfiguration.generation_key` now returns the frozen
`ReportingConfigurationGenerationKey(account_id, delivery_config_id,
delivery_config_version)` value instead of the beta.15 two-tuple. Replace tuple
unpacking and positional indexing with named attributes. Include the account
when indexing configurations or resolving leased work.

Older workers do not use the account-qualified key safely when accounts reuse
a configuration ID. Drain them before upgrading the PostgreSQL ledger, run
the upgraded store's `create_schema()`, and restart every participant on the
upgraded SDK. The [ledger migration guide](reporting-ledger-migration.md)
describes preserved records, locking and adopter-managed migrations.

## Rolling upgrade boundaries

The compatibility controls use the following exact integrated Git snapshots.
They are comparison inputs, not released package versions or a promise that
all operating modes can run together. Earlier snapshots of each component are
untested and unclaimed by these comparisons, even if they share ancestry.

| Component | Integrated comparison snapshot | Excluded earlier snapshots |
| --- | --- | --- |
| A: durable notification outbox | `17ee407ae3978c8a2bb54437287afbf9dafb8130` | Pre-`17ee407a`; the older `21bf443e` schema fingerprints depend on database collation. |
| B: webhook activity | `0f34c666ac1961e9832fce43ef0ef6937b3c1dde` | Pre-`0f34c666`, including the original `198d50e6` feature snapshot. |
| C: status notifications and rc.6 | `967b6e286301d7e5d089aea6fdbb90bea8ee5a16` | Pre-`967b6e28`, including `ea150fab`. |
| B1: strict selection and destination contracts | `5487f2bdef23c5102118b305be9e868228f6ce61` | Pre-`5487f2bd`, including `1c91311e`. |
| B2.1: durable materializer | `3fd62121c96a074e3ea458c30c5224d6a586f169` | Pre-`3fd62121`; the older `8e18ca12` scheduler can sort `served_at` as text. |
| B2.2: authenticated receipt ingress | `09fd87f79a746665d828dea66b3a1dd9d1fc189e` | Pre-`09fd87f7`, including `74b338d8`. |
| B2.3: frozen authorized feed | `2d777ace7b4bf8be519ce0abd4fd0a25ed4f1da7` | Pre-`2d777ace`, including `50e35f0a`. |
| Schema-proof and receipt-diagnostic hardening | `e16eb8cf3074cabd45aab42840950f05ad6d2b43` | Pre-`e16eb8cf`. |
| Production composition | `34c8f6d929aeac3407e2f595104a8e903e572623` | Pre-`34c8f6d9`. |

An older A outbox's aggregate notification readiness remains false on the C
schema, both before and after later migrations. These controls do not establish
whole-trigger startup support for A after C. Supported historical reads and
writes do not authorize concurrent incompatible autonomous materializers,
status projectors or clock sweepers. Follow the component-specific migration
procedures before starting those workers.

## Current and historical protocol versions

Live reporting mounts and callers use AdCP `3.2-rc.6`, whose bundle spelling is
`3.2.0-rc.6`. Omit an explicit pin to use the packaged default, or configure the
current supported reporting version consistently across the composition and
its mounts. Cross-cohort reporting requests are rejected even when optional
request validation is disabled.

Historical beta.6 and rc.3 schema bundles support offline validation and
retained historical walks. Their presence does not enable a historical live
mount or client pin. In rc.6, an exactly scoped bilateral waiver can retire
the public mismatch and project underlying seller health while retaining the
consumer evidence; rc.3 forbids waiver alone from clearing that health. The
current implementation therefore cannot advertise the historical live contract.

The explicit rc.6 mount minimum does not remove a previously accepted
configuration from the integrated SDK: its shared version resolver already
rejected older prerelease pins before the mount's minimum-version check.
See [signed protocol inputs and history boundaries](protocol-3.2-rc6.md)
for bundle identity, versioned status and continuation rules.

## Choose the installed feature tier

Capabilities describe the actual mounted composition and its readiness.
Installing the package or adding a capability flag does not activate a tier.

| Surface | Required composition |
| --- | --- |
| Core polling | A configured producer and source, retained immutable reporting records, and the applicable authenticated status/exact-read routes. |
| Core notifications | An eligible Core composition with durable queues, trusted subscriptions, the matching running notification workers and signing configuration. The frozen-feed store alone does not enable advertisement. |
| Managed delivery | Trusted source and destination contracts, a durable materializer with verified readback, complete status/exact reads, and the production configuration route. Polling does not require receipt ingress or HTTP notification workers. |
| Reconciled billing | Managed delivery plus official finality, canonical verification, and mounted revision/adjustment receipt ingress. |

Use the [production composition guide](reporting-production.md) for lower-level
durable wiring and activation. The [service guide](reliable-reporting-service.md)
and [source adapter contract](reporting-source-adapters.md) describe adapter
registration and lifecycle. The service's PostgreSQL factory makes the ledger
durable; default adapter staging and replay seals remain in memory. Supply
durable implementations if retained revisions must remain readable after a
restart. Multiple service schedulers still need a single account/configuration
lease owner until that integration is enabled.

## Migration and activation

1. Choose the intended tier and review the relevant historical comparison
   boundary above. Keep a maintenance window for schema migration and draining
   incompatible workers.
2. Drain older ledger writers and autonomous materializers, status projectors
   and clock sweepers. Reconcile uncertain external effects using the
   [materializer recovery procedure](reporting-durable-materializer.md#recovery-and-retention).
   Preserve the original external idempotency identities of pending attempts.
3. Run the upgraded stores' `create_schema()` methods before starting reporting
   workers. Follow the [ledger migration guide](reporting-ledger-migration.md)
   and [production migration sequence](reporting-production.md#migration-drain-and-activation).
4. Construct the actual source/provider/verifier and durable worker components,
   mount the support's authenticated handler, then start and activate the
   production support as described in that sequence. A schema-only upgrade
   does not establish readiness.
5. Check the advertised tier and exact reads, then exercise polling, enabled
   notifications and receipts as applicable. Validate restart and recovery
   using the intended installed package and the adopter's configuration before
   enabling production traffic.

Activation preserves epoch-zero work identities and permanently quarantined
readiness events. It does not promote old work or backfill an external
idempotency history. Only new qualified work enters the production epoch.

## Operational limits

- Positive schema-proof caches reduce repeated catalog discovery. They do not
  cache authorization or replace live topology checks, and they do not
  establish performance under load. Stop and drain the support for later DDL,
  migrate, construct fresh support and validate before restarting.
- Sampling hints can race between concurrent calls on one store and repeat
  candidate scans. Account locks, row locks and rechecked lease predicates
  still govern acquisition. No concurrent fairness guarantee is added.
- A changed snapshot boundary can invalidate a cursor and fail closed. These
  changes add no snapshot-retention or continuous-write pagination-liveness
  promise. Follow the [frozen feed contract](reporting-frozen-feed.md) for
  authorization, checkpoints and retained-walk behavior.
- Configure authentication and network access deliberately. The generic
  server's wildcard bind default and optional authentication do not provide
  a private deployment boundary. Production reporting routes require trusted
  account and consumer resolution.

Complete adapter-first service acceptance and the installed Python/TypeScript
stable/candidate interoperability matrix remain required for the full rollout
([#1172](https://github.com/adcontextprotocol/adcp-client-python/issues/1172),
[#1199](https://github.com/adcontextprotocol/adcp-client-python/issues/1199)).
Partial storyboard results, component comparisons and source integration do
not establish that acceptance. Publication and subsequent registry-install
verification follow the [guarded release runbook](releasing.md).
