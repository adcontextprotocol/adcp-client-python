#!/usr/bin/env node
'use strict';

/* Public installed TypeScript client for the receipt and exact-read stage. */

const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { callWithProbeAbort, createProbeAbort } = require('./ts_probe_abort.cjs');

function argsFrom(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    if (!key?.startsWith('--') || !argv[i + 1] || argv[i + 1].startsWith('--')) {
      throw new Error('arguments must be --key value pairs');
    }
    if (Object.hasOwn(result, key.slice(2))) throw new Error(`duplicate ${key}`);
    result[key.slice(2)] = argv[i + 1];
  }
  return result;
}

function required(args, key) {
  if (!args[key]) throw new Error(`missing --${key}`);
  return args[key];
}

async function main() {
  const args = argsFrom(process.argv.slice(2));
  const lock = path.resolve(required(args, 'package-lock'));
  const packageRequire = createRequire(path.join(path.dirname(lock), 'package.json'));
  const packageName = '@adcp/sdk';
  const installed = packageRequire(`${packageName}/package.json`);
  const pinned = JSON.parse(fs.readFileSync(lock, 'utf8')).packages?.[`node_modules/${packageName}`];
  if (installed.version !== required(args, 'expected-version') ||
      pinned?.version !== installed.version ||
      pinned?.integrity !== required(args, 'expected-integrity')) {
    throw new Error('installed TypeScript artifact does not match its immutable pin');
  }
  const sdk = packageRequire(packageName);
  if (typeof sdk.ProtocolClient?.callTool !== 'function' ||
      typeof sdk.unwrapProtocolResponse !== 'function' ||
      typeof sdk.closeMCPConnections !== 'function') {
    throw new Error('installed TypeScript client lacks the required public calls');
  }
  const endpoint = new URL(required(args, 'endpoint'));
  if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username ||
      endpoint.password || endpoint.search || endpoint.hash) {
    throw new Error('invalid seller endpoint');
  }
  const token = process.env[required(args, 'auth-env')];
  if (!token) throw new Error('seller authorization token is missing');
  const version = required(args, 'adcp-version');
  const agent = {
    agent_uri: endpoint.toString(), auth_token: token,
    id: 'full-interop-seller', name: 'full-interop-seller', protocol: 'mcp',
  };
  const abort = createProbeAbort('installed full-lifecycle receipt');
  const call = async (name, params) => {
    const raw = await callWithProbeAbort(abort, signal => sdk.ProtocolClient.callTool(
      agent, name, params, {
        adcpVersion: version, wireAdcpVersion: version, signal,
        transport: { requestTimeoutMs: 30_000, maxResponseBytes: 16 * 1024 * 1024 },
      },
    ));
    return sdk.unwrapProtocolResponse(raw, name, 'mcp', { responseAdcpVersion: version });
  };
  try {
    const account = { account_id: 'acct_a' };
    const revision = await call('get_reporting_status', {
      account, view: 'revision', reporting_revision_id: 'production-revision',
    });
    const materializations = revision.materializations || [];
    if (materializations.length !== 1 ||
        materializations[0].reporting_revision_id !== 'production-revision' ||
        materializations[0].status !== 'delivered') {
      throw new Error('exact revision read did not return the delivered winner');
    }
    const materialization = materializations[0];
    const proof = materialization.verification;
    if (!proof || proof.row_count !== 2 ||
        proof.verification_profile !== 'canonical_digest' ||
        !proof.canonical_content_digest?.value) {
      throw new Error('exact revision read lacks canonical managed evidence');
    }
    const submission = {
      account, idempotency_key: '10000000-0000-4000-8000-000000000001',
      receipts: [{
        reporting_receipt_id: 'interop-receipt-0001',
        reporting_obligation_id: materialization.reporting_obligation_id,
        reporting_revision_id: materialization.reporting_revision_id,
        reporting_materialization_id: materialization.reporting_materialization_id,
        status: 'accepted', verification_profile: proof.verification_profile,
        observed_row_count: proof.row_count,
        observed_control_totals: proof.control_totals,
        observed_canonical_content_digest: proof.canonical_content_digest,
        observed_at: new Date().toISOString(),
      }],
    };
    const accepted = await call('sync_reporting_receipts', submission);
    if (accepted.status !== 'completed' || accepted.results?.length !== 1 ||
        accepted.results[0].receipt?.reporting_revision_id !== 'production-revision' ||
        accepted.results[0].receipt?.status !== 'accepted') {
      throw new Error('receipt was not accepted for the exact delivered revision');
    }
    const period = await call('get_reporting_status', { account, view: 'periods' });
    if (period.periods?.length !== 1 || period.periods[0].reconciliation_status !== 'accepted' ||
        period.periods[0].receipt_count !== 1) {
      throw new Error('accepted receipt is absent from Reconciled Billing status');
    }
    return {
      status: 'passed', package_version: installed.version,
      reporting_revision_id: materialization.reporting_revision_id,
      reporting_materialization_id: materialization.reporting_materialization_id,
      canonical_content_digest: proof.canonical_content_digest.value,
      accepted_receipt_count: period.periods[0].receipt_count,
      reconciliation_status: period.periods[0].reconciliation_status,
      exact_revision_read: true,
    };
  } finally {
    abort.abort('receipt stage settled');
    await sdk.closeMCPConnections();
    abort.dispose();
  }
}

main().then(
  report => process.stdout.write(`${JSON.stringify(report)}\n`),
  error => {
    process.stderr.write(`${String(error.message || error)}\n`);
    process.exitCode = 1;
  },
);
