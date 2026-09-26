#!/usr/bin/env node
'use strict';

/* Real-MCP focused preflight for managed/reconciled controller routes. */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { CANONICAL_DIGEST } = require('./ts_managed_reporting_fixture.cjs');
const { callWithProbeAbort, createProbeAbort } = require('./ts_probe_abort.cjs');

function parseArgs(argv) {
  const out = {};
  for (let index = 0; index < argv.length; index += 2) {
    if (!argv[index]?.startsWith('--') || argv[index + 1] === undefined) throw new Error('invalid arguments');
    out[argv[index].slice(2)] = argv[index + 1];
  }
  return out;
}

async function main() {
  const input = parseArgs(process.argv.slice(2));
  const lockPath = path.resolve(input['package-lock']);
  const req = createRequire(path.join(path.dirname(lockPath), 'package.json'));
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const installed = req('@adcp/sdk/package.json');
  const entry = lock.packages?.['node_modules/@adcp/sdk'];
  if (installed.version !== input['expected-version'] || entry?.integrity !== input['expected-integrity']) {
    throw new Error('refusing to execute an unpinned SDK installation');
  }
  const sdk = req('@adcp/sdk');
  const mode = input.mode;
  if (!['managed', 'billing'].includes(mode)) throw new Error('mode must be managed or billing');
  const token = process.env.REPORTING_MANAGED_TOKEN;
  if (!token) throw new Error('REPORTING_MANAGED_TOKEN is required');
  const agent = {
    agent_uri: input.endpoint,
    auth_token: token,
    id: `managed-reporting-${mode}`,
    name: `managed-reporting-${mode}`,
    protocol: 'mcp',
  };
  const probeAbort = createProbeAbort(`managed reporting ${mode} probe`);
  const call = async (tool, params) => {
    const raw = await callWithProbeAbort(probeAbort, signal => sdk.ProtocolClient.callTool(
      agent, tool, params, {
        adcpVersion: input['adcp-version'],
        signal,
        wireAdcpVersion: input['adcp-version'],
        transport: { requestTimeoutMs: 10_000, maxResponseBytes: 4 * 1024 * 1024 },
      },
    ));
    return sdk.unwrapProtocolResponse(raw, tool, 'mcp', { responseAdcpVersion: input['adcp-version'] });
  };
  const accountId = 'reporting_core_lab';
  const account = { account_id: accountId, sandbox: true };
  const scenario = mode === 'billing'
    ? 'reliable_reporting_reconciled_billing_probe'
    : 'reliable_reporting_managed_delivery_probe';
  const controller = operation => call('comply_test_controller', {
    account, scenario, params: { operation },
  });
  const expectSelectorRejected = async selector => {
    try {
      await call('get_reporting_status', { account: selector, view: 'periods' });
    } catch (error) {
      assert.match(String(error?.message || error), /authorized|FORBIDDEN/i);
      return { selector, rejected: true };
    }
    assert.fail(`unadmitted account selector was accepted: ${JSON.stringify(selector)}`);
  };
  try {
    const capabilities = await call('get_adcp_capabilities', {});
    assert.equal(capabilities.media_buy.reporting_delivery.managed_delivery, true);
    assert.equal(capabilities.media_buy.reporting_delivery.reconciled_billing, mode === 'billing' ? true : undefined);
    const selectorNegatives = [
      await expectSelectorRejected({ account_id: accountId }),
      await expectSelectorRejected({ account_id: accountId, sandbox: false }),
    ];
    const prepared = await controller('prepare');
    assert.equal(prepared.success, true);
    let operations;
    if (mode === 'managed') {
      const suppressed = await controller('suppress_readiness');
      assert.equal(suppressed.simulated.readiness_notification_suppressed, true);
      const status = await call('get_reporting_status', {
        account, view: 'revision', reporting_revision_id: prepared.simulated.reporting_revision_id,
      });
      assert.equal(status.materializations[0].reporting_materialization_id,
        prepared.simulated.reporting_materialization_id);
      assert.equal(status.materializations[0].status, 'available');
      assert.ok(status.materializations[0].verification.verified_at);
      assert.ok(status.materializations[0].resource.expires_at);
      const retention = await controller('advance_within_retention');
      assert.equal(retention.simulated.resource_readable, true);
      const revoked = await controller('revoke_access');
      assert.equal(revoked.simulated.access_revoked, true);
      assert.equal(revoked.simulated.historical_metadata_retained, true);
      operations = {
        prepare: prepared.simulated,
        suppress_readiness: suppressed.simulated,
        status: status.materializations[0].status,
        retention: retention.simulated,
        revocation: revoked.simulated,
      };
    } else {
      assert.deepEqual(prepared.simulated.canonical_content_digest, CANONICAL_DIGEST);
      const baseline = await call('get_reporting_status', { account, view: 'periods' });
      assert.equal(baseline.periods[0].health, 'action_required');
      assert.equal(baseline.periods[0].issues[0].code, 'RECEIPT_REQUIRED');
      const common = {
        reporting_obligation_id: prepared.simulated.reporting_obligation_id,
        reporting_revision_id: prepared.simulated.reporting_revision_id,
        reporting_materialization_id: prepared.simulated.reporting_materialization_id,
        verification_profile: 'canonical_digest',
        observed_row_count: 2,
        observed_control_totals: [
          { name: 'impressions', value: '5', value_type: 'integer', unit: 'impressions' },
          { name: 'spend', value: '8.00', value_type: 'decimal', unit: 'USD' },
        ],
      };
      const rejected = await call('sync_reporting_receipts', {
        account, idempotency_key: '10000000-0000-4000-8000-000000000001',
        receipts: [{
          ...common, reporting_receipt_id: 'rr-billing-official-receipt-0001', status: 'rejected',
          rejection_codes: ['CANONICAL_DIGEST_MISMATCH'], observed_at: '2026-08-27T04:01:00Z',
        }],
      });
      assert.equal(rejected.results[0].result, 'recorded');
      const accepted = await call('sync_reporting_receipts', {
        account, idempotency_key: '10000000-0000-4000-8000-000000000002',
        receipts: [{
          ...common, reporting_receipt_id: 'rr-billing-official-repaired-0001',
          supersedes_reporting_receipt_id: 'rr-billing-official-receipt-0001', status: 'accepted',
          observed_canonical_content_digest: CANONICAL_DIGEST, observed_at: '2026-08-27T04:02:00Z',
        }],
      });
      assert.equal(accepted.results[0].result, 'recorded');
      const staleRevision = await call('sync_reporting_receipts', {
        account, idempotency_key: '10000000-0000-4000-8000-000000000003',
        receipts: [{
          ...common, reporting_receipt_id: 'rr-billing-official-stale-0001',
          supersedes_reporting_receipt_id: 'rr-billing-official-repaired-0001', status: 'accepted',
          observed_control_totals: [], observed_canonical_content_digest: CANONICAL_DIGEST,
          observed_at: '2026-08-27T04:03:00Z',
        }],
      });
      assert.equal(staleRevision.results[0].result, 'failed');
      const acceptedStatus = await call('get_reporting_status', { account, view: 'periods' });
      assert.equal(acceptedStatus.periods[0].reconciliation_status, 'accepted');
      assert.equal(acceptedStatus.periods[0].health, 'complete');
      const history = await call('get_reporting_status', {
        account, view: 'revision', reporting_revision_id: prepared.simulated.reporting_revision_id,
      });
      assert.equal(history.receipts.length, 2);

      const published = await controller('publish_adjustment');
      assert.equal(published.simulated.adjustments.length, 2);
      const reopened = await call('get_reporting_status', { account, view: 'periods' });
      assert.equal(reopened.periods[0].reconciliation_status, 'pending');
      assert.equal(reopened.periods[0].issues[0].code, 'ADJUSTMENT_RECEIPT_REQUIRED');
      const [acceptedAdjustment, disputedAdjustment] = published.simulated.adjustments;
      const adjustmentReceipts = await call('sync_reporting_receipts', {
        account, idempotency_key: '10000000-0000-4000-8000-000000000004',
        adjustment_receipts: [
          {
            reporting_receipt_id: 'rr-adjustment-accepted-receipt-01',
            reporting_adjustment_id: acceptedAdjustment.reporting_adjustment_id,
            adjusts_reporting_revision_id: prepared.simulated.reporting_revision_id,
            status: 'accepted', observed_adjustment_sha256: acceptedAdjustment.canonical_adjustment_sha256,
            observed_at: '2026-08-29T10:01:00Z',
          },
          {
            reporting_receipt_id: 'rr-adjustment-rejected-receipt-01',
            reporting_adjustment_id: disputedAdjustment.reporting_adjustment_id,
            adjusts_reporting_revision_id: prepared.simulated.reporting_revision_id,
            status: 'rejected',
            observed_adjustment_sha256: published.simulated.disputed_observed_adjustment_sha256,
            rejection_codes: ['CANONICAL_DIGEST_MISMATCH'], observed_at: '2026-08-29T10:01:01Z',
          },
        ],
      });
      assert.deepEqual(adjustmentReceipts.results.map(value => value.result), ['recorded', 'recorded']);
      const rejectedStatus = await call('get_reporting_status', { account, view: 'periods' });
      assert.equal(rejectedStatus.periods[0].reconciliation_status, 'rejected');
      assert.equal(rejectedStatus.periods[0].issues[0].code, 'ADJUSTMENT_RECEIPT_REJECTED');
      const repair = await call('sync_reporting_receipts', {
        account, idempotency_key: '10000000-0000-4000-8000-000000000005',
        adjustment_receipts: [{
          reporting_receipt_id: 'rr-adjustment-repaired-receipt-01',
          supersedes_reporting_receipt_id: 'rr-adjustment-rejected-receipt-01',
          reporting_adjustment_id: disputedAdjustment.reporting_adjustment_id,
          adjusts_reporting_revision_id: prepared.simulated.reporting_revision_id,
          status: 'accepted', observed_adjustment_sha256: disputedAdjustment.canonical_adjustment_sha256,
          observed_at: '2026-08-29T10:02:00Z',
        }],
      });
      assert.equal(repair.results[0].result, 'recorded');
      const staleAdjustment = await call('sync_reporting_receipts', {
        account, idempotency_key: '10000000-0000-4000-8000-000000000006',
        adjustment_receipts: [{
          reporting_receipt_id: 'rr-adjustment-stale-receipt-01',
          supersedes_reporting_receipt_id: 'rr-adjustment-repaired-receipt-01',
          reporting_adjustment_id: disputedAdjustment.reporting_adjustment_id,
          adjusts_reporting_revision_id: prepared.simulated.reporting_revision_id,
          status: 'accepted', observed_adjustment_sha256: disputedAdjustment.canonical_adjustment_sha256,
          observed_at: '2026-08-29T10:03:00Z',
        }],
      });
      assert.equal(staleAdjustment.results[0].result, 'failed');
      const restored = await call('get_reporting_status', { account, view: 'periods' });
      assert.equal(restored.periods[0].reconciliation_status, 'accepted');
      assert.equal(restored.periods[0].health, 'complete');
      assert.equal(restored.periods[0].adjustment_receipt_count, 3);
      const repaired = await call('get_reporting_status', {
        account, view: 'periods', changes_after: baseline.changes_checkpoint,
      });
      assert.equal(repaired.adjustments.length, 2);
      assert.equal(repaired.adjustment_receipts.length, 3);
      operations = {
        prepare: prepared.simulated,
        baseline: { health: baseline.periods[0].health, issue: baseline.periods[0].issues[0].code },
        revision_receipts: [rejected.results[0].result, accepted.results[0].result, staleRevision.results[0].result],
        revision_history: history.receipts.length,
        adjustments: published.simulated.adjustments,
        adjustment_receipts: adjustmentReceipts.results.map(value => value.adjustment_receipt.status),
        adjustment_repair: [repair.results[0].result, staleAdjustment.results[0].result],
        restored: { health: restored.periods[0].health, status: restored.periods[0].reconciliation_status },
        checkpoint_repair: { adjustments: repaired.adjustments.length, receipts: repaired.adjustment_receipts.length },
      };
    }
    const result = {
      kind: 'managed_reporting_controller_real_mcp_preflight',
      status: 'passed',
      blocking_acceptance: false,
      mode,
      package: { name: installed.name, version: installed.version },
      selector_negatives: selectorNegatives,
      operations,
      limitations: [
        'focused_real_mcp_preflight_not_cli_storyboard_execution',
        'real_postgresql_real_clock',
        ...(mode === 'managed' ? ['notification_suppression_not_signed_webhook_credit'] : []),
      ],
    };
    fs.writeFileSync(path.resolve(input.output), `${JSON.stringify(result, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } finally {
    probeAbort.abort('probe settled');
    try {
      await sdk.closeMCPConnections();
    } finally {
      probeAbort.dispose();
    }
  }
}

main().catch(error => {
  process.stderr.write(`${JSON.stringify({ kind: 'managed_reporting_http_probe_error', message: error.message, stack: error.stack })}\n`);
  process.exitCode = 1;
});
