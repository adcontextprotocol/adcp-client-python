# Signed AdCP 3.2.0-rc.6 inputs and reporting integration

The default protocol remains `3.2.0-rc.6`, already integrated with the reporting
stack. This change packages its signed compliance fixtures and provenance,
adds immutable historical schemas for offline use, and retains the current
live-version policy. It does not regenerate the integrated rc.6 models or
rewrite any existing cached schema.

## Immutable inputs and transformations

The authoritative release is
[v3.2.0-rc.6](https://github.com/adcontextprotocol/adcp/releases/tag/v3.2.0-rc.6),
target `f7932355a52f81c64f4c8ff8cc0a36f57677d711`. The original tarball has
SHA-256 `2837bcd4ee2d74b326ffff573ee0cdeb87b3920967fb99bc02671c51718834dd`.
`schemas/releases/3.2.0-rc.6.json` records the official versioned URL and exact
byte lengths and hashes of the tarball, checksum, signature and certificate.

Syncing an audited pin requires all four exact assets and successful
`cosign verify-blob` with certificate identity
`https://github.com/adcontextprotocol/adcp/.github/workflows/release.yml@refs/heads/main`
and issuer `https://token.actions.githubusercontent.com`. Certificate and
transparency checks remain enabled. Missing assets or verifier, changed bytes,
signature failure, a signature bypass or an alternate base URL fails the sync.
An audited pin never falls back to `latest`; `--require-signed-release` also
rejects versions without an audited descriptor.

The archive is inspected for its exact root, unique regular file paths and
traversal before extraction. Its manifest enumerates 4,131 files, including
1,620 schemas. Reference repair removes `$id`, converts `/schemas/` references
to relative cache paths and serializes JSON with two-space indentation and no
final newline. The transformed hashes match all 1,620 existing rc.6 cache
files. The signed archive and transformed cache are distinct byte identities.

`adcp/_compliance/3.2.0-rc.6/provenance.json` records the original and transformed
hashes for every schema, the repair-script hash, and the exact original hashes
of 143 packaged inputs: 141 vector files, the reporting-core compliance document
and bundle manifest. VCS wheels and sdist-built wheels must preserve these
assets and the current schema cache. A vector's presence is not a claim that
its domain has a runtime implementation.

## Offline schemas and live versions are different contracts

Distributions include the stable 2.5, 3.0 and 3.1 bundles, the existing immutable
beta.6 and rc.3 caches, and the current rc.6 cache. Historical `adcp.types.v32`
models keep their beta.6 identity. Their schema bytes come from the retained
repository history, not the rc.6 signed bundle. The original feature's rc.4
cache and release inputs remain historical source records; they are not the
current installed contract.

Adding offline beta.6 and rc.3 files is an intentional packaging change from
integrated PR1192. It does not change its recorded wheel inventories. Installed
controls assert the actual selected current and historical roots inside the
installed prefix, compare the complete historical map against a separately
copied immutable test reference outside both the prefix and workspace, and
repeat those checks after conformance and the strict adopter. Expected bundle
mappings alone do not prove whole-wheel membership or absence; actual archive
inventories provide that separate evidence.

The live SDK policy remains stable 3.0, stable 3.1 and current 3.2-rc.6. Reporting
mounts require the current reporting contract; stable versions lack its reporting
schemas. Offline rc.3 validation does **not** enable an rc.3 reporting mount or
client pin. Its immutable waiver rules differ from rc.6: the current exact-scope
bilateral waiver projects underlying seller health, while rc.3 prohibits that
health recovery. Advertising rc.3 from this implementation would therefore make
a promise that packaging historical schemas cannot satisfy. Older and future
unadvertised pins continue to fail construction.

`ReportingReceiptHandler`, `ReportingStatusNotificationHandler` and
`ReportingProductionSupport` use the public `adcp_version` constructor argument
and `get_adcp_version()` hook for rendering and mounted MCP/A2A schemas. The
default is rc.6, including an unnegotiated request on these reporting mounts.
General SDK fallback behavior is unchanged. Production discovery advertises the
composition's frozen usable pin; changing its value or getter invalidates the
component proof even after positive schema validation. Create a new composition
and mounts when changing that contract.

## Forecasting and stored boundaries

The integrated rc.6 response contract distinguishes four cases:

| Response | `next_expected_at` |
| --- | --- |
| Complete summary | Nearest committed period start strictly after `ledger_as_of`, across captured configurations in scope |
| Open summary | Next obligation due instant |
| Complete periods | Absent under the authoritative rc.6 schema |
| Open periods | Existing obligation due-time projection |

Forecasting uses the captured configuration and creates no obligation, lease,
coverage, count or health change. Civil-time/DST and UTC-offset behavior use the
producer's period boundaries. Historical direct-store rc.3 representations retain
their due-time semantics; their readability does not advertise live rc.3 support.
The existing rc.3 effective-schema correction is retained only for that version.

New rc.6 snapshots bind the normalized version in their semantic filters.
Authentication, caller, token and position checks precede version diagnostics.
A position cannot move between version-bound contracts. The already-integrated
exception for an authenticated, unmarked representation-v1 snapshot with no
ownership mode remains narrowly scoped to rc.6 and exact equality of every
other filter. It neither rewrites stored bytes nor generalizes to other
versions, marked filters, representation-v2 or ownership-bound walks.

The installed comparison is now integrated PR1192 commit
`34c8f6d929aeac3407e2f595104a8e903e572623`, tree
`2dd33404cb50e6d87ae875ccfa1c983e7faabd44`. It creates an actual rc.6 page,
snapshot and checkpoint, then checks current VCS/sdist continuations and cold
restarts after configuration/time changes. Stored documents, page bytes, counts
and checkpoints must remain identical; mismatched versions must fail through
both store and mounted boundaries. This comparison does not qualify older
pre-`34c8f6d9` snapshots or the original feature's explicit rc.3 live route.

All nine rolling release-note exclusions remain mandatory: pre-`17ee407a` A,
pre-`0f34c666` B, pre-`967b6e28` C, pre-`5487f2bd` B1, pre-`3fd62121` B2.1,
pre-`09fd87f7` B2.2, pre-`2d777ace` B2.3, pre-`e16eb8cf` hardening, and now
pre-`34c8f6d9` production. Old A whole-trigger startup after C remains unsupported.
Earlier false notification-readiness results are not promoted by later source
integration. Existing worker-drain, ownership and recovery rules still apply.

## PostgreSQL and verification boundaries

The lease integration takes the account lock before configuration-row locks,
including for a base store sharing a higher-tier schema. Sampling is bounded
and advances past a busy prefix. Standalone acquisition can briefly join the
account-lock queue; caller transactions never gain that blocking edge. Lease
updates preserve the materializer generation when only lease fields changed,
and release remains fenced by worker and exact expiry. Deployments must drain
old row-first autonomous workers before introducing this lock order.

Catalog validation batches each object kind across the schema while retaining
fresh reads on the caller's connection and exact object fingerprints. No SQL
DDL, required manifest, tolerance or schema-presence cache changes accompany it.
The separate per-instance schema-proof cache and its invalidation remain intact.

The existing alias-free schema materialization cache remains per immutable
loader state. Successful results are reused; failed/missing loads may retry.
First loads of different keys serialize on the state lock; warm reads bypass
it. No cold-throughput improvement, authorization caching or runtime activation
is inferred from that cache.

The signed summary fixture supplies 22 structural cases across modular, bundled
and MCP schemas. Other controls cover exact provenance, historical offline
models, account result branches, error-recovery declarations, timestamps,
current mounted contracts, old-pin rejection, PostgreSQL locking and installed
restarts. Structural vectors do not establish scheduling behavior. Installed
progress records and retained phase logs support diagnosis; they do not turn
partial execution into acceptance.

Fresh CI must establish the complete matrix, receipt shards and aggregate,
installed VCS/sdist controls, frozen artifacts and unchanged coverage floor.
Source integration is not artifact, release, activation, TypeScript, #1172 or
#1199 acceptance. Epoch-zero, quarantine and advertisement vetoes remain in
force. Historical verdicts, failures and evidence stay attached to their own
commits and scopes.
