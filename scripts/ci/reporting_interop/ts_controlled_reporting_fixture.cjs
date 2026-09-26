'use strict';

/*
 * Controlled-clock Reliable Reporting fixture for the installed rc.45 SDK.
 *
 * This implements the public ReportingConsumerStatusLedgerStore boundary and
 * the read methods consumed by createReportingStatusHandler.  It deliberately
 * is not a PostgreSQL substitute: durable/store concurrency lanes continue to
 * use PostgresReportingLedgerStore.  Its only purpose is to drive storyboard
 * instants that the PostgreSQL store (correctly) takes from the database clock.
 */

const { createHash } = require('node:crypto');

const PERIOD_START = '2026-08-01T00:00:00.000Z';
const PERIOD_END = '2026-08-01T01:00:00.000Z';
const EXPECTED_AT = '2026-08-01T02:00:00.000Z';
const RECOVERY_DEADLINE = '2026-08-01T04:00:00.000Z';
const DELAYED_AT = '2026-08-01T02:05:00.000Z';
const ACTION_REQUIRED_AT = '2026-08-01T04:05:00.000Z';
const DIGEST = '5199a776b3a99915f084e16c921b2e501fcedd949017236d51a2303c5c2f5cd1';

function clone(value) {
  return structuredClone(value);
}

function sha(value) {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

function sourceRequest(source) {
  return source.redactedReportingSourceRequestV1();
}

function configuration(accountId, source) {
  const request = sourceRequest(source);
  return {
    configurationId: `configuration-${accountId}`,
    account: { account_id: accountId },
    sourceScope: clone(request.sourceScope),
    delivery_config_id: `delivery-${accountId}`,
    delivery_config_version: 1,
    offeringId: request.offeringId,
    report_definition_id: request.report_definition_id,
    feedPurpose: 'analytics',
    requiredFinality: 'snapshot',
    requestedMetrics: clone(request.requestedMetrics),
    requestedDimensions: clone(request.requestedDimensions),
    constituents: clone(request.coverage.constituents),
    mediaBuyIds: clone(request.coverage.mediaBuyIds),
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
    sourceSettings: clone(request.sourceSettings),
    contract: clone(request.contract),
    installedAt: PERIOD_START,
    // Freeze this fixture generation to its single storyboard period. Later
    // elapsed periods belong to a successor configuration in real sellers.
    supersededAt: PERIOD_END,
    semanticFingerprint: `sha256:${'a'.repeat(64)}`,
  };
}

function obligation(accountId, source) {
  const request = sourceRequest(source);
  const config = configuration(accountId, source);
  return {
    reporting_obligation_id: `obligation-${accountId}`,
    configurationId: config.configurationId,
    account: { account_id: accountId },
    sourceScope: clone(request.sourceScope),
    delivery_config_id: config.delivery_config_id,
    delivery_config_version: config.delivery_config_version,
    offeringId: config.offeringId,
    report_definition_id: config.report_definition_id,
    feedPurpose: 'analytics',
    requiredFinality: 'snapshot',
    periodOrdinal: 0,
    period: { start: PERIOD_START, end: PERIOD_END, sourceTimezone: 'UTC' },
    schedule: clone(config.schedule),
    scopeResolvedAt: PERIOD_END,
    coverage: {
      status: 'full',
      evaluatedAt: PERIOD_END,
      mediaBuyIds: clone(request.coverage.mediaBuyIds),
      fullyCoveredMediaBuyIds: clone(request.coverage.mediaBuyIds),
      partiallyCoveredMediaBuyIds: [],
      unsupportedMediaBuyIds: [],
      unknownMediaBuyIds: [],
    },
    requestedMetrics: clone(request.requestedMetrics),
    requestedDimensions: clone(request.requestedDimensions),
    constituents: clone(request.coverage.constituents),
    mediaBuyIds: clone(request.coverage.mediaBuyIds),
    sourceSettings: clone(request.sourceSettings),
    contract: clone(request.contract),
    expectedAt: EXPECTED_AT,
    recoveryDeadlineAt: RECOVERY_DEADLINE,
    publicationOffsets: [],
    nextAttemptAt: EXPECTED_AT,
    attemptCount: 0,
    state: 'pending',
    semanticFingerprint: `sha256:${'b'.repeat(64)}`,
    createdAt: PERIOD_END,
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

function revision(accountId, source, options = {}) {
  const {
    number = 1,
    createdAt = ACTION_REQUIRED_AT,
    supersedes,
    id: fixedId,
    rows = [],
    controlTotals = [],
    digest = DIGEST,
  } = options;
  const accountObligation = obligation(accountId, source);
  const id = fixedId ?? (number === 1
    ? `revision-${accountId}-1`
    : `revision-${accountId}-${number}`);
  return {
    reporting_revision_id: id,
    reporting_obligation_id: `obligation-${accountId}`,
    revisionNumber: number,
    finality: 'snapshot',
    kind: 'snapshot',
    ...(supersedes ? { supersedes_reporting_revision_id: supersedes } : {}),
    binding: {
      algorithm: 'rfc8785_jcs_v1',
      sha256: digest,
      byteCount: Buffer.byteLength(JSON.stringify({ reporting_revision_id: id, row_count: rows.length, control_totals: controlTotals, reporting_rows: rows })),
      rowCount: rows.length,
    },
    rows: clone(rows),
    observedAt: createdAt,
    dataThrough: PERIOD_END,
    sourceReadCutoffAt: createdAt,
    createdAt,
    manifest: {},
    sourcePublicationId: `publication-${id}`,
    wireRevision: {
      account_id: accountId,
      reporting_revision_id: id,
      revision_content_sha256: digest,
      report_definition_id: accountObligation.report_definition_id,
      report_definition_uri: accountObligation.contract.reportDefinitionUri,
      report_definition_sha256: accountObligation.contract.reportDefinitionSha256,
      reporting_profile: accountObligation.contract.reportingProfile,
      schema_version: accountObligation.contract.schemaVersion,
      schema_uri: accountObligation.contract.schemaUri,
      schema_sha256: accountObligation.contract.schemaSha256,
      schema_dialect: accountObligation.contract.schemaDialect,
      schema_ref_policy: accountObligation.contract.schemaRefPolicy,
      media_buy_ids: clone(accountObligation.mediaBuyIds),
      coverage: {
        status: accountObligation.coverage.status,
        evaluated_at: accountObligation.coverage.evaluatedAt,
        media_buy_ids: clone(accountObligation.coverage.mediaBuyIds),
        fully_covered_media_buy_ids: clone(accountObligation.coverage.fullyCoveredMediaBuyIds),
        partially_covered_media_buy_ids: [],
        unsupported_media_buy_ids: [],
        unknown_media_buy_ids: [],
        package_ids: [],
        covered_package_ids: [],
        unsupported_package_ids: [],
        unknown_package_ids: [],
        limitations: [],
      },
      period: {
        start: accountObligation.period.start,
        end: accountObligation.period.end,
        source_timezone: accountObligation.period.sourceTimezone,
      },
      finality: 'snapshot',
      observed_at: createdAt,
      data_through: PERIOD_END,
      data_through_precision: 'exact',
      ...(supersedes ? { supersedes_reporting_revision_id: supersedes } : {}),
      row_count: rows.length,
      control_totals: clone(controlTotals),
      created_at: createdAt,
    },
  };
}

class ControlledReportingStore {
  constructor(api, accountId) {
    this.api = api;
    this.accountId = accountId;
    this.ledgerAsOf = EXPECTED_AT;
    this.configurations = [configuration(accountId, api.source)];
    this.obligations = [obligation(accountId, api.source)];
    this.revisions = [];
    this.adjustments = [];
    this.issues = [];
    this.consumerStatements = [];
    this.consumerStatusBatches = new Map();
    this.snapshots = new Map();
  }

  setClock(value) {
    if (!Number.isFinite(Date.parse(value))) throw new TypeError('fixture clock must be an RFC 3339 instant');
    this.ledgerAsOf = new Date(value).toISOString();
  }

  async listConfigurations(accountId) {
    return clone(this.configurations.filter(value => value.account.account_id === accountId));
  }

  async getObligation(id, accountId) {
    return clone(this.obligations.find(value =>
      value.reporting_obligation_id === id && (!accountId || value.account.account_id === accountId)) ?? null);
  }

  async getRevisionMetadata(id, accountId) {
    const value = this.revisions.find(item =>
      item.reporting_revision_id === id && item.wireRevision.account_id === accountId);
    return clone(value ?? null);
  }

  async getRevision(id, accountId) {
    return this.getRevisionMetadata(id, accountId);
  }

  async listRevisionMetadata(obligationId, accountId) {
    return clone(this.revisions.filter(value =>
      value.reporting_obligation_id === obligationId && value.wireRevision.account_id === accountId));
  }

  scopedStatuses(consumerId) {
    if (!consumerId) return [];
    return clone(this.consumerStatements.filter(value =>
      value.account_id === this.accountId && value.consumerId === consumerId));
  }

  chainKey(status) {
    return [
      status.delivery_config_id,
      status.delivery_config_version,
      status.report_definition_id,
      status.period.start,
      status.period.end,
      status.period.source_timezone,
    ].join('\u0000');
  }

  currentLeaf(status, consumerId) {
    const chain = this.consumerStatements.filter(value =>
      value.account_id === this.accountId && value.consumerId === consumerId &&
      this.chainKey(value) === this.chainKey(status));
    const superseded = new Set(chain.map(value => value.supersedes_reporting_status_id).filter(Boolean));
    return chain.find(value => !superseded.has(value.reporting_status_id));
  }

  async getConsumerStatusBatchReplay({ account_id, consumerId, idempotencyKey, requestFingerprint }) {
    if (account_id !== this.accountId) return null;
    const key = [account_id, consumerId, idempotencyKey].join('\u0000');
    const prior = this.consumerStatusBatches.get(key);
    if (!prior) return null;
    if (prior.requestFingerprint !== requestFingerprint) {
      throw new this.api.ledger.ReportingConsumerStatusConflictError('idempotency key was reused');
    }
    return clone(prior.results);
  }

  async syncConsumerStatusBatch(input) {
    const batchKey = [input.account_id, input.consumerId, input.idempotencyKey].join('\u0000');
    const prior = this.consumerStatusBatches.get(batchKey);
    if (prior) {
      if (prior.requestFingerprint !== input.requestFingerprint) {
        throw new this.api.ledger.ReportingConsumerStatusConflictError('idempotency key was reused');
      }
      return clone(prior.results);
    }
    const idCounts = new Map();
    const chainCounts = new Map();
    for (const entry of input.entries) {
      const id = entry.status?.reporting_status_id ?? entry.reporting_status_id;
      idCounts.set(id, (idCounts.get(id) ?? 0) + 1);
      if (entry.status) {
        const key = this.chainKey(entry.status);
        chainCounts.set(key, (chainCounts.get(key) ?? 0) + 1);
      }
    }
    const results = [];
    for (const entry of input.entries) {
      const id = entry.status?.reporting_status_id ?? entry.reporting_status_id;
      const fail = (errorCode, safeMessage) => ({
        inserted: false,
        reporting_status_id: id,
        errorCode,
        safeMessage,
        ...(entry.validationField ? { errorField: entry.validationField } : {}),
        ...(entry.validationKeyword ? { errorKeyword: entry.validationKeyword } : {}),
      });
      if (entry.validationError) {
        results.push(fail('VALIDATION_ERROR', entry.validationError));
        continue;
      }
      if (idCounts.get(id) > 1 || chainCounts.get(this.chainKey(entry.status)) > 1) {
        results.push(fail('IDEMPOTENCY_CONFLICT', 'Reporting status batch names the same chain twice'));
        continue;
      }
      const existing = this.consumerStatements.find(value =>
        value.reporting_status_id === id && value.consumerId === input.consumerId);
      const candidate = { ...entry.status, recorded_at: this.ledgerAsOf };
      if (existing) {
        const { recorded_at: _left, ...left } = existing;
        const { recorded_at: _right, ...right } = candidate;
        if (JSON.stringify(left) !== JSON.stringify(right)) {
          results.push(fail('IDEMPOTENCY_CONFLICT', 'Reporting status ID was reused with different content'));
        } else {
          results.push({ inserted: false, value: clone(existing) });
        }
        continue;
      }
      const leaf = this.currentLeaf(entry.status, input.consumerId);
      if ((leaf?.reporting_status_id) !== entry.status.supersedes_reporting_status_id) {
        results.push(fail('IDEMPOTENCY_CONFLICT', 'Reporting status did not name the current leaf'));
        continue;
      }
      this.consumerStatements.push(clone(candidate));
      results.push({ inserted: true, value: clone(candidate) });
    }
    this.consumerStatusBatches.set(batchKey, { requestFingerprint: input.requestFingerprint, results: clone(results) });
    return clone(results);
  }

  async createSnapshot(query) {
    if (query.account_id !== this.accountId) throw new Error('account unavailable');
    const snapshot = {
      snapshotId: `snapshot-${this.accountId}-${this.snapshots.size}`,
      ledgerAsOf: this.ledgerAsOf,
      changesCheckpoint: this.ledgerAsOf,
      queryFingerprint: sha(query),
      query: clone(query),
      configurations: clone(this.configurations),
      coverageOrdinals: this.obligations.map(value => ({
        configurationId: value.configurationId,
        periodOrdinal: value.periodOrdinal,
      })),
      obligations: clone(this.obligations),
      revisions: clone(this.revisions),
      adjustments: clone(this.adjustments),
      consumerStatuses: this.scopedStatuses(query.consumer_id),
      issues: clone(this.issues),
    };
    this.snapshots.set(snapshot.snapshotId, snapshot);
    return clone(snapshot);
  }

  async readSnapshotPage(snapshotId, accountId, cursor, limit) {
    const snapshot = this.snapshots.get(snapshotId);
    if (!snapshot || accountId !== this.accountId) {
      throw new this.api.ledger.ReportingLedgerSnapshotUnavailableError();
    }
    const items = snapshot.obligations.flatMap(item => [
      ...(snapshot.query.view === 'revision' ? [] : [{ kind: 'obligation', value: item }]),
      ...snapshot.revisions.filter(value => value.reporting_obligation_id === item.reporting_obligation_id)
        .map(value => ({ kind: 'revision', value })),
    ]);
    const offset = cursor ? JSON.parse(Buffer.from(cursor, 'base64url')).offset : 0;
    const selected = items.slice(offset, offset + limit);
    const nextOffset = offset + selected.length;
    return {
      snapshot: clone(snapshot),
      obligations: selected.filter(value => value.kind === 'obligation').map(value => clone(value.value)),
      revisions: selected.filter(value => value.kind === 'revision').map(value => clone(value.value)),
      adjustments: [],
      consumerStatuses: clone(snapshot.consumerStatuses),
      totalCount: items.length,
      offset,
      limit,
      hasMore: nextOffset < items.length,
      ...(nextOffset < items.length
        ? { nextCursor: Buffer.from(JSON.stringify({ offset: nextOffset })).toString('base64url') }
        : {}),
    };
  }

  prepare() {
    const value = this.obligations[0];
    return {
      account_id: this.accountId,
      delivery_config_id: value.delivery_config_id,
      delivery_config_version: value.delivery_config_version,
      expected_at: value.expectedAt,
      recovery_deadline: value.recoveryDeadlineAt,
      reporting_obligation_id: value.reporting_obligation_id,
      period: clone(value.period),
      resolved_configuration: {
        delivery_config_id: value.delivery_config_id,
        delivery_config_version: value.delivery_config_version,
        report_definition_id: value.report_definition_id,
      },
    };
  }

  advance(targetHealth) {
    this.setClock(targetHealth === 'delayed' ? DELAYED_AT : ACTION_REQUIRED_AT);
    return { target_health: targetHealth, ledger_as_of: this.ledgerAsOf };
  }

  publishZero() {
    if (this.revisions.length === 0) this.revisions.push(revision(this.accountId, this.api.source));
    const value = this.revisions.at(-1);
    return {
      finality: value.finality,
      reporting_obligation_id: value.reporting_obligation_id,
      reporting_revision_id: value.reporting_revision_id,
      revision_content_sha256: value.binding.sha256,
      row_count: value.binding.rowCount,
    };
  }

  publishNonempty() {
    if (this.revisions.length === 0) {
      const rows = exactRows();
      const id = 'reporting-revision.ecc62efa00946aa1e2788ad9';
      const controlTotals = [{ name: 'impressions', value: '5', value_type: 'integer', unit: 'impressions' }];
      const bytes = this.api.sdk.canonicalize({
        reporting_revision_id: id,
        row_count: rows.length,
        control_totals: controlTotals,
        reporting_rows: rows,
      });
      const digest = createHash('sha256').update(bytes).digest('hex');
      if (digest !== DIGEST) throw new Error(`literal reporting vector digest changed: ${digest}`);
      this.revisions.push(revision(this.accountId, this.api.source, { id, rows, controlTotals, digest }));
    }
    const value = this.revisions.at(-1);
    return {
      finality: value.finality,
      reporting_obligation_id: value.reporting_obligation_id,
      reporting_revision_id: value.reporting_revision_id,
      revision_content_sha256: value.binding.sha256,
      row_count: value.binding.rowCount,
    };
  }

  restate(withinGrace) {
    if (this.revisions.length === 0) this.publishZero();
    if (this.revisions.length === 1) {
      this.revisions.push(revision(this.accountId, this.api.source, {
        number: 2,
        createdAt: '2026-08-01T04:10:00.000Z',
        supersedes: this.revisions[0].reporting_revision_id,
      }));
    }
    this.setClock(withinGrace === false ? '2026-08-01T05:15:00.000Z' : '2026-08-01T04:30:00.000Z');
    return {
      reporting_revision_id: this.revisions.at(-1).reporting_revision_id,
      supersedes_reporting_revision_id: this.revisions[0].reporting_revision_id,
      within_grace: withinGrace !== false,
      ledger_as_of: this.ledgerAsOf,
    };
  }
}

function createControlledController(api) {
  const stores = new Map();
  const allowed = new Set([
    'reporting_core_lab',
    'reporting_core_nonempty_lab',
    'reporting_consumer_omission_lab',
    'reporting_consumer_revision_lab',
    'reporting_consumer_silence_lab',
    'reporting_consumer_content_lab',
    'reporting_consumer_mismatch_lab',
  ]);
  const getStore = accountId => {
    if (!stores.has(accountId)) stores.set(accountId, new ControlledReportingStore(api, accountId));
    return stores.get(accountId);
  };
  const factory = {
    scenarios: ['reporting_core_lifecycle_probe'],
    createStore(input) {
      const accountId = input?.account?.account_id;
      if (!allowed.has(accountId) || input?.account?.sandbox !== true) {
        throw new api.server.TestControllerError('FORBIDDEN', 'Controller requires an allowed sandbox account');
      }
      const store = getStore(accountId);
      return {
        async reportingCoreLifecycleProbe(params) {
          switch (params.operation) {
            case 'prepare':
              return { success: true, simulated: store.prepare() };
            case 'advance_time':
              return { success: true, simulated: store.advance(params.target_health) };
            case 'publish_zero_row':
              return { success: true, simulated: store.publishZero() };
            case 'publish_nonempty':
              return { success: true, simulated: store.publishNonempty() };
            case 'omit_obligation': {
              const prepared = store.prepare();
              store.obligations = [];
              return { success: true, simulated: {
                account_id: accountId,
                expected_reporting_obligation_id: prepared.reporting_obligation_id,
                omitted_period: clone(prepared.period),
                resolved_configuration: clone(prepared.resolved_configuration),
              } };
            }
            case 'restate_after_received':
              return { success: true, simulated: store.restate(params.within_grace) };
            case 'advance_past_status_deadline':
              store.setClock(ACTION_REQUIRED_AT);
              return { success: true, simulated: { ledger_as_of: store.ledgerAsOf } };
            case 'advance_past_escalation':
              store.setClock('2026-08-01T04:45:00.000Z');
              return { success: true, simulated: { ledger_as_of: store.ledgerAsOf } };
            default:
              throw new api.server.TestControllerError('INVALID_PARAMS', `Unsupported operation: ${String(params.operation)}`);
          }
        },
      };
    },
  };
  return { factory, getStore, stores };
}

module.exports = {
  ACTION_REQUIRED_AT,
  ControlledReportingStore,
  DELAYED_AT,
  DIGEST,
  EXPECTED_AT,
  PERIOD_END,
  PERIOD_START,
  RECOVERY_DEADLINE,
  createControlledController,
};
