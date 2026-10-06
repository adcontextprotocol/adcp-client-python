# Releasing adcp

Release Please opens or updates the release PR after a push to `main`. It uses the
release App credential so the release PR receives normal CI. The workflow
normalizes the proposed `pyproject.toml` prerelease version to PEP 440 (a
no-op for stable versions). Review and merge that PR once its required checks
and reviews pass.

SDK 8 uses normal SemVer versioning: `release-please-config.json` has no
`versioning: prerelease`, `prerelease-type`, or `prerelease` keys. To choose an
exact version (for example the first stable release after an rc series), the
squash commit that lands on `main` must end its body with a
`Release-As: X.Y.Z` footer. The repository squashes with the branch commit
messages as the default body, so check the final message in the merge dialog
before confirming; the PR title and description alone are not parsed.
After re-enabling a disabled Release Please workflow, run it once manually on
`main` to process the current head; subsequent main pushes start it automatically.

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
  `release-publish.yml`, and environment `release-publish`.
- Keep the existing `IPR_APP_ID` and `IPR_APP_PRIVATE_KEY` secrets available to
  `release-please.yml`.
  The App needs repository contents and pull-request write access.

If publication fails, fix the cause and rerun `release-publish.yml` for the same
release SHA and tag. PyPI will reject files it has already accepted; check its
published file list before rerunning a partial upload. Do not create another
release PR to retry publication.

## SDK 8 maintenance line (`release/8.x`)

On the `release/8.x` branch, `release-please.yml` and `release-publish.yml` are
copies of main's workflows retargeted to that branch: Release Please runs on
pushes to `release/8.x`, opens the release PR against `release/8.x`, and on
merge queues `release-publish.yml --ref release/8.x`. Publication waits for
successful push CI on `release/8.x` at the exact release commit, then the
same `release-publish` environment approval and PyPI Trusted Publisher apply
(the environment's deployment branches include `release/8.x`). Backports land
on `release/8.x` by squash merge; use a `Release-As: 8.Y.Z` footer when the
squashed commits would otherwise select another version, and remove any
breaking-change section from the generated 8.x changelog before merging the
release PR. main's release flow is unaffected.
