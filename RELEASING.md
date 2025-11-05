# Releasing adcp-client-python

This project uses [Release Please](https://github.com/googleapis/release-please) for automated versioning and releases. It's the Python equivalent of Changesets but works per-PR instead of per-commit.

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
