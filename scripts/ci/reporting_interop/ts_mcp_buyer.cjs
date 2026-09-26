#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { callWithProbeAbort, createProbeAbort } = require('./ts_probe_abort.cjs');

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
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

const SENSITIVE_KEY = /(authorization|credential|password|secret|token|private.?key|signed.?url)/i;

function redact(value, secrets = []) {
  if (Array.isArray(value)) return value.map(item => redact(item, secrets));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key,
      SENSITIVE_KEY.test(key) ? '[REDACTED]' : redact(item, secrets),
    ]));
  }
  if (typeof value !== 'string') return value;
  let result = value;
  for (const secret of secrets) {
    if (secret) result = result.split(secret).join('[REDACTED]');
  }
  result = result.replace(/(https?:\/\/[^\s?#]+)\?[^\s#]*/gi, '$1?[REDACTED]');
  result = result.replace(/\bBearer\s+[^\s,;]+/gi, 'Bearer [REDACTED]');
  return result;
}

function readInput(inputPath) {
  const value = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('input must be a JSON object');
  }
  const expectedPeriods = value.expected_periods ?? value.expectedPeriods;
  if (!value.request || typeof value.request !== 'object') throw new Error('input.request is required');
  if (!Array.isArray(expectedPeriods)) throw new Error('input.expected_periods must be an array');
  if (!value.now || Number.isNaN(Date.parse(value.now))) throw new Error('input.now must be an RFC 3339 timestamp');
  const inspection = value.inspection || { mode: 'assert_not_called' };
  if (inspection.mode !== 'assert_not_called') {
    throw new Error('only fail-closed inspection mode assert_not_called is implemented');
  }
  const expectedOutcome = value.expected_outcome ?? value.expectedOutcome;
  if (!expectedOutcome || typeof expectedOutcome !== 'object' || Array.isArray(expectedOutcome)) {
    throw new Error('input.expected_outcome is required');
  }
  return { expectedOutcome, expectedPeriods, inspection, value };
}

function assertExpectedOutcome(result, expected, inspectionCalls) {
  if (typeof expected.definitive !== 'boolean') {
    throw new Error('expected_outcome.definitive must be boolean');
  }
  if (result.definitive !== expected.definitive) {
    throw new Error(`semantic outcome mismatch: definitive=${String(result.definitive)}`);
  }
  if (expected.inspection_calls !== undefined && inspectionCalls !== expected.inspection_calls) {
    throw new Error(`semantic outcome mismatch: inspection_calls=${inspectionCalls}`);
  }
  const obligations = Array.isArray(result.obligations) ? result.obligations : [];
  if (expected.obligation_count !== undefined && obligations.length !== expected.obligation_count) {
    throw new Error(`semantic outcome mismatch: obligation_count=${obligations.length}`);
  }
  const reasons = new Set(obligations.flatMap(item => Array.isArray(item.reasons) ? item.reasons : []));
  for (const reason of expected.reasons_absent || []) {
    if (reasons.has(reason)) throw new Error(`semantic outcome mismatch: unexpected reason ${reason}`);
  }
  for (const reason of expected.reasons_present || []) {
    if (!reasons.has(reason)) throw new Error(`semantic outcome mismatch: missing reason ${reason}`);
  }
}

function validateEndpoint(endpoint) {
  const parsed = new URL(endpoint);
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('endpoint must use HTTP or HTTPS');
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('endpoint must not contain credentials, a query, or a fragment');
  }
  return parsed.toString();
}

function verifyPackagePin(packageRequire, packageName, expectedVersion, expectedIntegrity, lockPath) {
  const installed = packageRequire(`${packageName}/package.json`);
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const entry = lock.packages?.[`node_modules/${packageName}`];
  if (installed.version !== expectedVersion || entry?.version !== expectedVersion ||
      entry?.integrity !== expectedIntegrity) {
    throw new Error('refusing to execute an unpinned SDK installation');
  }
  return installed;
}

function numericArg(args, name, fallback) {
  if (!args[name]) return fallback;
  const value = Number(args[name]);
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`--${name} must be a positive integer`);
  return value;
}

function installRedactedConsole(secrets) {
  for (const level of ['log', 'warn', 'error', 'info', 'debug']) {
    console[level] = (...items) => {
      const line = items.map(item => {
        if (typeof item === 'string') return item;
        try {
          return JSON.stringify(item);
        } catch {
          return String(item);
        }
      }).join(' ');
      process.stderr.write(`${redact(line, secrets)}\n`);
    };
  }
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  const packageName = args.package || '@adcp/sdk';
  const expectedVersion = requireArg(args, 'expected-version');
  const expectedIntegrity = requireArg(args, 'expected-integrity');
  const lockPath = path.resolve(requireArg(args, 'package-lock'));
  const adcpVersion = requireArg(args, 'adcp-version');
  const endpoint = validateEndpoint(requireArg(args, 'endpoint'));
  const inputPath = path.resolve(requireArg(args, 'input'));
  const authEnv = args['auth-env'];
  const authToken = authEnv ? process.env[authEnv] : undefined;
  if (authEnv && !authToken) throw new Error(`authentication environment variable is unset: ${authEnv}`);
  const secrets = authToken ? [authToken] : [];
  installRedactedConsole(secrets);
  const packageRequire = createRequire(path.join(path.dirname(lockPath), 'package.json'));
  const installed = verifyPackagePin(
    packageRequire, packageName, expectedVersion, expectedIntegrity, lockPath,
  );
  const { expectedOutcome, expectedPeriods, value: input } = readInput(inputPath);
  const sdk = packageRequire(packageName);
  for (const name of ['ProtocolClient', 'unwrapProtocolResponse', 'reconcileReporting', 'closeMCPConnections']) {
    if (typeof sdk[name] !== 'function') throw new Error(`required public SDK export is unavailable: ${name}`);
  }

  const transport = {
    maxResponseBytes: numericArg(args, 'max-response-bytes', 16 * 1024 * 1024),
    requestTimeoutMs: numericArg(args, 'request-timeout-ms', 30_000),
  };
  const agent = {
    agent_uri: endpoint,
    auth_token: authToken,
    id: 'reporting-interop-seller',
    name: 'reporting-interop-seller',
    protocol: 'mcp',
  };
  const probeAbort = createProbeAbort('managed reconciliation buyer probe');
  const calls = [];
  const call = async (toolName, params) => {
    calls.push(toolName);
    const raw = await callWithProbeAbort(probeAbort, signal => sdk.ProtocolClient.callTool(
      agent, toolName, params, { adcpVersion, signal, transport, wireAdcpVersion: adcpVersion },
    ));
    return sdk.unwrapProtocolResponse(raw, toolName, 'mcp', { responseAdcpVersion: adcpVersion });
  };
  const client = {
    getMediaBuyDelivery: (params) => call('get_media_buy_delivery', params),
    getReportingStatus: (params) => call('get_reporting_status', params),
    syncReportingReceipts: (params) => call('sync_reporting_receipts', params),
    syncReportingStatus: (params) => call('sync_reporting_status', params),
  };
  let inspectionCalls = 0;
  const inspect = async () => {
    inspectionCalls += 1;
    throw new Error('reporting artifact inspection was not declared for this scenario');
  };
  const options = {
    client,
    expectedPeriods,
    inspect,
    ledgerLimits: input.ledger_limits,
    maxInspectionAttempts: input.max_inspection_attempts,
    maxSnapshotRestarts: input.max_snapshot_restarts,
    now: new Date(input.now),
    operationsContact: input.operations_contact,
    request: input.request,
  };
  for (const key of Object.keys(options)) {
    if (options[key] === undefined) delete options[key];
  }

  let result;
  let executionError;
  let cleanupError;
  try {
    result = await sdk.reconcileReporting(options);
  } catch (error) {
    executionError = error;
  } finally {
    probeAbort.abort('probe settled');
    try {
      await sdk.closeMCPConnections();
    } catch (error) {
      cleanupError = error;
    } finally {
      probeAbort.dispose();
    }
  }
  if (cleanupError) throw new Error(`MCP cleanup failed: ${redact(String(cleanupError.message || cleanupError), secrets)}`);
  if (executionError) throw executionError;
  assertExpectedOutcome(result, expectedOutcome, inspectionCalls);

  return {
    adcp_version: adcpVersion,
    call_counts: Object.fromEntries([...new Set(calls)].sort().map(name => [
      name,
      calls.filter(value => value === name).length,
    ])),
    inspection_calls: inspectionCalls,
    kind: 'reporting_interop_ts_mcp_buyer',
    package: {
      integrity: expectedIntegrity,
      name: packageName,
      version: installed.version,
    },
    reconciliation: redact(result, secrets),
    runtime: { node: process.version },
    schema_version: 1,
    status: 'passed',
  };
}

(async () => {
  let report;
  try {
    report = await run();
  } catch (error) {
    let failureSecrets = [];
    try {
      const args = parseArgs(process.argv.slice(2));
      const token = args['auth-env'] ? process.env[args['auth-env']] : undefined;
      if (token) failureSecrets = [token];
    } catch {
      // Argument parsing already failed; there is no trusted auth-env selector.
    }
    report = {
      error: {
        code: typeof error.code === 'string' ? error.code : null,
        message: redact(String(error.message || error), failureSecrets),
        name: error.name || 'Error',
      },
      kind: 'reporting_interop_ts_mcp_buyer',
      schema_version: 1,
      status: 'failed',
    };
    process.exitCode = 1;
  }
  process.stdout.write(`${JSON.stringify(stable(report), null, 2)}\n`);
})();
