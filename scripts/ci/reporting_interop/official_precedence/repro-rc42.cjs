'use strict';
// rc.42-adapted derivative of the historical RC.38 buyer reproduction.
// Fixture bytes are byte-identical to the historical input (hash-asserted).
// Differences from the historical probe, all deliberate:
//   * pins/version/schema identity re-resolved at rc.42 (ADCP_VERSION 3.2.0-rc.4).
//   * the historical compiled-reconciler hash assertion is NOT inherited; rc.42
//     bytes are recorded instead.
//   * NO expected-defect exemption. Every one of the 17 inputs must meet the
//     correct semantics. capture() classifies any error rather than aborting.
// Fixture data adapted from adcontextprotocol/adcp-client, Apache-2.0,
// copyright 2025 AgenticAdvertising.Org.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const sdk = require('@adcp/sdk');
const { TOOL_RESPONSE_SCHEMAS, getCanonicalToolValidator } = require('@adcp/sdk/schemas');
const pins = require('./pins.json');
const fixtureBytes = fs.readFileSync(path.join(__dirname, 'fixture.json'));
const fixture = JSON.parse(fixtureBytes);
const sha256 = v => crypto.createHash('sha256').update(v).digest('hex');
const officialId = 'revision-august-official';
const snapshotId = 'revision-august-snapshot';
const officialMaterializationId = 'materialization-billing';
const now = new Date('2026-09-03T00:00:00Z');

// ---- identity gate (independent, at the actual pin) ----
assert.equal(sha256(fixtureBytes), pins.fixtureSha256, 'fixture bytes changed');
const packageFile = require.resolve('@adcp/sdk/package.json');
const installed = require(packageFile);
assert.equal(installed.version, pins.version, 'unexpected SDK version');
assert.equal(sdk.ADCP_VERSION, pins.defaultAdcpVersion, 'unexpected default AdCP version');
const sdkRoot = path.dirname(packageFile);
const compiled = path.join(sdkRoot, 'dist/lib/reporting/reconciliation.js');
const compiledSha = sha256(fs.readFileSync(compiled));
const publicEntry = fs.realpathSync(require.resolve('@adcp/sdk'));
assert.ok(publicEntry.startsWith(fs.realpathSync(sdkRoot) + path.sep), 'entry escaped package root');
for (const n of ['evaluateReportingLedger', 'reconcileReporting', 'buildReportingReceipt'])
  assert.equal(typeof sdk[n], 'function', 'missing public API: ' + n);
const canonicalValidator = getCanonicalToolValidator('get_reporting_status', 'sync', {
  adcpVersion: pins.defaultAdcpVersion,
});
assert.equal(typeof canonicalValidator, 'function', 'missing public canonical response validator');

// ---- case construction: identical to the historical makeCase ----
function makeCase(parameters) {
  const { requiredFinality, officialArtifact, linked, reversed, singleOfficial = false } = parameters;
  const wire = structuredClone(fixture.wire);
  const expected = { ...structuredClone(fixture.expected), requiredFinality };
  Object.assign(wire.periods[0], { required_finality: requiredFinality, health: 'complete' });
  wire.scope.finality = ['snapshot', 'official'];
  wire.ledger_as_of = '2026-09-02T00:02:00Z';
  wire.ledger_snapshot_id = 'snapshot-complete-repro-history';
  const official = structuredClone(wire.revisions[0]);
  const snapshot = {
    ...structuredClone(official),
    reporting_revision_id: snapshotId,
    finality: 'snapshot',
    observed_at: '2026-09-01T00:00:00Z',
    created_at: '2026-09-01T00:00:00Z',
  };
  for (const key of ['finality_basis', 'finality_policy_id', 'finalized_at']) delete snapshot[key];
  if (linked) official.supersedes_reporting_revision_id = snapshotId;
  const officialMaterialization = structuredClone(wire.materializations[0]);
  const snapshotMaterialization = {
    ...structuredClone(officialMaterialization),
    reporting_materialization_id: 'materialization-snapshot',
    reporting_revision_id: snapshotId,
  };
  wire.revisions = singleOfficial ? [official] : [snapshot, official];
  wire.materializations = singleOfficial ? [officialMaterialization] : [
    snapshotMaterialization, ...(officialArtifact ? [officialMaterialization] : []),
  ];
  wire.receipts = wire.materializations.map((materialization, index) => sdk.buildReportingReceipt(
    {
      obligation: wire.periods[0],
      revision: wire.revisions.find(i => i.reporting_revision_id === materialization.reporting_revision_id),
      materialization,
      expected,
    },
    {
      rowCount: 7,
      controlTotals: structuredClone(fixture.wire.revisions[0].control_totals),
      canonicalContentDigest: structuredClone(fixture.wire.revisions[0].canonical_content_digest),
    },
    'reporting-receipt-probe-' + index,
    '2026-09-02T00:01:00Z',
  ));
  Object.assign(wire.periods[0], {
    revision_count: wire.revisions.length,
    materialization_count: wire.materializations.length,
    successful_materialization_count: wire.materializations.length,
    receipt_count: wire.receipts.length,
    accepted_receipt_count: wire.receipts.length,
    pending_adjustment_count: 0,
    reconciliation_status: officialArtifact ? 'accepted' : 'pending',
    health: officialArtifact ? 'complete' : 'action_required',
    issues: officialArtifact ? [] : [{
      issue_id: 'issue-official-receipt-required',
      code: 'RECEIPT_REQUIRED',
      severity: 'action_required',
      responsible_party: 'seller',
      recommended_action: 'contact_seller',
      reporting_obligation_id: 'obligation-billing',
    }],
  });
  if (reversed) for (const key of ['revisions', 'materializations', 'receipts']) wire[key].reverse();
  wire.pagination.total_count = wire.periods.length + wire.revisions.length +
    wire.materializations.length + wire.receipts.length;
  return { wire, expected };
}

// ---- independent input validation at the actual pin ----
function validateInput(wire, expected, parameters) {
  const v = { errors: [] };
  const parsed = TOOL_RESPONSE_SCHEMAS.get_reporting_status.safeParse(wire);
  v.convenienceSchemaValid = parsed.success;
  if (!parsed.success) v.errors.push({ validator: 'zod_convenience', issues: parsed.error.issues.slice(0, 6) });
  v.canonicalSchemaValid = Boolean(canonicalValidator(wire));
  if (!v.canonicalSchemaValid) v.errors.push({ validator: 'canonical_ajv', issues: (canonicalValidator.errors || []).slice(0, 6) });
  // structural/ownership invariants (same checks as the historical probe)
  const s = [];
  const ob = wire.periods[0];
  const ids = new Map(wire.revisions.map(i => [i.reporting_revision_id, i]));
  const expectedCount = parameters.singleOfficial ? 1 : parameters.officialArtifact ? 2 : 1;
  const chk = (cond, label) => { if (!cond) s.push(label); };
  chk(wire.status === 'completed', 'status');
  chk(wire.view === 'periods', 'view');
  chk(wire.account_id === 'account-1', 'account_id');
  chk(wire.periods.length === 1, 'one_obligation');
  chk(wire.scope.scope_closed === true, 'scope_closed');
  chk(wire.scope.coverage_complete === true, 'coverage_complete');
  chk(wire.pagination.has_more === false, 'pagination_terminated');
  chk(wire.pagination.cursor === undefined, 'no_cursor');
  chk(JSON.stringify(wire.scope.finality) === JSON.stringify(['snapshot', 'official']), 'scope_finality');
  chk(ob.required_finality === parameters.requiredFinality, 'required_finality');
  chk(ob.reconciliation_mode === 'consumer_receipt', 'reconciliation_mode');
  chk(ob.pending_adjustment_count === 0, 'pending_adjustment_count');
  chk(JSON.stringify(ob.issues.map(i => i.code)) === JSON.stringify(parameters.officialArtifact ? [] : ['RECEIPT_REQUIRED']), 'issue_codes');
  chk(ids.size === wire.revisions.length, 'unique_revision_ids');
  const official = ids.get(officialId);
  chk(official && official.finality === 'official', 'official_finality');
  chk(official && official.supersedes_reporting_revision_id === (parameters.linked ? snapshotId : undefined), 'supersession_link');
  chk(wire.materializations.length === expectedCount, 'materialization_count');
  chk(wire.receipts.length === expectedCount, 'receipt_count');
  chk(wire.materializations.some(i => i.reporting_revision_id === officialId) === parameters.officialArtifact, 'official_artifact_presence');
  for (const r of wire.revisions) {
    for (const k of ['account_id', 'report_definition_id', 'reporting_profile', 'media_buy_ids', 'coverage', 'period'])
      chk(JSON.stringify(r[k]) === JSON.stringify(ob[k]), 'revision_ownership:' + k);
    chk(Date.parse(r.created_at) <= Date.parse(wire.ledger_as_of), 'revision_time_bound');
  }
  for (const m of wire.materializations) {
    chk(ids.has(m.reporting_revision_id), 'materialization_joined');
    chk(m.status === 'available', 'materialization_available');
    const rec = wire.receipts.filter(i => i.reporting_materialization_id === m.reporting_materialization_id);
    chk(rec.length === 1 && rec[0].status === 'accepted', 'one_accepted_receipt');
    if (rec[0]) chk(Date.parse(rec[0].observed_at) <= Date.parse(wire.ledger_as_of), 'receipt_time_bound');
  }
  chk(wire.pagination.total_count === 1 + wire.revisions.length + expectedCount * 2, 'total_count');
  v.structuralViolations = s;
  v.inputValid = v.convenienceSchemaValid && v.canonicalSchemaValid && s.length === 0;
  return v;
}

function toLedger(wire) {
  return {
    ledgerSnapshotId: wire.ledger_snapshot_id, ledgerAsOf: wire.ledger_as_of,
    accountId: wire.account_id, scope: wire.scope, obligations: wire.periods,
    revisions: wire.revisions, materializations: wire.materializations, receipts: wire.receipts,
  };
}

// classify every outcome; never abort on an unexpected error
async function capture(action) {
  try {
    const result = await action();
    const obligations = result && result.obligations;
    if (!Array.isArray(obligations) || obligations.length !== 1)
      return { kind: 'unexpected_shape', obligationCount: Array.isArray(obligations) ? obligations.length : null };
    return { kind: 'returned', obligation: obligations[0] };
  } catch (error) {
    return { kind: 'threw', code: error.code, name: error.name, message: error.message };
  }
}

// required-correct semantics (README "Required result" column); no defect exemption
function semanticPass(outcome, officialArtifact) {
  if (outcome.kind !== 'returned') return false;
  const item = outcome.obligation;
  if (item.reportingRevisionId !== officialId) return false;
  if (!officialArtifact) return item.definitive === false && item.reportingMaterializationId === undefined;
  return item.definitive === true && item.reportingMaterializationId === officialMaterializationId &&
    Array.isArray(item.reasons) && item.reasons.length === 0;
}
function requiredDescription(officialArtifact) {
  return officialArtifact
    ? `returned; reportingRevisionId=${officialId}; definitive=true; reportingMaterializationId=${officialMaterializationId}; reasons=[]`
    : `returned; reportingRevisionId=${officialId}; definitive=false; reportingMaterializationId=undefined`;
}
function summarize(o) {
  if (o.kind !== 'returned') return o;
  const i = o.obligation;
  return { kind: 'returned', reportingRevisionId: i.reportingRevisionId,
    reportingMaterializationId: i.reportingMaterializationId, definitive: i.definitive,
    reasons: i.reasons, obligationKeys: Object.keys(i).sort() };
}

async function main() {
  const cases = [];
  for (const requiredFinality of ['snapshot', 'official'])
    for (const officialArtifact of [true, false])
      for (const linked of [false, true])
        for (const reversed of [false, true]) cases.push({ requiredFinality, officialArtifact, linked, reversed });
  cases.push({ requiredFinality: 'official', officialArtifact: true, linked: false, reversed: false, singleOfficial: true });

  const rows = [];
  for (const parameters of cases) {
    const { wire, expected } = makeCase(parameters);
    const validation = validateInput(wire, expected, parameters);
    const before = structuredClone(wire);
    const effects = [];
    let statusReads = 0;
    const direct = await capture(() => sdk.evaluateReportingLedger(toLedger(wire), [expected], now));
    const buyer = await capture(() => sdk.reconcileReporting({
      client: {
        async getReportingStatus() { statusReads += 1; return structuredClone(wire); },
        async syncReportingReceipts() { effects.push('receipt'); throw new Error('unexpected receipt submission'); },
      },
      request: { account: { account_id: 'account-1' } },
      expectedPeriods: [expected],
      async inspect(ctx) { effects.push('inspect:' + ctx.revision.reporting_revision_id); throw new Error('unexpected inspection'); },
      now,
    }));
    const mutated = JSON.stringify(wire) !== JSON.stringify(before);
    const positiveControl = Boolean(parameters.singleOfficial || (parameters.linked && parameters.officialArtifact));
    const pass = semanticPass(direct, parameters.officialArtifact);
    rows.push({
      caseId: `official_precedence/${parameters.singleOfficial ? 'single_official' :
        `${parameters.requiredFinality}/${parameters.officialArtifact ? 'both_artifacts' : 'official_without_materialization'}/${parameters.linked ? 'linked' : 'unlinked'}/${parameters.reversed ? 'reversed' : 'natural'}`}`,
      parameters, positiveControl,
      inputSha256: sha256(JSON.stringify({ wire, expected })),
      validation: { convenienceSchemaValid: validation.convenienceSchemaValid,
        canonicalSchemaValid: validation.canonicalSchemaValid,
        structuralViolations: validation.structuralViolations, inputValid: validation.inputValid,
        errors: validation.errors },
      required: requiredDescription(parameters.officialArtifact),
      evaluateReportingLedger: summarize(direct),
      reconcileReporting: summarize(buyer),
      apisAgree: JSON.stringify(summarize(direct)) === JSON.stringify(summarize(buyer)),
      callerHistoryMutated: mutated, statusReads, sideEffects: effects,
      semanticPass: pass,
      observedDefect: pass ? null
        : direct.kind === 'threw' ? `graph_error:${direct.code || direct.name}`
        : direct.kind === 'returned' && Array.isArray(direct.obligation.reasons) && direct.obligation.reasons.length
          ? `nondefinitive:${direct.obligation.reasons.join(',')}`
          : `other:${direct.kind}`,
    });
  }
  const report = {
    scope: 'synthetic_in_process_public_buyer_apis_only__NOT_http_quadrant_acceptance',
    derivedFrom: { historicalBundle: 'rc38 official-precedence reproduction',
      fixtureSha256: pins.fixtureSha256, historicalPin: pins.historicalPin,
      defectExemptionsRemoved: true },
    pin: { package: pins.package, version: installed.version, integrity: pins.integrity,
      shasum: pins.shasum, tarballSha256: pins.tarballSha256, gitHead: pins.gitHead,
      adcpVersion: sdk.ADCP_VERSION, engines: installed.engines },
    runtime: { node: process.version, platform: process.platform, arch: process.arch },
    source: { fixtureSha256: sha256(fixtureBytes), compiledReconcilerSha256: compiledSha,
      compiledReconcilerBytes: fs.statSync(compiled).size },
    totals: {
      inputs: rows.length,
      convenienceSchemaValid: rows.filter(r => r.validation.convenienceSchemaValid).length,
      canonicalSchemaValid: rows.filter(r => r.validation.canonicalSchemaValid).length,
      structurallyValid: rows.filter(r => r.validation.structuralViolations.length === 0).length,
      publicApiCalls: rows.length * 2,
      apisAgree: rows.filter(r => r.apisAgree).length,
      callerHistoryMutations: rows.filter(r => r.callerHistoryMutated).length,
      unexpectedSideEffects: rows.reduce((n, r) => n + r.sideEffects.length, 0),
      semanticPass: rows.filter(r => r.semanticPass).length,
      semanticFail: rows.filter(r => !r.semanticPass).length,
      positiveControls: rows.filter(r => r.positiveControl).length,
      positiveControlsPassing: rows.filter(r => r.positiveControl && r.semanticPass).length,
      ambiguityDefectCases: rows.filter(r => !r.positiveControl && r.parameters.officialArtifact).length,
      ambiguityDefectReproduced: rows.filter(r => !r.positiveControl && r.parameters.officialArtifact &&
        r.observedDefect === 'nondefinitive:AMBIGUOUS_REVISION_CHAIN').length,
      missingArtifactCases: rows.filter(r => !r.parameters.officialArtifact).length,
      missingArtifactGraphErrors: rows.filter(r => !r.parameters.officialArtifact &&
        r.observedDefect === 'graph_error:LEDGER_GRAPH_INTEGRITY_FAILED').length,
    },
    semanticCompatibilityPassed: rows.every(r => r.semanticPass),
    allInputsValid: rows.every(r => r.validation.inputValid),
    cases: rows,
  };
  const outFile = process.argv[2];
  if (outFile) fs.writeFileSync(outFile, JSON.stringify(report, null, 2) + '\n');
  else process.stdout.write(JSON.stringify(report, null, 2) + '\n');
  console.error(JSON.stringify(report.totals, null, 2));
  console.error('allInputsValid              = ' + report.allInputsValid);
  console.error('semanticCompatibilityPassed = ' + report.semanticCompatibilityPassed);
  process.exitCode = report.semanticCompatibilityPassed && report.allInputsValid ? 0 : 1;
}
main().catch(e => {
  process.stderr.write(JSON.stringify({ kind: 'harness_failure', message: e.message, stack: String(e.stack).split('\n').slice(0,4) }) + '\n');
  process.exitCode = 2;
});
