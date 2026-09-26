#!/usr/bin/env node
'use strict';

/* Focused rc.45 public-handler probe for the controlled-clock controller. */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { createControlledController, DIGEST } = require('./ts_controlled_reporting_fixture.cjs');

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
  const value = args[name];
  if (!value) throw new Error(`missing --${name}`);
  return value;
}

function loadPinned(args) {
  const lockPath = path.resolve(required(args, 'package-lock'));
  const req = createRequire(path.join(path.dirname(lockPath), 'package.json'));
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const entry = lock.packages?.['node_modules/@adcp/sdk'];
  const installed = req('@adcp/sdk/package.json');
  if (installed.version !== required(args, 'expected-version') || entry?.version !== installed.version ||
      entry?.integrity !== required(args, 'expected-integrity')) {
    throw new Error('refusing to execute an unpinned SDK installation');
  }
  const api = {
    installed,
    sdk: req('@adcp/sdk'),
    ledger: req('@adcp/sdk/reporting/ledger'),
    schemas: req('@adcp/sdk/schemas'),
    server: req('@adcp/sdk/server'),
    source: req('@adcp/sdk/reporting/source'),
  };
  for (const [object, name] of [
    [api.ledger, 'createReportingStatusHandler'],
    [api.ledger, 'createSyncReportingStatusHandler'],
    [api.server, 'handleTestControllerRequest'],
  ]) assert.equal(typeof object[name], 'function', `missing public export ${name}`);
  return api;
}

async function controller(api, factory, accountId, operation, extra = {}) {
  const response = await api.server.handleTestControllerRequest(factory, {
    account: { account_id: accountId, sandbox: true },
    scenario: 'reporting_core_lifecycle_probe',
    params: { operation, ...extra },
  });
  const parsed = api.schemas.TOOL_RESPONSE_SCHEMAS.comply_test_controller.safeParse(response);
  assert.equal(parsed.success, true, JSON.stringify(parsed.error?.issues));
  assert.equal(response.success, true, JSON.stringify(response));
  return response;
}

function handlers(api, store, consumerId) {
  const context = { account: { id: store.accountId }, consumer: consumerId };
  return {
    context,
    delivery: api.ledger.createReportingDeliveryHandler(store),
    read: api.ledger.createReportingStatusHandler(store, {
      resolveConsumerId: value => value.consumer,
      consumerMismatchEscalation: {
        escalationSeconds: 1_800,
        operationsContact: { email: 'reporting-ops@example.test' },
      },
    }),
    sync: api.ledger.createSyncReportingStatusHandler(store, {
      resolveConsumerId: value => value.consumer,
      now: () => new Date(store.ledgerAsOf),
    }),
  };
}

function validate(api, tool, response) {
  const wire = { status: 'completed', ...api.server.toStructuredContent(response) };
  const parsed = api.schemas.TOOL_RESPONSE_SCHEMAS[tool].safeParse(wire);
  assert.equal(parsed.success, true, `${tool}: ${JSON.stringify(parsed.error?.issues)}`);
}

function statusInput(store, values) {
  const prepared = store.prepare();
  return {
    reporting_status_id: values.id,
    ...(values.supersedes ? { supersedes_reporting_status_id: values.supersedes } : {}),
    delivery_config_id: prepared.delivery_config_id,
    delivery_config_version: prepared.delivery_config_version,
    report_definition_id: prepared.resolved_configuration.report_definition_id,
    period: {
      start: prepared.period.start,
      end: prepared.period.end,
      source_timezone: prepared.period.sourceTimezone,
    },
    reporting_obligation_id: prepared.reporting_obligation_id,
    ...(values.revisionId ? { reporting_revision_id: values.revisionId } : {}),
    ...(values.digest ? { observed_revision_content_sha256: values.digest } : {}),
    consumer_status: values.consumerStatus,
    status_as_of: store.ledgerAsOf,
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const api = loadPinned(args);
  const fixture = createControlledController(api);
  const operations = {};

  const coreAccount = 'reporting_core_lab';
  operations.prepare = await controller(api, fixture.factory, coreAccount, 'prepare');
  const coreStore = fixture.getStore(coreAccount);
  const core = handlers(api, coreStore, 'consumer-core');

  await controller(api, fixture.factory, coreAccount, 'advance_time', { target_health: 'delayed' });
  const delayed = await core.read({ account: { account_id: coreAccount }, view: 'periods' }, core.context);
  validate(api, 'get_reporting_status', delayed);
  assert.equal(delayed.periods[0].health, 'delayed');
  operations.delayed = { ledger_as_of: delayed.ledger_as_of, health: delayed.periods[0].health };

  await controller(api, fixture.factory, coreAccount, 'advance_time', { target_health: 'action_required' });
  const overdue = await core.read({ account: { account_id: coreAccount }, view: 'periods' }, core.context);
  validate(api, 'get_reporting_status', overdue);
  assert.equal(overdue.periods[0].health, 'action_required');
  operations.action_required = { ledger_as_of: overdue.ledger_as_of, health: overdue.periods[0].health };

  const published = await controller(api, fixture.factory, coreAccount, 'publish_zero_row');
  const complete = await core.read({ account: { account_id: coreAccount }, view: 'periods' }, core.context);
  validate(api, 'get_reporting_status', complete);
  assert.equal(complete.periods[0].health, 'complete');
  operations.zero_row = {
    revision: published.simulated.reporting_revision_id,
    row_count: published.simulated.row_count,
    health: complete.periods[0].health,
  };

  const nonemptyAccount = 'reporting_core_nonempty_lab';
  await controller(api, fixture.factory, nonemptyAccount, 'prepare');
  const nonempty = await controller(api, fixture.factory, nonemptyAccount, 'publish_nonempty');
  assert.equal(nonempty.simulated.reporting_revision_id, 'reporting-revision.ecc62efa00946aa1e2788ad9');
  assert.equal(nonempty.simulated.revision_content_sha256, DIGEST);
  assert.equal(nonempty.simulated.row_count, 2);
  const nonemptyStore = fixture.getStore(nonemptyAccount);
  const nonemptyHandlers = handlers(api, nonemptyStore, 'consumer-core');
  const firstPage = await nonemptyHandlers.delivery({
    account: { account_id: nonemptyAccount },
    reporting_revision_id: nonempty.simulated.reporting_revision_id,
    pagination: { max_results: 1 },
  }, nonemptyHandlers.context);
  validate(api, 'get_media_buy_delivery', firstPage);
  assert.equal(firstPage.reporting_rows.length, 1);
  assert.equal(firstPage.pagination.has_more, true);
  assert.equal(firstPage.reporting_revision_binding.content_sha256, DIGEST);
  const secondPage = await nonemptyHandlers.delivery({
    account: { account_id: nonemptyAccount },
    reporting_revision_id: nonempty.simulated.reporting_revision_id,
    pagination: { max_results: 1, cursor: firstPage.pagination.cursor },
  }, nonemptyHandlers.context);
  validate(api, 'get_media_buy_delivery', secondPage);
  assert.equal(secondPage.reporting_rows.length, 1);
  assert.equal(secondPage.pagination.has_more, false);
  assert.equal(secondPage.reporting_revision_binding.content_sha256, DIGEST);
  operations.nonempty_exact_revision = {
    revision: nonempty.simulated.reporting_revision_id,
    digest: nonempty.simulated.revision_content_sha256,
    rows: [...firstPage.reporting_rows, ...secondPage.reporting_rows],
  };

  const revisionAccount = 'reporting_consumer_revision_lab';
  await controller(api, fixture.factory, revisionAccount, 'prepare');
  await controller(api, fixture.factory, revisionAccount, 'advance_time', { target_health: 'action_required' });
  const revisionStore = fixture.getStore(revisionAccount);
  const consumerId = 'https://buyer.example.test/adcp';
  const consumer = handlers(api, revisionStore, consumerId);
  const missingStatus = statusInput(revisionStore, {
    id: 'consumer-status.revision-missing.0001',
    consumerStatus: 'revision_missing',
  });
  const missing = await consumer.sync({
    account: { account_id: revisionAccount },
    idempotency_key: '00000000-0000-4000-8000-000000000001',
    statuses: [missingStatus],
  }, consumer.context);
  validate(api, 'sync_reporting_status', missing);
  assert.equal(missing.results[0].result, 'recorded');

  const revisionResult = await controller(api, fixture.factory, revisionAccount, 'publish_zero_row');
  const revisionId = revisionResult.simulated.reporting_revision_id;
  const mismatched = await consumer.read({ account: { account_id: revisionAccount }, view: 'periods' }, consumer.context);
  validate(api, 'get_reporting_status', mismatched);
  assert.equal(mismatched.periods[0].health, 'action_required');
  assert.ok(mismatched.periods[0].issues.some(issue => issue.code === 'CONSUMER_STATUS_MISMATCH'));

  const wrongBinding = await consumer.sync({
    account: { account_id: revisionAccount },
    idempotency_key: '00000000-0000-4000-8000-000000000002',
    statuses: [statusInput(revisionStore, {
      id: 'consumer-status.revision-wrong-binding.0001',
      consumerStatus: 'received',
      revisionId,
      digest: '0'.repeat(64),
      supersedes: missingStatus.reporting_status_id,
    })],
  }, consumer.context);
  validate(api, 'sync_reporting_status', wrongBinding);
  assert.equal(wrongBinding.results[0].result, 'failed');

  const receivedRequest = {
    account: { account_id: revisionAccount },
    idempotency_key: '00000000-0000-4000-8000-000000000003',
    statuses: [statusInput(revisionStore, {
      id: 'consumer-status.revision-received.0001',
      consumerStatus: 'received',
      revisionId,
      digest: DIGEST,
      supersedes: missingStatus.reporting_status_id,
    })],
  };
  const received = await consumer.sync(receivedRequest, consumer.context);
  validate(api, 'sync_reporting_status', received);
  assert.equal(received.results[0].result, 'recorded');
  const replay = await consumer.sync(receivedRequest, consumer.context);
  assert.deepEqual(replay, received, 'idempotent replay must return the retained ordered result');

  const resolved = await consumer.read({ account: { account_id: revisionAccount }, view: 'periods' }, consumer.context);
  validate(api, 'get_reporting_status', resolved);
  assert.equal(resolved.periods[0].health, 'complete');
  assert.equal(resolved.periods[0].consumer_status_count, 2);

  const other = handlers(api, revisionStore, 'opaque-other-consumer');
  const isolated = await other.read({ account: { account_id: revisionAccount }, view: 'periods' }, other.context);
  validate(api, 'get_reporting_status', isolated);
  assert.equal(isolated.periods[0].consumer_status_count, 0);
  operations.consumer_status = {
    missing: missing.results[0].result,
    wrong_binding: wrongBinding.results[0].result,
    received: received.results[0].result,
    retained_count: resolved.periods[0].consumer_status_count,
    other_consumer_count: isolated.periods[0].consumer_status_count,
  };

  const result = {
    kind: 'controlled_clock_reporting_controller_preflight',
    status: 'passed',
    blocking_acceptance: false,
    package: { name: api.installed.name, version: api.installed.version },
    operations,
    limitations: [
      'focused_public_handler_preflight_not_storyboard_execution',
      'controlled_clock_fixture_not_postgresql_durability_evidence',
      'probe_scheduler_dst_unavailable_in_stock_rc45_producer',
      'managed_delivery_and_reconciled_billing_not_exercised',
    ],
  };
  fs.writeFileSync(required(args, 'output'), `${JSON.stringify(result, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

main().catch(error => {
  process.stderr.write(`${JSON.stringify({ kind: 'controlled_clock_probe_error', message: String(error?.message || error), stack: String(error?.stack || '').split('\n').slice(0, 5) })}\n`);
  process.exitCode = 1;
});
