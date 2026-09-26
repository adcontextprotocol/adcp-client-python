#!/usr/bin/env node
'use strict';

/* Real-MCP focused probe for the controlled reporting controller route. */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { callWithProbeAbort, createProbeAbort } = require('./ts_probe_abort.cjs');

function args(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!argv[i]?.startsWith('--') || argv[i + 1] === undefined) throw new Error('invalid arguments');
    out[argv[i].slice(2)] = argv[i + 1];
  }
  return out;
}

async function main() {
  const input = args(process.argv.slice(2));
  const lockPath = path.resolve(input['package-lock']);
  const req = createRequire(path.join(path.dirname(lockPath), 'package.json'));
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const installed = req('@adcp/sdk/package.json');
  const entry = lock.packages?.['node_modules/@adcp/sdk'];
  if (installed.version !== input['expected-version'] || entry?.integrity !== input['expected-integrity']) {
    throw new Error('refusing to execute an unpinned SDK installation');
  }
  const sdk = req('@adcp/sdk');
  const agent = {
    agent_uri: input.endpoint,
    auth_token: process.env.REPORTING_CONTROLLED_TOKEN,
    id: 'controlled-reporting-fixture',
    name: 'controlled-reporting-fixture',
    protocol: 'mcp',
  };
  if (!agent.auth_token) throw new Error('REPORTING_CONTROLLED_TOKEN is required');
  const probeAbort = createProbeAbort('controlled reporting HTTP probe');
  const call = async (toolName, params) => {
    const raw = await callWithProbeAbort(probeAbort, signal => sdk.ProtocolClient.callTool(
      agent, toolName, params, {
        adcpVersion: input['adcp-version'],
        signal,
        wireAdcpVersion: input['adcp-version'],
        transport: { requestTimeoutMs: 10_000, maxResponseBytes: 4 * 1024 * 1024 },
      },
    ));
    return sdk.unwrapProtocolResponse(raw, toolName, 'mcp', { responseAdcpVersion: input['adcp-version'] });
  };
  const account = { account_id: 'reporting_core_lab' };
  try {
    const prepared = await call('comply_test_controller', {
      account: { ...account, sandbox: true },
      scenario: 'reporting_core_lifecycle_probe',
      params: { operation: 'prepare' },
    });
    assert.equal(prepared.success, true);
    await call('comply_test_controller', {
      account: { ...account, sandbox: true },
      scenario: 'reporting_core_lifecycle_probe',
      params: { operation: 'advance_time', target_health: 'delayed' },
    });
    const delayed = await call('get_reporting_status', { account, view: 'periods' });
    assert.equal(delayed.periods[0].health, 'delayed');
    const published = await call('comply_test_controller', {
      account: { ...account, sandbox: true },
      scenario: 'reporting_core_lifecycle_probe',
      params: { operation: 'publish_zero_row' },
    });
    assert.equal(published.simulated.row_count, 0);
    const complete = await call('get_reporting_status', { account, view: 'periods' });
    assert.equal(complete.periods[0].health, 'complete');

    const nonemptyAccount = { account_id: 'reporting_core_nonempty_lab' };
    await call('comply_test_controller', {
      account: { ...nonemptyAccount, sandbox: true },
      scenario: 'reporting_core_lifecycle_probe',
      params: { operation: 'prepare' },
    });
    const nonempty = await call('comply_test_controller', {
      account: { ...nonemptyAccount, sandbox: true },
      scenario: 'reporting_core_lifecycle_probe',
      params: { operation: 'publish_nonempty' },
    });
    assert.equal(nonempty.simulated.reporting_revision_id, 'reporting-revision.ecc62efa00946aa1e2788ad9');
    assert.equal(nonempty.simulated.revision_content_sha256, '5199a776b3a99915f084e16c921b2e501fcedd949017236d51a2303c5c2f5cd1');
    const pageOne = await call('get_media_buy_delivery', {
      account: nonemptyAccount,
      reporting_revision_id: nonempty.simulated.reporting_revision_id,
      pagination: { max_results: 1 },
    });
    const pageTwo = await call('get_media_buy_delivery', {
      account: nonemptyAccount,
      reporting_revision_id: nonempty.simulated.reporting_revision_id,
      pagination: { max_results: 1, cursor: pageOne.pagination.cursor },
    });
    assert.equal(pageOne.reporting_rows.length, 1);
    assert.equal(pageOne.pagination.has_more, true);
    assert.equal(pageTwo.reporting_rows.length, 1);
    assert.equal(pageTwo.pagination.has_more, false);

    const consumerAccount = { account_id: 'reporting_consumer_revision_lab' };
    const consumerPrepared = await call('comply_test_controller', {
      account: { ...consumerAccount, sandbox: true },
      scenario: 'reporting_core_lifecycle_probe',
      params: { operation: 'prepare' },
    });
    await call('comply_test_controller', {
      account: { ...consumerAccount, sandbox: true },
      scenario: 'reporting_core_lifecycle_probe',
      params: { operation: 'advance_time', target_health: 'action_required' },
    });
    const preparedStatus = consumerPrepared.simulated;
    const missing = {
      reporting_status_id: 'consumer-status.http-revision-missing.0001',
      delivery_config_id: preparedStatus.delivery_config_id,
      delivery_config_version: preparedStatus.delivery_config_version,
      report_definition_id: preparedStatus.resolved_configuration.report_definition_id,
      period: {
        start: preparedStatus.period.start,
        end: preparedStatus.period.end,
        source_timezone: preparedStatus.period.sourceTimezone,
      },
      reporting_obligation_id: preparedStatus.reporting_obligation_id,
      consumer_status: 'revision_missing',
      status_as_of: '2026-08-01T04:05:00.000Z',
    };
    const missingResult = await call('sync_reporting_status', {
      account: consumerAccount,
      idempotency_key: '00000000-0000-4000-8000-000000000011',
      statuses: [missing],
    });
    assert.equal(missingResult.results[0].result, 'recorded');
    const recovered = await call('comply_test_controller', {
      account: { ...consumerAccount, sandbox: true },
      scenario: 'reporting_core_lifecycle_probe',
      params: { operation: 'publish_zero_row' },
    });
    const receivedResult = await call('sync_reporting_status', {
      account: consumerAccount,
      idempotency_key: '00000000-0000-4000-8000-000000000012',
      statuses: [{
        ...missing,
        reporting_status_id: 'consumer-status.http-received.0001',
        supersedes_reporting_status_id: missing.reporting_status_id,
        reporting_revision_id: recovered.simulated.reporting_revision_id,
        observed_revision_content_sha256: recovered.simulated.revision_content_sha256,
        consumer_status: 'received',
      }],
    });
    assert.equal(receivedResult.results[0].result, 'recorded');
    const consumerComplete = await call('get_reporting_status', { account: consumerAccount, view: 'periods' });
    assert.equal(consumerComplete.periods[0].health, 'complete');
    assert.equal(consumerComplete.periods[0].consumer_status_count, 2);
    const result = {
      kind: 'controlled_reporting_controller_real_mcp_preflight',
      status: 'passed',
      blocking_acceptance: false,
      package: { name: installed.name, version: installed.version },
      operations: {
        prepare: prepared.simulated.reporting_obligation_id,
        delayed: delayed.periods[0].health,
        publish_zero_row: published.simulated.reporting_revision_id,
        complete: complete.periods[0].health,
        publish_nonempty: nonempty.simulated.reporting_revision_id,
        exact_pages: [pageOne.reporting_rows.length, pageTwo.reporting_rows.length],
        consumer_status: [missingResult.results[0].result, receivedResult.results[0].result],
        consumer_complete: consumerComplete.periods[0].health,
      },
      limitations: ['focused_real_mcp_preflight_not_cli_storyboard_execution', 'controlled_clock_fixture_not_postgresql'],
    };
    fs.writeFileSync(input.output, `${JSON.stringify(result, null, 2)}\n`);
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
  process.stderr.write(`${JSON.stringify({ kind: 'controlled_reporting_http_probe_error', message: String(error?.message || error) })}\n`);
  process.exitCode = 1;
});
