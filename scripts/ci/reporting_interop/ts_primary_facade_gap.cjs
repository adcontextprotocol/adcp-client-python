#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');

function parseArgs(argv) {
  const values = { 'expect-gap': false };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--expect-gap') {
      values['expect-gap'] = true;
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

function main() {
  const args = parseArgs(process.argv.slice(2));
  const expectedVersion = requireArg(args, 'expected-version');
  const expectedIntegrity = requireArg(args, 'expected-integrity');
  const lockPath = path.resolve(requireArg(args, 'package-lock'));
  const adcpVersion = requireArg(args, 'adcp-version');
  const packageName = args.package || '@adcp/sdk';
  const packageRequire = createRequire(path.join(path.dirname(lockPath), 'package.json'));
  const installed = packageRequire(`${packageName}/package.json`);
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const lockEntry = lock.packages?.[`node_modules/${packageName}`];
  if (installed.version !== expectedVersion || lockEntry?.version !== expectedVersion ||
      lockEntry?.integrity !== expectedIntegrity) {
    throw new Error('refusing to reproduce against an unpinned SDK installation');
  }

  const sdk = packageRequire(packageName);
  const requiredLowerLevel = ['ProtocolClient', 'unwrapProtocolResponse', 'reconcileReporting'];
  const lowerLevel = Object.fromEntries(requiredLowerLevel.map(name => [name, typeof sdk[name]]));
  if (Object.values(lowerLevel).some(type => type !== 'function')) {
    throw new Error('required public lower-level reporting primitives are unavailable');
  }
  if (typeof sdk.ADCPMultiAgentClient !== 'function') {
    throw new Error('public ADCPMultiAgentClient constructor is unavailable');
  }

  // Construction is deliberately offline. The reproduction inspects only the
  // documented primary facade and never calls the reserved endpoint.
  const client = new sdk.ADCPMultiAgentClient([{
    agent_uri: 'http://127.0.0.1:9/mcp',
    id: 'installed-package-surface-probe',
    name: 'installed-package-surface-probe',
    protocol: 'mcp',
  }], { adcpVersion, wireAdcpVersion: adcpVersion });
  if (client.getAdcpVersion() !== adcpVersion) {
    throw new Error('primary facade did not retain the exact AdCP version pin');
  }
  const agent = client.agent('installed-package-surface-probe');
  const requiredFacade = [
    'getReportingStatus',
    'syncReportingReceipts',
    'syncReportingStatus',
  ];
  const facade = Object.fromEntries(requiredFacade.map(name => [name, typeof agent[name]]));
  facade.reconcileReporting = typeof agent.reconcileReporting;
  facade.reporting = typeof agent.reporting;
  const missing = requiredFacade.filter(name => facade[name] !== 'function');
  const gapReproduced = missing.length > 0;

  return {
    adcp_version: adcpVersion,
    expected_gap_mode: Boolean(args['expect-gap']),
    gap_reproduced: gapReproduced,
    kind: 'reporting_interop_ts_primary_facade_gap',
    lower_level_public_exports: lowerLevel,
    missing_primary_facade_methods: missing,
    package: {
      integrity: expectedIntegrity,
      name: packageName,
      version: installed.version,
    },
    primary_facade: facade,
    primary_facade_adcp_version: client.getAdcpVersion(),
    runtime: { node: process.version },
    schema_version: 1,
    semantic_lane_complete: false,
    surface_available: !gapReproduced,
    status: gapReproduced ? 'surface_gap_reproduced' : 'surface_available_nonsemantic',
    limitation: 'offline method-presence introspection does not execute reporting semantics',
  };
}

try {
  const args = parseArgs(process.argv.slice(2));
  const result = main();
  process.stdout.write(`${JSON.stringify(stable(result), null, 2)}\n`);
  if (result.gap_reproduced && !args['expect-gap']) process.exitCode = 1;
  if (!result.gap_reproduced && args['expect-gap']) process.exitCode = 2;
} catch (error) {
  process.stdout.write(`${JSON.stringify(stable({
    error: { message: String(error.message || error), name: error.name || 'Error' },
    kind: 'reporting_interop_ts_primary_facade_gap',
    schema_version: 1,
    semantic_lane_complete: false,
    status: 'failed',
  }), null, 2)}\n`);
  process.exitCode = 2;
}
