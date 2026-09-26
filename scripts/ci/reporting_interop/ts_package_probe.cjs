#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');

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
  const value = args[name];
  if (!value) throw new Error(`missing required argument: --${name}`);
  return value;
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
  }
  return value;
}

function fail(message, details = {}) {
  const error = new Error(message);
  error.details = details;
  throw error;
}

function packageLockEntry(lockPath, packageName) {
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const entry = lock.packages?.[`node_modules/${packageName}`];
  if (!entry) fail('package lock does not contain the SDK', { package: packageName });
  return entry;
}

function registryVersionEntry(metadataPath, version) {
  const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
  if (metadata.version === version) return metadata;
  const entry = metadata.versions?.[version];
  if (!entry) fail('registry metadata does not contain the expected version', { version });
  return entry;
}

function assertEqual(actual, expected, field) {
  if (actual !== expected) fail(`installed package identity mismatch: ${field}`, {
    actual: actual ?? null,
    expected,
    field,
  });
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const packageName = args.package || '@adcp/sdk';
  const expectedVersion = requireArg(args, 'expected-version');
  const expectedIntegrity = requireArg(args, 'expected-integrity');
  const expectedTarball = requireArg(args, 'expected-tarball');
  const expectedGitHead = requireArg(args, 'expected-git-head');
  const expectedShasum = requireArg(args, 'expected-shasum');
  const lockPath = path.resolve(requireArg(args, 'package-lock'));
  const metadataPath = path.resolve(requireArg(args, 'registry-metadata'));
  const packageRequire = createRequire(path.join(path.dirname(lockPath), 'package.json'));

  const packageFile = packageRequire.resolve(`${packageName}/package.json`);
  const packageRoot = fs.realpathSync(path.dirname(packageFile));
  const installed = packageRequire(packageFile);
  const rootEntry = fs.realpathSync(packageRequire.resolve(packageName));
  if (!rootEntry.startsWith(`${packageRoot}${path.sep}`)) {
    fail('public package entry resolved outside the installed package root');
  }

  const lockEntry = packageLockEntry(lockPath, packageName);
  const registryEntry = registryVersionEntry(metadataPath, expectedVersion);
  assertEqual(installed.name, packageName, 'package.name');
  assertEqual(installed.version, expectedVersion, 'package.version');
  assertEqual(lockEntry.version, expectedVersion, 'lock.version');
  assertEqual(lockEntry.integrity, expectedIntegrity, 'lock.integrity');
  assertEqual(lockEntry.resolved, expectedTarball, 'lock.resolved');
  assertEqual(registryEntry.name, packageName, 'registry.name');
  assertEqual(registryEntry.version, expectedVersion, 'registry.version');
  assertEqual(registryEntry.gitHead, expectedGitHead, 'registry.gitHead');
  assertEqual(registryEntry.dist?.integrity, expectedIntegrity, 'registry.dist.integrity');
  assertEqual(registryEntry.dist?.tarball, expectedTarball, 'registry.dist.tarball');
  assertEqual(registryEntry.dist?.shasum, expectedShasum, 'registry.dist.shasum');

  const sdk = packageRequire(packageName);
  const requiredRootExports = [
    'ProtocolClient',
    'unwrapProtocolResponse',
    'reconcileReporting',
    'closeMCPConnections',
  ];
  const rootExports = Object.fromEntries(
    requiredRootExports.map(name => [name, typeof sdk[name]]),
  );
  for (const [name, type] of Object.entries(rootExports)) {
    if (type !== 'function') fail('required public SDK export is unavailable', { export: name, type });
  }

  const declaredSubpaths = [
    '.',
    './client',
    './schemas',
    './server',
    './reporting/ledger',
    './reporting/service',
  ];
  const publicSubpaths = Object.fromEntries(
    declaredSubpaths.map(subpath => [subpath, Object.hasOwn(installed.exports || {}, subpath)]),
  );
  if (Object.values(publicSubpaths).some(value => !value)) {
    fail('required public package subpath is not declared', { publicSubpaths });
  }

  return {
    identity: {
      git_head: expectedGitHead,
      integrity: expectedIntegrity,
      package: packageName,
      shasum: expectedShasum,
      tarball: expectedTarball,
      version: expectedVersion,
    },
    kind: 'reporting_interop_ts_package_probe',
    public_subpaths: publicSubpaths,
    root_exports: rootExports,
    runtime: {
      arch: process.arch,
      node: process.version,
      platform: process.platform,
    },
    schema_version: 1,
    status: 'passed',
  };
}

try {
  process.stdout.write(`${JSON.stringify(stable(main()), null, 2)}\n`);
} catch (error) {
  process.stdout.write(`${JSON.stringify(stable({
    error: {
      details: error.details || {},
      message: String(error.message || error),
      name: error.name || 'Error',
    },
    kind: 'reporting_interop_ts_package_probe',
    schema_version: 1,
    status: 'failed',
  }), null, 2)}\n`);
  process.exitCode = 1;
}
