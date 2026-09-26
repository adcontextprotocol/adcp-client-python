#!/usr/bin/env node
'use strict';

/*
 * Installed-package MCP server for controlled-clock Core/consumer-status
 * storyboard operations. Durable PostgreSQL lanes use ts_core_server.cjs;
 * this server exists only for fixed protocol-clock narratives.
 */

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { createControlledController } = require('./ts_controlled_reporting_fixture.cjs');
const { assertFixtureCapabilityVersion, wireAdcpVersion } = require('./ts_adcp_version.cjs');
const { storyboardPrivateInputs } = require('./ts_private_inputs.cjs');

const LAB_ACCOUNTS = new Set([
  'reporting_core_lab',
  'reporting_core_nonempty_lab',
  'reporting_consumer_omission_lab',
  'reporting_consumer_revision_lab',
  'reporting_consumer_silence_lab',
  'reporting_consumer_content_lab',
  'reporting_consumer_mismatch_lab',
]);
const STORYBOARD_OPERATOR = 'test.example';
const CONTROLLED_STORYBOARDS = new Set([
  'reporting_core',
  'reporting_consumer_status',
  'reporting_core_declaration',
]);

function parseArgs(argv) {
  const values = { host: '127.0.0.1', probe: false };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--probe') {
      values.probe = true;
      continue;
    }
    if (!token.startsWith('--')) throw new Error(`unexpected positional argument: ${token}`);
    const value = argv[index + 1];
    if (!value || value.startsWith('--')) throw new Error(`missing value for ${token}`);
    values[token.slice(2)] = value;
    index += 1;
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
  if (installed.version !== required(args, 'expected-version') || entry?.version !== installed.version ||
      entry?.integrity !== required(args, 'expected-integrity')) {
    throw new Error('refusing to execute an unpinned SDK installation');
  }
  const api = {
    expectedIntegrity: required(args, 'expected-integrity'),
    installed,
    sdk: req('@adcp/sdk'),
    ledger: req('@adcp/sdk/reporting/ledger'),
    schemas: req('@adcp/sdk/schemas'),
    server: req('@adcp/sdk/server'),
    source: req('@adcp/sdk/reporting/source'),
    z: req('zod').z,
  };
  for (const [object, name] of [
    [api.ledger, 'createReportingDeliveryHandler'],
    [api.ledger, 'createReportingStatusHandler'],
    [api.ledger, 'createSyncReportingStatusHandler'],
    [api.server, 'createTaskCapableServer'],
    [api.server, 'handleTestControllerRequest'],
    [api.server, 'legacyCapabilitiesResponse'],
    [api.server, 'serve'],
    [api.server, 'toMcpResponse'],
    [api.server, 'verifyApiKey'],
  ]) assert.equal(typeof object[name], 'function', `missing public export ${name}`);
  assert.ok(api.server.TOOL_INPUT_SHAPE, 'missing public controller input shape');
  return api;
}

function capabilities(adcpVersion) {
  const wire = wireAdcpVersion(adcpVersion);
  return {
    adcp_version: wire,
    adcp: { major_versions: [3], supported_versions: [wire], idempotency: { supported: false } },
    supported_protocols: ['media_buy'],
    experimental_features: ['media_buy.reporting_delivery'],
    compliance_testing: { scenarios: ['reporting_core_lifecycle_probe'] },
    account: {
      require_operator_auth: true,
      supported_billing: ['operator'],
      supported_account_currency_modes: ['fixed'],
      timezone: { mode: 'seller_fixed', fixed_timezone: 'UTC' },
      required_for_products: true,
      sandbox: true,
    },
    media_buy: {
      reporting_delivery: {
        supported: true,
        reliable_reporting_version: '1.0',
        managed_delivery: false,
        reconciled_billing: false,
        configuration_task: 'sync_accounts',
        status_task: 'get_reporting_status',
        consumer_status_task: 'sync_reporting_status',
        consumer_mismatch_escalation_seconds: 1_800,
        operations_contact: { email: 'reporting-ops@example.test' },
        revision_content_task: 'get_media_buy_delivery',
        offerings: [{
          offering_id: 'controlled-core-hourly',
          feed_purpose: 'analytics',
          report_definition_id: 'delivery-daily-v1',
          report_definition_uri: 'https://example.com/report-definition.json',
          report_definition_sha256: 'a'.repeat(64),
          reporting_profile: {
            id: 'controlled-core-hourly',
            version: '1',
            schema_uri: 'https://example.com/reporting-schema.json',
            schema_sha256: 'b'.repeat(64),
            schema_dialect: 'https://json-schema.org/draft/2020-12/schema',
            schema_ref_policy: 'local_fragment_only',
            grain: 'media_buy',
            primary_keys: ['media_buy_id'],
          },
          schedule: {
            period_duration: 'PT1H',
            alignment: 'source_timezone',
            period_timezone_policy: 'fixed',
            period_timezone: 'UTC',
            delivery_sla: 'PT1H',
          },
          supported_finality: ['snapshot'],
          reconciliation_mode: 'delivery_only',
        }],
        automated_recovery_window_seconds: 7_200,
        status_retention_days: 30,
      },
    },
  };
}

function mcpSuccess(api, data, summary) {
  return {
    content: [{ type: 'text', text: summary }],
    structuredContent: { status: 'completed', ...api.server.toStructuredContent(data) },
  };
}

function createAccountRouter(storyboardId) {
  if (!CONTROLLED_STORYBOARDS.has(storyboardId)) {
    throw new Error(`unsupported controlled storyboard: ${storyboardId}`);
  }
  const consumerAccounts = [
    'reporting_consumer_omission_lab',
    'reporting_consumer_revision_lab',
    'reporting_consumer_silence_lab',
    'reporting_consumer_content_lab',
  ];
  let activeAccount = storyboardId === 'reporting_core' ? 'reporting_core_lab' : undefined;
  let corePrepareCount = 0;
  let consumerPrepareCount = 0;
  return {
    resolve(request, { controller = false } = {}) {
      const exact = request?.account?.account_id;
      if (typeof exact === 'string' && LAB_ACCOUNTS.has(exact)) {
        activeAccount = exact;
        return exact;
      }
      if (request?.account?.operator !== STORYBOARD_OPERATOR || request?.account?.sandbox !== true) {
        throw new Error('request does not select the trusted storyboard sandbox operator');
      }
      if (controller && request?.params?.operation === 'prepare') {
        if (storyboardId === 'reporting_consumer_status') {
          if (consumerPrepareCount >= consumerAccounts.length) {
            throw new Error('consumer-status controller prepare sequence exceeded its declared accounts');
          }
          activeAccount = consumerAccounts[consumerPrepareCount++];
        } else if (storyboardId === 'reporting_core') {
          activeAccount = corePrepareCount++ === 0
            ? 'reporting_core_lab'
            : 'reporting_core_nonempty_lab';
        }
      }
      if (!activeAccount) {
        throw new Error('storyboard account is unavailable before its prepare operation');
      }
      return activeAccount;
    },
  };
}

function accountIdFor(request, extra, router, options) {
  const allowed = extra?.authInfo?.extra?.accounts;
  const accountId = router.resolve(request, options);
  if (!Array.isArray(allowed) || !allowed.includes(accountId) || !LAB_ACCOUNTS.has(accountId)) {
    throw new Error('caller is not authorized for this sandbox reporting account');
  }
  return accountId;
}

function validateAdvertisedCapabilities(api, adcpVersion, advertised = capabilities(adcpVersion)) {
  assertFixtureCapabilityVersion(advertised, adcpVersion);
  const validator = api.schemas.getCanonicalToolValidator(
    'get_adcp_capabilities', 'sync', { adcpVersion },
  );
  assert.equal(typeof validator, 'function', 'canonical capability validator is unavailable');
  assert.equal(Boolean(validator({ ...advertised, status: 'completed' })), true,
    JSON.stringify(validator.errors || []));
  return advertised;
}

function buildReadyRecord(api, { adcpVersion, authToken, port, startupProof, url }) {
  return {
    auth_binding_sha256: crypto.createHash('sha256').update(authToken).digest('hex'),
    kind: 'controlled_reporting_storyboard_server_ready',
    node: { executable: fs.realpathSync(process.execPath), version: process.version },
    seller_package: {
      adcp_version: adcpVersion,
      integrity: api.expectedIntegrity,
      name: api.installed.name,
      version: api.installed.version,
    },
    startup_proof_sha256: crypto.createHash('sha256').update(startupProof).digest('hex'),
    pid: process.pid,
    port,
    url,
  };
}

function createAgent(api, fixture, advertised, storyboardId) {
  const router = createAccountRouter(storyboardId);
  return () => {
    const mcp = api.server.createTaskCapableServer('Controlled reporting storyboard fixture', '1.0.0');
    mcp.registerTool('get_adcp_capabilities', {
      description: 'Return controlled reporting fixture capabilities.',
      inputSchema: api.schemas.TOOL_INPUT_SHAPES.get_adcp_capabilities,
    }, async () => api.server.legacyCapabilitiesResponse(advertised));
    mcp.registerTool('comply_test_controller', {
      description: 'Drive fixed-clock reporting state in declared sandbox accounts.',
      inputSchema: api.server.TOOL_INPUT_SHAPE,
    }, async (input, extra) => {
      const accountId = accountIdFor(input, extra, router, { controller: true });
      const response = await api.server.handleTestControllerRequest(fixture.factory, {
        ...input,
        account: { account_id: accountId, sandbox: true },
      });
      return api.server.toMcpResponse(response);
    });
    for (const tool of ['get_reporting_status', 'get_media_buy_delivery', 'sync_reporting_status']) {
      mcp.registerTool(tool, {
        description: `Controlled reporting fixture ${tool}.`,
        inputSchema: api.schemas.TOOL_INPUT_SHAPES[tool],
      }, async (request, extra) => {
        const accountId = accountIdFor(request, extra, router);
        const store = fixture.getStore(accountId);
        const consumer = extra.authInfo.extra.consumer_id;
        const context = { account: { account_id: accountId }, consumer, authInfo: extra.authInfo };
        const options = { resolveConsumerId: value => value.consumer };
        const result = tool === 'get_reporting_status'
          ? await api.ledger.createReportingStatusHandler(store, {
              ...options,
              consumerMismatchEscalation: {
                escalationSeconds: 1_800,
                operationsContact: { email: 'reporting-ops@example.test' },
              },
            })(request, context)
          : tool === 'get_media_buy_delivery'
            ? await api.ledger.createReportingDeliveryHandler(store)(request, context)
            : await api.ledger.createSyncReportingStatusHandler(store, {
                ...options,
                now: () => new Date(store.ledgerAsOf),
              })(request, context);
        return mcpSuccess(api, result, `${tool} completed`);
      });
    }
    return mcp;
  };
}

async function main(argv = process.argv.slice(2), dependencies = {}) {
  const args = parseArgs(argv);
  const api = (dependencies.loadPinned || loadPinned)(args);
  const adcpVersion = required(args, 'adcp-version');
  const storyboardId = args['storyboard-id'] || 'reporting_core';
  if (!CONTROLLED_STORYBOARDS.has(storyboardId)) {
    throw new Error(`invalid --storyboard-id: ${storyboardId}`);
  }
  const advertised = validateAdvertisedCapabilities(api, adcpVersion);
  let normalInputs;
  if (!args.probe) {
    const port = Number(required(args, 'port'));
    if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) throw new Error('invalid --port');
    normalInputs = {
      port,
      ...storyboardPrivateInputs(dependencies.environment || process.env),
      readyFile: path.resolve(required(args, 'ready-file')),
    };
  }
  const fixture = (dependencies.createControlledController || createControlledController)(api);
  if (args.probe) {
    (dependencies.writeOutput || (value => process.stdout.write(value)))(`${JSON.stringify({
      kind: 'controlled_reporting_storyboard_server_probe',
      status: 'passed',
      blocking_acceptance: false,
      package: { name: api.installed.name, version: api.installed.version },
      scenarios: fixture.factory.scenarios,
      operations: ['prepare', 'advance_time', 'publish_zero_row', 'omit_obligation',
        'publish_nonempty', 'restate_after_received', 'advance_past_status_deadline', 'advance_past_escalation'],
      limitations: ['controlled_clock_fixture_not_postgresql', 'probe_scheduler_dst_unavailable'],
    })}\n`);
    return;
  }
  const { authToken, port, readyFile, startupProof } = normalInputs;
  const authenticate = api.server.verifyApiKey({
    verify(token) {
      if (token !== authToken) return null;
      return {
        principal: 'reporting-controlled-fixture-operator',
        extra: { accounts: [...LAB_ACCOUNTS], consumer_id: 'https://buyer.example.test/adcp' },
      };
    },
  });
  const server = api.server.serve(createAgent(api, fixture, advertised, storyboardId), {
    allowedHosts: [args.host, 'localhost', '127.0.0.1'],
    authenticate,
    path: '/mcp',
    port,
    onListening(url) {
      const ready = buildReadyRecord(api, { adcpVersion, authToken, port, startupProof, url });
      fs.writeFileSync(readyFile, `${JSON.stringify(ready)}\n`, { flag: 'wx', mode: 0o600 });
      process.stdout.write(`${JSON.stringify(ready)}\n`);
    },
  });
  let closing = false;
  const close = () => new Promise(resolve => {
    if (closing) return resolve();
    closing = true;
    server.close(resolve);
  });
  process.once('SIGINT', () => close().then(() => process.exit(0)));
  process.once('SIGTERM', () => close().then(() => process.exit(0)));
}

if (require.main === module) {
  main().catch(error => {
    process.stderr.write(`${JSON.stringify({ kind: 'controlled_reporting_server_error', message: String(error?.message || error) })}\n`);
    process.exitCode = 1;
  });
}

module.exports = { buildReadyRecord, capabilities, main, validateAdvertisedCapabilities };
