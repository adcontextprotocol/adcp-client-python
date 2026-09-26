#!/usr/bin/env node
'use strict';

/*
 * Installed-package TypeScript Reliable Reporting Core seller.
 *
 * This is intentionally a Core-only composition.  It uses the public ledger,
 * source, schema, and MCP server exports from the package selected by
 * --package-lock.  It does not import package internals, use the mock server,
 * or opt into ReliableReportingService / Managed Delivery.
 */

const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const crypto = require('node:crypto');
const { assertFixtureCapabilityVersion, wireAdcpVersion } = require('./ts_adcp_version.cjs');
const { corePrivateInputs } = require('./ts_private_inputs.cjs');

const FIXED_NOW = '2026-09-03T00:00:00.000Z';
const PERIOD_START = '2026-08-01T00:00:00.000Z';
const PERIOD_END = '2026-08-02T00:00:00.000Z';
const DAY_MILLISECONDS = 86_400_000;
const ACCOUNTS = ['interop-account-a', 'interop-account-b'];
const PRINCIPAL_BINDINGS = [
  { account_id: ACCOUNTS[0], consumer_id: 'https://buyer-a.example.test/adcp' },
  { account_id: ACCOUNTS[1], consumer_id: 'opaque-consumer-b' },
];

function parseArgs(argv) {
  const values = { host: '127.0.0.1', package: '@adcp/sdk', probe: false };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--probe') {
      values.probe = true;
      continue;
    }
    if (!token.startsWith('--')) throw new Error(`unexpected positional argument: ${token}`);
    const key = token.slice(2);
    const value = argv[index + 1];
    if (!value || value.startsWith('--')) throw new Error(`missing value for --${key}`);
    if (Object.hasOwn(values, key) && !['host', 'package'].includes(key)) {
      throw new Error(`duplicate argument: --${key}`);
    }
    values[key] = value;
    index += 1;
  }
  return values;
}

function requireArg(args, name) {
  if (!args[name]) throw new Error(`missing required argument: --${name}`);
  return args[name];
}

function positivePort(value) {
  const port = Number(value);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
    throw new Error('--port must be an integer from 1 through 65535');
  }
  return port;
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
  }
  return value;
}

function emit(stream, value) {
  stream.write(`${JSON.stringify(stable(value))}\n`);
}

function packageContext(args) {
  const packageName = args.package;
  const expectedVersion = requireArg(args, 'expected-version');
  const expectedIntegrity = requireArg(args, 'expected-integrity');
  const lockPath = path.resolve(requireArg(args, 'package-lock'));
  const packageRequire = createRequire(path.join(path.dirname(lockPath), 'package.json'));
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const lockEntry = lock.packages?.[`node_modules/${packageName}`];
  if (!lockEntry) throw new Error(`package lock does not contain ${packageName}`);

  const packageFile = packageRequire.resolve(`${packageName}/package.json`);
  const packageRoot = fs.realpathSync(path.dirname(packageFile));
  const installed = packageRequire(packageFile);
  if (installed.name !== packageName || installed.version !== expectedVersion ||
      lockEntry.version !== expectedVersion || lockEntry.integrity !== expectedIntegrity) {
    throw new Error('refusing to execute an unpinned SDK installation');
  }
  for (const subpath of [
    packageName,
    `${packageName}/reporting/ledger`,
    `${packageName}/reporting/source`,
    `${packageName}/schemas`,
    `${packageName}/server`,
  ]) {
    const resolved = fs.realpathSync(packageRequire.resolve(subpath));
    if (!resolved.startsWith(`${packageRoot}${path.sep}`)) {
      throw new Error(`public package entry resolved outside the installed package root: ${subpath}`);
    }
  }
  return {
    expectedIntegrity,
    expectedVersion,
    installed,
    lockPath,
    packageName,
    packageRequire,
    packageRoot,
  };
}

function loadPublicSurface(context, adcpVersion) {
  const sdk = context.packageRequire(context.packageName);
  const ledger = context.packageRequire(`${context.packageName}/reporting/ledger`);
  const source = context.packageRequire(`${context.packageName}/reporting/source`);
  const schemas = context.packageRequire(`${context.packageName}/schemas`);
  const server = context.packageRequire(`${context.packageName}/server`);
  const pg = context.packageRequire('pg');
  const required = [
    [ledger, 'PostgresReportingLedgerStore'],
    [ledger, 'createReportingProducer'],
    [ledger, 'createReportingDeliveryHandler'],
    [ledger, 'createReportingStatusHandler'],
    [ledger, 'createSyncReportingStatusHandler'],
    [source, 'createInlineReportingSourceExecutor'],
    [schemas, 'getCanonicalToolValidator'],
    [server, 'createTaskCapableServer'],
    [server, 'legacyCapabilitiesResponse'],
    [server, 'serve'],
    [server, 'toStructuredContent'],
    [server, 'verifyApiKey'],
    [pg, 'Pool'],
  ];
  for (const [container, name] of required) {
    if (typeof container[name] !== 'function') {
      throw new Error(`required public installed-package export is unavailable: ${name}`);
    }
  }
  for (const name of ['REPORTING_LEDGER_MIGRATION', 'REPORTING_LEDGER_FINALITY_WRITER_FENCE_MIGRATION']) {
    if (typeof ledger[name] !== 'string' || ledger[name].length === 0) {
      throw new Error(`required public installed-package export is unavailable: ${name}`);
    }
  }
  for (const tool of [
    'get_adcp_capabilities',
    'get_media_buy_delivery',
    'get_reporting_status',
    'sync_reporting_status',
  ]) {
    if (!schemas.TOOL_INPUT_SHAPES?.[tool]) {
      throw new Error(`required public canonical input shape is unavailable: ${tool}`);
    }
  }
  if (sdk.ADCP_VERSION !== adcpVersion) {
    throw new Error(`installed SDK protocol ${String(sdk.ADCP_VERSION)} does not match --adcp-version`);
  }
  return { ledger, pg, schemas, sdk, server, source };
}

function reportingContract() {
  return {
    report_definition_id: 'interop-core-v1',
    reportDefinitionUri: 'https://contracts.example/interop-core-v1.json',
    reportDefinitionSha256: 'a'.repeat(64),
    reportingProfile: 'interop-core-v1',
    schemaVersion: '1',
    schemaUri: 'https://contracts.example/interop-core-rows-v1.json',
    schemaSha256: 'b'.repeat(64),
    schemaDialect: 'https://json-schema.org/draft/2020-12/schema',
    schemaRefPolicy: 'local_fragment_only',
    mappingId: 'interop-core-v1',
    mappingVersion: '1',
    mappingSha256: 'c'.repeat(64),
  };
}

function sourceOffering(source) {
  const offering = structuredClone(source.redactedReportingSourceOfferingV1);
  offering.offeringId = 'interop-core-daily';
  offering.publicationNamespace = 'reporting-source:interop-core';
  offering.applicability.productIds = ['interop-product'];
  offering.contract = reportingContract();
  offering.dimensions = [
    { name: 'media_buy_id', support: 'exact' },
    { name: 'vendor', support: 'exact' },
  ];
  return offering;
}

function constituent(accountId) {
  const suffix = accountId.at(-1);
  const mediaBuyId = `media-buy-${suffix}`;
  return {
    constituentId: `interop-constituent-${suffix}`,
    constituentKind: 'media_buy',
    productId: 'interop-product',
    mediaBuyId,
    productBinding: {
      owner: 'caller',
      bindingId: `interop-product-binding-${suffix}`,
      bindingVersion: 1,
      bindingSha256: 'c'.repeat(64),
      productId: 'interop-product',
      bindingKind: 'media_buy_product',
      mediaBuyId,
    },
  };
}

function configurationInput(accountId, offering, source) {
  const suffix = accountId.at(-1);
  const template = source.redactedReportingSourceRequestV1();
  return {
    account: { account_id: accountId },
    sourceScope: { connection: `interop-${suffix}`, region: 'test' },
    delivery_config_id: 'shared-daily-key',
    delivery_config_version: 1,
    offeringId: offering.offeringId,
    report_definition_id: 'interop-core-v1',
    feedPurpose: 'analytics',
    requiredFinality: 'snapshot',
    requestedMetrics: ['impressions'],
    requestedDimensions: ['media_buy_id'],
    constituents: [constituent(accountId)],
    mediaBuyIds: [`media-buy-${suffix}`],
    sourceTimezone: 'UTC',
    schedule: {
      anchor: PERIOD_START,
      periodMilliseconds: DAY_MILLISECONDS,
      deliverySlaMilliseconds: 3_600_000,
      recoveryWindowMilliseconds: DAY_MILLISECONDS,
      periodDuration: 'P1D',
      alignment: 'utc',
      deliverySlaDuration: 'PT1H',
    },
    sourceSettings: structuredClone(template.sourceSettings),
    contract: structuredClone(offering.contract),
  };
}

function capabilities(adcpVersion) {
  const wireVersion = wireAdcpVersion(adcpVersion);
  return {
    adcp_version: wireVersion,
    adcp: {
      major_versions: [3],
      supported_versions: [wireVersion],
      idempotency: { supported: false },
    },
    supported_protocols: ['media_buy'],
    experimental_features: ['media_buy.reporting_delivery'],
    account: {
      require_operator_auth: true,
      supported_billing: ['operator'],
      supported_account_currency_modes: ['fixed'],
      timezone: { mode: 'seller_fixed', fixed_timezone: 'UTC' },
      required_for_products: true,
      sandbox: false,
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
        revision_content_task: 'get_media_buy_delivery',
        offerings: [{
          offering_id: 'interop-core-daily',
          feed_purpose: 'analytics',
          report_definition_id: 'interop-core-v1',
          report_definition_uri: 'https://contracts.example/interop-core-v1.json',
          report_definition_sha256: 'a'.repeat(64),
          reporting_profile: {
            id: 'interop-core-v1',
            version: '1',
            schema_uri: 'https://contracts.example/interop-core-rows-v1.json',
            schema_sha256: 'b'.repeat(64),
            schema_dialect: 'https://json-schema.org/draft/2020-12/schema',
            schema_ref_policy: 'local_fragment_only',
            grain: 'media_buy',
            primary_keys: ['media_buy_id'],
          },
          schedule: { period_duration: 'P1D', alignment: 'utc', delivery_sla: 'PT1H' },
          supported_finality: ['snapshot'],
          reconciliation_mode: 'delivery_only',
        }],
        automated_recovery_window_seconds: 86_400,
        status_retention_days: 30,
      },
    },
  };
}

function validateAdvertisedCapabilities(publicApi, adcpVersion,
  advertised = capabilities(adcpVersion)) {
  assertFixtureCapabilityVersion(advertised, adcpVersion);
  const validator = publicApi.schemas.getCanonicalToolValidator(
    'get_adcp_capabilities', 'sync', { adcpVersion },
  );
  if (typeof validator !== 'function' || !validator({ ...advertised, status: 'completed' })) {
    throw new Error(`Core capability document failed the installed canonical validator: ${JSON.stringify(validator?.errors || [])}`);
  }
  return advertised;
}

function authenticationBindings(privateInputs) {
  // Entropy is supplied by the trusted launcher (secrets.token_urlsafe(32)).
  // ts_private_inputs validates minimum length/distinctness before database
  // allocation.  A readiness digest is an identity binding, not added entropy.
  const tokens = [privateInputs.tokenA, privateInputs.tokenB];
  return new Map(tokens.map((token, index) => [token, PRINCIPAL_BINDINGS[index]]));
}

function buildReadyRecord(context, {
  adcpVersion, authBindings, port, sellerRole, startupProof, url,
}) {
  return {
    accounts: ACCOUNTS,
    auth_binding_sha256: Object.fromEntries(
      [...authBindings].map(([token, binding]) => [
        binding.account_id,
        crypto.createHash('sha256').update(token).digest('hex'),
      ]),
    ),
    kind: 'reporting_interop_ts_core_server_ready',
    node: { executable: fs.realpathSync(process.execPath), version: process.version },
    pid: process.pid,
    port,
    process_start_token: processStartToken(),
    seller_package: {
      integrity: context.expectedIntegrity,
      language: 'typescript',
      name: context.packageName,
      package_role: sellerRole,
      protocol: adcpVersion,
      version: context.installed.version,
      wire_adcp_version: wireAdcpVersion(adcpVersion),
    },
    startup_proof_sha256: crypto.createHash('sha256').update(startupProof).digest('hex'),
    status: 'ready',
    url,
  };
}

function processStartToken() {
  if (process.platform !== 'linux') {
    throw new Error('Core seller owned-start proof currently requires Linux /proc');
  }
  const stat = fs.readFileSync(`/proc/${process.pid}/stat`, 'utf8');
  const commandEnd = stat.lastIndexOf(')');
  const fields = commandEnd < 0 ? [] : stat.slice(commandEnd + 2).trim().split(/\s+/);
  const startToken = fields[19]; // proc(5) field 22; fields starts at field 3.
  if (!/^\d+$/.test(startToken || '')) {
    throw new Error('Core seller could not establish its Linux process start identity');
  }
  return startToken;
}

function mcpSuccess(server, data, summary) {
  return {
    content: [{ type: 'text', text: summary }],
    structuredContent: server.toStructuredContent(data),
  };
}

function fixedInstallStore(store, diagnostic) {
  return new Proxy(store, {
    get(target, property) {
      if (property === 'putConfiguration') {
        return configuration => target.putConfiguration({ ...configuration, installedAt: PERIOD_START });
      }
      const value = Reflect.get(target, property, target);
      if (typeof value !== 'function') return value;
      return async (...args) => {
        try {
          return await value.apply(target, args);
        } catch (error) {
          diagnostic.error = error;
          diagnostic.operation = String(property);
          throw error;
        }
      };
    },
  });
}

async function prepareDatabase(publicApi, databaseUrl) {
  const pool = new publicApi.pg.Pool({
    connectionString: databaseUrl,
    max: 4,
    application_name: 'adcp-reporting-interop-ts-core',
  });
  try {
    await pool.query(publicApi.ledger.REPORTING_LEDGER_MIGRATION);
    await pool.query(publicApi.ledger.REPORTING_LEDGER_FINALITY_WRITER_FENCE_MIGRATION);
    const occupied = await pool.query(`
      SELECT
        (SELECT count(*)::int FROM adcp_reporting_configurations) AS configurations,
        (SELECT count(*)::int FROM adcp_reporting_obligations) AS obligations,
        (SELECT count(*)::int FROM adcp_reporting_revisions) AS revisions
    `);
    if (Object.values(occupied.rows[0]).some(value => Number(value) !== 0)) {
      throw new Error('reporting interoperability server requires a fresh isolated PostgreSQL database');
    }

    const store = new publicApi.ledger.PostgresReportingLedgerStore(pool, {
      acknowledgeIsolatedDatabase: true,
    });
    const offering = sourceOffering(publicApi.source);
    const sourceExecutor = publicApi.source.createInlineReportingSourceExecutor(input => ({
      reporting_period: { start: input.start_date, end: input.end_date },
      currency: 'USD',
      reporting_rows: [{
        media_buy_id: input.media_buy_ids[0],
        impressions: 5,
        vendor: { fraction: '1.25', enabled: true, ordinal: 1 },
      }],
      data_through: input.end_date,
      observed_at: input.end_date,
    }), offering);
    let sourceFailure;
    const executeSource = sourceExecutor.execute.bind(sourceExecutor);
    sourceExecutor.execute = async (...input) => {
      const result = await executeSource(...input);
      if (!result.ok) sourceFailure = result.error;
      return result;
    };
    const storeDiagnostic = {};
    const producer = publicApi.ledger.createReportingProducer({
      store: fixedInstallStore(store, storeDiagnostic),
      source: sourceExecutor,
      offerings: [offering],
      contact: { name: 'Reporting interoperability operations' },
    });

    for (const accountId of ACCOUNTS) {
      await producer.installConfiguration(configurationInput(accountId, offering, publicApi.source));
      const planned = await producer.planObligations(PERIOD_END, { account_id: accountId, maxObligations: 2 });
      if (planned.length !== 1 || planned[0].period.start !== PERIOD_START || planned[0].period.end !== PERIOD_END) {
        throw new Error(`failed to seed the deterministic reporting obligation for ${accountId}`);
      }
      const worker = await producer.runWorker({
        account_id: accountId,
        maxIterations: 2,
        now: () => new Date(FIXED_NOW),
      });
      if (worker.revisionsCommitted !== 1 || worker.failed !== 0 || worker.reconcilesDeferred !== 0) {
        const storeMessage = storeDiagnostic.error
          ? `${storeDiagnostic.operation}: ${String(storeDiagnostic.error?.message || storeDiagnostic.error)}`
          : undefined;
        const sourceMessage = sourceFailure?.message || sourceFailure?.safeMessage || storeMessage;
        const detail = sourceFailure?.code
          ? `: source ${sourceFailure.code}${sourceMessage ? ` (${sourceMessage})` : ''}`
          : '';
        throw new Error(`failed to seed the deterministic reporting revision for ${accountId}${detail}`);
      }
    }
    return { pool, store };
  } catch (error) {
    await pool.end().catch(() => {});
    throw error;
  }
}

function createAgent(publicApi, store, advertised) {
  const resolveConsumerId = context => {
    const consumerId = context?.authInfo?.extra?.consumer_id;
    if (typeof consumerId !== 'string' || consumerId.length === 0) {
      throw new TypeError('reporting status requires an authenticated consumer principal');
    }
    return consumerId;
  };
  const status = publicApi.ledger.createReportingStatusHandler(store, {
    resolveConsumerId,
  });
  const delivery = publicApi.ledger.createReportingDeliveryHandler(store);
  const syncStatus = publicApi.ledger.createSyncReportingStatusHandler(store, {
    resolveConsumerId,
  });
  return () => {
    const mcp = publicApi.server.createTaskCapableServer(
      'Reporting interoperability TypeScript Core seller',
      '1.0.0',
    );
    mcp.registerTool('get_adcp_capabilities', {
      description: 'Return the installed seller capability document.',
      inputSchema: publicApi.schemas.TOOL_INPUT_SHAPES.get_adcp_capabilities,
    }, async () => publicApi.server.legacyCapabilitiesResponse(advertised));
    mcp.registerTool('get_reporting_status', {
      description: 'Read the durable Reliable Reporting Core ledger.',
      inputSchema: publicApi.schemas.TOOL_INPUT_SHAPES.get_reporting_status,
    }, async (request, extra) => {
      const accountId = request?.account?.account_id;
      const authorizedAccount = extra?.authInfo?.extra?.account_id;
      if (typeof accountId !== 'string' || accountId !== authorizedAccount) {
        throw new Error('caller is not authorized for this reporting account');
      }
      const result = await status(request, {
        account: { account_id: accountId },
        authInfo: extra.authInfo,
      });
      return mcpSuccess(publicApi.server, result, 'Reporting status retrieved');
    });
    mcp.registerTool('get_media_buy_delivery', {
      description: 'Read immutable rows for an exact reporting revision.',
      inputSchema: publicApi.schemas.TOOL_INPUT_SHAPES.get_media_buy_delivery,
    }, async (request, extra) => {
      const accountId = request?.account?.account_id;
      const authorizedAccount = extra?.authInfo?.extra?.account_id;
      if (typeof accountId !== 'string' || accountId !== authorizedAccount) {
        throw new Error('caller is not authorized for this reporting account');
      }
      const result = await delivery(request, {
        account: { account_id: accountId },
        authInfo: extra.authInfo,
      });
      return mcpSuccess(publicApi.server, result, 'Reporting revision content retrieved');
    });
    mcp.registerTool('sync_reporting_status', {
      description: 'Persist authenticated consumer reporting status.',
      inputSchema: publicApi.schemas.TOOL_INPUT_SHAPES.sync_reporting_status,
    }, async (request, extra) => {
      const accountId = request?.account?.account_id;
      const authorizedAccount = extra?.authInfo?.extra?.account_id;
      if (typeof accountId !== 'string' || accountId !== authorizedAccount) {
        throw new Error('caller is not authorized for this reporting account');
      }
      const result = await syncStatus(request, {
        account: { account_id: accountId },
        authInfo: extra.authInfo,
      });
      return mcpSuccess(publicApi.server, result, 'Reporting consumer status synchronized');
    });
    return mcp;
  };
}

async function run(argv = process.argv.slice(2), dependencies = {}) {
  const args = parseArgs(argv);
  const context = (dependencies.packageContext || packageContext)(args);
  const adcpVersion = requireArg(args, 'adcp-version');
  const publicApi = (dependencies.loadPublicSurface || loadPublicSurface)(context, adcpVersion);
  const advertised = validateAdvertisedCapabilities(publicApi, adcpVersion);

  if (args.probe) {
    (dependencies.emit || emit)(dependencies.stdout || process.stdout, {
      adcp_version: adcpVersion,
      kind: 'reporting_interop_ts_core_server_probe',
      package: {
        integrity: context.expectedIntegrity,
        name: context.packageName,
        version: context.installed.version,
      },
      public_composition: {
        postgres_ledger: true,
        producer: true,
        status_handler: true,
        consumer_status_handler: true,
        mcp_http_server: true,
        bearer_auth: true,
        reliable_reporting_service: false,
      },
      status: 'passed',
    });
    return;
  }

  const port = positivePort(requireArg(args, 'port'));
  const privateInputs = corePrivateInputs(dependencies.environment || process.env);
  const authBindings = authenticationBindings(privateInputs);
  const startupProof = privateInputs.startupProof;
  const readyFile = path.resolve(requireArg(args, 'ready-file'));
  const sellerRole = requireArg(args, 'seller-role');
  if (!new Set(['candidate', 'historical_candidate', 'previous', 'lead']).has(sellerRole)) {
    throw new Error('Core seller role is not a declared immutable package role');
  }
  const databaseUrl = args['database-url'] || process.env.DATABASE_URL;
  if (!databaseUrl) throw new Error('--database-url or DATABASE_URL is required');
  const { pool, store } = await prepareDatabase(publicApi, databaseUrl);
  const authenticate = publicApi.server.verifyApiKey({
    verify(token) {
      const binding = authBindings.get(token);
      if (!binding) return null;
      return {
        principal: `principal-${binding.account_id.at(-1)}`,
        extra: structuredClone(binding),
      };
    },
  });
  const httpServer = publicApi.server.serve(createAgent(publicApi, store, advertised), {
    allowedHosts: [args.host, 'localhost', '127.0.0.1'],
    authenticate,
    path: '/mcp',
    port,
    readinessCheck: async () => { await pool.query('SELECT 1'); },
    onListening(url) {
      const ready = buildReadyRecord(context, {
        adcpVersion, authBindings, port, sellerRole, startupProof, url,
      });
      fs.writeFileSync(readyFile, `${JSON.stringify(stable(ready))}\n`, {
        flag: 'wx', mode: 0o600,
      });
      emit(process.stdout, ready);
    },
  });

  let closing = false;
  async function close() {
    if (closing) return;
    closing = true;
    await new Promise(resolve => httpServer.close(resolve));
    await pool.end();
  }
  process.once('SIGINT', () => { close().then(() => process.exit(0), () => process.exit(1)); });
  process.once('SIGTERM', () => { close().then(() => process.exit(0), () => process.exit(1)); });
}

if (require.main === module) {
  run().catch(error => {
    emit(process.stderr, {
      error: { message: String(error?.message || error), name: error?.name || 'Error' },
      kind: 'reporting_interop_ts_core_server_error',
      status: 'failed',
    });
    process.exitCode = 1;
  });
}

module.exports = {
  authenticationBindings,
  buildReadyRecord,
  capabilities,
  corePrivateInputs,
  run,
  validateAdvertisedCapabilities,
};
