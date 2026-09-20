# AdCP 3.2.0-rc.4 adoption

The default protocol is the published, signed `3.2.0-rc.4` prerelease. The SDK
continues to package explicit `3.2-rc.3` validation for historical reporting
walks, together with the existing stable 2.5, 3.0 and 3.1 bundles. The `v32`
model module retains its documented beta.6 schema identity. Changing the
current protocol pin does not relabel historical models or persisted data.
The distributions now also include the existing beta.6 cache: the accepted
B2.4 wheel omitted it, leaving `v32` models unavailable offline. These historical
bytes come unchanged from the accepted B2.4 tree, not from the rc.4 signed bundle.

## Immutable inputs and transformations

The authoritative release is
[v3.2.0-rc.4](https://github.com/adcontextprotocol/adcp/releases/tag/v3.2.0-rc.4),
target `94976657c8456e5ad6de55d9793a883542a4fc5f`. The original tarball has
SHA-256 `773bee016d279345d6fae91a0ce684a22ca9b81c2edd2cdb9a71dbfea65954d2`.
`schemas/releases/3.2.0-rc.4.json` records the versioned official URL, byte
lengths and digests of the tarball, checksum, signature and certificate.

Syncing this audited pin requires all four exact assets and successful
`cosign verify-blob` with certificate identity
`https://github.com/adcontextprotocol/adcp/.github/workflows/release.yml@refs/heads/main`
and issuer `https://token.actions.githubusercontent.com`. Certificate and
transparency checks stay enabled. A missing verifier, missing sidecar, changed
asset, signature failure, signature bypass or alternate base URL fails the
sync. This pin never falls back to `latest`. `--require-signed-release` also
rejects versions without an audited descriptor.

Independent verification used cosign v3.0.6, checked official CDN bytes against
GitHub release asset digests and committed protocol blobs, and verified the
rc.3 comparison bundle separately. The certificate's source commit is
`8a4aac73c89e350ab2f59dcd1561b27336c9ccfa`, the release target's direct parent.
The upstream Version Packages workflow builds/signs the prebuilt artifacts;
the release workflow then verifies/publishes their committed bytes. Successful
signing run `35475167268` and the source-pinned release workflow establish that
relationship. The manifest has no source-commit field.

Generation follows the repository pipeline:

```sh
python scripts/sync_schemas.py --require-signed-release
python scripts/fix_schema_refs.py
python scripts/bundle_schemas.py
python scripts/generate_versioned_stubs.py
python scripts/generate_types.py
python scripts/consolidate_exports.py
python scripts/generate_ergonomic_coercion.py
```

The signed archive is inspected for its exact root, regular files/directories,
unique paths and traversal before extraction. Its 4,111 files include 1,613
schemas and 141 compliance vector files. Reference repair removes `$id`,
converts `/schemas/` references to relative cache paths and serializes JSON
with two-space indentation and no final newline. Bundling copies those cache
bytes into the package. Code generation additionally normalizes references,
stabilizes nested discriminators, applies the existing public-model repairs
and consolidates exports. Generated models are not hand edited.

`adcp/_compliance/3.2.0-rc.4/provenance.json` records both original signed and
transformed cache hashes for every schema, the repair script hash, and exact
original hashes for the 141 vector files, reporting-core compliance document
and bundle manifest. Transformed cache bytes are not represented as signed
tarball byte identity. Both VCS wheels and sdist-built wheels include the
current cache and these offline fixture assets.

## Reporting versions and frozen history

`ReportingProductionSupport(adcp_version=...)` selects the public handler's
rendering and mounted MCP/A2A validation contract. Production discovery advertises
that exact usable pin in both `adcp_version` and `adcp.supported_versions`.
Clients continuing a retained walk must select the matching pin.
Reporting constructors reject stable 3.0/3.1 pins because those bundles have no
reporting schemas; general SDK support for those versions remains available.
The production pin is frozen for the composition's lifetime. Changing its value
or getter invalidates the existing cheap component proof, even after a positive
schema check. Change protocol versions by creating a new composition and mounts.

The normative rc.4 response rule distinguishes four cases:

| Response | `next_expected_at` |
| --- | --- |
| Complete summary | Nearest committed period **start** strictly after `ledger_as_of`, across captured configuration generations in the selected scope |
| Open summary | Next obligation due instant |
| Complete periods | Absent, as required by the authoritative rc.4 schema |
| Open periods | Existing obligation due-time projection |

For an hourly configuration activated at 00:00 with a one-hour SLA, a complete
summary at 00:30 forecasts 01:00 under rc.4. Explicit rc.3 retains 02:00. The
producer still creates the period's obligation only after period close and
sets its `expected_at` to period end plus SLA. Delivery webhook timing is a
separate contract and is unchanged.

The forecast uses the captured configuration rather than a mutable registry
read. Full committed periods start at or after activation and strictly before
deactivation. Mid-period deactivation retains that already-started full period
and its SLA; it creates no later period. Forecasting creates no obligations,
leases, coverage or counts and does not change health. Civil-time/DST and
UTC-offset handling use the same period boundaries as the producer.

Set the public `adcp_version` constructor argument on
`ReportingReceiptHandler`, `ReportingStatusNotificationHandler` or
`ReportingProductionSupport` to select the mount's rendering and advertised
schema. Its default is rc.4. The public `ADCPClient(..., adcp_version=...)`
selects the corresponding client contract. MCP and A2A use the handler's
public `get_adcp_version()` hook; a plain attribute named `adcp_version` is
not a mount pin. Requests crossing the rc.3/rc.4 boundary on a frozen reporting
mount fail with actionable `VERSION_UNSUPPORTED`, including when optional
transport validation is disabled.

Transport schemas are materialized once per tool/direction in each immutable
protocol-version loader state. Concurrent first callers share that work;
failed materializations are not cached. Each caller receives an independent
JSON tree without shared mutable branches. A pinned mount copies only tool
metadata before selecting its versioned schemas, rather than copying current
model schemas that it immediately replaces. The current-model fallback is
unchanged. Installed schema assets remain fixed for the process lifetime;
upgrading those assets requires a new process, and the test reset clears all
loader state. This schema cache does not cache handler selection, authorization,
reporting capability proofs, pool lifecycle, or mutable reporting state.

New rc.4 periods snapshots bind the normalized protocol version in their
existing semantic-filter representation. Historical unmarked filters retain
rc.3 meaning. Stored page membership, counts, `as_of`, checkpoints and snapshot
bytes are not rewritten. Authentic positions presented under a different
protocol produce `REPORTING_FEED_VERSION_MISMATCH`; callers must continue on
their original version or deliberately start a fresh walk without a cursor or
checkpoint. Authentication and position verification precede that diagnosis.

The supported B2.4 continuation path installs the actual accepted artifact
`e3a44d281d019ebf6aec738c2cfe18f8bba97462`, tree
`06a0abcfe38392501455fbda806be69871d6f0c6`, creates an rc.3 page/snapshot/checkpoint,
then continues with this SDK on an explicit rc.3 mount after process restart.
Old complete-periods pages can retain a future timestamp. Serving those bytes
under an rc.4 output schema is unsupported; preserving their stored bytes
does not make that combination schema compatible. The narrowly scoped rc.3
effective-schema correction remains intact. Current rc.4 validates through
the authoritative modular/bundled schemas without that exception; older
explicit prerelease failures remain unchanged.

No SQL migration, manifest/catalog identity change or historical snapshot
rewrite is introduced. Drain workers before changing a mount's protocol pin.
Retain a version-aligned rc.3 continuation route while serving retained rc.3
positions. A rollback to B2.4 cannot serve newly created rc.4 walks: route those
to an rc.4-capable instance or explicitly restart them under rc.3. Existing
production admission, writer/projector drain and recovery requirements in
`reporting-production.md` still apply.

## Other schema and public-model changes

| Surface | Adopted declaration/wire change |
| --- | --- |
| `SyncAccountsAccount` | Adds optional `account: AccountReference`; `brand`, `operator` and `status` become nullable/default `None`. Raw schemas require either account reference or brand/operator identity; successful items require status and failed items require a nonempty errors list. |
| Capability discovery | Adds optional `anonymous_discovery` to media-buy and signals. The authoritative schema rejects anonymous discovery combined with account-required product discovery. |
| Collections | Adds optional publisher namespace with its domain constraint. |
| Registry badge events | Adds optional `grading_profile` (`legacy` or `spec`). |
| Compliance controller | Adds `advance_past_status_deadline` and `advance_past_escalation` operation declarations. This adoption does not implement an unrelated new service scheduler. |
| Verification token claims | Adds the upstream schema and generated internal model; no new curated public export or token-verification promise. |
| Error/adagents/controller/discovery metadata | Carries upstream description, recovery documentation and specialism/index updates. |

The account annotation widening is a deliberate source compatibility change:
adopters must handle absent brand/operator/status for the newly permitted
reference/error forms. Empty item error lists are now invalid. Conditional
wire constraints remain enforced by schema validation. Public semantic aliases,
lazy imports and export names stay stable; private generator class numbers are
not a semantic API. Regeneration also updates inherited private generator drift;
parent nonzero diagnostics are retained separately from current generation.
The generator also identifies the unchanged registry track verdict
`Tracks.pass_ = 'pass'` with a line-specific Bandit B105 annotation. The fixed
protocol value is not a credential. Syntax and enum values remain unchanged;
unrelated assignments retain normal scanning. The original parent and commit-hook
false-positive outputs and a negative scanner control are retained with the audit.

The raw ReportingDeliveryCapabilities schema still carries the rc.3 defaults
and conditional rules. The existing `fix_reporting_capability_defaults` repair
remains necessary in both canonical and bundled Python models: all four task/
notification promises are nullable/default `None`, absent promises stay absent
through nested/subclass round trips, and inverse/cumulative tier validation
rejects false claims before serialization. Status and ledger notifications are
independent opt-ins. No serialization-only replacement is introduced.

## Fixture execution and inherited acceptance

The complete-summary fixture supplies 22 structural cases, executed against
modular, bundled and MCP rc.4 schemas with date-time checking. Runtime tests
separately exercise producer boundaries, mounted MCP/A2A, immutable continuations,
real PostgreSQL, installed artifacts and the exact B2.4 installed baseline.
Structural vectors do not prove scheduling behavior.

The request-signing inventory comprises 48 files (47 JSON files and a README).
Exactly 35 request bodies changed; signature inputs, keys, headers, signatures,
verdicts and reference times did not. The request fixture manifest is refreshed
with the original upstream bytes. All 31 webhook-signing files and 20 reporting
reconciliation files are unchanged. Current products-only vectors and skills
are refreshed by sync. All remaining vector groups are carried offline with
provenance; carrying a vector does not claim its domain's runtime implementation.
The 14 error-recovery cases exercise the schema contract, while the supply-path
vectors remain inputs for their separately owned verification behavior.

The existing thirteen B2 acceptance rows remain required: durable materializer;
canonical authentication; immutable receipt transactions; exact batch replay;
middleware/schema boundaries; receipt financial graph; authorized frozen feed;
checkpoint/closure; versioned status capture; tier-correct projection; private
scope/ownership; truthful production capabilities; and actual rolling binaries.
No SQL identity, diagnostic privacy, fail-stop behavior, numeric precision guard,
signature canonicalization or capability-proof cache rule is relaxed here.

Final acceptance comprises the inherited 17 functional groups and 15 required
static/generated/hooks gates, plus these rc.4 and B2.4 continuation regressions
and installed Python 3.10–3.13 checks. Original command/log/hash records identify
the tested commit and distinguish development failures, parent diagnostics,
source tests, installed inner suites and outer harness counts. Overlapping
counts are never summed. The final PR carries the actual evidence inventory.

This adoption does not qualify a TypeScript release, complete #1172, authorize
integration or choose a Python stable version. The separately reviewed service
stack, independent experts and real-process four-quadrant MCP HTTP matrix remain
required. Release Please and trusted publishing remain the normal release path.
