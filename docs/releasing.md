# Releasing adcp

Release Please opens or updates the release PR after a push to `main`. It uses the
release App credential so the release PR receives normal CI. The workflow
normalizes the proposed `pyproject.toml` prerelease version to PEP 440 (a
no-op for stable versions). Review and merge that PR once its required checks
and reviews pass.

Version-only bot release PRs, including betas, run interpreter compatibility
checks and verify the metadata in their built wheel and sdist.

SDK 9 is on the beta channel. The root package in
`release-please-config.json` uses `versioning: prerelease`,
`prerelease-type: beta`, and `prerelease: true`, so subsequent releases
remain beta and GitHub marks them as prereleases. The adopting commit ends
with `Release-As: 9.0.0-beta.1`; the release workflow normalizes that to
`9.0.0b1` in `pyproject.toml` and on PyPI. Do not merge a release PR that
proposes stable `9.0.0` while this beta channel is active.

To choose an exact version, the squash commit that lands on `main` must
end its body with a `Release-As: X.Y.Z` footer. The repository squashes with
the branch commit messages as the default body, so check the final message in the merge dialog
before confirming; the PR title and description alone are not parsed.
After re-enabling a disabled Release Please workflow, run it once manually on
`main` to process the current head; subsequent main pushes start it automatically.

The public declaration naming in
[#1401](https://github.com/adcontextprotocol/adcp-client-python/issues/1401) is
resolved: `ProductFormatDeclaration` is a `Format` subclass that reads the root
rules of `core/product-format-declaration.json` on every validation. That keeps
the four things a public-name change had to preserve — the
`canonical_formats_only`/`v1_format_ref` mutual exclusion, credential-key
screening, the explicit legacy projection boundary, and a constructible,
`isinstance`-able type whose `params_as` reaches typed parameters. Typed
`params` attribute access lives on `LegacyProductFormatDeclaration`, the
generated branch union. Make any further public-name changes during beta and
document the migration.

At the reviewed GA exit, remove the three prerelease configuration keys,
update the release-channel contract test, and select `Release-As: 9.0.0`.

On the merge, Release Please creates the tag and GitHub release, then starts
`release-publish.yml` on `main`. The publish workflow waits for successful CI on
the **exact release commit**, builds the sdist and wheel once, and checks both
with Twine. Its `release-publish` job waits for the environment reviewer. Brian
approves that job to publish the built files to PyPI via Trusted Publishing.
Verify the new version and both files at <https://pypi.org/project/adcp/>.

One-time setup:

- In GitHub Settings → Environments, configure `release-publish` with Brian as
  required reviewer and `main` as its deployment branch.
- In PyPI → adcp → Publishing, add a Trusted Publisher for owner
  `adcontextprotocol`, repository `adcp-client-python`, workflow
  `release-publish.yml`, and environment `release-publish`. The
  `adcp-reporting-storage` project needs the same (see below).
- Keep the existing `IPR_APP_ID` and `IPR_APP_PRIVATE_KEY` secrets available to
  `release-please.yml`.
  The App needs repository contents and pull-request write access.

## adcp-reporting-storage

`packages/adcp-reporting-storage` is a second Release Please component
(`separate-pull-requests` is on, so it gets its own release PR) on the same
beta channel. Its tags carry the component name
(`adcp-reporting-storage-v0.1.0-beta.1`); the root keeps bare `vX.Y.Z` tags.
Only commits that touch `packages/adcp-reporting-storage/` bump it, so a
protocol fix in `adcp` does not release the storage package and a storage fix
does not release `adcp`. Conventional-commit scopes are free text, but
`feat(reporting-storage):` keeps the two changelogs readable.

On a merge, `release-please.yml` queues `release-publish.yml` with
`package=adcp-reporting-storage`; the workflow builds that package's sdist and
wheel from `packages/adcp-reporting-storage` and publishes them through the same
`release-publish` environment approval. The storage wheel pins
`adcp>=9.0.0bN,<10`; publish the `adcp` release it depends on first.

One-time setup, before the first storage release:

- In PyPI, add a **pending** Trusted Publisher for a new project named
  `adcp-reporting-storage` (owner `adcontextprotocol`, repository
  `adcp-client-python`, workflow `release-publish.yml`, environment
  `release-publish`). A pending publisher creates the project on first upload;
  without it the publish job fails and the `adcp` release is unaffected.
- The first release PR bumps `0.1.0-beta.0` (the manifest's bootstrap value,
  never published) to `0.1.0-beta.1`. Check the PR's `pyproject.toml` version
  was normalized to `0.1.0b1` the same way the root is.

If publication fails, fix the cause and rerun `release-publish.yml` for the same
release SHA and tag. PyPI will reject files it has already accepted; check its
published file list before rerunning a partial upload. Do not create another
release PR to retry publication.
