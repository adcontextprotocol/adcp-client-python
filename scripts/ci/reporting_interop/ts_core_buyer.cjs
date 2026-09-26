#!/usr/bin/env node
'use strict';

/* Public Core-only TypeScript buyer probe.
 *
 * Historical rc.42 deliberately failed this probe at the public-export gate.
 * The exact selected candidate must exercise reconcileReportingCoreV1 over
 * real MCP HTTP; older supplemental pins cannot promote it.
 */

const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { callWithProbeAbort, createProbeAbort } = require('./ts_probe_abort.cjs');

function parseArgs(argv) {
  const values = { 'expect-missing-core-api': false };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--expect-missing-core-api') {
      values['expect-missing-core-api'] = true;
      continue;
    }
    if (!token.startsWith('--')) throw new Error(`unexpected positional argument: ${token}`);
    const key = token.slice(2);
    const value = argv[index + 1];
    if (!value || value.startsWith('--')) throw new Error(`missing value for --${key}`);
    if (Object.hasOwn(values, key)) throw new Error(`duplicate argument: --${key}`);
    values[key] = value;
    index += 1;
  }
  return values;
}

function requireArg(args, name) {
  if (!args[name]) throw new Error(`missing required argument: --${name}`);
  return args[name];
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
  }
  return value;
}

function validateEndpoint(endpoint) {
  const parsed = new URL(endpoint);
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('endpoint must use HTTP or HTTPS');
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('endpoint must not contain credentials, a query, or a fragment');
  }
  return parsed.toString();
}

function packageContext(args) {
  const packageName = args.package || '@adcp/sdk';
  const expectedVersion = requireArg(args, 'expected-version');
  const expectedIntegrity = requireArg(args, 'expected-integrity');
  const lockPath = path.resolve(requireArg(args, 'package-lock'));
  const packageRequire = createRequire(path.join(path.dirname(lockPath), 'package.json'));
  const installed = packageRequire(`${packageName}/package.json`);
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const entry = lock.packages?.[`node_modules/${packageName}`];
  if (installed.version !== expectedVersion || entry?.version !== expectedVersion ||
      entry?.integrity !== expectedIntegrity) {
    throw new Error('refusing to execute an unpinned SDK installation');
  }
  return { expectedIntegrity, installed, packageName, packageRequire };
}

function installRedactedConsole(secret) {
  for (const level of ['log', 'warn', 'error', 'info', 'debug']) {
    console[level] = (...items) => {
      let line = items.map(item => typeof item === 'string' ? item : JSON.stringify(item)).join(' ');
      if (secret) line = line.split(secret).join('[REDACTED]');
      process.stderr.write(`${line}\n`);
    };
  }
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  const context = packageContext(args);
  const sdk = context.packageRequire(context.packageName);
  const expectMissing = Boolean(args['expect-missing-core-api']);
  const hasCoreApi = typeof sdk.reconcileReportingCoreV1 === 'function';
  let coreSubpath = 'available';
  try {
    context.packageRequire.resolve(`${context.packageName}/client/core`);
  } catch (error) {
    coreSubpath = error.code || error.name;
  }

  if (!hasCoreApi && !args.endpoint) {
    const result = {
      core_client_subpath: coreSubpath,
      core_reconciler_export: typeof sdk.reconcileReportingCoreV1,
      kind: 'reporting_interop_ts_core_buyer',
      package: {
        integrity: context.expectedIntegrity,
        name: context.packageName,
        version: context.installed.version,
      },
      schema_version: 1,
      semantic_lane_complete: false,
      status: 'unsupported',
    };
    if (!expectMissing) process.exitCode = 1;
    return result;
  }
  if (hasCoreApi && expectMissing) throw new Error('Core API unexpectedly exists in expected-gap mode');
  for (const name of ['ProtocolClient', 'unwrapProtocolResponse', 'closeMCPConnections']) {
    if (typeof sdk[name] !== 'function') throw new Error(`missing public transport export: ${name}`);
  }

  const endpoint = validateEndpoint(requireArg(args, 'endpoint'));
  const adcpVersion = requireArg(args, 'adcp-version');
  const accountId = requireArg(args, 'account');
  const authEnv = requireArg(args, 'auth-env');
  const authToken = process.env[authEnv];
  if (!authToken) throw new Error(`authentication environment variable is unset: ${authEnv}`);
  installRedactedConsole(authToken);
  const agent = {
    agent_uri: endpoint,
    auth_token: authToken,
    id: 'reporting-interop-core-seller',
    name: 'reporting-interop-core-seller',
    protocol: 'mcp',
  };
  const probeAbort = createProbeAbort('reporting Core buyer probe');
  const call = async (toolName, params) => {
    const raw = await callWithProbeAbort(probeAbort, signal => sdk.ProtocolClient.callTool(
      agent, toolName, params, {
        adcpVersion,
        signal,
        transport: { maxResponseBytes: 16 * 1024 * 1024, requestTimeoutMs: 30_000 },
        wireAdcpVersion: adcpVersion,
      },
    ));
    return sdk.unwrapProtocolResponse(raw, toolName, 'mcp', {
      responseAdcpVersion: adcpVersion,
    });
  };

  let capability;
  let page;
  try {
    capability = await call('get_adcp_capabilities', {});
    page = await call('get_reporting_status', {
      account: { account_id: accountId },
      period: { start: '2026-08-01T00:00:00Z', end: '2026-08-02T00:00:00Z' },
      view: 'periods',
    });
  } finally {
    probeAbort.abort('probe settled');
    try {
      await sdk.closeMCPConnections();
    } finally {
      probeAbort.dispose();
    }
  }
  const rawFacts = {
    account_id: page.account_id,
    ledger_as_of: page.ledger_as_of,
    obligation_count: page.periods.length,
    revision_count: page.revisions.length,
  };
  if (!hasCoreApi) {
    if (!expectMissing) process.exitCode = 1;
    return {
      core_client_subpath: coreSubpath,
      core_reconciler_export: typeof sdk.reconcileReportingCoreV1,
      http_exercised: true,
      kind: 'reporting_interop_ts_core_buyer',
      package: {
        integrity: context.expectedIntegrity,
        name: context.packageName,
        version: context.installed.version,
      },
      raw_facts: rawFacts,
      runtime: { node: process.version },
      schema_version: 1,
      semantic_lane_complete: false,
      status: 'unsupported',
    };
  }
  const recoveryWindow = capability?.media_buy?.reporting_delivery?.automated_recovery_window_seconds;
  if (!Number.isSafeInteger(recoveryWindow) || recoveryWindow < 0) {
    throw new Error('seller did not advertise a valid Core automated recovery window');
  }
  const coreInput = {
    clocks: {
      automatedRecoveryWindowSeconds: recoveryWindow,
      ledgerAsOf: page.ledger_as_of,
    },
    obligations: page.periods,
    revisions: page.revisions,
    scope: {
      closed: page.scope.scope_closed,
      coverageComplete: page.scope.coverage_complete,
      recordsComplete: page.pagination.has_more === false,
    },
  };
  const inputBefore = JSON.stringify(coreInput);
  const nativeResult = sdk.reconcileReportingCoreV1(coreInput);
  if (nativeResult.health !== 'complete' || nativeResult.obligations.length !== 1 ||
      nativeResult.obligations[0].satisfied !== true) {
    throw new Error('Core native reconciliation did not satisfy the deterministic obligation');
  }
  if (JSON.stringify(coreInput) !== inputBefore) {
    throw new Error('Core reconciler mutated caller-owned input');
  }
  const coverageIncomplete = sdk.reconcileReportingCoreV1({
    ...structuredClone(coreInput),
    scope: { ...coreInput.scope, coverageComplete: false },
  });
  if (coverageIncomplete.health !== 'action_required' ||
      coverageIncomplete.obligations[0]?.satisfied !== true ||
      coverageIncomplete.obligations[0]?.health !== 'complete') {
    throw new Error('Core coverage-incomplete top-level control changed semantics');
  }
  let incompleteRecordsError;
  try {
    sdk.reconcileReportingCoreV1({
      ...structuredClone(coreInput),
      scope: { ...coreInput.scope, recordsComplete: false },
    });
  } catch (error) {
    incompleteRecordsError = String(error.message || error);
  }
  if (incompleteRecordsError !==
      'Core reporting reconciliation requires every reporting-status cursor page') {
    throw new Error('Core incomplete-cursor control did not fail closed');
  }
  const partialCoverageInput = structuredClone(coreInput);
  partialCoverageInput.obligations[0].coverage = { status: 'partial' };
  const partialCoverage = sdk.reconcileReportingCoreV1(partialCoverageInput);
  if (partialCoverage.obligations[0]?.satisfied !== false ||
      !partialCoverage.obligations[0]?.issues?.some(
        issue => issue.code === 'REPORTING_COVERAGE_INCOMPLETE')) {
    throw new Error('Core partial-coverage control did not report the declared issue');
  }
  const obligation = nativeResult.obligations[0];
  const neutralSatisfied = nativeResult.health === 'complete' &&
    nativeResult.obligations.every(item => item.satisfied === true);
  return {
    core_client_subpath: coreSubpath,
    core_reconciler_export: typeof sdk.reconcileReportingCoreV1,
    kind: 'reporting_interop_ts_core_buyer',
    native_result: nativeResult,
    native_controls: {
      caller_input_mutated: false,
      coverage_incomplete: coverageIncomplete,
      incomplete_records_error: incompleteRecordsError,
      partial_coverage: partialCoverage,
    },
    neutral_result: {
      health: nativeResult.health,
      obligations: nativeResult.obligations.map(item => ({
        health: item.health,
        reporting_revision_ids: item.reportingRevisionIds,
        requirements_satisfied: item.satisfied,
      })),
      requirements_satisfied: neutralSatisfied,
      selected_obligation_satisfied: obligation.satisfied,
    },
    package: {
      integrity: context.expectedIntegrity,
      name: context.packageName,
      version: context.installed.version,
    },
    raw_facts: rawFacts,
    runtime: { node: process.version },
    schema_version: 1,
    semantic_lane_complete: true,
    status: 'passed',
  };
}

(async () => {
  try {
    process.stdout.write(`${JSON.stringify(stable(await run()), null, 2)}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify(stable({
      error: { message: String(error.message || error), name: error.name || 'Error' },
      kind: 'reporting_interop_ts_core_buyer',
      schema_version: 1,
      status: 'failed',
    }), null, 2)}\n`);
    process.exitCode = 2;
  }
})();
