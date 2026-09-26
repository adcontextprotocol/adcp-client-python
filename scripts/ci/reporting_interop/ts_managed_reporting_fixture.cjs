'use strict';

/*
 * PostgreSQL-backed Managed Delivery / Reconciled Billing controller fixture.
 *
 * This module deliberately uses only public rc.45 exports. Controller setup
 * creates immutable Core facts through the public stores, while delivery,
 * resource reads, revocation, status projection, receipts, and adjustment
 * readback traverse the public runtime/handler contracts that the storyboards
 * exercise. It is not a virtual-clock fixture and does not claim DST coverage.
 */

const { createHash } = require('node:crypto');

const ACCOUNT_ID = 'reporting_core_lab';
const CONSUMER_ID = 'https://buyer.example.test/adcp';
const CANONICAL_DIGEST = {
  algorithm: 'sha256',
  value: 'bcd079902f3c8edb4315dbbdaf9b4e37f6fd5af33c80d1fe6ac8c655581342d4',
  canonicalization_id: 'billing-rows-v1',
  canonicalization_uri: 'https://test-agent.adcontextprotocol.org/reporting/canonicalization/billing-rows-v1.json',
  canonicalization_sha256: 'f98d55f8b7ad49c058b17f04630576c6b27b3564090b838e49834dd7714cdefa',
};

function iso(value) {
  return new Date(value).toISOString();
}

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

function deliveryOffering(mode) {
  const billing = mode === 'billing';
  return {
    offering_id: billing ? 'billing-files-v1' : 'managed-files-v1',
    feed_purpose: billing ? 'billing' : 'analytics',
    report_definition_id: billing ? 'billing-v1' : 'managed-analytics-v1',
    report_definition_uri: 'https://schemas.fixture.example/report-definition.json',
    report_definition_sha256: 'd'.repeat(64),
    reporting_profile: {
      id: billing ? 'billing-v1' : 'managed-analytics-v1',
      version: '1.0',
      schema_uri: 'https://schemas.fixture.example/reporting-profile.json',
      schema_sha256: 'e'.repeat(64),
      schema_dialect: 'https://json-schema.org/draft/2020-12/schema',
      schema_ref_policy: 'local_fragment_only',
      grain: 'media_buy/day',
      primary_keys: ['media_buy_id'],
      ...(billing ? {
        canonicalization_id: CANONICAL_DIGEST.canonicalization_id,
        canonicalization_contract_version: '1.0',
        canonicalization_media_type: 'application/vnd.adcp.reporting-canonicalization+json',
        canonicalization_uri: CANONICAL_DIGEST.canonicalization_uri,
        canonicalization_sha256: CANONICAL_DIGEST.canonicalization_sha256,
      } : {}),
    },
    schedule: { period_duration: 'PT30M', alignment: 'utc', delivery_sla: 'PT0S' },
    supported_finality: ['official'],
    reconciliation_mode: billing ? 'consumer_receipt' : 'delivery_only',
    method: {
      pattern: 'file_transfer',
      transport: 'fixture_object_store',
      orchestration: 'producer_managed',
      destination_modes: ['existing'],
      provider: { domain: 'fixture.example' },
      format: 'jsonl',
    },
  };
}

function makeProvider() {
  const resources = new Map();
  const grants = new Set();
  const events = [];
  return {
    resources,
    grants,
    events,
    adapter: {
      verificationProfiles: ['canonical_digest'],
      revocationFencesDeliveryGenerations: true,
      async deliver(input) {
        const key = `${input.binding.destination_ref}:${input.binding.authorization_generation}`;
        const resourceRef = `resource:${input.materialization.reporting_materialization_id}`;
        const bytes = Buffer.from(input.revision.rows.map(row => JSON.stringify(row)).join('\n') + '\n', 'utf8');
        grants.add(key);
        resources.set(resourceRef, { bytes, key });
        events.push({ kind: 'deliver', resource_ref: resourceRef, destination_ref: input.binding.destination_ref });
        return {
          status: 'available',
          resource: {
            resource_ref: resourceRef,
            kind: 'manifest',
            location: `fixture.reports.${input.revision.reporting_revision_id}.manifest`,
            manifest_version: '1.0',
            manifest_sha256: sha256(bytes),
            immutability: 'immutable_location',
            expires_at: iso(Date.now() + 31 * 86_400_000),
          },
          verification: {
            verified_at: iso(Date.now()),
            verification_path: 'representative_consumer',
            verification_profile: 'canonical_digest',
            row_count: input.revision.wireRevision.row_count,
            control_totals: structuredClone(input.revision.wireRevision.control_totals),
            physical_checksums: [{ object_ref: 'rows.jsonl', algorithm: 'sha256', value: sha256(bytes) }],
            canonical_content_digest: structuredClone(input.revision.wireRevision.canonical_content_digest),
          },
        };
      },
      async read(input) {
        const found = resources.get(input.resource.resource_ref);
        if (!found || !grants.has(found.key)) throw new Error('provider grant is unavailable');
        if (found.bytes.byteLength > input.maxBytes) throw new Error('provider object exceeds the requested limit');
        events.push({ kind: 'read', resource_ref: input.resource.resource_ref });
        return Uint8Array.from(found.bytes);
      },
      async revoke({ authorization }) {
        const key = `${authorization.destination_ref}:${authorization.generation}`;
        grants.delete(key);
        events.push({ kind: 'revoke', destination_ref: authorization.destination_ref });
      },
    },
  };
}

async function createManagedReportingFixture(api, pool, { mode }) {
  if (!['managed', 'billing'].includes(mode)) throw new Error(`unsupported fixture mode: ${String(mode)}`);
  await pool.query(api.ledger.REPORTING_LEDGER_MIGRATION);
  await pool.query(api.ledger.REPORTING_LEDGER_FINALITY_WRITER_FENCE_MIGRATION);
  await pool.query(api.ledger.REPORTING_MANAGED_DELIVERY_MIGRATION);

  const core = new api.ledger.PostgresReportingLedgerStore(pool, {
    acknowledgeIsolatedDatabase: true,
    managedDelivery: true,
  });
  const managed = new api.ledger.PostgresReportingManagedDeliveryStore(pool, {
    evidenceRetentionDays: 90,
    statusRetentionDays: 90,
  });
  const provider = makeProvider();
  const offering = deliveryOffering(mode);
  const runtime = await api.ledger.createReportingManagedDeliveryRuntime({
    coreStore: core,
    store: managed,
    adapter: provider.adapter,
    offerings: [offering],
    automatedRecoveryWindowSeconds: 60,
    statusRetentionDays: 90,
    resourceRetentionDays: 30,
    authorizationRevocationSeconds: 60,
    resolveConsumerId: context => context.consumer_id,
  });
  const context = { account: { account_id: ACCOUNT_ID }, consumer_id: CONSUMER_ID };
  const state = {
    prepared: false,
    readinessNotificationSuppressed: false,
    adjustments: [],
  };

  async function status(view = 'periods', extra = {}) {
    return runtime.getReportingStatus({ account: { account_id: ACCOUNT_ID }, view, ...extra }, context);
  }

  async function prepare() {
    if (state.prepared) return state;
    const nowMs = Date.now();
    const now = iso(nowMs);
    // Keep exactly one elapsed period. Unfiltered periods reads fail closed if
    // the configuration implies another elapsed obligation that the fixture
    // did not persist, so the next 30-minute boundary remains in the future.
    const period = { start: iso(nowMs - 1_860_000), end: iso(nowMs - 60_000) };
    const billing = mode === 'billing';
    const prefix = billing ? 'billing' : 'managed';
    const destinationRef = `destination-${prefix}-generation-1`;
    const configuration = {
      configurationId: `configuration-${prefix}-1`,
      account: { account_id: ACCOUNT_ID },
      sourceScope: { warehouse: `fixture-${prefix}` },
      delivery_config_id: `${prefix}-files`,
      delivery_config_version: 1,
      offeringId: offering.offering_id,
      report_definition_id: offering.report_definition_id,
      feedPurpose: offering.feed_purpose,
      requiredFinality: 'official',
      ...(billing ? {
        canonicalization: {
          id: CANONICAL_DIGEST.canonicalization_id,
          uri: CANONICAL_DIGEST.canonicalization_uri,
          sha256: CANONICAL_DIGEST.canonicalization_sha256,
          primaryKeys: ['media_buy_id'],
        },
      } : {}),
      requestedMetrics: billing ? ['impressions', 'spend'] : ['impressions'],
      requestedDimensions: ['media_buy_id'],
      constituents: [],
      mediaBuyIds: ['buy-1', 'buy-2'],
      sourceTimezone: 'UTC',
      schedule: {
        anchor: period.start,
        periodMilliseconds: 1_800_000,
        deliverySlaMilliseconds: 0,
        recoveryWindowMilliseconds: 60_000,
      },
      sourceSettings: {},
      contract: { reportingProfile: offering.reporting_profile.id },
      installedAt: period.start,
      semanticFingerprint: `configuration-${prefix}-fingerprint`,
    };
    await core.putConfiguration(configuration);
    await managed.authorizeDestination({
      account_id: ACCOUNT_ID,
      destination_ref: destinationRef,
      generation: 1,
      authorized_at: now,
    });
    const binding = api.ledger.reportingManagedDeliveryBindingV1({
      configurationId: configuration.configurationId,
      account_id: ACCOUNT_ID,
      delivery_config_id: configuration.delivery_config_id,
      delivery_config_version: 1,
      destination_ref: destinationRef,
      authorization_generation: 1,
      feed_purpose: offering.feed_purpose,
      method: 'file_transfer',
      transport: 'fixture_object_store',
      verification_profile: 'canonical_digest',
      reconciliation_mode: billing ? 'consumer_receipt' : 'delivery_only',
      resource_retention_days: 30,
      created_at: now,
    });
    await managed.installBinding(binding);
    const obligation = {
      reporting_obligation_id: `obligation-${prefix}-1`,
      configurationId: configuration.configurationId,
      account: configuration.account,
      sourceScope: configuration.sourceScope,
      delivery_config_id: configuration.delivery_config_id,
      delivery_config_version: 1,
      offeringId: configuration.offeringId,
      report_definition_id: configuration.report_definition_id,
      feedPurpose: configuration.feedPurpose,
      requiredFinality: 'official',
      periodOrdinal: 0,
      period: { ...period, sourceTimezone: 'UTC' },
      schedule: configuration.schedule,
      scopeResolvedAt: period.end,
      coverage: {
        status: 'full', evaluatedAt: period.end,
        mediaBuyIds: ['buy-1', 'buy-2'], fullyCoveredMediaBuyIds: ['buy-1', 'buy-2'],
        partiallyCoveredMediaBuyIds: [], unsupportedMediaBuyIds: [], unknownMediaBuyIds: [],
      },
      requestedMetrics: configuration.requestedMetrics,
      requestedDimensions: configuration.requestedDimensions,
      constituents: [],
      mediaBuyIds: configuration.mediaBuyIds,
      sourceSettings: {},
      contract: configuration.contract,
      expectedAt: period.end,
      recoveryDeadlineAt: iso(Date.parse(period.end) + 60_000),
      publicationOffsets: [], nextAttemptAt: period.end, attemptCount: 0, state: 'pending',
      semanticFingerprint: `obligation-${prefix}-fingerprint`, createdAt: now,
    };
    await core.putObligation(obligation);
    const lease = await core.claimObligation({
      owner: `${prefix}-core-worker`, now, leaseMilliseconds: 600_000, account_id: ACCOUNT_ID,
    });
    if (!lease) throw new Error('public Core store did not lease the prepared obligation');
    const rows = [
      { media_buy_id: 'buy-1', impressions: 2, ...(billing ? { spend: '3.00' } : {}) },
      { media_buy_id: 'buy-2', impressions: 3, ...(billing ? { spend: '5.00' } : {}) },
    ];
    const controlTotals = [
      { name: 'impressions', value: '5', value_type: 'integer', unit: 'impressions' },
      ...(billing ? [{ name: 'spend', value: '8.00', value_type: 'decimal', unit: 'USD' }] : []),
    ];
    const revisionId = `revision-${prefix}-official-1`;
    const revisionBytes = Buffer.from(api.jcs.canonicalize({
      reporting_revision_id: revisionId,
      row_count: rows.length,
      control_totals: controlTotals,
      reporting_rows: rows,
    }), 'utf8');
    const revision = {
      reporting_revision_id: revisionId,
      reporting_obligation_id: obligation.reporting_obligation_id,
      revisionNumber: 1,
      finality: 'official', kind: 'official',
      manifest: { level: 'basic', objectRef: `${prefix}-manifest`, sha256: 'a'.repeat(64), byteCount: 1 },
      sourcePublicationId: `publication-${prefix}-1`,
      binding: {
        algorithm: 'rfc8785_jcs_v1', sha256: sha256(revisionBytes),
        byteCount: revisionBytes.byteLength, rowCount: rows.length,
      },
      rows, observedAt: now, dataThrough: period.end, sourceReadCutoffAt: now, createdAt: now,
      wireRevision: {
        reporting_revision_id: revisionId,
        revision_content_sha256: sha256(revisionBytes),
        report_definition_id: offering.report_definition_id,
        report_definition_uri: offering.report_definition_uri,
        report_definition_sha256: offering.report_definition_sha256,
        reporting_profile: offering.reporting_profile.id,
        schema_version: offering.reporting_profile.version,
        schema_uri: offering.reporting_profile.schema_uri,
        schema_sha256: offering.reporting_profile.schema_sha256,
        schema_dialect: offering.reporting_profile.schema_dialect,
        schema_ref_policy: offering.reporting_profile.schema_ref_policy,
        account_id: ACCOUNT_ID,
        media_buy_ids: ['buy-1', 'buy-2'],
        coverage: {
          status: 'full', evaluated_at: now,
          media_buy_ids: ['buy-1', 'buy-2'], fully_covered_media_buy_ids: ['buy-1', 'buy-2'],
          partially_covered_media_buy_ids: [], unsupported_media_buy_ids: [], unknown_media_buy_ids: [],
          package_ids: [], covered_package_ids: [], unsupported_package_ids: [], unknown_package_ids: [], limitations: [],
        },
        period: { ...period, source_timezone: 'UTC' },
        finality: 'official', finality_basis: 'contractual_cutoff',
        finality_policy_id: 'contractual-cutoff-v1', finalized_at: now,
        observed_at: now, data_through: period.end, data_through_precision: 'exact',
        row_count: rows.length, control_totals: controlTotals,
        canonical_content_digest: structuredClone(CANONICAL_DIGEST), created_at: now,
      },
    };
    await core.commitRevision(revision, lease);
    const worker = await runtime.runWorker({ account_id: ACCOUNT_ID, maxIterations: 2 });
    if (worker.delivered !== 1 || worker.failed !== 0) {
      throw new Error(`managed worker did not deliver exactly once: ${JSON.stringify(worker)}`);
    }
    const revisionView = await status('revision', { reporting_revision_id: revisionId });
    const materialization = revisionView.materializations?.[0];
    if (!materialization || materialization.status !== 'available' || !materialization.verification) {
      throw new Error('authoritative status did not expose a verified available materialization');
    }
    Object.assign(state, {
      prepared: true, now, period, configuration, obligation, binding, lease, revision,
      materialization, worker,
    });
    return state;
  }

  async function publishAdjustments() {
    if (mode !== 'billing') throw new Error('adjustments require the Reconciled Billing fixture');
    await prepare();
    if (state.adjustments.length) return state.adjustments;
    const specs = [
      { id: 'adjustment-billing-accepted-0001', number: 1, impressions: '1', reason: 'source_correction' },
      { id: 'adjustment-billing-disputed-0001', number: 2, impressions: '-1', reason: 'invalid_traffic' },
    ];
    for (const spec of specs) {
      const rows = [];
      const bytes = Buffer.from(api.jcs.canonicalize(rows), 'utf8');
      const withoutDigest = {
        reporting_adjustment_id: spec.id,
        adjusts_reporting_revision_id: state.revision.reporting_revision_id,
        reason_code: spec.reason,
        accounting_period: { start: state.period.start, end: state.period.end },
        control_total_deltas: [{ name: 'impressions', value: spec.impressions, value_type: 'integer', unit: 'impressions' }],
        correction_observed_at: state.now,
        created_at: state.now,
      };
      const digest = api.ledger.reportingCanonicalAdjustmentSha256V1(withoutDigest);
      const adjustment = {
        reporting_adjustment_id: spec.id,
        reporting_obligation_id: state.obligation.reporting_obligation_id,
        adjusts_reporting_revision_id: state.revision.reporting_revision_id,
        adjustmentNumber: spec.number,
        manifest: { level: 'basic', objectRef: `${spec.id}-manifest`, sha256: 'f'.repeat(64), byteCount: 1 },
        sourcePublicationId: `${spec.id}-publication`,
        binding: { algorithm: 'rfc8785_jcs_v1', sha256: sha256(bytes), byteCount: bytes.byteLength, rowCount: 0 },
        rows, observedAt: state.now, dataThrough: state.period.end,
        sourceReadCutoffAt: state.now, createdAt: state.now,
        wireAdjustment: { ...withoutDigest, canonical_adjustment_sha256: digest },
      };
      await core.commitAdjustment(adjustment, state.lease);
      state.adjustments.push(adjustment);
    }
    return state.adjustments;
  }

  async function controller(scenario, operation) {
    const expected = mode === 'billing'
      ? 'reliable_reporting_reconciled_billing_probe'
      : 'reliable_reporting_managed_delivery_probe';
    if (scenario !== expected) throw new Error(`scenario ${scenario} is unavailable in ${mode} mode`);
    if (operation === 'prepare') {
      await prepare();
      return { success: true, simulated: {
        destination_ref: state.binding.destination_ref,
        reporting_obligation_id: state.obligation.reporting_obligation_id,
        reporting_revision_id: state.revision.reporting_revision_id,
        reporting_materialization_id: state.materialization.reporting_materialization_id,
        canonical_content_digest: structuredClone(state.revision.wireRevision.canonical_content_digest),
      } };
    }
    if (mode === 'managed' && operation === 'suppress_readiness') {
      await prepare();
      state.readinessNotificationSuppressed = true;
      return { success: true, simulated: { readiness_notification_suppressed: true } };
    }
    if (mode === 'managed' && operation === 'advance_within_retention') {
      await prepare();
      const bytes = await runtime.readResource({
        account_id: ACCOUNT_ID,
        resource_ref: state.materialization.resource.resource_ref,
      });
      return { success: true, simulated: {
        resource_readable: Boolean(bytes?.byteLength),
        reporting_materialization_id: state.materialization.reporting_materialization_id,
      } };
    }
    if (mode === 'managed' && operation === 'revoke_access') {
      await prepare();
      const started = Date.now();
      const revoked = await managed.revokeDestination({
        account_id: ACCOUNT_ID,
        destination_ref: state.binding.destination_ref,
        generation: state.binding.authorization_generation,
        revoked_at: iso(started),
      });
      const counts = await runtime.runWorker({ account_id: ACCOUNT_ID, maxIterations: 2 });
      const bytes = await runtime.readResource({
        account_id: ACCOUNT_ID,
        resource_ref: state.materialization.resource.resource_ref,
      });
      const retained = await status('revision', { reporting_revision_id: state.revision.reporting_revision_id });
      return { success: true, simulated: {
        access_revoked: revoked && bytes === null && counts.revocationsCompleted === 1,
        historical_metadata_retained: retained.materializations?.[0]?.reporting_materialization_id ===
          state.materialization.reporting_materialization_id,
        revocation_elapsed_seconds: (Date.now() - started) / 1000,
      } };
    }
    if (mode === 'billing' && operation === 'publish_adjustment') {
      const adjustments = await publishAdjustments();
      return { success: true, simulated: {
        adjustments: adjustments.map(value => ({
          reporting_adjustment_id: value.reporting_adjustment_id,
          adjusts_reporting_revision_id: value.adjusts_reporting_revision_id,
          canonical_adjustment_sha256: value.wireAdjustment.canonical_adjustment_sha256,
        })),
        disputed_observed_adjustment_sha256: '0'.repeat(64),
      } };
    }
    throw new Error(`unsupported ${expected} operation: ${String(operation)}`);
  }

  return {
    accountId: ACCOUNT_ID,
    consumerId: CONSUMER_ID,
    mode,
    core,
    managed,
    runtime,
    provider,
    state,
    context,
    controller,
    prepare,
    publishAdjustments,
    status,
  };
}

module.exports = {
  ACCOUNT_ID,
  CANONICAL_DIGEST,
  CONSUMER_ID,
  createManagedReportingFixture,
  deliveryOffering,
};
