'use strict';
// Drives the separate-process RFC 9421 verifier over real HTTP with deterministic
// injected clocks. Also exercises the SEPARATE legacy HMAC webhook utility so the
// two are never conflated.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const { mintEphemeralEd25519Key } = require('@adcp/sdk/signing/testing');
const { signRequest, requestSigningEncodingForVersion } = require('@adcp/sdk/signing/client');
const { verifyWebhookRequest } = require('@adcp/sdk/webhooks');
const { CLOCK_SKEW_TOLERANCE_SECONDS, MAX_SIGNATURE_WINDOW_SECONDS, ALLOWED_ALGS,
        MANDATORY_COMPONENTS, WEBHOOK_MANDATORY_COMPONENTS } = require('@adcp/sdk/signing/server');

const PORT = Number(process.env.PROBE_PORT || 54321);
const PG_URL = process.env.PROBE_PG_URL;
const ADCP_VERSION_PIN = '3.2.0-rc.4';
const BINARY_ENCODING = requestSigningEncodingForVersion(ADCP_VERSION_PIN);
// Every `now` injection point in this SDK is UNIX SECONDS (verifier, signer and
// webhook all default to Math.floor(Date.now()/1e3)). Passing milliseconds makes
// PostgresReplayStore fail closed on its year-9999 upper bound.
const T0_SEC = Math.floor(Date.parse('2026-09-22T12:00:00Z') / 1000);
const BODY = JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call',
  params: { name: 'get_reporting_status', arguments: { account: { account_id: 'account-1' } } } });

let child;
const children = [];
function boot(jwks) {
  return new Promise((resolve, reject) => {
    const c = spawn(process.execPath, ['verifier-server.cjs'], {
      cwd: __dirname,
      env: { ...process.env, PROBE_PORT: String(PORT), PROBE_PG_URL: PG_URL, PROBE_JWKS: JSON.stringify(jwks) },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    children.push(c);
    let buf = '';
    c.stdout.on('data', d => { buf += d; if (buf.includes('LISTENING')) resolve(c); });
    c.stderr.on('data', d => process.stderr.write('[server] ' + d));
    c.on('exit', code => { if (!buf.includes('LISTENING')) reject(new Error('server exited ' + code)); });
    setTimeout(() => reject(new Error('server boot timeout')), 15000);
  });
}
async function reap() {
  for (const c of children) { try { c.kill('SIGTERM'); } catch {} }
  await new Promise(r => setTimeout(r, 300));
  for (const c of children) { try { if (c.exitCode === null) c.kill('SIGKILL'); } catch {} }
}

function signed(key, { nowSec = T0_SEC, nonce, body = BODY, path = '/mcp', method = 'POST', coverContentDigest = true } = {}) {
  const base = {
    method, url: `http://127.0.0.1:${PORT}${path}`,
    headers: { 'content-type': 'application/json' }, body,
  };
  return signRequest(base, key, { coverContentDigest, now: () => nowSec, binaryEncoding: BINARY_ENCODING, ...(nonce ? { nonce } : {}) });
}

async function post(headers, body, { nowSec = T0_SEC, operation } = {}) {
  const res = await fetch(`http://127.0.0.1:${PORT}/mcp`, {
    method: 'POST',
    headers: { ...headers, 'x-probe-now-sec': String(nowSec), ...(operation ? { 'x-probe-operation': operation } : {}) },
    body,
  });
  return res.json();
}

const rows = [];
function record(id, expectation, observed, pass, extra = {}) {
  rows.push({ vectorId: id, expected: expectation, observed, pass, ...extra });
  console.error(`  [${pass ? 'PASS' : 'FAIL'}] ${id}  expected=${expectation}  observed=${observed}`);
}

async function main() {
  const reqKey = await mintEphemeralEd25519Key({ kid: 'probe-request-key-1', adcp_use: 'request-signing' });
  const otherKey = await mintEphemeralEd25519Key({ kid: 'probe-unknown-key', adcp_use: 'request-signing' });
  const signerKey = { keyid: reqKey.kid, alg: reqKey.algorithm, privateKey: reqKey.privateKey };
  child = await boot([reqKey.publicKey]);   // only reqKey is published

  // V1 valid signed request
  {
    const s = signed(signerKey, { nonce: 'nonce-v1' });
    const r = await post(s.headers, s.body ?? BODY, { operation: 'get_reporting_status' });
    const st = r.ok ? r.result.status : 'threw:' + r.error.message;
    record('sig/valid_signed_request', 'verified', st, st === 'verified',
      { keyid: r.ok ? r.result.keyid : undefined, rawBodySha256: r.rawBodySha256 });
  }
  // V2 exact replay of the same nonce -> must be rejected by the PG replay store
  {
    const s = signed(signerKey, { nonce: 'nonce-replay' });
    const first = await post(s.headers, s.body ?? BODY, { operation: 'get_reporting_status' });
    const second = await post(s.headers, s.body ?? BODY, { operation: 'get_reporting_status' });
    const f = first.ok ? first.result.status : 'threw';
    const sec = second.ok ? second.result.status : 'threw';
    record('sig/replay_same_nonce_rejected', 'first=verified,second!=verified',
      `first=${f},second=${sec}`, f === 'verified' && sec !== 'verified',
      { secondDetail: second.ok ? second.result : second.error });
  }
  // V3 body tampered after signing -> content-digest / signature must fail
  {
    const s = signed(signerKey, { nonce: 'nonce-tamper' });
    const tampered = BODY.replace('account-1', 'account-2');
    assert.notEqual(tampered, BODY);
    const r = await post(s.headers, tampered, { operation: 'get_reporting_status' });
    const st = r.ok ? r.result.status : 'threw';
    record('sig/tampered_body_rejected', '!verified', st, st !== 'verified',
      { detail: r.ok ? r.result : r.error });
  }
  // V4 signature older than MAX_SIGNATURE_WINDOW_SECONDS (injected verifier clock)
  {
    const s = signed(signerKey, { nonce: 'nonce-expired', nowSec: T0_SEC });
    const late = T0_SEC + MAX_SIGNATURE_WINDOW_SECONDS + 100;
    const r = await post(s.headers, s.body ?? BODY, { nowSec: late, operation: 'get_reporting_status' });
    const st = r.ok ? r.result.status : 'threw';
    record('sig/expired_beyond_max_window_rejected', '!verified', st, st !== 'verified',
      { maxWindowSeconds: MAX_SIGNATURE_WINDOW_SECONDS, detail: r.ok ? r.result : r.error });
  }
  // V5 signature created in the future beyond CLOCK_SKEW_TOLERANCE_SECONDS
  {
    const future = T0_SEC + CLOCK_SKEW_TOLERANCE_SECONDS + 120;
    const s = signed(signerKey, { nonce: 'nonce-future', nowSec: future });
    const r = await post(s.headers, s.body ?? BODY, { nowSec: T0_SEC, operation: 'get_reporting_status' });
    const st = r.ok ? r.result.status : 'threw';
    record('sig/future_beyond_skew_rejected', '!verified', st, st !== 'verified',
      { skewToleranceSeconds: CLOCK_SKEW_TOLERANCE_SECONDS, detail: r.ok ? r.result : r.error });
  }
  // V6 unknown keyid (key never published in the JWKS)
  {
    const s = signed({ keyid: otherKey.kid, alg: otherKey.algorithm, privateKey: otherKey.privateKey },
      { nonce: 'nonce-unknown-key' });
    const r = await post(s.headers, s.body ?? BODY, { operation: 'get_reporting_status' });
    const st = r.ok ? r.result.status : 'threw';
    record('sig/unknown_keyid_rejected', '!verified', st, st !== 'verified',
      { detail: r.ok ? r.result : r.error });
  }
  // V7 unsigned request for an operation in capability.required_for
  {
    const r = await post({ 'content-type': 'application/json' }, BODY, { operation: 'get_reporting_status' });
    const st = r.ok ? r.result.status : 'threw';
    record('sig/unsigned_required_operation_rejected', '!verified', st, st !== 'verified',
      { detail: r.ok ? r.result : r.error });
  }
  // V8 unsigned request for an operation NOT in required_for -> unsigned, not an error
  {
    const r = await post({ 'content-type': 'application/json' }, BODY, { operation: 'list_creative_formats' });
    const st = r.ok ? r.result.status : 'threw';
    record('sig/unsigned_unrequired_operation_not_verified_but_not_error', 'unsigned-ish (!verified, no throw)',
      st, r.ok === true && st !== 'verified', { detail: r.ok ? r.result : r.error });
  }
  // V9 concurrent duplicate nonce race -> exactly one verified
  {
    const s = signed(signerKey, { nonce: 'nonce-race' });
    const results = await Promise.all(Array.from({ length: 8 }, () =>
      post(s.headers, s.body ?? BODY, { operation: 'get_reporting_status' })));
    const verified = results.filter(r => r.ok && r.result.status === 'verified').length;
    record('sig/concurrent_duplicate_nonce_exactly_one', 'verifiedCount=1',
      `verifiedCount=${verified}`, verified === 1, { attempts: results.length });
  }
  // V10 PG replay durability across verifier process restart (same durable DB)
  {
    const s = signed(signerKey, { nonce: 'nonce-durable' });
    const first = await post(s.headers, s.body ?? BODY, { operation: 'get_reporting_status' });
    await reap();
    child = await boot([reqKey.publicKey]);          // fresh process, same PostgreSQL
    const afterRestart = await post(s.headers, s.body ?? BODY, { operation: 'get_reporting_status' });
    const f = first.ok ? first.result.status : 'threw';
    const a = afterRestart.ok ? afterRestart.result.status : 'threw';
    record('sig/replay_survives_verifier_restart', 'first=verified,afterRestart!=verified',
      `first=${f},afterRestart=${a}`, f === 'verified' && a !== 'verified',
      { note: 'same durable PostgreSQL replay cache, new verifier process' });
  }

  // W1..W3 the SEPARATE legacy HMAC webhook utility - distinct from RFC 9421
  {
    const secret = 'probe-shared-secret';
    const ts = T0_SEC;
    const payload = JSON.stringify({ notification_type: 'reporting_ledger_changed' });
    const mac = crypto.createHmac('sha256', secret).update(String(ts)).update('.').update(Buffer.from(payload)).digest('hex');
    const good = verifyWebhookRequest({ secret, rawBody: payload,
      headers: { 'x-adcp-signature': `sha256=${mac}`, 'x-adcp-timestamp': String(ts) }, now: () => T0_SEC });
    record('hmac_webhook/valid_accepted', 'ok=true', `ok=${good.ok}`, good.ok === true, { detail: good });

    const badMac = verifyWebhookRequest({ secret, rawBody: payload,
      headers: { 'x-adcp-signature': `sha256=${'0'.repeat(64)}`, 'x-adcp-timestamp': String(ts) }, now: () => T0_SEC });
    record('hmac_webhook/wrong_mac_rejected', 'ok=false', `ok=${badMac.ok}`, badMac.ok === false,
      { reason: badMac.reason });

    const tampered = verifyWebhookRequest({ secret, rawBody: payload.replace('changed', 'ready'),
      headers: { 'x-adcp-signature': `sha256=${mac}`, 'x-adcp-timestamp': String(ts) }, now: () => T0_SEC });
    record('hmac_webhook/tampered_body_rejected', 'ok=false', `ok=${tampered.ok}`, tampered.ok === false,
      { reason: tampered.reason });
  }

  const report = {
    scope: 'real_separate_process_http_rfc9421_verifier_with_real_postgresql_replay_store',
    pin: { package: '@adcp/sdk', version: require('@adcp/sdk/package.json').version,
           adcpVersion: require('@adcp/sdk').ADCP_VERSION },
    runtime: { node: process.version, platform: process.platform, arch: process.arch },
    constants: {
      CLOCK_SKEW_TOLERANCE_SECONDS, MAX_SIGNATURE_WINDOW_SECONDS,
      ALLOWED_ALGS: Array.from(ALLOWED_ALGS || []),
      MANDATORY_COMPONENTS, WEBHOOK_MANDATORY_COMPONENTS,
    },
    distinction: {
      rfc9421: '@adcp/sdk/signing/server verifyRequestSignature - asymmetric JWKS, Signature-Input, replay store, content-digest',
      legacyHmac: '@adcp/sdk/webhooks verifyWebhookRequest - shared-secret HMAC-SHA256 over timestamp + "." + rawBody, header x-adcp-signature: sha256=<hex>',
    },
    deterministicClockBaseUnixSeconds: T0_SEC,
    binaryEncoding: BINARY_ENCODING,
    adcpVersionPin: ADCP_VERSION_PIN,
    totals: { vectors: rows.length, passed: rows.filter(r => r.pass).length, failed: rows.filter(r => !r.pass).length },
    vectors: rows,
  };
  fs.writeFileSync(process.argv[2], JSON.stringify(report, null, 2) + '\n');
  console.error(JSON.stringify(report.totals));
  process.exitCode = report.totals.failed === 0 ? 0 : 1;
}

main().catch(e => { console.error('HARNESS FAILURE', e); process.exitCode = 2; })
  .finally(async () => { await reap(); });
