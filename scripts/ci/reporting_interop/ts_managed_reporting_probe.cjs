#!/usr/bin/env node
'use strict';

/* Focused public-API preflight; this is not storyboard execution. */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const {
  ACCOUNT_ID,
  CANONICAL_DIGEST,
  createManagedReportingFixture,
} = require('./ts_managed_reporting_fixture.cjs');

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith('--') || value === undefined) throw new Error(`invalid argument near ${String(flag)}`);
    values[flag.slice(2)] = value;
  }
  return values;
}

function required(args, name) {
  if (!args[name]) throw new Error(`missing --${name}`);
  return args[name];
}

function loadPinned(args) {
  const lockPath = path.resolve(required(args, 'package-lock'));
  const req = createRequire(path.join(path.dirname(lockPath), 'package.json'));
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const entry = lock.packages?.['node_modules/@adcp/sdk'];
  const installed = req('@adcp/sdk/package.json');
  assert.equal(installed.version, required(args, 'expected-version'));
  assert.equal(entry?.version, installed.version);
  assert.equal(entry?.integrity, required(args, 'expected-integrity'));
  const sdk = req('@adcp/sdk');
  const ledger = req('@adcp/sdk/reporting/ledger');
  const schemas = req('@adcp/sdk/schemas');
  for (const name of [
    'PostgresReportingLedgerStore', 'PostgresReportingManagedDeliveryStore',
    'createReportingManagedDeliveryRuntime', 'createSyncReportingReceiptsHandler',
    'reportingManagedDeliveryBindingV1', 'reportingCanonicalAdjustmentSha256V1',
  ]) assert.ok(ledger[name], `missing public ledger export: ${name}`);
  return {
    installed,
    sdk,
    ledger,
    schemas,
    pg: req('pg'),
    jcs: { canonicalize: sdk.canonicalize },
  };
}

function validate(api, tool, value) {
  const convenience = api.schemas.TOOL_RESPONSE_SCHEMAS[tool].safeParse(value);
  assert.equal(convenience.success, true, JSON.stringify(convenience.error?.issues || []));
  const canonical = api.schemas.getCanonicalToolValidator(tool, 'sync', { adcpVersion: '3.2.0-rc.4' });
  assert.equal(typeof canonical, 'function', `canonical ${tool} response validator unavailable`);
  assert.equal(Boolean(canonical(value)), true, JSON.stringify(canonical.errors || []));
}

async function withFixture(api, bootstrap, mode, run) {
  const schema = `adcp_interop_${mode}_${process.pid}_${Math.random().toString(16).slice(2)}`;
  await bootstrap.query(`CREATE SCHEMA "${schema}"`);
  const pool = new api.pg.Pool({
    connectionString: bootstrap.options.connectionString,
    options: `-c search_path="${schema}"`,
    max: 2,
  });
  try {
    const fixture = await createManagedReportingFixture(api, pool, { mode });
    return await run(fixture);
  } finally {
    await pool.end();
    await bootstrap.query(`DROP SCHEMA IF EXISTS "${schema}" CASCADE`);
  }
}

function revisionReceipt(fixture, overrides = {}) {
  return {
    reporting_receipt_id: 'rr-billing-official-receipt-0001',
    reporting_obligation_id: fixture.state.obligation.reporting_obligation_id,
    reporting_revision_id: fixture.state.revision.reporting_revision_id,
    reporting_materialization_id: fixture.state.materialization.reporting_materialization_id,
    status: 'rejected',
    verification_profile: 'canonical_digest',
    observed_row_count: 2,
    observed_control_totals: structuredClone(fixture.state.revision.wireRevision.control_totals),
    rejection_codes: ['CANONICAL_DIGEST_MISMATCH'],
    observed_at: fixture.state.now,
    ...overrides,
  };
}

async function managedPreflight(api, fixture) {
  const prepared = await fixture.controller('reliable_reporting_managed_delivery_probe', 'prepare');
  assert.equal(prepared.success, true);
  const suppressed = await fixture.controller('reliable_reporting_managed_delivery_probe', 'suppress_readiness');
  assert.equal(suppressed.simulated.readiness_notification_suppressed, true);
  const status = await fixture.status('revision', {
    reporting_revision_id: prepared.simulated.reporting_revision_id,
  });
  validate(api, 'get_reporting_status', status);
  assert.equal(status.materializations[0].reporting_materialization_id,
    prepared.simulated.reporting_materialization_id);
  assert.equal(status.materializations[0].status, 'available');
  assert.ok(status.materializations[0].verification?.verified_at);
  assert.ok(status.materializations[0].resource?.expires_at);
  const retention = await fixture.controller('reliable_reporting_managed_delivery_probe', 'advance_within_retention');
  assert.equal(retention.simulated.resource_readable, true);
  assert.equal(retention.simulated.reporting_materialization_id, prepared.simulated.reporting_materialization_id);
  const revoked = await fixture.controller('reliable_reporting_managed_delivery_probe', 'revoke_access');
  assert.equal(revoked.simulated.access_revoked, true);
  assert.equal(revoked.simulated.historical_metadata_retained, true);
  assert.equal(typeof revoked.simulated.revocation_elapsed_seconds, 'number');
  return {
    prepared: prepared.simulated,
    status: {
      materialization_status: status.materializations[0].status,
      verified: Boolean(status.materializations[0].verification),
      adjustments_present: Array.isArray(status.adjustments),
    },
    retention: retention.simulated,
    revocation: revoked.simulated,
    provider_events: structuredClone(fixture.provider.events),
  };
}

async function billingPreflight(api, fixture) {
  const prepared = await fixture.controller('reliable_reporting_reconciled_billing_probe', 'prepare');
  assert.equal(prepared.success, true);
  assert.deepEqual(prepared.simulated.canonical_content_digest, CANONICAL_DIGEST);
  const baseline = await fixture.status('periods');
  validate(api, 'get_reporting_status', baseline);
  if (baseline.status === 'failed') {
    await fixture.core.createSnapshot({
      account_id: ACCOUNT_ID,
      consumer_id: fixture.consumerId,
      view: 'periods',
    });
  }
  assert.ok(Array.isArray(baseline.revisions) && baseline.revisions.length > 0,
    `periods baseline omitted revisions: ${JSON.stringify(baseline)}`);
  assert.equal(baseline.revisions[0].reporting_revision_id, prepared.simulated.reporting_revision_id);
  assert.equal(baseline.revisions[0].finality, 'official');
  assert.equal(baseline.periods[0].health, 'action_required');
  assert.ok(baseline.periods[0].issues.some(value => value.code === 'RECEIPT_REQUIRED'));
  assert.ok(baseline.changes_checkpoint);

  const rejected = await fixture.runtime.syncReportingReceipts({
    account: { account_id: ACCOUNT_ID },
    idempotency_key: 'd09c6b4b-4ff4-4f46-8279-bcf3bb48b501',
    receipts: [revisionReceipt(fixture)],
  }, fixture.context);
  validate(api, 'sync_reporting_receipts', rejected);
  assert.equal(rejected.results[0].result, 'recorded');

  const accepted = await fixture.runtime.syncReportingReceipts({
    account: { account_id: ACCOUNT_ID },
    idempotency_key: 'd09c6b4b-4ff4-4f46-8279-bcf3bb48b502',
    receipts: [revisionReceipt(fixture, {
      reporting_receipt_id: 'rr-billing-official-repaired-0001',
      supersedes_reporting_receipt_id: 'rr-billing-official-receipt-0001',
      status: 'accepted',
      observed_canonical_content_digest: structuredClone(CANONICAL_DIGEST),
      rejection_codes: undefined,
    })],
  }, fixture.context);
  validate(api, 'sync_reporting_receipts', accepted);
  assert.equal(accepted.results[0].result, 'recorded');

  const staleRevision = await fixture.runtime.syncReportingReceipts({
    account: { account_id: ACCOUNT_ID },
    idempotency_key: 'd09c6b4b-4ff4-4f46-8279-bcf3bb48b503',
    receipts: [revisionReceipt(fixture, {
      reporting_receipt_id: 'rr-billing-official-stale-0001',
      supersedes_reporting_receipt_id: 'rr-billing-official-repaired-0001',
      status: 'accepted',
      observed_control_totals: [],
      observed_canonical_content_digest: structuredClone(CANONICAL_DIGEST),
      rejection_codes: undefined,
    })],
  }, fixture.context);
  validate(api, 'sync_reporting_receipts', staleRevision);
  assert.equal(staleRevision.results[0].result, 'failed');
  const acceptedStatus = await fixture.status('periods');
  validate(api, 'get_reporting_status', acceptedStatus);
  assert.equal(acceptedStatus.periods[0].reconciliation_status, 'accepted');
  assert.equal(acceptedStatus.periods[0].health, 'complete');
  const revisionHistory = await fixture.status('revision', {
    reporting_revision_id: prepared.simulated.reporting_revision_id,
  });
  assert.equal(revisionHistory.receipts.length, 2);

  const published = await fixture.controller('reliable_reporting_reconciled_billing_probe', 'publish_adjustment');
  assert.equal(published.success, true);
  assert.equal(published.simulated.adjustments.length, 2);
  const reopened = await fixture.status('periods');
  validate(api, 'get_reporting_status', reopened);
  assert.equal(reopened.periods[0].reconciliation_status, 'pending');
  assert.ok(reopened.periods[0].issues.some(value => value.code === 'ADJUSTMENT_RECEIPT_REQUIRED'));

  const [acceptedAdjustment, disputedAdjustment] = published.simulated.adjustments;
  const adjustmentReceipts = await fixture.runtime.syncReportingReceipts({
    account: { account_id: ACCOUNT_ID },
    idempotency_key: 'd09c6b4b-4ff4-4f46-8279-bcf3bb48b504',
    adjustment_receipts: [
      {
        reporting_receipt_id: 'rr-adjustment-accepted-receipt-01',
        reporting_adjustment_id: acceptedAdjustment.reporting_adjustment_id,
        adjusts_reporting_revision_id: prepared.simulated.reporting_revision_id,
        status: 'accepted',
        observed_adjustment_sha256: acceptedAdjustment.canonical_adjustment_sha256,
        observed_at: fixture.state.now,
      },
      {
        reporting_receipt_id: 'rr-adjustment-rejected-receipt-01',
        reporting_adjustment_id: disputedAdjustment.reporting_adjustment_id,
        adjusts_reporting_revision_id: prepared.simulated.reporting_revision_id,
        status: 'rejected',
        observed_adjustment_sha256: published.simulated.disputed_observed_adjustment_sha256,
        rejection_codes: ['CANONICAL_DIGEST_MISMATCH'],
        observed_at: fixture.state.now,
      },
    ],
  }, fixture.context);
  validate(api, 'sync_reporting_receipts', adjustmentReceipts);
  assert.deepEqual(adjustmentReceipts.results.map(value => value.result), ['recorded', 'recorded']);
  assert.equal(adjustmentReceipts.results[0].adjustment_receipt.status, 'accepted');
  assert.equal(adjustmentReceipts.results[1].adjustment_receipt.status, 'rejected');
  const rejectedStatus = await fixture.status('periods');
  assert.equal(rejectedStatus.periods[0].reconciliation_status, 'rejected');
  assert.ok(rejectedStatus.periods[0].issues.some(value => value.code === 'ADJUSTMENT_RECEIPT_REJECTED'));

  const repair = await fixture.runtime.syncReportingReceipts({
    account: { account_id: ACCOUNT_ID },
    idempotency_key: 'd09c6b4b-4ff4-4f46-8279-bcf3bb48b505',
    adjustment_receipts: [{
      reporting_receipt_id: 'rr-adjustment-repaired-receipt-01',
      supersedes_reporting_receipt_id: 'rr-adjustment-rejected-receipt-01',
      reporting_adjustment_id: disputedAdjustment.reporting_adjustment_id,
      adjusts_reporting_revision_id: prepared.simulated.reporting_revision_id,
      status: 'accepted',
      observed_adjustment_sha256: disputedAdjustment.canonical_adjustment_sha256,
      observed_at: fixture.state.now,
    }],
  }, fixture.context);
  assert.equal(repair.results[0].result, 'recorded');
  const staleAdjustment = await fixture.runtime.syncReportingReceipts({
    account: { account_id: ACCOUNT_ID },
    idempotency_key: 'd09c6b4b-4ff4-4f46-8279-bcf3bb48b506',
    adjustment_receipts: [{
      reporting_receipt_id: 'rr-adjustment-stale-receipt-01',
      supersedes_reporting_receipt_id: 'rr-adjustment-repaired-receipt-01',
      reporting_adjustment_id: disputedAdjustment.reporting_adjustment_id,
      adjusts_reporting_revision_id: prepared.simulated.reporting_revision_id,
      status: 'accepted',
      observed_adjustment_sha256: disputedAdjustment.canonical_adjustment_sha256,
      observed_at: fixture.state.now,
    }],
  }, fixture.context);
  assert.equal(staleAdjustment.results[0].result, 'failed');
  const restored = await fixture.status('periods');
  validate(api, 'get_reporting_status', restored);
  assert.equal(restored.periods[0].reconciliation_status, 'accepted');
  assert.equal(restored.periods[0].health, 'complete');
  assert.equal(restored.periods[0].adjustment_receipt_count, 3);
  const repaired = await fixture.status('periods', { changes_after: baseline.changes_checkpoint });
  validate(api, 'get_reporting_status', repaired);
  assert.equal(repaired.revisions[0].reporting_revision_id, prepared.simulated.reporting_revision_id);
  assert.equal(repaired.periods[0].adjustment_count, 2);
  assert.equal(repaired.adjustments.length, 2);
  assert.equal(repaired.adjustment_receipts.length, 3);
  return {
    prepared: prepared.simulated,
    baseline: {
      health: baseline.periods[0].health,
      issue_codes: baseline.periods[0].issues.map(value => value.code),
      checkpoint: baseline.changes_checkpoint,
    },
    revision_receipts: {
      rejected: rejected.results[0].result,
      accepted_replacement: accepted.results[0].result,
      stale_after_terminal: staleRevision.results[0].result,
      history_count: revisionHistory.receipts.length,
    },
    adjustments: published.simulated,
    adjustment_receipts: {
      initial: adjustmentReceipts.results.map(value => value.adjustment_receipt.status),
      repaired: repair.results[0].result,
      stale_after_terminal: staleAdjustment.results[0].result,
      final_count: restored.periods[0].adjustment_receipt_count,
    },
    final: {
      reconciliation_status: restored.periods[0].reconciliation_status,
      health: restored.periods[0].health,
      checkpoint_repair_adjustments: repaired.adjustments.length,
      checkpoint_repair_receipts: repaired.adjustment_receipts.length,
    },
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const api = loadPinned(args);
  assert.equal(typeof api.sdk.canonicalize, 'function', 'missing public canonicalize export');
  const pgUrl = required(args, 'pg-url');
  const bootstrap = new api.pg.Pool({ connectionString: pgUrl, max: 1 });
  try {
    const version = await bootstrap.query('SHOW server_version');
    const managed = await withFixture(api, bootstrap, 'managed', fixture => managedPreflight(api, fixture));
    const billing = await withFixture(api, bootstrap, 'billing', fixture => billingPreflight(api, fixture));
    const report = {
      kind: 'managed_reporting_controller_preflight',
      status: 'passed',
      blocking_acceptance: false,
      package: { name: api.installed.name, version: api.installed.version },
      runtime: { node: process.version, postgres: version.rows[0].server_version },
      operations: { managed, billing },
      limitations: [
        'focused_public_controller_preflight_not_storyboard_execution',
        'real_postgresql_and_real_clock_not_controlled_clock_fixture',
        'notification_suppression_is_fixture_gate_not_signed_webhook_credit',
        'probe_scheduler_dst_unavailable_in_stock_rc45_producer',
      ],
    };
    fs.writeFileSync(path.resolve(required(args, 'output')), `${JSON.stringify(report, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify(report)}\n`);
  } finally {
    await bootstrap.end();
  }
}

main().catch(error => {
  process.stderr.write(`${JSON.stringify({ kind: 'managed_reporting_controller_preflight_failure', message: error.message, stack: error.stack })}\n`);
  process.exitCode = 1;
});
