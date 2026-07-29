# TypeScript 13.0.0-rc.3 canonical creative corpus

This directory is a byte-for-byte vendored copy of the canonical creative
reference corpus from `adcontextprotocol/adcp-client` tag
`@adcp/sdk@13.0.0-rc.3` (commit
`cced846ef961eb6539895e8affe7331b767b0630`). It intentionally includes the
JavaScript transition tests as executable-contract source fixtures, not only
the 16 migrated option-ID examples.

`tests/test_typescript_rc3_corpus.py` pins every source file by SHA-256 and
loads every canonical product fixture through Python's primary model boundary.
The Python projection, downgrade, catalog, persistence, and MCP transition
tests mirror the behavior specified by the vendored JavaScript sources.

Do not edit these files in place. A later TypeScript reference release must be
vendored into a new versioned directory with new digests.
