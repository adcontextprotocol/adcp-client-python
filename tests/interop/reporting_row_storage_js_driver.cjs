#!/usr/bin/env node
/**
 * JavaScript half of the cross-SDK row storage interop test.
 *
 * Drives the shipped JavaScript row storage (`ReportingRowStorageV1`) against a
 * PostgreSQL schema and a filesystem bucket root shared with the Python SDK.
 *
 *   node reporting_row_storage_js_driver.cjs write --out js-written.json
 *   node reporting_row_storage_js_driver.cjs read  --in python-written.json
 *
 * Environment (flags override): ADCP_PG_URL, ADCP_PG_SCHEMA, ADCP_ROWS_ROOT,
 * ADCP_ROWS_NAMESPACE, ADCP_JS_SDK_DIR (a built adcp-client checkout, or a directory
 * whose node_modules holds @adcp/sdk and pg).
 *
 * `write` stores PostgreSQL-bound and object-bound row sets from the shared golden
 * vectors and prints the sets it wrote. `read` verifies every row set in `--in`
 * (written by Python) byte for byte through the JavaScript readers, and exits
 * non-zero on the first mismatch.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

const mode = process.argv[2];
const sdkDir = process.env.ADCP_JS_SDK_DIR;
if (!sdkDir) throw new Error('set ADCP_JS_SDK_DIR to a built adcp-client checkout');
const requireFromSdk = module => require(require.resolve(module, { paths: [sdkDir] }));
const sdkEntry = fs.existsSync(path.join(sdkDir, 'dist/lib/reporting/ledger/index.js'))
  ? path.join(sdkDir, 'dist/lib/reporting/ledger/index.js')
  : require.resolve('@adcp/sdk/reporting/ledger', { paths: [sdkDir] });
const ledger = require(sdkEntry);
// SDK 16 moved the storage implementations into their own workspace package.
const rowStorage = typeof ledger.ReportingRowStorageV1 === 'function'
  && typeof ledger.createFilesystemReportingRowObjectProviderV1 === 'function'
  ? ledger
  : requireFromSdk('@adcp/reporting-storage');
const canonicalize = fs.existsSync(path.join(sdkDir, 'dist/lib/utils/jcs.js'))
  ? require(path.join(sdkDir, 'dist/lib/utils/jcs.js')).canonicalize
  : requireFromSdk('@adcp/sdk').canonicalize;
const { Pool } = requireFromSdk('pg');

const url = arg('url', process.env.ADCP_PG_URL);
const schema = arg('schema', process.env.ADCP_PG_SCHEMA);
const root = arg('root', process.env.ADCP_ROWS_ROOT);
const namespace = arg('namespace', process.env.ADCP_ROWS_NAMESPACE ?? 'interop');
const fixturePath = arg('fixture', path.join(__dirname, '..', 'fixtures', 'reporting-row-storage-v1.json'));

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

function binding(rowSetId, rows, controlTotals) {
  const bytes = Buffer.from(
    canonicalize({
      reporting_revision_id: rowSetId,
      row_count: rows.length,
      control_totals: controlTotals,
      reporting_rows: rows,
    }),
    'utf8'
  );
  return { sha256: sha256(bytes), byteCount: bytes.byteLength, rowCount: rows.length };
}

function rowsOnly(rows) {
  const bytes = Buffer.from(canonicalize(rows), 'utf8');
  return { sha256: sha256(bytes), byteCount: bytes.byteLength, rowCount: rows.length };
}

async function main() {
  const pool = new Pool({ connectionString: url, options: `-c search_path="${schema}"` });
  const storage = new rowStorage.ReportingRowStorageV1(pool, {
    deploymentNamespace: namespace,
    bindings: {
      inline: { kind: 'postgres' },
      reports: {
        kind: 'object',
        provider: 'filesystem',
        location: { root: 'reports' },
        prefix: 'adcp-rows',
        compression: 'gzip',
      },
    },
    providers: { filesystem: rowStorage.createFilesystemReportingRowObjectProviderV1({ roots: { reports: root } }) },
  });
  await storage.ready();
  try {
    if (mode === 'write') {
      const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
      const written = [];
      for (const vector of fixture.vectors.filter(item => item.rows.length > 0)) {
        for (const [kind, bindingId] of [
          ['revision', 'inline'],
          ['revision', 'reports'],
          ['adjustment', 'inline'],
          ['adjustment', 'reports'],
        ]) {
          const rowSetId = `rrev_js_${vector.id}_${kind}_${bindingId}`;
          const controlTotals = kind === 'revision' ? vector.envelope.control_totals : undefined;
          const bound = kind === 'revision' ? binding(rowSetId, vector.rows, controlTotals) : rowsOnly(vector.rows);
          const prepared = await storage.prepare({
            rowSetId,
            rowSetKind: kind,
            accountId: 'acct_interop',
            obligationId: 'robl_interop',
            rows: vector.rows,
            ...(kind === 'revision' ? { finality: 'snapshot', controlTotals } : {}),
            periodStart: '2026-10-01T00:00:00Z',
            sourceTimezone: 'America/Los_Angeles',
            binding: bound,
            bindingId,
          });
          let locators;
          if (bindingId === 'reports') {
            locators = await storage.upload(prepared, 'js-interop-intent', async () => {});
          }
          const client = await pool.connect();
          try {
            await client.query('BEGIN');
            await storage.writeInTransaction(client, prepared, locators);
            await client.query('COMMIT');
          } catch (error) {
            await client.query('ROLLBACK');
            throw error;
          } finally {
            client.release();
          }
          written.push({
            row_set_id: rowSetId,
            row_set_kind: kind,
            binding_id: bindingId,
            account_id: 'acct_interop',
            obligation_id: 'robl_interop',
            digest_profile: kind === 'revision' ? 'revision_envelope_v1' : 'rows_v1',
            content_sha256: bound.sha256,
            content_size_bytes: bound.byteCount,
            control_totals: controlTotals ?? null,
            rows: vector.rows,
          });
        }
      }
      fs.writeFileSync(arg('out'), JSON.stringify(written, null, 2));
      console.log(`js wrote ${written.length} row sets`);
    } else if (mode === 'read') {
      const expected = JSON.parse(fs.readFileSync(arg('in'), 'utf8'));
      let verified = 0;
      for (const item of expected) {
        const handle = await storage.find(item.row_set_id, item.account_id);
        if (!handle) throw new Error(`row set ${item.row_set_id} is missing`);
        if (handle.contentSha256 !== item.content_sha256 || handle.digestProfile !== item.digest_profile) {
          throw new Error(`row set ${item.row_set_id} has a different binding`);
        }
        const rows = await storage.readAll(handle, item.control_totals ?? undefined);
        if (canonicalize(rows) !== canonicalize(item.rows)) throw new Error(`rows differ for ${item.row_set_id}`);
        // Paged reads verify segment by segment too.
        const page = await storage.readPage(handle, 0, Math.min(500, item.rows.length));
        if (canonicalize(page) !== canonicalize(item.rows.slice(0, 500))) {
          throw new Error(`first page differs for ${item.row_set_id}`);
        }
        verified += 1;
      }
      console.log(`js verified ${verified} row sets`);
    } else {
      throw new Error('usage: write|read');
    }
  } finally {
    await pool.end();
  }
}

main().catch(error => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
