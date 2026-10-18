# Releasing adcp

See [docs/releasing.md](docs/releasing.md) for the Release Please PR, GitHub
environment approval, and PyPI verification steps on `main`.

## Branches

Each branch carries one spec line. Draft-spec work stays out of stable
releases ([#1525](https://github.com/adcontextprotocol/adcp-client-python/issues/1525)).

| Branch | Carries | Releases |
| --- | --- | --- |
| `main` | Work against the latest ratified AdCP version (3.2.x today) | The current major (9.x), through release-please |
| `release/N.x` (`release/8.x`, `release/7.x`) | Fixes for an older major | Patch releases of that major, from the branch's own release workflow |
| `next` | Work against draft specs, such as AdCP 3.3 agent identity and Web Bot Auth | Pre-releases only (`10.0.0-alpha.N`), from its own release-please configuration |

Open a PR against the branch that carries the spec it implements. Code written
to an unratified spec (an open adcontextprotocol/adcp PR or a draft) targets
`next`, never `main`.

## Moving changes between branches

- **Fixes land on `main` first.** A maintainer then backports the fix to each
  `release/N.x` that needs it, as a cherry-pick PR
  (`git cherry-pick -x <sha>`) into that branch.
- **`next` takes `main` regularly**, so it doesn't drift. Merge `origin/main`
  into `next` with a true merge: push the merge to `next` directly, or merge
  the sync PR with **Create a merge commit**. Never squash or rebase a sync, or
  every later sync repeats the same conflicts. A forward-port PR (a cherry-pick
  onto `next`) also works for a single urgent fix.
- **Draft-only changes stay on `next`.** If a change is needed only by
  draft-spec code, it never goes to `main`.
- **At ratification, `next` merges into `main`**, and the next major releases
  from `main` (see [Ratification](#ratification)).
- **Labels mark PRs that need a port**: `backport release/8.x` (and so on, one
  per maintained line) and `forward-port next`. Add the label to the merged PR;
  whoever opens the port PR links it back and removes the label. A backport
  action can open the port PRs later; until then this is manual.

### Resolving `main` → `next` merge conflicts

`next` uses separate release files, so release automation conflicts are
limited:

- `CHANGELOG.md`, `release-please-config.json`, and
  `.release-please-manifest.json` belong to `main`. Take `main`'s version; on
  `next` nothing reads them except a test that accepts `main`'s version.
- `CHANGELOG.next.md`, `release-please-config.next.json`,
  `.release-please-manifest.next.json`, and
  `.github/workflows/release-please-next.yml` exist only on `next`. `main`
  never changes them.
- The `[project] version` line in `pyproject.toml` conflicts after both lines
  have released. Keep `next`'s version (`10.0.0aN`).

## Releasing from `next`

`next` has its own release-please setup. The files are separate from `main`'s
so that merges in either direction never change the other line's release flow:

- `.github/workflows/release-please-next.yml` runs on pushes to `next` only,
  with its own concurrency group and `target-branch: next`.
- `release-please-config.next.json` sets `versioning: prerelease`,
  `prerelease-type: alpha`, and `prerelease: true`, and writes
  `CHANGELOG.next.md`. `.release-please-manifest.next.json` holds `next`'s
  current version.
- The commit that added the setup ends with `Release-As: 10.0.0-alpha.0`, so
  the first release is `10.0.0-alpha.0`. Every later release increments the
  alpha number (`10.0.0-alpha.1`, ...), whatever the commit type. Don't
  merge a `next` release PR that proposes a version without `-alpha.N`.
- After release-please opens or updates the release PR, the workflow runs
  `scripts/normalize_pyproject_prerelease --release-prs --target-branch next`
  to rewrite `pyproject.toml` to PEP 440 (`10.0.0a0`). The normalizer accepts
  only `release-please--branches--next--components--adcp` PRs into `next`.
- When the release PR merges, release-please tags `v10.0.0-alpha.N`, creates a
  GitHub release marked as a pre-release, and dispatches `release-publish.yml`
  with `--ref next`. That workflow waits for `next`'s CI at the release commit,
  then publishes to PyPI after the `release-publish` environment approval.
  PyPI treats `10.0.0aN` as a pre-release: `pip install adcp` keeps resolving
  to `main`'s releases, and adopters opt in with `pip install --pre adcp` or
  `adcp==10.0.0a0`.

One-time setup: add `next` to the `release-publish` environment's deployment
branches (GitHub Settings → Environments). The PyPI Trusted Publisher needs no
change, because it is bound to the `release-publish.yml` file name and the
`release-publish` environment, not to a branch.

## Moving #1502 from `main` to `next` (one time)

#1502 added the experimental AdCP 3.3 identity code to `main`.
The revert PR removes it from `main` before `9.0.0-beta.4` (#1465) ships;
`next` already has the code. The trap: the next time `next` takes `main`, the
revert would come along and delete the identity code from `next`.

Right after the revert merges to `main`, and before anyone else merges `main`
into `next`, record the revert on `next` as merged without applying it:

```sh
git fetch origin
git switch next && git merge --ff-only origin/next
REVERT=$(git log origin/main --format=%H -1 --grep='^revert: move experimental AdCP 3.3 identity')
git merge --no-edit "$REVERT^"     # main up to just before the revert
git merge -s ours "$REVERT" -m "chore: record main's revert of #1502 as merged; next keeps the identity code"
git merge --no-edit origin/main    # the rest of main
git push origin next
```

The `-s ours` merge makes the revert an ancestor of `next` but keeps `next`'s
tree, so later syncs never apply it. Keep these merge commits: push directly
or merge with **Create a merge commit**.

Equivalent alternative: merge `origin/main` into `next` as usual, then
`git revert <REVERT>` on `next` (a revert of the revert). It gives the same
result but reports modify/delete conflicts wherever `next` has changed #1502's
files since, so prefer the `-s ours` sequence.

Use the same procedure for any later change that has to leave `main` but stay
on `next`.

## Ratification

When the draft spec that `next` tracks is ratified:

1. Merge `main` into `next` one last time and make sure `next`'s CI passes.
2. Update `next` to the ratified spec (`src/adcp/ADCP_VERSION`, schemas,
   generated types, and the identity code's experimental markers).
3. On `next`, delete `.github/workflows/release-please-next.yml`,
   `release-please-config.next.json`, `.release-please-manifest.next.json`,
   and `CHANGELOG.next.md`. Set `pyproject.toml`'s version back to `main`'s.
4. Pick `main`'s first version of the new major. Commit it on `next` as a
   conventional commit that ends with a `Release-As:` footer, for example
   `Release-As: 10.0.0` or `Release-As: 10.0.0-beta.0`, and update
   `release-please-config.json`'s channel to match (see `docs/releasing.md`).
   This must be the newest `Release-As:` commit. Release-please uses the
   newest footer it finds, so it overrides `next`'s earlier
   `Release-As: 10.0.0-alpha.0`.
5. If the 9.x line still needs fixes, create `release/9.x` from `main`, with
   its own release-please workflow like `release/8.x`'s.
6. Open a PR from `next` into `main` and merge it with **Create a merge
   commit**, so `next`'s history (and the `-s ours` merges above) stays intact.
7. Reset `next` to `main` (or delete it and recreate it when the next draft
   spec starts).
