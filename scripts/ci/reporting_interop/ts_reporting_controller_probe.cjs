#!/usr/bin/env node
'use strict';

/*
 * Focused preflight for the rc.45 Reliable Reporting Core controller adapter.
 *
 * This is deliberately not a storyboard runner. It drives the installed
 * comply_test_controller dispatcher and the installed public producer/store
 * interfaces, then records which literal controller operations are ready for
 * CLI execution. It never rewrites status output or moves the PostgreSQL clock.
 */

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { createRequire } = require('node:module');

const PERIOD_START = '2026-08-01T00:00:00.000Z';
const PERIOD_END = '2026-08-01T01:00:00.000Z';
const EXPECTED_AT = '2026-08-01T02:00:00.000Z';
const RECOVERY_DEADLINE = '2026-08-01T04:00:00.000Z';
const WORKER_NOW = '2026-08-01T04:05:00.000Z';
const ACCOUNTS = ['reporting_core_lab', 'reporting_core_nonempty_lab'];

function parseArgs(argv) {
  const out = {};
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith('--') || value === undefined) throw new Error(`invalid argument near ${String(flag)}`);
    out[flag.slice(2)] = value;
  }
  return out;
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
  if (installed.version !== required(args, 'expected-version') ||
      entry?.version !== installed.version || entry?.integrity !== required(args, 'expected-integrity')) {
    throw new Error('refusing to execute an unpinned SDK installation');
  }
  return {
    installed,
    ledger: req('@adcp/sdk/reporting/ledger'),
    source: req('@adcp/sdk/reporting/source'),
    server: req('@adcp/sdk/server'),
    schemas: req('@adcp/sdk/schemas'),
    pg: req('pg'),
  };
}

function contract() {
  return {
    report_definition_id: 'reporting-core-hourly-v1',
    reportDefinitionUri: 'https://contracts.example/reporting-core-hourly-v1.json',
    reportDefinitionSha256: 'a'.repeat(64),
    reportingProfile: 'reporting-core-hourly-v1',
    schemaVersion: '1',
    schemaUri: 'https://contracts.example/reporting-core-hourly-rows-v1.json',
    schemaSha256: 'b'.repeat(64),
    schemaDialect: 'https://json-schema.org/draft/2020-12/schema',
    schemaRefPolicy: 'local_fragment_only',
    mappingId: 'reporting-core-hourly-v1',
    mappingVersion: '1',
    mappingSha256: 'c'.repeat(64),
  };
}

function offering(source) {
  const value = structuredClone(source.redactedReportingSourceOfferingV1);
  value.offeringId = 'reporting-core-hourly';
  value.publicationNamespace = 'reporting-source:interop-core-controller';
  value.applicability.productIds = ['reporting-core-product'];
  value.contract = contract();
  value.grain = 'source_hour';
  value.windowing.minimumWindow = 'PT1H';
  value.windowing.maximumWindow = 'PT1H';
  value.cadence.alignment = 'source_timezone';
  value.sourceExecution.maximumWindowDaysPerRequest = 1;
  return source.ReportingSourceOfferingV1Schema.parse(value);
}

function configuration(accountId, selectedOffering, source) {
  const request = source.redactedReportingSourceRequestV1();
  return {
    account: { account_id: accountId },
    sourceScope: { connection: accountId, region: 'test' },
    delivery_config_id: `delivery-${accountId}`,
    delivery_config_version: 1,
    offeringId: selectedOffering.offeringId,
    report_definition_id: selectedOffering.contract.report_definition_id,
    feedPurpose: 'analytics',
    requiredFinality: 'snapshot',
    requestedMetrics: ['impressions'],
    requestedDimensions: ['media_buy_id'],
    constituents: [{
      constituentId: `constituent-${accountId}`,
      constituentKind: 'media_buy',
      productId: 'reporting-core-product',
      mediaBuyId: `media-buy-${accountId}`,
      productBinding: {
        owner: 'caller',
        bindingId: `binding-${accountId}`,
        bindingVersion: 1,
        bindingSha256: 'd'.repeat(64),
        bindingKind: 'media_buy_product',
        productId: 'reporting-core-product',
        mediaBuyId: `media-buy-${accountId}`,
      },
    }],
    mediaBuyIds: [`media-buy-${accountId}`],
    sourceTimezone: 'UTC',
    schedule: {
      anchor: PERIOD_START,
      periodMilliseconds: 3_600_000,
      deliverySlaMilliseconds: 3_600_000,
      recoveryWindowMilliseconds: 7_200_000,
      periodDuration: 'PT1H',
      alignment: 'source_timezone',
      periodTimezone: 'UTC',
      deliverySlaDuration: 'PT1H',
    },
    sourceSettings: structuredClone(request.sourceSettings),
    contract: structuredClone(selectedOffering.contract),
  };
}

function exactRows() {
  return [
    {
      period_start: PERIOD_START,
      period_end: PERIOD_END,
      impressions: 2,
      dimensions: { media_buy_id: 'media-buy-core-001', package_id: 'package-core-001', country: 'US' },
      metrics: { impressions: 2, clicks: 1 },
    },
    {
      period_start: PERIOD_START,
      period_end: PERIOD_END,
      impressions: 3,
      dimensions: { media_buy_id: 'media-buy-core-002', package_id: 'package-core-002', country: 'CA' },
      metrics: { impressions: 3, clicks: 0 },
    },
  ];
}

function fixedInstallStore(store) {
  return new Proxy(store, {
    get(target, property) {
      if (property === 'putConfiguration') {
        return value => target.putConfiguration({ ...value, installedAt: PERIOD_START });
      }
      const selected = Reflect.get(target, property, target);
      return typeof selected === 'function' ? selected.bind(target) : selected;
    },
  });
}

async function createCoreController(api, pool) {
  await pool.query(api.ledger.REPORTING_LEDGER_MIGRATION);
  await pool.query(api.ledger.REPORTING_LEDGER_FINALITY_WRITER_FENCE_MIGRATION);
  const store = new api.ledger.PostgresReportingLedgerStore(pool, { acknowledgeIsolatedDatabase: true });
  const selectedOffering = offering(api.source);
  const sourceState = new Map();
  const sourceDiagnostic = {};
  const objects = new Map();
  const executor = {
    capabilities: api.source.reportingSourceCapabilitiesV1([selectedOffering]),
    async execute(request) {
      const accountId = request.account.account_id;
      const state = sourceState.get(accountId);
      if (!state) throw new Error(`source was not armed for ${accountId}`);
      const rows = structuredClone(state.rows);
      const bytes = Buffer.from(rows.map(row => JSON.stringify(row)).join('\n') + (rows.length ? '\n' : ''));
      const sha256 = createHash('sha256').update(bytes).digest('hex');
      const executionDigest = createHash('sha256')
        .update(request.identity.sourceExecutionKey)
        .digest('hex');
      const objectRef = `controller-${executionDigest}`;
      const objectGeneration = `sha256-${sha256}`;
      const dataThrough = request.period.end;
      const metrics = new Map(selectedOffering.metrics.map(metric => [metric.name, metric]));
      const metricAvailability = request.coverage.constituents.flatMap(constituent =>
        request.requestedMetrics.map(name => {
          const metric = metrics.get(name);
          if (!metric) throw new Error(`requested metric is absent from offering: ${name}`);
          return {
            constituentId: constituent.constituentId,
            metric: name,
            semanticContractId: metric.semanticContractId,
            semanticContractVersion: metric.semanticContractVersion,
            semanticContractSha256: metric.semanticContractSha256,
            status: rows.length === 0 ? 'explicit_zero' : 'present',
            dataThrough,
          };
        }));
      const coverageConstituents = request.coverage.constituents.map(constituent => ({
        ...structuredClone(constituent),
        status: rows.length === 0 ? 'explicit_zero' : 'present',
        dataThrough,
      }));
      const object = {
        ordinal: 0,
        objectRef,
        objectGeneration,
        mediaType: 'application/x-ndjson',
        compression: 'none',
        sha256,
        byteCount: bytes.byteLength,
        rowCount: rows.length,
      };
      const impressions = rows.reduce((sum, row) => sum + Number(row.impressions ?? 0), 0);
      let built;
      try {
        built = api.source.buildReportingSourceManifestV1({
          level: 'basic',
          request,
          stagedCommitRef: `manifest-${executionDigest}`,
          objects: [object],
          completeness: { terminal: true, rowsComplete: true, requestedGroupsComplete: true },
          controlTotals: [{ name: 'impressions', value: String(impressions), valueType: 'integer', unit: 'impressions' }],
          metricAvailability,
          coverage: { status: 'full', constituents: coverageConstituents },
          observedAt: dataThrough,
          dataThrough,
          finalityEvidence: { owner: 'adapter', basis: 'provisional_observation', observedAt: dataThrough },
          acquiredAt: dataThrough,
          explicitZero: rows.length === 0,
          ...(rows.length ? { eventTimeRange: { start: request.period.start, end: dataThrough } } : {}),
          warnings: [],
        });
      } catch (error) {
        sourceDiagnostic.error = error;
        throw error;
      }
      objects.set(objectRef, { bytes, generation: objectGeneration, request: structuredClone(request) });
      return {
        ok: true,
        response: api.source.completedReportingSourceResponseV1({ request, manifest: built.reference }),
        manifestBytes: built.manifestBytes,
      };
    },
    async read(input) {
      const object = objects.get(input.objectRef);
      if (!object || object.generation !== input.objectGeneration ||
          object.request.account.account_id !== input.account.account_id ||
          object.request.reporting_obligation_id !== input.reporting_obligation_id ||
          object.bytes.byteLength > input.maxBytes) {
        throw new Error('staged reporting object does not match its public read binding');
      }
      return Uint8Array.from(object.bytes);
    },
  };
  const producer = api.ledger.createReportingProducer({
    store: fixedInstallStore(store),
    source: executor,
    offerings: [selectedOffering],
    contact: { name: 'Reporting interoperability controller' },
  });
  const installed = new Set();
  const diagnostic = {};

  async function prepare(accountId) {
    if (!installed.has(accountId)) {
      await producer.installConfiguration(configuration(accountId, selectedOffering, api.source));
      installed.add(accountId);
    }
    const obligations = await producer.planObligations('2026-08-01T01:05:00.000Z', {
      account_id: accountId,
      maxObligations: 1,
    });
    const obligation = obligations[0] ?? (await store.listObligations(accountId))[0];
    if (!obligation) throw new Error(`producer did not plan the first obligation for ${accountId}`);
    if (obligation.period.start !== PERIOD_START || obligation.period.end !== PERIOD_END ||
        obligation.expectedAt !== EXPECTED_AT || obligation.recoveryDeadlineAt !== RECOVERY_DEADLINE) {
      throw new Error('producer planned unexpected Core period boundaries');
    }
    return obligation;
  }

  async function publish(accountId, rows) {
    const obligation = await prepare(accountId);
    sourceState.set(accountId, { rows: structuredClone(rows) });
    const worker = await producer.runWorker({
      account_id: accountId,
      maxIterations: 2,
      now: () => new Date(WORKER_NOW),
    });
    if (worker.revisionsCommitted !== 1 || worker.failed !== 0) {
      const issues = await store.listIssues(obligation.reporting_obligation_id);
      const sourceError = sourceDiagnostic.error
        ? String(sourceDiagnostic.error?.stack || sourceDiagnostic.error)
        : 'none';
      throw new Error(`public producer did not commit one revision: ${JSON.stringify(worker)}; issues=${JSON.stringify(issues)}; source=${sourceError}`);
    }
    const revisions = await store.listRevisions(obligation.reporting_obligation_id);
    const revision = revisions.at(-1);
    if (!revision) throw new Error('committed revision was not readable through the public store');
    return { obligation, revision, worker };
  }

  const factory = {
    scenarios: ['reporting_core_lifecycle_probe'],
    createStore(input) {
      const accountId = input?.account?.account_id;
      if (!ACCOUNTS.includes(accountId) || input?.account?.sandbox !== true) {
        throw new api.server.TestControllerError('FORBIDDEN', 'Controller requires a persisted sandbox account');
      }
      return {
        async reportingCoreLifecycleProbe(params) {
          try {
            const operation = params.operation;
            if (operation === 'prepare') {
              const obligation = await prepare(accountId);
              return { success: true, simulated: {
                delivery_config_id: obligation.delivery_config_id,
                expected_at: obligation.expectedAt,
                period: structuredClone(obligation.period),
                recovery_deadline: obligation.recoveryDeadlineAt,
                reporting_obligation_id: obligation.reporting_obligation_id,
              } };
            }
            if (operation === 'publish_zero_row' || operation === 'publish_nonempty') {
              const rows = operation === 'publish_zero_row' ? [] : exactRows();
              const { obligation, revision } = await publish(accountId, rows);
              return { success: true, simulated: {
                reporting_obligation_id: obligation.reporting_obligation_id,
              reporting_revision_id: revision.reporting_revision_id,
              revision_content_sha256: revision.binding.sha256,
              row_count: revision.binding.rowCount,
              } };
            }
            throw new api.server.TestControllerError('INVALID_PARAMS', `Unsupported Core operation: ${String(operation)}`);
          } catch (error) {
            diagnostic.error = error;
            throw error;
          }
        },
      };
    },
  };
  return { diagnostic, factory, store };
}

async function invoke(api, factory, diagnostic, accountId, operation) {
  const request = {
    account: { account_id: accountId, sandbox: true },
    scenario: 'reporting_core_lifecycle_probe',
    params: { operation },
    context: { correlation_id: `controller-preflight--${accountId}--${operation}` },
  };
  const response = await api.server.handleTestControllerRequest(factory, request);
  const parsed = api.schemas.TOOL_RESPONSE_SCHEMAS.comply_test_controller.safeParse(response);
  if (!parsed.success) throw new Error(`controller response failed installed schema: ${JSON.stringify(parsed.error.issues)}`);
  if (response.success !== true) {
    const detail = diagnostic.error ? `; cause=${String(diagnostic.error?.stack || diagnostic.error)}` : '';
    throw new Error(`controller operation failed: ${JSON.stringify(response)}${detail}`);
  }
  return response;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const api = loadPinned(args);
  const pool = new api.pg.Pool({ connectionString: required(args, 'database-url'), max: 1 });
  try {
    const { diagnostic, factory, store } = await createCoreController(api, pool);
    const waiting = await invoke(api, factory, diagnostic, ACCOUNTS[0], 'prepare');
    const zero = await invoke(api, factory, diagnostic, ACCOUNTS[0], 'publish_zero_row');
    const nonemptyPrepared = await invoke(api, factory, diagnostic, ACCOUNTS[1], 'prepare');
    const nonempty = await invoke(api, factory, diagnostic, ACCOUNTS[1], 'publish_nonempty');
    const zeroRevision = await store.getRevision(zero.simulated.reporting_revision_id, ACCOUNTS[0]);
    const nonemptyRevision = await store.getRevision(nonempty.simulated.reporting_revision_id, ACCOUNTS[1]);
    const result = {
      kind: 'reporting_storyboard_controller_preflight',
      status: 'passed',
      blocking_acceptance: false,
      package: { name: api.installed.name, version: api.installed.version },
      operations: {
        prepare_waiting_obligation: { controller: waiting, public_revision_count: 0 },
        publish_zero_row: { controller: zero, stored_row_count: zeroRevision?.binding?.rowCount },
        prepare_nonempty_core: { controller: nonemptyPrepared, public_revision_count: 0 },
        publish_nonempty_core: {
          controller: nonempty,
          stored_row_count: nonemptyRevision?.binding?.rowCount,
          literal_storyboard_identity_match:
            nonempty.simulated.reporting_revision_id === 'reporting-revision.ecc62efa00946aa1e2788ad9' &&
            nonempty.simulated.revision_content_sha256 === '5199a776b3a99915f084e16c921b2e501fcedd949017236d51a2303c5c2f5cd1',
        },
      },
      limitations: [
        'focused_public_controller_preflight_not_storyboard_execution',
        'postgres_status_clock_not_injectable',
        'consumer_status_and_optional_tiers_not_exercised',
      ],
    };
    fs.writeFileSync(required(args, 'output'), `${JSON.stringify(result, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } finally {
    await pool.end();
  }
}

main().catch(error => {
  process.stderr.write(`${JSON.stringify({ kind: 'controller_preflight_error', message: String(error?.message || error) })}\n`);
  process.exitCode = 1;
});
