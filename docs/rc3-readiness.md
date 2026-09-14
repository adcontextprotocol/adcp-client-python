# Python readiness for AdCP 3.2 RC.3

Checked September 14, 2026 against Python main at
`011db8fcf0c024dec44d39c80483a2eabde1be2f`, plus the control-intent fixes in
this change. The SDK remains pinned to published **AdCP 3.2.0-rc.2**.
The proposed rc.3 contents are tracked by
[adcp#7485](https://github.com/adcontextprotocol/adcp/pull/7485); recheck that
release's final contents before upgrading.

## Current baseline

- The negotiated list/buy coordinator is implemented by
  [#1148](https://github.com/adcontextprotocol/adcp-client-python/pull/1148).
- Error recovery metadata and native compact response envelopes are fixed by
  [#1156](https://github.com/adcontextprotocol/adcp-client-python/pull/1156) and
  [#1157](https://github.com/adcontextprotocol/adcp-client-python/pull/1157).
- The protocol repository's real MCP response probe passes **124/124** cases
  against this source checkout and the immutable rc.2 artifact: both empty
  catalogs, all 119 standard error classifications, and three extension-code
  recovery overrides. There are no skipped cases or findings in that scope.
  This is catalog/error evidence, not certification of all 78 protocol tools
  or completed purchase workflows.
- Compact control requests previously defaulted omitted `canceled` fields to
  `true`, both on the buy and on individual packages. This change fixes the
  generated defaults and the regeneration hook. Cancellation remains an
  explicit `true` operation; `false` is rejected.
- The client now preserves explicitly supplied `None` for the existing
  control-task clear operations: buy/package `daily_budget_cap` and buy
  `budget_cap_timezone`. Omitted fields remain absent and numeric zero stays
  zero. `tests/test_control_request_intent.py` exercises the actual MCP client,
  transport, and typed seller handler with request/response validation enabled.

The checkout still identifies its package as `8.0.0b14` until Release Please
updates the version. These results describe the source commit and fixes above,
**not** the previously published wheel with that same version string. Publish
an updated SDK and rerun the probe against that installed wheel before updating
the protocol repository's release-pinned conformance baseline.

## RC.3 upgrade work

Regeneration is necessary, but does not complete this upgrade:

| Area | Implementation and acceptance work |
| --- | --- |
| Published artifact | Wait for the immutable rc.3 bundle, then use the existing checksum/Sigstore verification and regeneration flow. Keep the current rc.2 pin until that artifact exists. Run schema-version, generated-code, public-import, and adopter type checks. |
| Nullable targeting inputs | Carry omission, replacement, and explicit null through both compact and established request paths, including nested purchases/packages and MCP/A2A. Current client serialization commonly uses `exclude_none=True`; the control fix here covers only the three existing clear fields above. Extend the request serialization deliberately for the new targeting input schemas. Accepted terms, discovery, and readbacks must keep their strict non-null shapes. |
| Frequency caps | Regenerate the aggregate MediaBuy cap, product participation, discovery constraints, action IDs, and refinement-removal inputs. Update handwritten checks in `src/adcp/media_buy_actions.py` and refinement preflight as needed; test valid native calls through the transport. An upgraded generated type must not still fail an older action whitelist. Test package and MediaBuy caps separately. |
| Reporting statements | Extend `ConsumerStatusRecord`, ingest, Postgres storage, and wire projection for `content_mismatch` and `mismatch_code`. Validate upgrades of existing databases, not only freshly created tables. Keep consumer evidence scoped to its authenticated caller. |
| Reporting health | Implement the stale-`received` grace window using the first superseding revision and the authoritative snapshot clock. Later restatements must not restart the window. Add the pending-consumer-status count without changing seller health or reliability statistics merely because a buyer is silent. |
| Reporting issue lifecycle | Implement issue timestamps/states, optional escalation/contact behavior, and the constraints on resolving or waiving a still-current mismatch. Reject reserved `authoritative_party: consumer` with `UNSUPPORTED_FEATURE` before installing a configuration. |

The reporting requirements come from
[adcp#7465](https://github.com/adcontextprotocol/adcp/pull/7465). Current
`project_consumer_mismatch()` immediately escalates a superseded received
revision and does not take the clock or full revision chain needed for rc.3's
grace rule. A schema-only upgrade cannot supply that behavior.

Keep `sync_reporting_status` opt-in during the published experimental notice
window; rc.3 does not make the consumer-status task universally required.

## Regression and release evidence

Run the existing CI gates, including all supported Python versions and the
Postgres reporting lifecycle test. In addition to schema checks, retain the
negotiated catalog/purchase, continuation replay, version-matrix, and native
legacy-handler tests. Add rc.3 cases proving the semantic distinctions in the
table above, then rerun the cross-language probe against the released SDK and
the exact served artifact.

The seller reverse facade
([#1147](https://github.com/adcontextprotocol/adcp-client-python/issues/1147))
and broader negotiated buyer parity
([#1154](https://github.com/adcontextprotocol/adcp-client-python/issues/1154))
remain separate work. A successful rc.3 schema adoption or response probe must
not be reported as completing either lifecycle feature.
