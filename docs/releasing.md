# Releasing the Python SDK

The independent #1198 guard (#1200) already merged at
`a9bc11c94e7f188d9a442def20c64f76a11b0cab`; retain it. The release-readiness
follow-up **must merge last, after the approved reporting foundations, AdCP rc.4,
and frozen installed foundation harness**, with independent security review of
the final exact integrated head. Preserve reviewed stack ancestry through the
coordinator's two-parent merges; do not change the integration strategy.

This staged prerelease does **not** complete #1172 or #1182. Acceptance of
`client.reporting`, `ReliableReportingService`, and provisional re-read remains
open for their later full rollout. Do not claim stable 8.0 or full RC closure
from foundation acceptance. Implementation and CI success do not authorize
publication or external configuration changes.

Workflow **204238826**, `.github/workflows/release-please.yml`, stays
`disabled_manually` permanently. Its new definition is a failing tombstone.
There is no push, tag, `workflow_run`, `publish=true`, or legacy dispatch route
to publication in the new definitions. Do not re-enable the retired workflow.

## Entry points and evidence

| Entry point | Capability | Credentials |
| --- | --- | --- |
| `release-proposal.yml` | Accept exact main, then create/update the normal Release Please PR | Read-only job token; dedicated, environment-scoped App token for PR/branch writes |
| `release-acceptance.yml` | Reusable read-only build and installed acceptance | Read-only token, no inherited secrets or OIDC |
| `release-publish.yml` | Accept exact main, publish accepted bytes to PyPI, then create the exact tag/release | Separate environment-approved PyPI OIDC and GitHub write jobs |
| `release-please.yml` | Always fail | No permissions or secrets |

Both operational entry points require `target_sha`, `ci_run_id`, and
`ci_run_attempt`. Publication additionally requires the normal release PR number;
its actual merge commit must equal `target_sha`. There is no user-selectable
acceptance artifact or acceptance workflow. The local reusable workflow resolves
at the caller's commit, and the guard checks GitHub's referenced-workflow metadata.

The target must be the full, lowercase 40-character current-main SHA and equal
both the dispatch SHA and workflow-definition SHA. Only a dispatch of
`refs/heads/main` in this repository, on attempt 1, can proceed. Current main is
read again at the end of each gate. A protection or API read error stops the run.

Required checks from **both** the effective ruleset inventory and the separate
`GET /branches/main` protection summary need an explicit App binding, exact SHA,
`completed/success`, and completion within 24 hours. The protected runtime CI floor
cannot disappear through configuration changes. The selected `ci.yml` main-push
run and every job in its selected, still-current attempt must also succeed;
required runtime and policy check IDs must belong to that run. Missing, skipped, neutral,
cancelled, failed, ambiguous, or incomplete evidence is never success. The
invocation and installed acceptance expire after 24 hours, including approval
waits. A fresh successful CI attempt can be selected explicitly; a later attempt
invalidates the previously selected one.

The native `IPR Policy / Signature` CI job follows the
[central signature policy](https://github.com/adcontextprotocol/adcp/blob/82a671607c92945f0fec513c4375af583fdea914/signatures/README.md)
and reads the ledger at one
resolved immutable `adcontextprotocol/adcp` main SHA. For every first-parent
integration after the last published source
`3e76aa54623529a3dda01cd690b8a5c287c75641` (beta.15), it verifies the unique
merged PR, exact merge SHA, base repository/main, and the author's numeric GitHub
ID against the canonical agreement records. That historical floor never advances
automatically. The canonical policy requires the PR author's agreement and
exempts authenticated bot accounts. Unknown authors, signatures recorded after
merge, direct pushes without an associated merged PR, and unreadable or ambiguous
records fail. No PR status or context name is used as a signature. The job has
only read permissions; GitHub Actions supplies the authenticated App 15368
check-run identity. The existing PR/comment workflow still records signatures;
after signing, rerun ordinary CI if its read-only check previously failed.

`Validate conventional commit format` checks the actual subjects **and bodies**
of all those main integrations, including two-parent merge commits. PR runs
also check the PR title/body and individual non-merge commits. A breaking `!`
subject requires its `BREAKING CHANGE:` footer in the actual commit. In
particular, #1174 must integrate with:

```text
fix(reporting)!: scope configuration generations by account (#1174)

BREAKING CHANGE: generation_key returns ReportingConfigurationGenerationKey instead of a two-tuple. Use its named fields.
```

The current pinned [Release Please 17.6.0 prerelease strategy](https://github.com/googleapis/release-please/blob/v17.6.0/src/versioning-strategies/prerelease.ts) increments an
existing prerelease's suffix. From `8.0.0-beta.15`, the staged recommendation is
**`8.0.0-beta.16` / PEP 440 `8.0.0b16`**, including the breaking foundation change.
`prerelease-type: rc` does not rename an existing beta suffix. An intentional
channel change needs a reviewed Release Please version instruction and its
normal proposal, not a standalone `pyproject.toml` edit. Review the proposed
manifest, project version and changelog together before the release merge.

Acceptance builds exactly one wheel and one sdist. It verifies the normalized
project/Release Please version, distribution metadata, SHA-256 hashes, every
archive member, complete SDK payload and tracked schema inventory, and sdist
source/build inputs. Each distribution is installed independently into a fresh
Python 3.10 environment outside the checkout. The installed file inventory and
public exports are checked, followed by the exact commit's
`tests/conformance/reporting/` suite against PostgreSQL 16. Zero tests, failures,
errors, or skips reject acceptance. New reporting conformance files participate
automatically; their dependencies and full #1172 contract need review on the
integrated head. This suite is not an assertion that the unmerged stack's separate
cross-language or independent-review acceptance has completed.

For the staged release, the old reporting directory alone is insufficient.
Acceptance must run the frozen real-process/PostgreSQL four-language-quadrant
foundation corpus, including stable/skew repetitions back-to-back, against
each installed wheel and sdist. Use the harness owner's actual applicability
contract; do not label raw transport as semantic reconciliation or invent
managed-delivery coverage for Core-only peers. The final integration must bind
that corpus, package pins and complete results into the existing acceptance
manifest and recheck them in both writers and recovery. Until that frozen
implementation is wired and independently accepted, publication is blocked.

**#1199 freeze blocker, confirmed by its owner on 2026-09-22:** no harness
commit or acceptance result contract is frozen. The development runner currently
marks every cell incomplete and always emits `acceptance: false`; it is not wired
into these release workflows. Accepted Python `1f953c40d761be71d11fde84c78359ff2074fe7c`
still has typed explicit-scope and exact-revision request blockers. The controlling
TypeScript rc.42 pin lacks the public Core buyer API and has managed
official-precedence failures; supplemental rc.44 is not a substitute. Several
required semantic, retry/activity, skew and CLI/storyboard lanes and immutable CI
pinning remain unfinished. Wait for the owning fixes, accepted immutable inputs,
frozen complete harness and final integrated review. Do not construct a success
adapter around development results or treat the old installed tests as that proof.

The frozen contract must resolve the blocking candidate and supported previous
TypeScript pins to exact versions, tarball integrity and npm-generated locks, and
record the actual Node 22.12.0 SDK floor executable. Existing floating `latest`
and `[adcp-3.0, latest]` CI results are not candidate interoperability acceptance.
Keep latest TypeScript/Python canaries visible and nonblocking, outside the
selected blocking main CI run whose jobs must all succeed. Preserve legacy
compatibility, resolving and recording the exact identity of any blocking floor,
and require the declared unsupported-version errors.

Resolve the complete five-storyboard inventory from **each exact installed pin**:
`reliable_reporting_managed_delivery`, `reliable_reporting_reconciled_billing`,
`reporting_consumer_status`, `reporting_core`, and `reporting_core_declaration`.
Persist each storyboard's step/stateful counts and assert complete execution
against that resolved inventory. The experts measured 64 steps/61 stateful at
rc.38, but **89 steps/84 stateful at rc.42 and rc.44**. The rc.38 count is historical
evidence, not an acceptance target or permission to run a subset. Provenance
verification and signing/DDL tests are separate evidence from the complete matrix.
The experts verified rc.42/rc.44 npm signatures and source-to-tarball attestations;
record their actual Node 24.19.0 verification tooling separately from the Node
22.12.0 execution floor. TypeScript's public seller composition exists; its
configuration work and incomplete matrix execution must not be described as
missing SDK primitives. Require the canonical validator to be a function, without
an optional/truthy fallback.

Candidate Python wheels and sdists must be installed by **local exact path and
SHA-256**, bound to accepted source SHA/tree and locked dependencies/runtime.
The accepted development source still declares occupied `8.0.0b15`; never obtain
the candidate by resolving `adcp==8.0.0b15` from PyPI. Published beta.15, each expert's
independent build of `1f953`, corrected source, and the final integrated/versioned
main build are distinct inputs with their own provenance and fresh acceptance.
Even identical versions and source inventories cannot substitute different bytes;
the writer and recovery regressions explicitly reject that substitution. The
final frozen harness must enforce the same negative, dependency identity and
artifact binding in its own result contract.

The immutable `release-acceptance-RUN_ID-1` artifact contains the two distributions
and `acceptance.json`: source SHA/tree, invocation/CI attempt, version, file hashes,
full archive inventories, installed identities, test inventory/report hashes,
and terminal test counts. Writers verify the artifact's ID, repository, run, SHA,
name, expiry, GitHub ZIP digest, file hashes and inventories, and successful
acceptance job in their own run. They never build or install the package. The
same complete gate runs again after approval and immediately before writes.

## External prerequisites — do not configure these as part of #1198

1. Complete the historical-workflow retirement below. Merely merging new YAML,
   changing a secret name, or reducing default token permissions is insufficient.
2. Configure `release-proposal` and `release-publish` environments with required
   reviewers, prevent self-review, disable administrator bypass, and allow exactly
   the **branch** `main` using a custom deployment branch policy. No tag or wildcard
   policy qualifies. The guard reads these settings; an auto-created, unprotected
   environment fails closed. Retain the repository's PR/review protections.
3. Put a dedicated `RELEASE_PROPOSAL_APP_PRIVATE_KEY` and
   `RELEASE_PROPOSAL_APP_ID` in the proposal environment (secret and variable,
   respectively). Its installation must be limited to this repository with
   contents/PR write permissions and no main-rule bypass. Never reuse the legacy
   IPR key. The pinned Release Please action has `skip-github-release: true`
   hardcoded, with no downstream publication steps or publishing credentials.
   GitHub's contents scope is coarse: this is a reviewed, pinned code boundary,
   not a claim that a stolen PR App token is restricted to branch writes.
4. Register the PyPI Trusted Publisher for the exact repository,
   **`release-publish.yml`**, and **`release-publish` environment**. No static PyPI
   credential or configurable upload server is used. Remove legacy publishing
   credentials and trust entries as part of the separately authorized cutover.
5. Before each proposal/publication window, freeze main using an active repository
   ruleset with **Restrict updates**, **no bypass actors**, and
   `update_allows_fetch_and_merge: false`. Keep existing deletion/force-push
   protections. Record the operator attestation described below in repository
   variable `RELEASE_MAIN_FREEZE`. The live applicable-rule read and ruleset
   revision must still match that attestation. Keep the freeze throughout acceptance,
   approval, publication, and any recovery; do not merge or rerun CI during that
   window. Remove the freeze only after all invocations have drained. The scripts
   never edit rulesets, environments, or main.
6. Protect release tags against updates/deletion, restrict tag creation to the
   designated publisher, and exclude the proposal App. Configure the publisher's
   allowed identity without using administrator bypass. Retain audit evidence
   of these settings and the PyPI trust policy.
7. Keep repository variable `RELEASE_PUBLICATION_ENABLED` unset/false until the
   reporting integration, operator cutover, and independent exact-head review
   have been accepted. A separately authorized operator may then set it to the
   literal `true` and enable the **new** publication workflow if externally
   disabled. This never requires enabling workflow 204238826.

The freeze is required because GitHub does not expose a transaction that combines
an equality check on `refs/heads/main` with a tag/release/PyPI write. Rechecking
main alone leaves a check/use race. The no-bypass freeze supplies the missing
serialization with merges; workflow concurrency only serializes the two new
release entry points. Operators must not remove the freeze, change credentials
or policies, or start CI retries during an active publication. Repository/PyPI
administrators and reviewed main code remain trust boundaries.

GitHub documents that `bypass_actors` can be hidden from read-only callers. The
guard never treats an omitted field as an empty list or adds administration
permissions to acceptance. An authorized administrator must inspect the full
ruleset, confirm the bypass list is present and empty, and record this JSON in
the **repository** variable `RELEASE_MAIN_FREEZE` (not a dispatch input or an
environment-scoped variable that preflight cannot access):

```json
{
  "schema": 1,
  "repository": "adcontextprotocol/adcp-client-python",
  "target_sha": "FULL_CURRENT_MAIN_SHA",
  "ruleset_id": 123,
  "ruleset_updated_at": "ACTUAL_RULESET_UPDATED_AT",
  "observed_at": "UTC_TIME_OF_PRIVILEGED_INSPECTION",
  "bypass_actors": []
}
```

Use the actual ruleset ID/revision and timezone-bearing ISO timestamps. The
attestation expires after 24 hours and authorizes only that target. Each gate
reads all pages of applicable rules, verifies the active restrict-updates rule
and matching revision, and rejects any visible bypass actors. A changed ruleset
or new release-PR merge requires a new attestation. This is an explicit trusted
operator assertion, not a cryptographic proof or a substitute for fresh CI and
artifact acceptance. Protect access to repository variables accordingly. If
this operator trust boundary is unacceptable, a separately reviewed policy
verifier is a design prerequisite; do not weaken the gate or grant an
administrative writer token to the build job.

**Observed external blockers on 2026-09-22:** the only visible environment is
`github-pages`; the release environments and main freeze are not configured.
The coordinator's authorized `PUT .../environments/release-proposal` returned
HTTP 403 `Resource not accessible by integration`, with no change. A reported
repository `permissions.admin: true` does not grant an integration token the
endpoint's Administration/write permission. Do not retry with that same
integration or ask for credentials in chat. Use a separately authorized operator
with the actual endpoint capability. Variables metadata also returned 403:
`RELEASE_PUBLICATION_ENABLED`, the freeze attestation, and App variables are
**unknown**, not proven absent. Dedicated App/PyPI trust must be verified by their
authorized administrators. The new workflows are already `active`, but were not
dispatched; workflow 204238826 remains `disabled_manually`.

The native main policy producers in this follow-up must actually succeed on the
final integrated main; local tests or old PR statuses do not supply those check
runs. The full classic branch-protection settings endpoint returned 403, but
`GET /branches/main` exposes a separate readable summary. On 2026-09-22 it listed
17 contexts versus 14 in `/rules/branches/main`, adding literal `check / check`
from App **15368**, `CodeQL` from App **57789**, and `GitGuardian Security Checks`
from App **46505**. The guard enforces both inventories without name aliases or
App substitution; missing context-to-App bindings or unreadable inventories fail.
The rules also retain PR reviews and separate CodeQL scanning merge protection.
An inaccessible administrative endpoint is a read limitation, not an exemption.

The #1174 head `6c74a458b8ee2a34c47c154186b950faed50f4fa` has no literal
`check / check` or `CodeQL` evidence in the complete check/status inventory.
The IPR workflow formerly called reusable job `check` from caller job `check`;
hardening commit `688b3e4c6f4551f7f04d4ce68f6a69a5ee943523` replaced that call with
a pinned local job named `check`. This matches GitHub's documented
[reusable-job naming](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/troubleshooting-rules),
and explains a possible obsolete protected name; it does not authorize changing
the rule or generating a success alias. An authorized administrator must verify
the intended IPR binding and review any migration to the real producer.

The visible `Analyze` jobs belong to **Code Quality**, run `35042345063`, path
`dynamic/github-code-quality/codeql`, with `analysis-kinds: code-quality` and
`python.quality.sarif`. The coordinator retained the original log with SHA-256
`5bebe1a8c9e917b77ceec59d97e8f728adcd0cca623cdeaa53989a49b78c08b4`.
These Actions/App 15368 jobs are not security CodeQL/App 57789 evidence. The
distinct active security producer is workflow **280935553**,
`dynamic/github-code-scanning/codeql`; its head-filtered run inventory reported
zero runs for that #1174 head. The security/default-setup/analysis and rule-suite
APIs remain 403, so the full reason for the blocked merge is not established.
An authorized security administrator must inspect the configured scanner and
actual PR/current-main results through the supported CodeQL setup path, then
resolve its protection binding if needed. GitHub distinguishes
[analysis jobs from code-scanning results](https://docs.github.com/en/code-security/how-tos/manage-security-alerts/manage-code-scanning-alerts/triage-alerts-in-pull-requests).
No scanner dispatch, rerun, settings change, fake native Actions check or
protection change is authorized by this audit. Ordinary protected merges and
independent exact-head review remain required.

### Concrete operator setup

Environment/branch-policy and ruleset changes require repository
[Administration/write capability](https://docs.github.com/en/rest/deployments/environments#create-or-update-an-environment). Repository variables require Variables/write;
environment variables/secrets require Environments/write. Legacy run retirement
requires Actions/write; credential retirement requires the owning App/PyPI
administrator. None of these capabilities is granted to acceptance jobs.

For **each** of `release-proposal` and `release-publish`, an authorized operator
can use this reviewed environment request body, then add the branch policy:

```json
{
  "wait_timer": 0,
  "prevent_self_review": true,
  "can_admins_bypass": false,
  "reviewers": [{"type": "User", "id": 134922}],
  "deployment_branch_policy": {
    "protected_branches": false,
    "custom_branch_policies": true
  }
}
```

```bash
# Operator only, after resolving the recorded 403; do not run from acceptance.
gh api --method PUT repos/adcontextprotocol/adcp-client-python/environments/release-proposal --input reviewed-environment.json
gh api --method POST repos/adcontextprotocol/adcp-client-python/environments/release-proposal/deployment-branch-policies -f name=main -f type=branch
# Repeat those two requests for release-publish, then GET both environments
# and all branch-policy pages to verify the exact settings and no extra policy.
```

Reviewer ID 134922 is `bokelley`. This workspace's API actor is also `bokelley`;
with self-review prohibited, a dispatch by that actor needs a different authorized
reviewer. Alternatively a different authorized actor can dispatch for that
reviewer. Do not disable self-review protection to resolve the mismatch.

Install a **dedicated proposal App** only on `adcp-client-python`, with Contents
and Pull requests write and no main-rule/tag-creation bypass. Store its ID as
`RELEASE_PROPOSAL_APP_ID` and its private key as
`RELEASE_PROPOSAL_APP_PRIVATE_KEY` only in `release-proposal`, using the secrets
UI or encrypted environment-secret endpoint. Never copy the shared IPR App key
or print a key in a command, log, PR or chat. Verify the installation repository
selection, permissions and environment metadata before dispatch.

In the existing PyPI `adcp` project's **Publishing** settings, verify a GitHub
[Trusted Publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
with owner `adcontextprotocol`, repository `adcp-client-python`,
workflow filename `release-publish.yml`, and environment `release-publish`.
The new workflow uses protected-environment OIDC and attestations, never
`PYPY_API_TOKEN`. Retire legacy trust/credentials only through the separate
historical audit below. Verify tag creation permits the designated publisher
but excludes the proposal App, and retain separate no-update/no-delete tag
protection; do not grant administrator bypass.

Complete integration and fresh CI **before** applying the no-bypass main freeze.
Then record the exact ruleset revision/target attestation in repository variable
`RELEASE_MAIN_FREEZE`. Keep `RELEASE_PUBLICATION_ENABLED` false until final
installed foundation acceptance, independent review, and operator cutover are
complete. The proposal and release-PR merge remain separate windows: drain and
unfreeze for the ordinary protected merge, then freeze and attest the new SHA.

## Disable, drain and retire historical definitions

GitHub reruns retain the original event SHA/ref and original actor's privileges;
failed-job reruns can reuse earlier successful jobs. New YAML, `needs`, environment
names and `run_attempt` checks do not retrofit those older definitions. The
disabled flag is not treated as proof that historical invocations cannot rerun.
The publication gate also inventories this workflow's runs and refuses every
nonterminal invocation and every run created within the last 30 days. Only
deleted runs or completed runs outside the rerun window clear this minimum
machine check; it does not replace the credential and historical-definition audit.

Before exposing the new writer, an authorized operator must inventory and drain
all queued, waiting, running and rerunnable legacy release executions and inspect
every historical definition that can still execute. Retain logs and artifact
identities, then delete eligible legacy runs or wait beyond GitHub's documented
30-day rerun window and verify none remain eligible. Keep the legacy workflow
disabled throughout; do not dispatch it at an old branch, tag or SHA. Revoke
legacy PyPI tokens, and rotate/retire or properly isolate App keys reachable by
historical definitions, including the shared IPR key without breaking IPR checks.
Old explicitly writable `GITHUB_TOKEN` jobs cannot be repaired by changing the
default permission setting. Do not declare retirement complete while any such
historical path retains its capabilities.

These actions require separate operator authorization; none are performed by the
guard or its implementation PR. If historical writers cannot be retired, **do
not enable the new publisher**. This is an external deployment blocker.

At the 2026-09-22 audit, all 718 retained legacy runs were terminal, but **69**
were still inside the rerun window. The latest is `34921539723`, created at
`2026-09-15T02:31:57Z`, source `3e76aa54623529a3dda01cd690b8a5c287c75641`.
Without authorized retirement, the guard rejects until **after
2026-10-15T02:31:57Z**, provided no newer invocation exists.

The narrow retirement sequence for an authorized operator is:

1. Re-inventory every page of workflow 204238826 runs while it stays disabled;
   drain any newly nonterminal run and repeat the inventory.
2. Before deletion, retain each eligible run's metadata, every attempt/job log,
   source workflow at its recorded SHA, artifact IDs/digests/bytes where still
   available, and published package/release provenance/attestations. Record
   unavailable or expired evidence explicitly. Keep checksums outside Actions
   retention; deleting the run must not destroy the only audit copy.
3. Audit the reachable old PyPI credentials, App keys and writable job tokens.
   Have the credential owners retire/isolate those old capabilities without
   breaking the central IPR signature recorder or the new dedicated proposal App.
4. Only after evidence preservation and separate authorization, delete the
   enumerated rerunnable legacy runs, or wait out their original 30-day windows.
   Do not delete unrelated CI, tags, releases, package files or attestations.
5. Re-read the full run inventory and disabled state. The guard must find no
   nonterminal/recent legacy run before the new publisher can be exposed.

Read-only preparation uses the existing APIs, not a new archive protocol:

```bash
gh api --paginate repos/adcontextprotocol/adcp-client-python/actions/workflows/204238826/runs > legacy-runs.json
gh api repos/adcontextprotocol/adcp-client-python/actions/runs/34921539723 > legacy-run-34921539723.json
gh api repos/adcontextprotocol/adcp-client-python/actions/runs/34921539723/attempts/1/logs > legacy-run-34921539723-attempt-1.zip
gh api --paginate repos/adcontextprotocol/adcp-client-python/actions/runs/34921539723/artifacts > legacy-run-34921539723-artifacts.json
gh api repos/adcontextprotocol/adcp-client-python/actions/workflows/204238826 --jq .state
```

Enumerate all attempts and eligible runs from the fresh inventory; the example
run is not the complete retirement list. No deletion or credential mutation is
performed by this follow-up.

## Proposal, release merge, final validation and publication

1. Finish the approved foundation/rc.4 stack and frozen installed harness, then
   merge the independently reviewed readiness correction last. #1172/#1182 remain
   open for their later service/provisional-read rollout. Leave the legacy
   workflow disabled. Record implementation commit
   `M_impl`, its successful main-push CI run/attempt, and independent review.
2. Complete proposal prerequisites, freeze main, and dispatch `release-proposal.yml`
   on ref `main` with those explicit identities. Review `acceptance.json` and the
   installed results before approving `release-proposal`. Only the normal release
   PR is created/updated; normalization edits its pyproject file through the API
   without checking out or executing code from its branch.
3. Drain the proposal run, remove the freeze, and review/merge the generated PR
   through ordinary protection. This produces a new **`M_release`**. All
   `M_impl` evidence is now insufficient for publication. Wait for fresh,
   complete exact-`M_release` main CI, then reinstate the freeze.
4. After the separately authorized guarded enable, dispatch `release-publish.yml`
   on ref `main` with `target_sha=M_release`, its CI run/attempt, and `release_pr`.
   Leave `recover_from` empty. Review the newly built artifacts, installed tests,
   hashes and version before approving the PyPI environment job. The approval
   occurs after read-only acceptance; a moved target or expired evidence fails
   again after the wait.
5. PyPI receives only the accepted wheel/sdist, with attestations. The separate
   GitHub job rechecks acceptance and the complete PyPI filename/hash set before
   creating a lightweight tag at the literal SHA and a release with that same
   `target_commitish`. GitHub ignores that field for existing tags, so readback
   verifies the actual tag object instead. Its JSON body is a durable acceptance
   receipt. The release PR is marked `autorelease:tagged`. Inspect both destinations, record exact
   identities, drain the run, then release the main freeze.

## Recovery and rollback

Never use GitHub's rerun controls for a guarded invocation. Both full reruns and
writer-only reruns are rejected; no previously green `needs` result authorizes a
writer in attempt 2. A normal fresh dispatch rejects an existing version/tag/release.

For a partial publication, keep main frozen and unchanged. Make a **fresh**
publication dispatch with the same `target_sha` and release PR, a current
successful explicit CI attempt, and `recover_from` naming the failed guarded run.
Only a completed failure/cancellation/timeout of `release-publish.yml` at that
exact main commit, on attempt 1, qualifies. Its unexpired artifact supplies the
original distribution bytes; fresh installed acceptance and environment approval
are mandatory. Success, legacy runs, foreign refs, expired/missing artifacts, or
changed source/version/hash cannot be adopted. Recovery uploads only files absent
from PyPI after verifying every existing filename, version and hash; it never
uses `skip-existing`, overwrites tags, or rebuilds already accepted distributions.
An already complete matching publication requires no further publication writes.

PyPI and GitHub are separate systems. An interrupted upload or a tag created just
before a failed release request can leave a partial publication. Recovery handles
these states under the same gate. If main has advanced, preserve the original
evidence and stop: this guard deliberately refuses to publish a stale commit.
Do not reset main, move tags, substitute a new build, or use admin bypass to rescue
an old target. Plan an ordinarily reviewed corrective version instead.

To stop releases, disable the new publication workflow, unset the publication
variable, and cancel/drain its active and waiting runs while retaining the freeze.
Changing the variable or disabled flag alone does not revoke a running job's
credentials. Keep the retired workflow disabled and retain receipts. A code
rollback must preserve the tombstone and guard; never restore the old combined
writer. Already published immutable files cannot be rolled back by this workflow;
any PyPI yank or credential revocation is a separately authorized operator action.

## Authoritative behavior used by this design

- [GitHub reruns: original SHA/ref/privileges and the 30-day window](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).
- [Dispatch uses the selected branch/tag](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch).
- [Reusable workflow permissions and rerun semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).
- [Environment protection and approval](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).
- [Restrict updates and bypass permissions](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#restrict-updates).
- [Ruleset reads can omit bypass actors](https://docs.github.com/en/rest/repos/rules#get-a-repository-ruleset).
- [Code-scanning merge protection is distinct from status checks](https://docs.github.com/en/code-security/concepts/code-scanning/merge-protection).
- [Artifact ID, digest, expiry and source-run metadata](https://docs.github.com/en/rest/actions/artifacts).
- [Release target semantics for existing tags](https://docs.github.com/en/rest/releases/releases#create-a-release).
- [Pinned Release Please skip-release implementation](https://github.com/googleapis/release-please-action/blob/45996ed1f6d02564a971a2fa1b5860e934307cf7/src/index.ts).
- [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
