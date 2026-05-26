# Releasing adcp-client-python

This project uses [Release Please](https://github.com/googleapis/release-please) for automated versioning and releases. It's the Python equivalent of Changesets but works per-PR instead of per-commit.

## Spec-version pinning for major releases

**Read this before cutting any major SDK version (e.g. 3.x → 4.x, 4.x → 5.x).**

The SDK's generated Pydantic types in `src/adcp/types/generated_poc/` are built from the AdCP spec bundle fetched at build time. Which bundle gets fetched is pinned by `src/adcp/ADCP_VERSION`:

- **`latest`** (default on `main`): tracks spec HEAD. Types drift between regens. Safe for rolling development; **never** safe for a stable release — buyers on that version would see types mutate under them at the next regen.
- **`3.0.0`** (or any semver tag): pinned to a specific spec release. Stable. What you want to ship.

### Release-day checklist

Execute in this order. All commands run from repo root.

1. **Confirm upstream spec is tagged.** The spec repo publishes a bundle at `https://adcontextprotocol.org/protocol/{version}.tgz`. Check it returns 200:

   ```bash
   curl -sI "https://adcontextprotocol.org/protocol/3.0.0.tgz" | head -1
   # Expected: HTTP/2 200
   ```

   If 404, the spec isn't tagged yet — abort the release.

2. **Pin `ADCP_VERSION`:**

   ```bash
   echo "3.0.0" > src/adcp/ADCP_VERSION
   ```

3. **Regenerate schemas + types:**

   ```bash
   make regenerate-schemas
   ```

   This fetches the pinned bundle, rewrites `schemas/cache/`, regenerates `src/adcp/types/generated_poc/` + `_generated.py` + `_ergonomic.py`, and updates `schemas/cache/index.json.adcp_version` to match `ADCP_VERSION`.

4. **Run the full pre-push check:**

   ```bash
   make pre-push
   ```

   Includes `tests/test_schemas_version_pin.py` — a paranoia check that `ADCP_VERSION` matches `schemas/cache/index.json.adcp_version`. If you skipped step 3, this fires.

5. **Review the diff.** Expect `schemas/cache/*`, `src/adcp/types/generated_poc/*`, `_generated.py`, `_ergonomic.py`, and `src/adcp/ADCP_VERSION` to change. Anything else means a regen script mutated source — investigate.

6. **Commit + open PR:**

   ```bash
   git checkout -b bokelley/pin-spec-X.Y.Z
   git add -A
   git commit -m "chore(types): pin AdCP spec to X.Y.Z and regenerate"
   gh pr create --title "chore(types): pin AdCP spec to X.Y.Z"
   ```

7. **Merge the spec-pin PR.** Release Please's open release PR (`chore(main): release X.Y.Z`) will pick up the new commit; review the updated changelog.

8. **Merge the release PR** (`chore(main): release X.Y.Z`). PyPI publish + GitHub release + tag happen automatically.

### Reverting to `latest`

After a stable release ships, `main` typically stays pinned until the next spec drop. To resume tracking HEAD:

```bash
echo "latest" > src/adcp/ADCP_VERSION
make regenerate-schemas
```

Commit as `chore(types): resume tracking spec HEAD after X.Y.Z release`.

### Drift protection

`tests/test_schemas_version_pin.py` cross-checks `ADCP_VERSION` against `schemas/cache/index.json.adcp_version` on every CI run. You cannot merge a PR where the two drift — which means you cannot accidentally ship stable types generated from `latest` or vice versa.

## How It Works

1. **Write Conventional Commits**: Use conventional commit messages in your PRs
2. **Automatic PR**: Release Please creates a "release PR" that updates version and CHANGELOG
3. **Merge to Release**: When you merge the release PR, it creates a GitHub release and publishes to PyPI

## Conventional Commits

Release Please determines version bumps based on commit message prefixes:

### Breaking Changes (Major Version: 1.0.0 → 2.0.0)
```bash
feat!: remove deprecated API
# or
feat: add new feature

BREAKING CHANGE: removed old API
```

### New Features (Minor Version: 0.1.0 → 0.2.0)
```bash
feat: add support for new protocol
feat(client): add retry logic
```

### Bug Fixes (Patch Version: 0.1.0 → 0.1.1)
```bash
fix: resolve authentication issue
fix(mcp): handle connection timeout
```

### Other Types (No Version Bump)
```bash
docs: update README
chore: update dependencies
test: add integration tests
refactor: simplify adapter code
style: format code
ci: update GitHub Actions
```

## Release Process

### 1. Development

Work on features using conventional commits:

```bash
git checkout -b feature/my-feature
# Make changes
git commit -m "feat: add new AdCP tool support"
git push origin feature/my-feature
# Create PR
```

### 2. Automatic Release PR

When PRs are merged to `main`, Release Please:
- Analyzes commits since last release
- Determines version bump (major/minor/patch)
- Creates/updates a "Release PR" with:
  - Updated `pyproject.toml` version
  - Generated CHANGELOG.md
  - Git tag

Example Release PR title: `chore(main): release 0.2.0`

### 3. Release

When you merge the Release PR, it automatically:
- Creates a GitHub release with changelog
- Publishes package to PyPI
- Tags the commit

## PyPI Publishing Setup

### Required: PyPI API Token

1. Create account on https://pypi.org
2. Generate API token at https://pypi.org/manage/account/token/
3. Add to GitHub Secrets as `PYPI_API_TOKEN`:
   - Go to repository → Settings → Secrets → Actions
   - New repository secret
   - Name: `PYPI_API_TOKEN`
   - Value: `pypi-...` (your token)

### Package Metadata

Configured in `pyproject.toml`:

```toml
[project]
name = "adcp"
version = "0.1.0"  # Updated by Release Please
description = "Official Python client for the Ad Context Protocol (AdCP)"
authors = [{name = "AdCP Community", email = "maintainers@adcontextprotocol.org"}]
```

## Version Numbering

We follow [Semantic Versioning](https://semver.org/):

- **Major (1.0.0)**: Breaking changes
- **Minor (0.1.0)**: New features, backward compatible
- **Patch (0.0.1)**: Bug fixes, backward compatible

### Beta Release Behavior

While the current manifest version is a beta (`X.Y.Z-beta.N`), Release Please
is configured to keep the base version fixed and increment only the beta
counter. For example:

- `feat:` after `6.3.0-beta.4` -> `6.3.0-beta.5`
- `fix:` after `6.3.0-beta.4` -> `6.3.0-beta.5`

Do not pass `release-type` directly in `.github/workflows/release-please.yml`;
that bypasses the manifest configuration. The workflow must use
`release-please-config.json` and `.release-please-manifest.json` so
`versioning: prerelease` takes effect.

### Pre-1.0 Behavior

Before v1.0.0:
- Breaking changes bump MINOR version (0.1.0 → 0.2.0)
- New features bump MINOR version
- Bug fixes bump PATCH version

Configured with `bump-minor-pre-major: true` in release-please-config.json.

## Manual Release (Fallback)

If you need to release manually:

```bash
# 1. Update version in pyproject.toml
vim pyproject.toml

# 2. Build package
python -m build

# 3. Upload to PyPI
twine upload dist/*
```

## Examples

### Example 1: Feature Release

```bash
# PR merged with commits:
feat: add webhook signature verification
fix: handle MCP timeout gracefully

# Release Please creates PR:
# - Version: 0.1.0 → 0.2.0 (feat = minor bump)
# - CHANGELOG updated with both changes
```

### Example 2: Bug Fix Release

```bash
# PR merged with commits:
fix: correct A2A endpoint URL
docs: update README

# Release Please creates PR:
# - Version: 0.2.0 → 0.2.1 (fix = patch bump)
# - CHANGELOG shows fix (docs ignored)
```

### Example 3: Breaking Change

```bash
# PR merged with commit:
feat!: require Python 3.10+

BREAKING CHANGE: Dropped Python 3.9 support

# Release Please creates PR:
# - Version: 0.2.1 → 0.3.0 (breaking = minor pre-1.0)
# - CHANGELOG highlights breaking change
```

## Tips

### Good Commit Messages

✅ **Do**:
```bash
feat(mcp): add session pooling
fix(a2a): correct message format
docs: add MCP examples
test: add protocol adapter tests
```

❌ **Don't**:
```bash
update code
fix bug
changes
wip
```

### Combining Changes

Multiple changes in one PR:

```bash
feat: add new tool support
fix: resolve connection issue

# Release Please will:
# - Bump MINOR (feat takes precedence)
# - List both in CHANGELOG under appropriate sections
```

### Skip Release

To prevent a PR from triggering a release:

```bash
chore: update dev dependencies

Release-As: false
```

Or use types that don't trigger releases: `docs`, `chore`, `style`, `test`

## Monitoring

### Check Release Status

- **Release PR**: https://github.com/your-org/adcp-client-python/pulls
  - Look for "chore(main): release x.y.z"
- **Published Releases**: https://github.com/your-org/adcp-client-python/releases
- **PyPI Package**: https://pypi.org/project/adcp/

### Troubleshooting

**Release PR not created?**
- Check commits use conventional format
- Ensure commits are on `main` branch
- Verify GitHub Actions are enabled

**PyPI publish failed?**
- Check `PYPI_API_TOKEN` secret is set
- Verify token has upload permissions
- Check package name is available on PyPI

**Wrong version bump?**
- Review commit message prefixes
- Use `feat!:` or `BREAKING CHANGE:` for breaking changes
- Remember pre-1.0 treats breaking as minor bump

## Resources

- [Release Please](https://github.com/googleapis/release-please)
- [Conventional Commits](https://www.conventionalcommits.org/)
- [Semantic Versioning](https://semver.org/)
- [Python Packaging](https://packaging.python.org/)
- [Uploading to PyPI](https://packaging.python.org/tutorials/packaging-projects/)
