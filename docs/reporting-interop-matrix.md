# Reliable Reporting foundation interoperability

This is the installed-artifact interoperability harness for
[#1199](https://github.com/adcontextprotocol/adcp-client-python/issues/1199).
The four required cells use published Python and TypeScript packages, fresh
PostgreSQL databases, and separate seller processes. The required aggregate
passes only when every cell completes positive semantic reconciliation.

## Immutable inputs

The exact artifact URLs, hashes, installed-member manifests, npm locks, and
protocol versions are in `scripts/ci/reporting_interop/pins.json` and
`installed_artifact_cells.json`. The prior-compatible sides are published
Python `adcp==8.0.0b16` and TypeScript `@adcp/sdk@14.0.0-rc.47`; the
candidate sides are published Python `adcp==8.0.0rc3` and corrected
TypeScript `@adcp/sdk@14.0.0-rc.48`. Brian approved compatible prereleases for
the prior sides, conditional on all four cells passing. The candidate Python
package includes the rc.7 protocol adoption; the prior package and both
TypeScript packages retain their own published protocol identities.

Each Python runtime is installed from the exact pinned wheel path. Each
TypeScript runtime is installed from the checked-in exact npm lock, and every
installed SDK member must match its pinned registry archive. Version strings
alone do not qualify an artifact. CPython 3.10 and Node 22.12.0 are the
matrix runtime floors.

Historical b15/rc.41 inputs remain in the foundation record. They lack the
reporting surfaces required for a positive stable cell and are not substituted
for the approved prior-compatible artifacts. Supplemental and latest-version
canaries remain non-blocking.

Each candidate runtime must expose `direct_url.json` for the exact downloaded
PyPI artifact path supplied to the runner; version-only or index-resolved
installs are rejected. The wheel and sdist retain their distinct SHA-256 and
common installed-member manifest, dependency set, and runtime identity.
Installed-member evidence never replaces the full wheel, sdist, or npm-tarball
digest/SRI for the specific artifact under test.

## Required topology

Issue #1199's installed-artifact acceptance topology is declared separately in
`installed_artifact_cells.json`. It is the Python seller artifact × TypeScript
client artifact cross-product, not the Q1-Q4 language-role topology:

| Cell | Installed Python seller | Installed TypeScript client | Required result |
| --- | --- | --- | --- |
| `python-stable__typescript-stable` | PyPI b16 | npm rc.47 | positive semantic Core reconciliation |
| `python-stable__typescript-candidate` | PyPI b16 | npm rc.48 | positive semantic Core reconciliation |
| `python-candidate__typescript-stable` | PyPI rc3 | npm rc.47 | positive semantic Core reconciliation |
| `python-candidate__typescript-candidate` | PyPI rc3 | npm rc.48 | positive semantic Core reconciliation |

All four cells run from clean installed environments with distinct PostgreSQL
databases and seller processes. A missing reporting surface or an expected
unsupported result is a failure in this final phase. The matrix must be rerun
on integrated `main` with #1172's factory and lifecycle implementation merged;
no earlier branch run transfers to that final acceptance.

`run_installed_artifact_matrix.py` executes this cross-product and
`Reliable reporting installed-artifact 2x2` is its dedicated CI aggregate;
the same execution result is also propagated into the ruleset-required
`Postgres conformance tests (Postgres 16)` aggregate.
The existing reference-seller storyboard aggregate is unrelated and supplies
no #1199 credit.

## Historical foundation topology

The foundation language-role topology remains supplemental coverage from the
earlier rc.6 qualification. Its Q/S results do not replace the four installed
artifact cells above:

`scripts/ci/reporting_interop/applicability.json` declares the cells before
execution:

| Contract | Client | Server | Disposition |
| --- | --- | --- | --- |
| Q1 | candidate Python rc.6 | candidate Python rc.6 | blocking aligned candidate quadrant |
| Q2 | candidate TypeScript rc.6 | candidate Python rc.6 | blocking aligned candidate quadrant |
| Q3 | candidate Python rc.6 | candidate TypeScript rc.6 | blocking aligned candidate quadrant |
| Q4 | candidate TypeScript rc.6 | candidate TypeScript rc.6 | blocking aligned candidate quadrant |
| S1 | previous Python | candidate TypeScript rc.6 | blocking positive skew requirement plus separate negative refusal control |
| S2 | candidate Python rc.6 | previous TypeScript | blocking positive skew requirement plus separate negative refusal control |

Q1-Q4 are the next-candidate contract, not a selection of pending artifacts.
Rc.47 is the first published TypeScript package in this sequence that pins
3.2.0-rc.6; rc.45/rc.46 pin rc.4, stable 13.1.1 exposes neither the reporting
surface nor these five storyboards, and released Python beta15 pins rc.3.
Consequently no currently selected published pair satisfies S1 or S2. An
actionable version/capability refusal is retained as a separately labelled
negative-skew control only: it cannot satisfy, score, or remove either positive
supported-skew requirement. The existing six-cell runs retain their original
pins and outcomes and are not rebound to this contract.

Latest-Python and latest-TypeScript canaries are separate and nonblocking.
Every cell owns a fresh process and a fresh PostgreSQL database. PostgreSQL
schemas are insufficient isolation: reporting advisory locks are keyed by
account and are database-wide.

## Corpus and public surfaces

The neutral scenario inventory is `scripts/ci/reporting_interop/corpus.json`.
It covers capabilities, retained history, exact reads, receipts, authenticated
account isolation, strict values, pagination, notifications/activity, and the
official-precedence regressions. Applicability is tier-aware and fixed before a
run.

The five reporting storyboard IDs are fixed, but their step counts are not.
`ts_storyboard_inventory.cjs` executes the exact installed CLI and freezes the
per-storyboard counts for each pin. Rc.42, rc.44, and rc.45 resolve 89 total
steps/84 stateful steps; the historical 64/61 figure belongs only to rc.38.
Inventory is not execution: every resolved step must still run before a cell
can complete. The pin resolver persists every literal phase/step ID, task,
stateful flag, controller scenario, and controller operation, and rejects an
aggregate count that does not match those identities. The development runner
invokes all five IDs separately for every supplied exact TypeScript install,
matches actual task executions back to those identities, and reports every
unexecuted ID rather than counting a requirement-skip wrapper as a storyboard
step. Against the current Core-only
seller, `reporting_core_declaration` executes its one step, while the other
storyboards report the missing public compliance controller instead of being
credited as executed. That is an explicit composition dependency, not a
reduced denominator or a storyboard pass.

The inventory now derives each storyboard's required tools, capability paths,
controller scenarios, and operations from the exact installed pin's YAML.
The current composition gaps are distinct:

- `reporting_core` is applicable to the Core seller, but needs a state-backed
  controller. Rc.45 exposes `registerTestController` and the flat-store
  `reportingCoreLifecycleProbe` hook; its additional
  `reliable_reporting_core_integrity_probe` has no turnkey exported adapter
  and must be wired through the public open-extension controller surface to
  real seller state. The focused adapter preflight now installs hourly Core
  configurations and commits zero-row and two-row revisions through the public
  producer/store interfaces. A second controlled-clock fixture now implements
  the public ledger/status-ingest boundary and is mounted through the documented
  `TOOL_INPUT_SHAPE`/`toMcpResponse` MCP extension. Focused real-MCP calls have
  driven `prepare`, `advance_time`, `publish_zero_row`, and authoritative status
  reads. The installed canonicalizer, literal two-row vector, fixed revision ID,
  and both exact-read cursor pages also pass over the focused real-MCP route.
  These are adapter preflights, not storyboard execution or PostgreSQL
  durability evidence. PostgreSQL snapshots still use their own clock. The
  pinned rc.45 producer rejects changing-offset source timezones and plans fixed
  millisecond periods. The calendar correction is merged at `f3c7accb` and
  published in rc.47 and retained in the selected rc.48 package, so this is no
  longer described as an open source defect. Literal rc.48 storyboard execution
  is still pending: `probe_scheduler_dst` remains mandatory and uncredited, and
  no precomputed boundary is substituted for it.
- `reporting_consumer_status` requires the public `sync_reporting_status`
  task, advertised `consumer_status_task`, consumer-scoped ledger state, and
  the same lifecycle controller. The controlled fixture now composes the real
  status and ingest handlers and has focused public-handler coverage for
  wrong-binding rejection, immutable supersession, idempotent replay, and
  caller isolation; its basic missing-to-received recovery also passes the
  focused real-MCP route.
  Several controller operations are implemented but still await complete
  storyboard preflight, so this is not an executed consumer-status pass.
- `reliable_reporting_managed_delivery` and
  `reliable_reporting_reconciled_billing` remain unsupported on the Core-only
  seller, but now have a separate rc.45 PostgreSQL seller/controller
  composition. It installs destination authorization and its immutable binding
  before the obligation, lets the public managed worker settle a real provider
  adapter resource, reads and revokes that resource through the public runtime,
  and retains authoritative materialization history. The billing mode adds the
  authenticated public receipt handler and two public immutable adjustment
  commits. Focused Node 22.12/PostgreSQL 16.4 real-MCP preflights cover rejected
  revision evidence, accepted replacement, terminal stale rejection,
  accepted/rejected adjustments, repaired adjustment evidence, and
  `changes_after` convergence. Rc.45 still exports no turnkey adapters for the
  named scenarios, so the harness registers the documented public controller
  tool and dispatches only those literal operations. These results prove the
  adapter composition, not CLI storyboard execution, a six-cell result, signed
  readiness delivery, or controlled-clock behavior.

The pin-derived inventory also records the public controller surface, so an
absent controller, an unsupported optional tier, and a missing turnkey scenario
adapter remain distinguishable in retained results. The literal operation map
and inspected public source boundaries are retained in
`scripts/ci/reporting_interop/controller-operation-map.json`.

Python buyer semantics use the installed standalone APIs in `adcp.reporting`,
including `reconcile_reporting_core` and `reconcile_reporting`. There is no
Python `client.reporting` facade in this staged scope. TypeScript rc.45 exposes
both the rich public `reconcileReporting()` path and the distinct Core-only
`reconcileReportingCoreV1()` primitive. Rc.42 lacked the Core primitive, so
its historical rich-reconciler/Core-page mismatch remains a missing-public-API
result rather than a semantic result transferable to rc.45.

The public TypeScript primary client also lacks reporting status/receipt
methods. The legitimate rich lane uses public `ProtocolClient`,
`unwrapProtocolResponse`, and `reconcileReporting()`; the tracked executable
gap reproduction keeps the primary-facade limitation visible.

`ts_core_server.cjs` is a real installed-package TypeScript Core seller. It
uses the public PostgreSQL ledger, source producer, status/exact-delivery
handlers, task-capable MCP HTTP server, and bearer verifier. It has passed
account-isolated reads and Python Core reconciliation. Its public inline source
projects scalar evidence only, it does not mount `sync_accounts`, and it must
not be credited for nested-JSON or typed-onboarding scenarios.

`ts_core_buyer.cjs` retains the raw native rc.45 Core result's
`satisfied`/`health`/`productionStatus`/`reportingRevisionIds` result and a
documented neutral mapping. Historical rc.42 produces an executable
missing-export result. Core availability does not satisfy managed lanes.

## Current development runner

`run_foundation_matrix.py` launches the Python and TypeScript public Core
sellers in separate processes and fresh UTF8/C PostgreSQL databases. It runs
both supplied Python artifact routes against both seller languages and
exercises rc.48's native TypeScript Core buyer API over actual HTTP. It also
executes the two
pinned skew controls, every resolved reporting storyboard ID, and a public
Python `WebhookReceiver` with a live loopback revocation-list fetch, issuer
outage, fail-closed stale state, fresh refresh, and recovery. It reaps process
groups, drops databases, freezes installed CLI inventories, hashes retained
evidence, and always emits `acceptance: false` while the declared cells remain
partial.

`storyboard_orchestration.py` is the separately reviewable process boundary for
the exact five-storyboard denominator. Its default mode validates the Node
executable, registry archive, complete archive-to-install member manifest,
lock/package identity, literal 89/84 inventory, DSN policy, timeouts, and
commands. Execution requires `--execute`; an explicit external test DSN is
preferred. The optional local mode is Linux-only and requires `/proc` plus
pidfds before spawning. Each requested program is held behind a pipe-gated
launcher until the parent owns its pidfd and verifies its new
process-group/session identity. It owns one `postgres` process directly (never
through `pg_ctl`), binds it only to a private Unix socket under its owner-marked
root, and proves `data_directory`, postmaster start time, PID, and process-start
identity before database writes. Shutdown enumerates the exact owned session
and signals every still-matching member through a pidfd rather than an
unguarded numeric process group. If postmaster or descendant death is
unproven, its data root is retained and named in the blocking cleanup error.

Every seller receives an unpredictable per-run bearer token and startup proof,
writes an exclusive owner-ready record containing its PID and port, and runs in
the identity-checked owned session. The CLI and `initdb` are separately owned;
timeout output is retained and execution and cleanup failures remain distinct.
Database connect, lock, statement, create, and exact-name drop operations are bounded.
Unexpected, duplicate, skipped, or leftover result rows fail the literal
denominator. A complete storyboard result is still not six-cell matrix or
release acceptance. The prior automated `cyberPolicy` reason is unavailable;
these controls define a new reviewable execution path and do not claim to
resolve or bypass that event.

Run it with installed Python 3.10 artifact environments that contain the
package's `pg` extra:

```bash
/path/to/python3.10 -I scripts/ci/reporting_interop/run_foundation_matrix.py \
  --python-runtime wheel=/path/to/wheel-venv/bin/python \
  --python-runtime sdist=/path/to/sdist-venv/bin/python \
  --python-artifact wheel=/path/to/candidate.whl \
  --python-artifact sdist=/path/to/candidate.tar.gz \
  --previous-python-runtime /path/to/released-b15-venv/bin/python \
  --previous-python-artifact /path/to/adcp-8.0.0b15-py3-none-any.whl \
  --python-dependency-runtime floor=/path/to/floor-venv/bin/python,2.13.0,2.0.0,wheel \
  --python-dependency-runtime current=/path/to/current-venv/bin/python,2.13.5,2.2.0,wheel \
  --node-runtime /path/to/node-22.12.0 \
  --typescript-install candidate=/path/to/exact-rc48-npm-install \
  --typescript-install previous=/path/to/exact-rc41-npm-install \
  --typescript-archive candidate=/path/to/sdk-14.0.0-rc.48.tgz \
  --typescript-archive previous=/path/to/sdk-14.0.0-rc.41.tgz \
  --pg-admin-url postgresql://USER:PASSWORD@HOST:PORT/postgres \
  --output .context/reporting-interop/foundation-run
```

The focused storyboard boundary receives the matching archive again as
`--typescript-tarball`, plus either `--external-pg-admin-dsn` or
`--local-pg-bin`. Its plan-only default starts nothing. The outer runner passes
`--execute` only in a separately authorized bounded run.

The previous released Python wheel keeps its own SHA-256 gate and derives its
installed SDK member count/digest from those exact wheel bytes. Candidate
routes retain the independently selected candidate-member manifest. An
actionable previous-version refusal remains a negative-only control and cannot
inherit candidate content expectations or acceptance credit.

Foundation accounting does not trust a returned cell ID. Required credit also
requires the declared contract ID, exact selected protocol/artifacts, positive
semantic polarity (or a genuinely selected positive skew pair), and a fresh
seller/database identity for that one cell. Artifact credit compares a
contract-bound aggregate identity digest rather than trusting an asserted
"selected" boolean, and seller/database identities must be unique across
credited cells. The retained rc.4 Core controls
are named `supplemental_shared_rc4__*`; the released-Python refusal is named
`negative_skew_refusal__*`. They remain visible evidence but leave Q1-Q4 and
the positive S1/S2 requirements unexecuted. Latest canaries have separate
executed/missing accounting and never confer blocking acceptance.

The older `scripts/ci/reporting_interop_matrix.py validate-results` command
still validates its historical four-cell preparation shape, evidence hashes,
and asserted isolation fields. Its command result is now explicitly
`legacy_result_shape_validated_nonaccepting`; synthetic assertions and process
IDs cannot promote the current matrix. Likewise, the offline TypeScript
primary-facade probe reports method-surface availability only and always keeps
`semantic_lane_complete: false`.

The managed/reconciled real-MCP preflight uses the same admitted
`{account_id, sandbox: true}` selector for controller, status, exact-history,
and receipt calls. Separate missing- and false-sandbox probes must be rejected;
the trusted seller guard is not weakened to accommodate a harness selector.

## Review and scanner lineage

Independent source review covered the 811-line orchestration boundary at exact
head `6df004940461a58d28591bad67720d5e5e8440d3`; it did not attest the full
61-file historical scaffold. GitGuardian check `108042611489` remains an
authentic failure with incident `37600271`, pointing to the deliberately
synthetic required-reject `kv-assignment` in historical commit `8cefe4629`.
That record is consumed only by the two seller-side resource-location guard
probes and contains no authentication consumer. Its fixture bytes and finding
semantics remain unchanged: no ignore, history rewrite, rule weakening,
obfuscation, alert dismissal, or substitution is made here.

The historical foundation result was `incomplete` and did not qualify the
four-cell release gate. Its candidate Python reconciled the rc.41
Core seller over real MCP HTTP. Released Python b15 rejected the explicit rc.4
candidate server request during public client construction with an actionable
supported-version error and no transport mutation. Those are concrete skew
scenario results, but neither completes the remaining applicable scenario set
for its cell. The frozen release entrypoint must additionally run Managed
Delivery/Reconciled Billing semantics, server-only durability/security
scenarios, complete resolved storyboard steps through the required controller,
and visible canaries.

## Historical foundation findings

- Historical rc.42 official precedence remains red with no exemption: all 17 adapted
  inputs validate against both public rc.4 validators, but only five controls
  meet required semantics. Four unlinked histories return
  `AMBIGUOUS_REVISION_CHAIN`; eight official-without-materialization histories,
  including linked variants, throw `LEDGER_GRAPH_INTEGRITY_FAILED`.
  The protocol's byte-identical normative rc.4 reconciliation fixture contains
  only a single official revision and no supersession field, retained
  snapshot/official history, multi-leaf chain, or artifact-free official. Its
  green conformance therefore does not cover either failing selection shape;
  the signed protocol bundle stays unchanged. Release protection comes from
  the shipped SDK's required-correct 17-case tests plus this separately pinned
  neutral 17-case harness corpus. Protocol-owned vector enrichment is a
  distinct future contribution, not a prerequisite or claim of current rc.4
  coverage.
  The separately identified rc.45 development candidate passes all 17
  required-correct cases through both public buyer APIs with all inputs valid,
  no caller mutation, and no inspection/receipt effects. That is candidate
  package evidence only; it neither rewrites rc.42 nor closes the HTTP,
  storyboard, skew, Python, or integrated-release rows.
- Historical rc.42 has no public `reconcileReportingCoreV1` or
  `@adcp/sdk/client/core` export. Its rich reconciler correctly remains a
  different Managed Delivery API and cannot close a Core buyer cell. Rc.45
  includes the Core API and must retain its native result shape; availability
  alone does not close a real HTTP Core cell or any managed lane.
- Accepted Python typed `sync_accounts` currently serializes an explicit
  `media_buy_ids` reporting scope together with default
  `all_media_buys: true`. The real production mount rejects the mutually
  exclusive pair before onboarding. A manually seeded Core ledger does not
  close that production-client lane.
- Accepted Python typed `get_media_buy_delivery` currently serializes both
  aggregate breakdown flags as false on an exact-revision request. The rc.4
  schema forbids either field in that mode, so a real produced revision is
  rejected before its first content page. A lower-level exact-read success is
  not typed-client evidence.
- The isolated adoption head's metadata still permits Pydantic 2.12, whose
  public generated-type imports fail. That historical red is already
  dispositioned by merged main-only #1190, which sets the reviewed floor to
  Pydantic 2.13.0. Development controls run exact CPython 3.10/Pydantic
  2.13.0/MCP 2.0.0 and a current 2.13.5/2.2.0 lane from locked dependency
  snapshots; their successful imports and preserved PY-PUBLIC-001/002 reds do
  not transfer to integrated main. Final artifacts must independently expose
  metadata excluding 2.12 and pass imports/models/reporting cases at the floor.
- A bounded installed-artifact sweep of 209 request/default cases per Python
  build route found no additional selector/default defect: 159 valid variants
  remained valid, 12 scoped-configuration variants reproduced only the typed
  onboarding defect above, five exact-read variants reproduced only the typed
  delivery defect above, and 33 invalid controls stayed invalid. This narrows
  correction scope but is request-serialization evidence, not production or
  semantic-quadrant acceptance.
- The Managed Delivery TypeScript seller/controller has focused public-runtime
  and real-MCP preflights, but its CLI storyboard execution and the managed
  Python composition, normalized transcript comparator, signed retry/activity
  execution, skew cells, and aggregate fail-closed validator are not yet
  frozen.
- Candidate Python still silently aliases some distinct wire-decoded integral
  filter values above 2^53 after Decimal-to-float conversion; that remains a
  blocking real-boundary row. The pre-existing `ctx_metadata` suffix screen is
  separately retained as a nonblocking documentation/hardening row: its
  guarantee must say best-effort defense in depth, not fail-closed.
- The TypeScript seller's resource-location persistence guard at the historical
  rc.42 pin admits
  bare JWTs, cloud key identifiers, and percent-encoded presigned forms that
  the Python seller rejects. The Python buyer does not re-validate these
  locations, and Python's provider-native-only rule is deliberately stricter.
  Rc.48 also accepts the synthetic `kv-assignment` required-reject detector, so
  the complete 17-case in-process comparison now records eight divergences as
  one visible nonblocking TypeScript hardening advisory, not eight equivalent defects or a
  quadrant result. Future hardening needs a compatibility/false-positive
  budget and bounded decoding that preserves legitimate native locations; a
  blanket parse-failure rejection is not assigned. This advisory never waives
  a red result if an actual public path emits a synthetic secret or signed URL.
- The repository's existing CI does not pin rc.45: its TypeScript jobs resolve
  floating `latest` or legacy dist-tags. Those greens are neither immutable
  candidate evidence nor a substitute for the harness's exact npm lock,
  integrity and Node-floor cells.
- T-1 remains a visible nonblocking transport-metadata defect: the independently
  reproduced TypeScript path omits `responseHeaders`, affecting all diagnostic
  response headers, at both the declared Undici 6.28.0 floor and 6.28.1. This
  row is retained separately from reporting semantics and is not inferred
  green from rc.45 publication. T-2 remains separate recurring specialism-test
  timeout fragility; no retry or test-budget expansion is hidden in this
  harness.
- The protocol-owned rc.4 webhook-signing corpus is byte-identical across the
  installed SDKs. Historical 1f953 evidence remains 27/29 Python and 28/29
  TypeScript. The selected PR1208 Python build now matches 28/29: mixed-base64
  vector 021 is classified as normative `header_malformed`, while both bare
  verifier calls still leave vector 019's receiver-runner revocation-staleness
  state unconfigured. Python's public `revocation_list` option passes a deterministic
  fresh/stale in-process control on both artifact routes, proving the state is
  expressible without a hidden option. The separate live receiver lane now
  exercises stale-fetch outage and refresh recovery. The harness's separate 13/13
  real-HTTP replay/signing controls do not replace these 29 normative vectors.
- A separate public signer/verifier 2x2 is asymmetric on the retained
  historical installed inputs: Python's webhook signer emitted padded standard
  Base64, which Python accepts but rc.42
  TypeScript rejects as `webhook_signature_header_malformed`; TypeScript emits
  unpadded Base64URL, which both verifiers accept. This is a blocking
  Python-to-TypeScript callback semantic red distinct from mixed-alphabet
  verifier classification and stale-revocation receiver state. Immutable rc.4
  protocol source requires unpadded Base64URL for the webhook Signature token,
  so current Python emission is the defect; Python decoder tolerance is not
  conformance. Content-Digest remains separately bounded because source wording
  and immutable vectors differ, and its bytes are not recoded by this finding.
  Historical same-language success and the 13-vector control do not close this
  direction. The selected PR1208 build instead emits the required unpadded
  Base64URL Signature and is accepted by the exact rc.45 TypeScript verifier;
  both artifact routes remain required in the development result.
- The real public receiver composition now exercises live revocation fetch,
  stale issuer outage and refresh recovery over raw loopback HTTP. Fresh and
  recovered callbacks are accepted and handled; the stale state fails closed.
  On the historical Python artifacts it escaped the verifier wrapper as an
  operational `RevocationListFreshnessError`, without a signature code or
  failed-step attribute, rather than the normative vector-019
  `webhook_signature_revocation_stale` classification. The harness retains
  that exact red and its HTTP fixture deliberately maps the otherwise-uncaught
  exception to 503; 503 is harness behavior, not an SDK `WebhookOutcome`.
  Normative inner-code matching and the SDK's current 401/`WWW-Authenticate`
  receiver convention are asserted separately. The blocking invocation exits
  nonzero for the historical red; an explicit diagnostic reproducer can exit
  zero only when that exact red occurs and reports `blocking_acceptance: false`.
  It fails as stale if a corrected artifact produces the normative result.
  On both selected PR1208 artifact routes, the same live composition returns
  the normative inner code and the receiver's current 401/`WWW-Authenticate`
  mapping, then recovers after refresh. This live cell configures
  `replay_store=None`, so it does not claim replay-persistence evidence.
  This is receiver-composition evidence, not polling-service deployment,
  cross-language signer interoperability, reporting retry/account activity, or
  a substitute for the 29-vector corpus.
- A late-account real production run repeatedly published a later account's
  revision but did not materialize it after an earlier account completed. An
  independent real-PostgreSQL store probe confirmed that retaining the
  pre-update `served_at` continuation can chase a continuously due first
  account and never wrap to the late `-infinity` row under the reproduced
  preconditions. This mechanism is sufficient for that red, but does not
  imply permanent starvation after a due gap or exclude every unrelated
  defect. A separate read-only public-loop observation recorded 26/26 completed
  claims during a 45-second late-account read window sampling and serving only
  the continuously due first account; all cursors remained on that account and
  the late row remained at `-infinity`. The distinct runtime correction and
  bounded public fairness/restart/concurrency regressions remain open.
- PY-FINALITY-001 is a separate Python producer-time blocker. A valid source
  can observe and finalize during acquisition after worker dispatch, while the
  historical producer records its pre-fetch worker clock as
  `revision.created_at`; this can make `finalized_at > created_at`, which the
  buyer correctly rejects as `FINALITY_EVIDENCE_INVALID`. Historical memory
  producer diagnostics and the b203 wire inversion remain distinct evidence:
  neither is an HTTP/PostgreSQL acceptance result. The eventual selected
  artifact must preserve source evidence and exercise real typed MCP status,
  exact read, reconciliation, and replay/restart, while future/skew/regressing
  clock negatives still fail before publication. This row applies only to
  Python producer-capable server cells and does not infer a TypeScript defect.

These are preparation/qualification results on the accepted stacked head.
They must be repeated against the actual integrated main tree; the conflict-free
prospective merge tree is not execution evidence.

## Development validation

```bash
uv run pytest -q \
  tests/test_reporting_interop_matrix.py \
  tests/test_reporting_interop_corpus.py

node --check scripts/ci/reporting_interop/ts_package_probe.cjs
node --check scripts/ci/reporting_interop/ts_mcp_buyer.cjs
node --check scripts/ci/reporting_interop/ts_primary_facade_gap.cjs
node --check scripts/ci/reporting_interop/ts_core_server.cjs
node --check scripts/ci/reporting_interop/ts_core_buyer.cjs
```

The older `scripts/ci/reporting_interop_matrix.py` remains the immutable
registry/protocol evidence verifier from the preparation commit. Its original
version cross-product is not the language-quadrant execution model and must not
be presented as matrix acceptance.
