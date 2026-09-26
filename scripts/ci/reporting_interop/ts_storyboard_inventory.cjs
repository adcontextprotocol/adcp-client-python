#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { createRequire } = require('node:module');

const REPORTING_STORYBOARDS = new Set([
  'reliable_reporting_managed_delivery',
  'reliable_reporting_reconciled_billing',
  'reporting_consumer_status',
  'reporting_core',
  'reporting_core_declaration',
]);

const STORYBOARD_FILES = {
  reliable_reporting_managed_delivery: 'reliable-reporting-managed-delivery.yaml',
  reliable_reporting_reconciled_billing: 'reliable-reporting-reconciled-billing.yaml',
  reporting_consumer_status: 'reporting-consumer-status.yaml',
  reporting_core: 'reporting-core.yaml',
  reporting_core_declaration: 'reporting-core-declaration.yaml',
};

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith('--') || !value) throw new Error(`invalid argument: ${key || ''}`);
    values[key.slice(2)] = value;
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

function sha256File(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function storyboardRequirements(document) {
  const scenarios = new Map();
  function walk(value) {
    if (Array.isArray(value)) {
      for (const item of value) walk(item);
      return;
    }
    if (!value || typeof value !== 'object') return;
    if (typeof value.comply_scenario === 'string') {
      if (!scenarios.has(value.comply_scenario)) scenarios.set(value.comply_scenario, new Set());
    }
    if (typeof value.scenario === 'string' && value.scenario.includes('reporting')) {
      if (!scenarios.has(value.scenario)) scenarios.set(value.scenario, new Set());
      if (typeof value.params?.operation === 'string') {
        scenarios.get(value.scenario).add(value.params.operation);
      }
    }
    for (const item of Object.values(value)) walk(item);
  }
  walk(document);
  return {
    capabilities: Array.isArray(document.requires_all_capabilities)
      ? document.requires_all_capabilities.map(item => ({ equals: item.equals, path: item.path }))
      : [],
    controller_required: Array.isArray(document.requires) && document.requires.includes('controller'),
    controller_scenarios: [...scenarios.entries()]
      .map(([scenario, operations]) => ({ operations: [...operations].sort(), scenario }))
      .sort((left, right) => left.scenario.localeCompare(right.scenario)),
    required_tools: Array.isArray(document.required_tools) ? [...document.required_tools] : [],
  };
}

function storyboardSteps(document) {
  const steps = [];
  for (const phase of document.phases || []) {
    for (const step of phase.steps || []) {
      steps.push({
        comply_scenario: step.comply_scenario ?? null,
        controller_operation: step.sample_request?.params?.operation ?? null,
        id: step.id,
        phase_id: phase.id,
        stateful: step.stateful === true,
        task: step.task,
        title: step.title,
      });
    }
  }
  return steps;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const packageName = args.package || '@adcp/sdk';
  const expectedVersion = requireArg(args, 'expected-version');
  const expectedIntegrity = requireArg(args, 'expected-integrity');
  const lockPath = path.resolve(requireArg(args, 'package-lock'));
  const packageRequire = createRequire(path.join(path.dirname(lockPath), 'package.json'));
  const installed = packageRequire(`${packageName}/package.json`);
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const lockEntry = lock.packages?.[`node_modules/${packageName}`];
  if (installed.version !== expectedVersion || lockEntry?.version !== expectedVersion ||
      lockEntry?.integrity !== expectedIntegrity) {
    throw new Error('refusing to inventory an unpinned SDK installation');
  }
  const packageFile = packageRequire.resolve(`${packageName}/package.json`);
  const sdk = packageRequire(packageName);
  const yaml = packageRequire('yaml');
  const testing = packageRequire(`${packageName}/testing`);
  const server = packageRequire(`${packageName}/server`);
  if (!server.CONTROLLER_SCENARIOS || typeof server.CONTROLLER_SCENARIOS !== 'object' ||
      Array.isArray(server.CONTROLLER_SCENARIOS)) {
    throw new Error('public CONTROLLER_SCENARIOS export is missing or malformed');
  }
  const builtInScenarios = Object.values(server.CONTROLLER_SCENARIOS);
  if (builtInScenarios.length === 0) {
    throw new Error('public CONTROLLER_SCENARIOS export contains no declared scenarios');
  }
  const storyboardRoot = path.join(
    path.dirname(packageFile), 'compliance', 'cache', sdk.ADCP_VERSION, 'universal');
  const cliRelative = typeof installed.bin === 'string' ? installed.bin : installed.bin?.adcp;
  if (!cliRelative) throw new Error('installed package has no public adcp CLI bin');
  const cli = path.resolve(path.dirname(packageFile), cliRelative);
  const completed = spawnSync(process.execPath, [cli, 'storyboard', 'list', '--json'], {
    cwd: path.dirname(lockPath),
    encoding: 'utf8',
    env: { ...process.env, NO_COLOR: '1' },
    maxBuffer: 8 * 1024 * 1024,
    timeout: 60_000,
  });
  if (completed.error) throw completed.error;
  if (completed.status !== 0) throw new Error(`storyboard list failed: ${completed.stderr}`);
  const document = JSON.parse(completed.stdout);
  const rows = Array.isArray(document.storyboards) ? document.storyboards : [];
  const reporting = rows
    .filter(row => REPORTING_STORYBOARDS.has(row.id))
    .map(row => {
      const file = STORYBOARD_FILES[row.id];
      if (!file) throw new Error(`missing storyboard file mapping for ${row.id}`);
      const source = path.join(storyboardRoot, file);
      const sourceReal = fs.realpathSync(source);
      const packageRoot = fs.realpathSync(path.dirname(packageFile));
      if (!sourceReal.startsWith(`${packageRoot}${path.sep}`) || !fs.statSync(sourceReal).isFile()) {
        throw new Error(`storyboard member escaped the installed package: ${row.id}`);
      }
      const parsed = yaml.parse(fs.readFileSync(source, 'utf8'));
      const requirements = storyboardRequirements(parsed);
      const steps = storyboardSteps(parsed);
      return {
        id: row.id,
        requirements,
        source: path.relative(path.dirname(packageFile), source),
        source_member_identity: {
          package_integrity: expectedIntegrity,
          package_version: installed.version,
          path: path.relative(path.dirname(packageFile), source),
          sha256: sha256File(sourceReal),
        },
        stateful_step_count: row.stateful_step_count,
        step_count: row.step_count,
        steps,
      };
    })
    .sort((left, right) => left.id.localeCompare(right.id));
  if (reporting.length !== REPORTING_STORYBOARDS.size ||
      new Set(reporting.map(row => row.id)).size !== REPORTING_STORYBOARDS.size) {
    throw new Error('installed CLI did not expose the exact five reporting storyboards');
  }
  for (const row of reporting) {
    if (!Number.isSafeInteger(row.step_count) || !Number.isSafeInteger(row.stateful_step_count) ||
        row.step_count < row.stateful_step_count || row.stateful_step_count < 0) {
      throw new Error(`invalid storyboard counts for ${row.id}`);
    }
    if (row.steps.length !== row.step_count ||
        row.steps.filter(step => step.stateful).length !== row.stateful_step_count ||
        new Set(row.steps.map(step => step.id)).size !== row.step_count) {
      throw new Error(`storyboard step identities drifted for ${row.id}`);
    }
  }
  return {
    inventory: reporting,
    controller_public_surface: {
      createComplyController: typeof testing.createComplyController,
      handleTestControllerRequest: typeof server.handleTestControllerRequest,
      registerTestController: typeof server.registerTestController,
      TOOL_INPUT_SHAPE: typeof server.TOOL_INPUT_SHAPE,
      toMcpResponse: typeof server.toMcpResponse,
      built_in_scenarios: builtInScenarios.sort(),
      reporting_core_flat_store_hook: 'reportingCoreLifecycleProbe',
      note: 'Reliable-reporting probe adapters are not turnkey exports; custom controller adapters must drive real seller state. Compliance cache paths are bound package members, not independent semver API promises.',
    },
    kind: 'reporting_interop_ts_storyboard_inventory',
    package: { integrity: expectedIntegrity, name: packageName, version: installed.version },
    runtime: { node: process.version },
    schema_version: 2,
    status: 'passed',
    totals: {
      stateful_steps: reporting.reduce((sum, row) => sum + row.stateful_step_count, 0),
      steps: reporting.reduce((sum, row) => sum + row.step_count, 0),
      storyboards: reporting.length,
    },
  };
}

try {
  process.stdout.write(`${JSON.stringify(stable(main()), null, 2)}\n`);
} catch (error) {
  process.stdout.write(`${JSON.stringify(stable({
    error: { message: String(error.message || error), name: error.name || 'Error' },
    kind: 'reporting_interop_ts_storyboard_inventory',
    schema_version: 1,
    status: 'failed',
  }), null, 2)}\n`);
  process.exitCode = 1;
}
