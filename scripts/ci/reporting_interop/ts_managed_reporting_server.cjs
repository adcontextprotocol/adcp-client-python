#!/usr/bin/env node
'use strict';

/* Official MCP wrapper for the public PostgreSQL managed controller fixture. */

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { ACCOUNT_ID, CONSUMER_ID, createManagedReportingFixture } = require('./ts_managed_reporting_fixture.cjs');
const { assertFixtureCapabilityVersion, wireAdcpVersion } = require('./ts_adcp_version.cjs');
const { storyboardPrivateInputs } = require('./ts_private_inputs.cjs');

const STORYBOARD_OPERATOR = 'test.example';

function parseArgs(argv) {
  const values = { host: '127.0.0.1', probe: false };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--probe') { values.probe = true; continue; }
    if (!token.startsWith('--') || !argv[index + 1]) throw new Error(`invalid argument near ${token}`);
    values[token.slice(2)] = argv[index + 1];
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
  const sdk = req('@adcp/sdk');
  return {
    expectedIntegrity: required(args, 'expected-integrity'),
    installed,
    sdk,
    ledger: req('@adcp/sdk/reporting/ledger'),
    schemas: req('@adcp/sdk/schemas'),
    server: req('@adcp/sdk/server'),
    pg: req('pg'),
    z: req('zod').z,
    jcs: { canonicalize: sdk.canonicalize },
  };
}

function capabilities(fixture, adcpVersion) {
  const wire = wireAdcpVersion(adcpVersion);
  return {
    adcp_version: wire,
    adcp: { major_versions: [3], supported_versions: [wire], idempotency: { supported: false } },
    supported_protocols: ['media_buy'],
    experimental_features: ['media_buy.reporting_delivery'],
    compliance_testing: {
      scenarios: [fixture.mode === 'billing'
        ? 'reliable_reporting_reconciled_billing_probe'
        : 'reliable_reporting_managed_delivery_probe'],
    },
    account: {
      require_operator_auth: true,
      supported_billing: ['operator'],
      supported_account_currency_modes: ['fixed'],
      timezone: { mode: 'seller_fixed', fixed_timezone: 'UTC' },
      required_for_products: true,
      sandbox: true,
    },
    media_buy: { reporting_delivery: fixture.runtime.reportingDeliveryCapabilities },
  };
}

function mcpSuccess(api, value, summary) {
  return {
    content: [{ type: 'text', text: summary }],
    structuredContent: { status: 'completed', ...api.server.toStructuredContent(value) },
  };
}

function accountContext(request, extra) {
  const account = request?.account;
  const accountId = account?.account_id;
  const operator = account?.operator;
  const selectedFixture = accountId === ACCOUNT_ID || operator === STORYBOARD_OPERATOR;
  if (!selectedFixture || account?.sandbox !== true ||
      !extra?.authInfo?.extra?.accounts?.includes(ACCOUNT_ID)) {
    throw new Error('caller is not authorized for the managed reporting sandbox account');
  }
  return { account: { account_id: ACCOUNT_ID }, consumer_id: extra.authInfo.extra.consumer_id };
}

function validateAdvertisedCapabilities(api, fixture, adcpVersion,
  advertised = capabilities(fixture, adcpVersion)) {
  assertFixtureCapabilityVersion(advertised, adcpVersion);
  const validator = api.schemas.getCanonicalToolValidator(
    'get_adcp_capabilities', 'sync', { adcpVersion },
  );
  assert.equal(typeof validator, 'function', 'canonical capability validator is unavailable');
  assert.equal(Boolean(validator({ ...advertised, status: 'completed' })), true,
    JSON.stringify(validator.errors || []));
  return advertised;
}

function buildReadyRecord(api, { adcpVersion, authToken, mode, port, startupProof, url }) {
  return {
    auth_binding_sha256: crypto.createHash('sha256').update(authToken).digest('hex'),
    kind: 'managed_reporting_storyboard_server_ready',
    mode,
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

function createAgent(api, fixture, advertised) {
  return () => {
    const mcp = api.server.createTaskCapableServer('Managed reporting storyboard fixture', '1.0.0');
    mcp.registerTool('get_adcp_capabilities', {
      description: 'Return exact managed reporting fixture capabilities.',
      inputSchema: api.schemas.TOOL_INPUT_SHAPES.get_adcp_capabilities,
    }, async () => api.server.legacyCapabilitiesResponse(advertised));
    mcp.registerTool('comply_test_controller', {
      description: 'Drive declared managed reporting sandbox transitions.',
      inputSchema: api.server.TOOL_INPUT_SHAPE,
    }, async (input, extra) => {
      accountContext(input, extra);
      const result = await fixture.controller(input.scenario, input.params?.operation);
      return api.server.toMcpResponse({ status: 'completed', ...result, context: input.context });
    });
    mcp.registerTool('get_reporting_status', {
      description: 'Read authoritative managed reporting status.',
      inputSchema: api.schemas.TOOL_INPUT_SHAPES.get_reporting_status,
    }, async (request, extra) => {
      const context = accountContext(request, extra);
      const result = await fixture.runtime.getReportingStatus(request, context);
      return mcpSuccess(api, result, 'get_reporting_status completed');
    });
    mcp.registerTool('get_media_buy_delivery', {
      description: 'Read exact managed reporting revision rows.',
      inputSchema: api.schemas.TOOL_INPUT_SHAPES.get_media_buy_delivery,
    }, async (request, extra) => {
      const context = accountContext(request, extra);
      const result = await fixture.runtime.getMediaBuyDelivery(request, context);
      return mcpSuccess(api, result, 'get_media_buy_delivery completed');
    });
    if (fixture.runtime.syncReportingReceipts) {
      mcp.registerTool('sync_reporting_receipts', {
        description: 'Record authenticated revision and adjustment receipts.',
        inputSchema: api.schemas.TOOL_INPUT_SHAPES.sync_reporting_receipts,
      }, async (request, extra) => {
        const context = accountContext(request, extra);
        const result = await fixture.runtime.syncReportingReceipts(request, context);
        return mcpSuccess(api, result, 'sync_reporting_receipts completed');
      });
    }
    return mcp;
  };
}

function closeServer(server) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const done = error => {
      if (settled) return;
      settled = true;
      if (error) reject(error); else resolve();
    };
    try {
      const result = server.close(done);
      if (result && typeof result.then === 'function') result.then(() => done(), done);
    } catch (error) {
      done(error);
    }
  });
}

function createResourceOwner(api, pgUrl, schema) {
  let bootstrap;
  let pool;
  let schemaCreated = false;
  let server;
  let serverErrorListener;
  let removeShutdownHandlers;
  let cleanupPromise;

  return {
    async acquire() {
      bootstrap = new api.pg.Pool({ connectionString: pgUrl, max: 1 });
      await bootstrap.query(`CREATE SCHEMA "${schema}"`);
      schemaCreated = true;
      pool = new api.pg.Pool({
        connectionString: pgUrl,
        options: `-c search_path="${schema}"`,
        max: 4,
      });
      return pool;
    },
    ownServer(value, onError) {
      server = value;
      serverErrorListener = onError;
      if (!server || typeof server.on !== 'function' || typeof server.removeListener !== 'function') {
        throw new Error('managed reporting server does not expose owned error-listener methods');
      }
      server.on('error', serverErrorListener);
    },
    ownShutdownHandlers(value) { removeShutdownHandlers = value; },
    cleanup() {
      if (cleanupPromise) return cleanupPromise;
      cleanupPromise = (async () => {
        const failures = [];
        if (removeShutdownHandlers) {
          try { removeShutdownHandlers(); } catch (error) { failures.push(error); }
        }
        // Server closure is always attempted first.  Every other owned cleanup
        // is still attempted if closure fails, while that uncertainty remains a
        // blocking cleanup error rather than being hidden by later settlement.
        if (server) {
          try { await closeServer(server); } catch (error) { failures.push(error); }
          if (serverErrorListener) {
            try { server.removeListener('error', serverErrorListener); }
            catch (error) { failures.push(error); }
          }
        }
        if (pool) {
          try { await pool.end(); } catch (error) { failures.push(error); }
        }
        if (schemaCreated && bootstrap) {
          try { await bootstrap.query(`DROP SCHEMA IF EXISTS "${schema}" CASCADE`); }
          catch (error) { failures.push(error); }
        }
        if (bootstrap) {
          try { await bootstrap.end(); } catch (error) { failures.push(error); }
        }
        if (failures.length) {
          throw new AggregateError(failures, 'managed reporting resource cleanup failed');
        }
      })();
      return cleanupPromise;
    },
  };
}

async function failAfterCleanup(primary, owner) {
  try {
    await owner.cleanup();
  } catch (cleanup) {
    const combined = new AggregateError(
      [primary, cleanup],
      'managed reporting initialization failed and cleanup also failed',
    );
    combined.cause = primary;
    throw combined;
  }
  throw primary;
}

function registerShutdownHandlers(close) {
  const handlers = new Map();
  for (const signal of ['SIGINT', 'SIGTERM']) {
    const handler = () => close().then(() => process.exit(0), () => process.exit(1));
    handlers.set(signal, handler);
    process.once(signal, handler);
  }
  return () => {
    for (const [signal, handler] of handlers) process.removeListener(signal, handler);
  };
}

async function main(argv = process.argv.slice(2), dependencies = {}) {
  const args = parseArgs(argv);
  const api = (dependencies.loadPinned || loadPinned)(args);
  const mode = required(args, 'mode');
  const pgUrl = required(args, 'pg-url');
  const adcpVersion = required(args, 'adcp-version');
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
  const schema = `adcp_interop_server_${mode}_${process.pid}`;
  const owner = createResourceOwner(api, pgUrl, schema);
  try {
    const pool = await owner.acquire();
    const fixture = await (dependencies.createManagedReportingFixture || createManagedReportingFixture)(
      api, pool, { mode },
    );
    const advertised = validateAdvertisedCapabilities(api, fixture, adcpVersion);
    if (args.probe) {
      (dependencies.writeOutput || (value => process.stdout.write(value)))(`${JSON.stringify({
        kind: 'managed_reporting_storyboard_server_probe',
        status: 'passed',
        blocking_acceptance: false,
        mode,
        package: { name: api.installed.name, version: api.installed.version },
        reporting_delivery: advertised.media_buy.reporting_delivery,
        limitations: ['server_composition_probe_not_storyboard_execution', 'real_postgresql_real_clock'],
      })}\n`);
      await owner.cleanup();
      return;
    }

    const { authToken, port, readyFile, startupProof } = normalInputs;
    const authenticate = api.server.verifyApiKey({
      verify(token) {
        if (token !== authToken) return null;
        return { principal: 'reporting-managed-fixture-operator', extra: { accounts: [ACCOUNT_ID], consumer_id: CONSUMER_ID } };
      },
    });
    let resolveStartup;
    let rejectStartup;
    const startup = new Promise((resolve, reject) => {
      resolveStartup = resolve;
      rejectStartup = reject;
    });
    const server = api.server.serve(createAgent(api, fixture, advertised), {
      allowedHosts: [args.host, 'localhost', '127.0.0.1'],
      authenticate,
      path: '/mcp',
      port,
      onListening(url) {
        try {
          const ready = buildReadyRecord(api, {
            adcpVersion, authToken, mode, port, startupProof, url,
          });
          (dependencies.writeReadyFile || fs.writeFileSync)(
            readyFile, `${JSON.stringify(ready)}\n`, { flag: 'wx', mode: 0o600 },
          );
          (dependencies.writeOutput || (value => process.stdout.write(value)))(
            `${JSON.stringify(ready)}\n`,
          );
          resolveStartup(ready);
        } catch (error) {
          rejectStartup(error);
        }
      },
    });
    owner.ownServer(server, error => rejectStartup(error));
    const close = () => owner.cleanup();
    owner.ownShutdownHandlers(
      (dependencies.registerShutdownHandlers || registerShutdownHandlers)(close),
    );
    const ready = await startup;
    return { close, ready };
  } catch (error) {
    return failAfterCleanup(error, owner);
  }
}

if (require.main === module) {
  main().catch(error => {
    process.stderr.write(`${JSON.stringify({ kind: 'managed_reporting_server_error', message: error.message, stack: error.stack })}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  buildReadyRecord,
  capabilities,
  closeServer,
  createResourceOwner,
  main,
  validateAdvertisedCapabilities,
};
