# Releasing adcp-client-python

Use the [guarded release runbook](docs/releasing.md) for the release procedure,
operator prerequisites, evidence requirements and recovery. Release Please
prepares the version and changelog in a normal pull request; publication is a
separate, protected operation after that PR is merged and its exact main
commit is accepted.

The legacy `.github/workflows/release-please.yml` is retired and must remain
disabled. The guarded publisher uses PyPI Trusted Publishing. Do not follow
older instructions to enable the legacy workflow, add a PyPI API token, or
upload locally rebuilt distributions as a fallback.

## Prepare the release

1. Integrate reviewed changes using concrete conventional commit subjects.
   Describe public breaking changes with `!` and a `BREAKING CHANGE:` footer.
2. Complete acceptance on the actual integrated main commit, including the
   applicable installed-artifact and interoperability checks. For the reporting
   rollout, carry the [upgrade and release notes](docs/reporting-release-notes.md)
   into the release proposal, including all nine historical comparison limits.
3. Complete the runbook's environment, proposal App, PyPI trust, historical
   workflow retirement and release-tag prerequisites. Freeze main only for
   the controlled proposal or publication window, after integration.
4. Use `release-proposal.yml` with the exact current-main SHA and successful
   main-push CI run and attempt. Its acceptance and protected environment gate
   precede creation or update of the Release Please PR. A proposal creates
   neither a release tag nor a PyPI upload.
5. Review the generated version, manifest, changelog and normalized Python
   package version. Merge the release PR through the repository's normal
   review and check requirements after the proposal window has drained and
   its main freeze has been removed.
6. Obtain fresh CI and acceptance for the release PR's actual main merge
   commit. Establish a new freeze and target-bound attestation, then use
   `release-publish.yml` with that commit, CI run/attempt and release PR number.
   Environment approval and final gate checks precede publication of the
   accepted distribution bytes and creation of the exact tag/release.
7. Verify registry package versions, hashes and a clean installed consumer.
   If publication stops partway through, use the runbook's guarded recovery
   procedure with the original accepted bytes.

This overview does not establish that the reporting rollout or external
publishing configuration is ready. The final guard integration, exact-main
acceptance and operator prerequisites remain release gates.

## Protocol version and generated types

`src/adcp/ADCP_VERSION` pins the protocol inputs used to generate the SDK's
types. A prerelease protocol pin is appropriate for an SDK prerelease; a
stable SDK release requires the separately reviewed stable protocol adoption.
Do not cut a stable release from a moving `latest` pin.

When changing the protocol pin, use the repository's signed-input update
procedure, regenerate schemas and types, and run the complete checks:

```sh
make regenerate-schemas
make pre-push
```

Review the resulting cache, generated model and provenance changes together
with the pin. See [signed AdCP inputs](docs/protocol-3.2-rc6.md) for the current
protocol and historical bundle policy. A successful download alone does not
establish signed provenance or installed-package contents.

## Versioning and commit subjects

`release-please-config.json` and `.release-please-manifest.json` control release
versioning. Review the proposal's exact version and PEP 440 normalization;
changing only `pyproject.toml` is not a release procedure. The current
configuration is for prereleases, so do not infer the next version from normal
stable SemVer examples.

Use concrete conventional subjects, for example:

```text
feat(reporting): add authenticated receipt ingress
fix(reporting): preserve the account on configuration leases
docs(reporting): explain activation and upgrade boundaries
```

For a breaking change, describe the required migration:

```text
fix(reporting)!: scope configuration generations by account

BREAKING CHANGE: generation_key returns ReportingConfigurationGenerationKey;
replace tuple indexing with its named account/configuration/version fields.
```

Documentation commits may not generate their own changelog entries. Preserve
the required migration and compatibility notes when reviewing the actual
release proposal even when Release Please does not select them automatically.

## Status and troubleshooting

Check the repository's [release PRs](https://github.com/adcontextprotocol/adcp-client-python/pulls),
[workflow runs](https://github.com/adcontextprotocol/adcp-client-python/actions)
and [published releases](https://github.com/adcontextprotocol/adcp-client-python/releases),
then the [PyPI package](https://pypi.org/project/adcp/).

If a proposal or publication is blocked, inspect the selected run's exact
target, CI attempt, acceptance result and environment approval. Follow the
runbook for missing operator configuration or recovery; neither a skipped
check nor a previous commit's successful build qualifies the current target.
