# Web Bot Auth profile vectors

Copied without modification from adcontextprotocol/adcp PR #8298, commit
`f44785a5b65db93988f30ad3d23ba5849f1a1d5e`,
`static/compliance/source/test-vectors/wba-profile/`, on 2026-10-19. The
upstream `README.md` is vendored as `UPSTREAM-README.md`. `UPSTREAM.json`
records each file's SHA-256, and
`tests/conformance/signing/test_wba_profile.py` checks them.

The specification is `docs/building/by-layer/L1/wba-profile.mdx` at the same
commit (proposed for AdCP 3.3). The public private seeds in `keys.json` are
test inputs only. `scenarios.json` holds the failure scenarios that the AdCP
interop harness (`tests/wba-interop`) signs at run time; this SDK's CI grades
them through `tests/wba_interop/adapter.py`.
