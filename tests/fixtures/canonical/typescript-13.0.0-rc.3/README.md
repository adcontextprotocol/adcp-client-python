# TypeScript 13.0.0-rc.3 canonical creative corpus

This directory is a byte-for-byte vendored snapshot of the canonical creative
transition contract from `adcontextprotocol/adcp-client` tag
`@adcp/sdk@13.0.0-rc.3` (commit
`cced846ef961eb6539895e8affe7331b767b0630`). It contains 16 JavaScript
canonical projection/transition test sources and all 15 files from the
upstream `test/lib/v2-projection-fixtures/` directory, not only the 16 migrated
option-ID examples.

`tests/test_typescript_rc3_corpus.py` pins all 31 files by SHA-256 and loads
every canonical product fixture through Python's primary model boundary. The
JavaScript files are immutable contract references; Python's projection,
downgrade, catalog, persistence, and transport tests exercise the corresponding
SDK behavior.

Do not edit these files in place. A later TypeScript reference release must be
vendored into a new versioned directory with new digests.
