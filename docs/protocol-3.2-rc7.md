# AdCP 3.2.0-rc.7 default

The SDK now uses AdCP `3.2.0-rc.7` by default (`3.2-rc.7` on the wire).
The published [rc.7 release](https://github.com/adcontextprotocol/adcp/releases/tag/v3.2.0-rc.7)
is bound to source commit `4ca13ae5cb65dff40aa514619f677293616522cd`.
Its signed tarball has SHA-256
`942b24c66500839b3db21e74f69f2fe34d6ac18f898acc1f67aa126e35547994`.
`schemas/releases/3.2.0-rc.7.json` pins the exact archive, checksum,
signature and certificate assets. Schema sync verifies their bytes, checksum,
Sigstore certificate identity and issuer before extraction. The installed
package carries the transformed rc.7 schema cache and the original compliance
inputs with per-file provenance.

Rc.7 adds `viewable_rate` optimization and its viewability-standard binding to
products and capabilities, and retains the deprecated account selector in
`list_creative_formats`. The generated Python models expose these fields;
authoritative JSON Schema validation enforces conditional goal requirements.
The current reporting summary and receipt shapes retain their rc.6 behavior.

The rc.6 and rc.3 bundles remain available for offline validation. They are not
advertised as live client or reporting mount pins; new mounts select rc.7.
Existing stored rc.6 snapshots and records are not rewritten. An rc.7 runtime
refuses an explicit rc.6 live mount or continuation, including after restart;
an rc.7 read cannot reuse an rc.6 cursor or checkpoint. A new rc.7 read creates
its own snapshot. As before, continuation requires the bound version and
authorization checks; a signed
legacy unversioned representation-v1 feed checkpoint may continue only with
the same captured filters and no ownership mode. See the
[rc.6 history notes](protocol-3.2-rc6.md) and
[reporting release notes](reporting-release-notes.md) for those boundaries.

Durable buyer receipt submission plans made under rc.6 also keep their exact
request and confirmation bytes. Completed plans remain readable; retrying their
original receipt list returns the old completed result, even after another rc.7
plan completes. A new rc.7 reservation can follow them. An unresolved rc.6 plan
remains readable, but the rc.7 SDK refuses to send its pending request (`INVALID_SUBMISSION_PLAN`),
even when a new proposal is offered. It never rewrites an uncertain request or
its idempotency key to rc.7.
