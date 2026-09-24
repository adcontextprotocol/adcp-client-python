# Releasing the Python SDK

The #1198 guard is implemented independently and **must merge last, after the
reporting stack and #1172**. Rebase, review the installed acceptance suite against
the integrated reporting contract, and validate the final exact head before
merging. This implementation does not authorize stack integration or publication.

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

All effective required status checks need an explicit App binding, exact SHA,
`completed/success`, and completion within 24 hours. The protected runtime CI floor
cannot disappear through configuration changes. The selected `ci.yml` main-push
run and every job in its selected, still-current attempt must also succeed;
required runtime check IDs must belong to that run. Missing, skipped, neutral,
cancelled, failed, ambiguous, or incomplete evidence is never success. The
invocation and installed acceptance expire after 24 hours, including approval
waits. A fresh successful CI attempt can be selected explicitly; a later attempt
invalidates the previously selected one.

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

**Observed configuration blockers on 2026-09-20:** effective main rules require
`IPR Policy / Signature` and `Validate conventional commit format`. On main
`f6e9c15333db8e657ba96a79d806194dfc0e0447`, the first is absent and the second
is skipped; they currently run as PR policy. No release environment or main
freeze existed. The guard intentionally cannot authorize that state. Provide
reviewed, real exact-main successful checks without weakening protection or
substituting PR/parent checks. The classic branch-protection endpoint returned
403; effective rules were readable through `/rules/branches/main`. An inaccessible
administrative endpoint is a read limitation, not an exemption or approval.
The rules also retain PR reviews and CodeQL merge protection. GitHub evaluates
code-scanning merge protection separately from status checks; the guard does
not equate an `Analyze` job with that policy's result. Ordinary protected merges
and independent exact-head review remain required. Administrative rule-suite
history also returned 403 with this workspace's integration credential.

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

## Proposal, release merge, final validation and publication

1. Finish the reporting stack and #1172, then merge this independently reviewed
   guard last. Leave the legacy workflow disabled. Record implementation commit
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
