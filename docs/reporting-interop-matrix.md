# Reliable Reporting foundation interoperability

This is the staged-foundation qualification harness for
[#1199](https://github.com/adcontextprotocol/adcp-client-python/issues/1199).
It is not final compatibility acceptance. Issue #1172 service composition,
`client.reporting`, and #1182 provisional re-read remain explicitly deferred.

## Immutable inputs

The reviewed coordinates are in `scripts/ci/reporting_interop/pins.json`.

This harness source is ported to accepted Python main
`940c95e0c2d93758ed334b3edff59cbe933362d4`. Its retained execution pins do
not move from source ancestry alone. PR #1208 head `a124afd3` still has no
final artifact pin. Published TypeScript rc.47 is selected only as the
developmental rc.6-aligned Q1-Q4 candidate; it has not been installed, locked,
executed, promoted, or accepted by this harness.

- Python development candidate: exact PR #1208 head `25e0c7278a19493345975881d1578c76fc64dc1b`,
  tree `bcc4ad30ea4ee43d7b82f39e12f60b5a3ac2aead`, built as an installed wheel or
  sdist-derived wheel before publication. Registry publication is not required
  for this prepublication candidate; the bytes supplied to a run are.
- Retained historical executable TypeScript input: exact published
  `@adcp/sdk@14.0.0-rc.45`, SHA-256
  `a0952ed8edaaad958bdb8f474c4f5cb68333ee272e7a8f3419d12930e9c57e6b`,
  integrity
  `sha512-ywglF0pBXDUfxW6IUdXMf+xKAkIKFezxKf+Jq7gDuY2NdqCg6Ctgi496pUYk23oFiBEe8NRjzWadjWzSo/+J7w==`,
  publishing commit `94171fe20d632f027eb362604c715ed361fadb51`, and
  approved tree `8d3c29d9a1779f04520c33cdc9ed3e15e5296cdd`. Its harness-owned
  lock is checked in under `scripts/ci/reporting_interop/npm/rc45/`. Rc.42
  remains the exact historical Matrix12 candidate and is never rewritten as
  rc.45 evidence. Rc.45 pins protocol rc.4 and is therefore ineligible for
  required rc.6 Q-cell credit even when its archive and installed members are
  intact.
- Developmentally selected TypeScript rc.6 candidate:
  `@adcp/sdk@14.0.0-rc.47`, SHA-256
  `1b14aeddab973809f1850e189f833ef645d061e25c82d74046c97fd92d346e49`,
  integrity
  `sha512-Zi2r9CZ2HJmB7i8XTlt5Z1QrVq+nnKkTcl8fSQUkjsi3ChhVtR24BIu/6Gg7yz5ctBWfMR2Lxqn4wv8kh3Wygw==`,
  registry git head `b0d2886f0f5b8668fc134568c4cd22a4ef89e2fa`, and tree
  `12515822c5814dcf2a0bb3c4d92e7b3ad023df46`. Independent source review
  approved the frozen calendar behavior and T1 correction. This coordinate
  is not executable harness input until its own lock and complete installed
  member binding are selected; rc.45 cannot substitute for that missing input.
  No executable row is rebound from rc.45 and no prior result transfers.
- Previous compatible TypeScript skew input: exact `@adcp/sdk@14.0.0-rc.41`,
  integrity
  `sha512-Qfs+ujKBpIjf7m9jcuzxvN/XCXUssxhR5xCvxCq4oqMR3aMJjnuTgzjxltPtv+A6LOxwmX1ia8sMRdhVSehdBg==`,
  from the checked-in `scripts/ci/reporting_interop/npm/rc41/` lock. The
  previous Python input is the released b15 wheel installed by local path and
  asserted as SHA-256 `608636a4fe774bc4846b5bb0eab6367d0b8e69f6f371aa6085845d9f8329f48f`;
  its occupied version string is never used as candidate identity.
- Protocol: signed `3.2.0-rc.4` bundle SHA-256
  `773bee016d279345d6fae91a0ce684a22ca9b81c2edd2cdb9a71dbfea65954d2`.
- Runtime floors: CPython 3.10 and Node 22.12.0. Provenance tooling that needs a
  newer Node runtime is separate from the interoperability floor.

Historical rc.42 and supplemental rc.44 identities remain recorded with their
own locks and results. Neither is a substitute for rc.45, and rc.45 does not
retroactively change their results. PyPI `adcp==7.0.2` is a visible unsupported
latest-stable canary, not a reporting pass.

The candidate still declares the already published `8.0.0b15` version. A
candidate runtime must therefore expose `direct_url.json` for the exact local
artifact path supplied to the runner; version-only or index-resolved installs
are rejected. Each independently built artifact retains its own SHA-256,
source tree, dependency set, and runtime identity. It is not conflated with
the smaller released b15 artifacts or another exact-source build. Matching
installed members or reconciliation files are supplementary content evidence
only: they never replace the full wheel, sdist, or npm-tarball digest/SRI for
the specific artifact under test.

## Required topology

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
  published as rc.47, so this is no longer described as an open source defect.
  Exact rc.47 byte qualification/selection and literal storyboard execution are
  still pending: `probe_scheduler_dst` remains mandatory and uncredited, and no
  precomputed boundary is substituted for it.
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
exercises rc.45's native TypeScript Core buyer API over actual HTTP. It also
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
  --typescript-install candidate=/path/to/future-selected-exact-rc47-npm-install \
  --typescript-install previous=/path/to/exact-rc41-npm-install \
  --typescript-archive candidate=/path/to/future-selected-sdk-14.0.0-rc.47.tgz \
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

The current result is deliberately `incomplete`; it cannot be wired into a
release acceptance gate yet. Candidate Python currently reconciles the rc.41
Core seller over real MCP HTTP. Released Python b15 rejects the explicit rc.4
candidate server request during public client construction with an actionable
supported-version error and no transport mutation. Those are concrete skew
scenario results, but neither completes the remaining applicable scenario set
for its cell. The frozen release entrypoint must additionally run Managed
Delivery/Reconciled Billing semantics, server-only durability/security
scenarios, complete resolved storyboard steps through the required controller,
and visible canaries.

## Current blocking findings

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
  locations, and Python's provider-native-only rule is deliberately stricter,
  so the complete 17-case in-process comparison is retained as one visible
  nonblocking TypeScript hardening advisory, not seven equivalent defects or a
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
