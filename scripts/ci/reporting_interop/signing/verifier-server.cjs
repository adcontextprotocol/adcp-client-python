'use strict';
// Real separate-process RFC 9421 verifier over real HTTP, backed by a real
// PostgreSQL replay store. Verifies RAW request bytes, never a re-serialized body.
const http = require('node:http');
const { Pool } = require('pg');
const {
  verifyRequestSignature, StaticJwksResolver, PostgresReplayStore, InMemoryRevocationStore,
} = require('@adcp/sdk/signing/server');

const PORT = Number(process.env.PROBE_PORT);
const PG_URL = process.env.PROBE_PG_URL;
const JWKS = JSON.parse(process.env.PROBE_JWKS);           // public JWKs
const pool = new Pool({ connectionString: PG_URL, max: 4 });
const jwks = new StaticJwksResolver(JWKS);
const replayStore = new PostgresReplayStore(pool, { tableName: 'adcp_replay_cache' });
const revocationStore = new InMemoryRevocationStore();
const capability = {
  supported: true,
  covers_content_digest: 'always',
  required_for: ['get_reporting_status', 'sync_reporting_receipts'],
};

const server = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', async () => {
    const raw = Buffer.concat(chunks);
    // Verifier clock is injected per-request by the probe. This is the SDK's own
    // documented `now` option, not a global clock patch.
    const injected = req.headers['x-probe-now-sec'];
    const nowFn = injected === undefined ? undefined : () => Number(injected);
    const operation = req.headers['x-probe-operation'] || undefined;
    const request = {
      method: req.method,
      url: `http://127.0.0.1:${PORT}${req.url}`,
      headers: req.headers,
      body: raw.toString('utf8'),
    };
    let out;
    try {
      const result = await verifyRequestSignature(request, {
        capability, jwks, replayStore, revocationStore,
        ...(nowFn ? { now: nowFn } : {}),
        ...(operation ? { operation } : {}),
        adcpVersion: '3.2.0-rc.4',
      });
      out = { ok: true, result, rawBodySha256: require('node:crypto').createHash('sha256').update(raw).digest('hex') };
    } catch (e) {
      out = { ok: false, error: { name: e.name, code: e.code, message: e.message } };
    }
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(out));
  });
});
server.listen(PORT, '127.0.0.1', () => process.stdout.write('LISTENING\n'));
const shutdown = async () => {
  server.close();
  try { await pool.end(); } catch {}
  process.exit(0);
};
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
