# Definition-Driven Official Reporting Finality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Python reporting producer publish official revisions only after proving the exact selected report-definition finality policy.

**Architecture:** Add an immutable three-variant finality-policy model to the retained definition binding, validate its source requirements during production admission, and pass a policy-specific proof into revision construction. Source-final uses authoritative source evidence; contractual cutoff promotes a post-cutoff snapshot; stabilized promotes snapshot content only after a bounded durable observation history proves an uninterrupted unchanged interval.

**Tech Stack:** Python 3.10+, frozen dataclasses, Pydantic source contracts, asyncio, in-memory and PostgreSQL reporting stores, pytest/pytest-asyncio, mypy, pyright, Ruff.

**Spec:** `docs/superpowers/specs/2026-11-18-definition-driven-official-reporting-finality-design.md`

## Global Constraints

- Never infer or default a finality policy.
- Only `source_final` requires an authoritative upstream signal.
- `contractual_cutoff` and `stabilized` promote verified snapshot content.
- `stable_since`, `official_close_lag`, source offering IDs, and manifest evidence never select a policy.
- Reject calendar-relative years and months instead of approximating them.
- Keep the shared PostgreSQL configuration and obligation column layouts unchanged.
- Preserve unchanged-content suppression: successful unchanged observations advance history without creating revisions.
- Legacy retained records remain readable; unfinished official work without a policy fails closed.
- Use `apply_patch` for edits and explicitly stage named files only.

---

### Task 1: Immutable finality-policy model and retained binding

**Files:**
- Create: `src/adcp/reporting/ledger/finality.py`
- Modify: `src/adcp/reporting/ledger/models.py:120-190`
- Modify: `src/adcp/reporting/ledger/_record_rows.py:100-160`
- Modify: `src/adcp/reporting/ledger/__init__.py:90-330`
- Test: `tests/test_reporting_ledger.py`
- Test: `tests/type_checks/reporting_finality.py`

**Interfaces:**
- Produces: `ReportingSourceFinalPolicy`, `ReportingContractualCutoffPolicy`, `ReportingStabilizedPolicy`, and `ReportingFinalityPolicy`.
- Produces: `reporting_finality_policy_from_storage(value: Mapping[str, Any]) -> ReportingFinalityPolicy`.
- Produces: `ReportingDefinitionBinding.finality_policy: ReportingFinalityPolicy | None`.
- Consumes later: admission and producer tasks read the selected policy only from the obligation's retained definition binding.

- [ ] **Step 1: Write failing policy validation and storage tests**

```python
def test_definition_binding_retains_selected_finality_policy_off_wire() -> None:
    policy = ReportingStabilizedPolicy(
        finality_policy_id="daily-stable-v1",
        minimum_age="P2D",
        unchanged_for="PT12H",
    )
    binding = replace(DEFINITION, finality_policy=policy)

    assert binding.to_wire() == DEFINITION.to_wire()
    assert binding.to_storage()["finality_policy"] == {
        "finality_policy_id": "daily-stable-v1",
        "basis": "stabilized",
        "minimum_age": "P2D",
        "unchanged_for": "PT12H",
    }
    assert _definition_from_payload(binding.to_storage()) == binding


@pytest.mark.parametrize("value", ["P1M", "P1Y", "not-a-duration"])
def test_finality_policy_refuses_unsupported_duration(value: str) -> None:
    with pytest.raises(ValueError, match="supported reporting duration"):
        ReportingContractualCutoffPolicy("cutoff-v1", value)
```

Add a type-check fixture that constructs all three variants, assigns each to `ReportingFinalityPolicy`, and passes it to `ReportingDefinitionBinding`.

- [ ] **Step 2: Run the new tests and confirm the missing API failure**

```bash
uv run pytest tests/test_reporting_ledger.py -k 'finality_policy or selected_finality' -q
uv run mypy tests/type_checks/reporting_finality.py
```

Expected: pytest collection/import fails because the policy classes do not exist; mypy reports missing exports.

- [ ] **Step 3: Implement the frozen policy union**

```python
@dataclass(frozen=True)
class ReportingSourceFinalPolicy:
    finality_policy_id: str
    source_signal: str
    basis: Literal["source_final"] = field(default="source_final", init=False)

    def __post_init__(self) -> None:
        _validate_policy_id(self.finality_policy_id)
        if not 1 <= len(self.source_signal) <= 512:
            raise ValueError("source_signal must contain 1 to 512 characters")


@dataclass(frozen=True)
class ReportingContractualCutoffPolicy:
    finality_policy_id: str
    duration_after_period_end: str
    basis: Literal["contractual_cutoff"] = field(default="contractual_cutoff", init=False)

    def __post_init__(self) -> None:
        _validate_policy_id(self.finality_policy_id)
        iso_duration_milliseconds_v1(self.duration_after_period_end)


@dataclass(frozen=True)
class ReportingStabilizedPolicy:
    finality_policy_id: str
    minimum_age: str
    unchanged_for: str
    basis: Literal["stabilized"] = field(default="stabilized", init=False)

    def __post_init__(self) -> None:
        _validate_policy_id(self.finality_policy_id)
        iso_duration_milliseconds_v1(self.minimum_age)
        iso_duration_milliseconds_v1(self.unchanged_for)


ReportingFinalityPolicy = (
    ReportingSourceFinalPolicy | ReportingContractualCutoffPolicy | ReportingStabilizedPolicy
)
```

Implement `to_storage()` on each variant and a basis-discriminated closed decoder. Reject unknown keys and bases so retained policy bytes cannot be silently reinterpreted.

- [ ] **Step 4: Add the policy to `ReportingDefinitionBinding` storage only**

Add `finality_policy: ReportingFinalityPolicy | None = None`, validate its exact runtime type in `__post_init__`, append it only in `to_storage()`, and leave `to_wire()` byte-for-byte unchanged. `_definition_from_payload()` must pop and decode `finality_policy` before constructing the binding.

- [ ] **Step 5: Export the policy API and run focused tests**

```bash
uv run pytest tests/test_reporting_ledger.py -k 'finality_policy or selected_finality' -q
uv run mypy tests/type_checks/reporting_finality.py
uv run ruff check src/adcp/reporting/ledger/finality.py src/adcp/reporting/ledger/models.py src/adcp/reporting/ledger/_record_rows.py tests/test_reporting_ledger.py tests/type_checks/reporting_finality.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit the policy model**

```bash
git add src/adcp/reporting/ledger/finality.py src/adcp/reporting/ledger/models.py src/adcp/reporting/ledger/_record_rows.py src/adcp/reporting/ledger/__init__.py tests/test_reporting_ledger.py tests/type_checks/reporting_finality.py
git commit -m "feat(reporting): retain selected finality policies"
```

---

### Task 2: Definition-driven production admission and source routing

**Files:**
- Modify: `src/adcp/reporting/service.py:790-875`
- Modify: `src/adcp/reporting/production/offerings.py:40-240`
- Modify: `src/adcp/reporting/production/configuration.py:35-105`
- Test: `tests/test_reliable_reporting_service.py`
- Test: `tests/conformance/reporting/test_reporting_production_configuration.py`

**Interfaces:**
- Consumes: `ReportingDefinitionBinding.finality_policy` and the policy variants from Task 1.
- Produces: one source rule mapping `source_final -> official offering` and `contractual_cutoff|stabilized -> snapshot offering`.
- Produces: fail-fast configuration errors before obligation creation.

- [ ] **Step 1: Write failing admission tests for all three bases**

```python
@pytest.mark.parametrize(
    ("policy", "expected_offering"),
    [
        (ReportingSourceFinalPolicy("source-v1", "delivery.is_final"), OFFICIAL_OFFERING_ID),
        (ReportingContractualCutoffPolicy("cutoff-v1", "P2D"), SNAPSHOT_OFFERING_ID),
        (ReportingStabilizedPolicy("stable-v1", "P1D", "PT6H"), SNAPSHOT_OFFERING_ID),
    ],
)
async def test_official_configuration_routes_by_selected_policy(policy, expected_offering):
    configuration = official_configuration(policy)
    await service.configure(configuration)
    binding = service._bindings[configuration.generation_key]
    assert binding.producer._offerings.offering_for(finality_policy_source_class(policy)) == expected_offering


async def test_official_configuration_without_policy_fails_before_obligation():
    with pytest.raises(ReliableReportingConfigurationError, match="selected finality policy"):
        await service.configure(official_configuration(None))
    assert await service.store.list_obligations(account_id=ACCOUNT) == []
```

Add the explicit-close admission case:

```python
async def test_snapshot_official_close_requires_selected_policy() -> None:
    configuration = snapshot_configuration(definition=DEFINITION)
    context = account_context_with_official_close(configuration)
    service = ReliableReportingService.memory(account_context=lambda _: context)
    service.sources.register("gam", closing_adapter())

    with pytest.raises(ReliableReportingConfigurationError, match="selected finality policy"):
        await service.configure(configuration)
```

- [ ] **Step 2: Run the admission tests and verify current routing fails**

```bash
uv run pytest tests/test_reliable_reporting_service.py -k 'official_configuration or selected_policy' -q
uv run pytest tests/conformance/reporting/test_reporting_production_configuration.py -k finality -q
```

Expected: missing-policy admission succeeds or the service insists on an authoritative offering for derived policies.

- [ ] **Step 3: Centralize the source-finality requirement**

```python
def finality_policy_source_class(policy: ReportingFinalityPolicy) -> Literal["snapshot", "official"]:
    return "official" if isinstance(policy, ReportingSourceFinalPolicy) else "snapshot"
```

Use this helper in service and production offering validation. Obtain the policy from the exact `ReportingDefinitionBinding` shared by the configuration and verification key. Reject `required_finality="official"` with no policy, reject the wrong source offering class, and never fall back to `required_finality` to choose a source.

- [ ] **Step 4: Validate explicit snapshot-to-official configuration**

Keep ordinary snapshot-only configurations unchanged. When a snapshot source declares an explicit official-close path, require `definition.finality_policy`; validate that policy's source class exactly as for an official-required configuration. Do not interpret `official_close_lag` as a policy or policy duration.

- [ ] **Step 5: Run production admission regressions**

```bash
uv run pytest tests/test_reliable_reporting_service.py -q
uv run pytest tests/conformance/reporting/test_reporting_production_configuration.py -q
uv run ruff check src/adcp/reporting/service.py src/adcp/reporting/production/offerings.py src/adcp/reporting/production/configuration.py tests/test_reliable_reporting_service.py tests/conformance/reporting/test_reporting_production_configuration.py
```

Expected: all commands pass; snapshot-only fixtures without an official-close path need no policy.

- [ ] **Step 6: Commit admission and routing**

```bash
git add src/adcp/reporting/ledger/finality.py src/adcp/reporting/service.py src/adcp/reporting/production/offerings.py src/adcp/reporting/production/configuration.py tests/test_reliable_reporting_service.py tests/conformance/reporting/test_reporting_production_configuration.py
git commit -m "feat(reporting): validate official finality admission"
```

---

### Task 3: Bounded durable observation-history reads

**Files:**
- Modify: `src/adcp/reporting/ledger/provisional.py:190-220`
- Modify: `src/adcp/reporting/ledger/store.py:830-850,1320-1440`
- Modify: `packages/adcp-reporting-storage/src/adcp_reporting_storage/ledger/pg.py:1785-1820`
- Test: `tests/test_reporting_provisional_observations.py`

**Interfaces:**
- Produces: `ProvisionalObservationStore.list_provisional_observations(*, account_id: str, reporting_obligation_id: str, limit: int) -> tuple[ProvisionalObservation, ...]`.
- Guarantees: account/obligation scoped, bounded by `limit`, ascending observation ordinal.
- Consumes later: stabilization requests `ceil(unchanged_for / cadence) + 2` records.

- [ ] **Step 1: Write failing memory/PostgreSQL history tests**

```python
async def test_observation_history_is_bounded_ordered_and_account_scoped(make_harness):
    producer, store, _, clock = await make_harness(
        _capabilities(restatement_window="P3D", restatement_cadence="PT1H")
    )
    await producer.run_worker()
    clock[0] += timedelta(hours=1)
    await producer.run_worker()
    clock[0] += timedelta(hours=1)
    await producer.run_worker()
    obligation = await _only_obligation(store)

    history = await store.list_provisional_observations(
        account_id=ACCOUNT,
        reporting_obligation_id=obligation.reporting_obligation_id,
        limit=2,
    )

    assert [item.acquisition.ordinal for item in history] == [1, 2]
    assert all(item.acquisition.account_id == ACCOUNT for item in history)
```

Also assert `limit <= 0` raises `ValueError` and a different account returns an empty tuple.

- [ ] **Step 2: Run the test and confirm the protocol method is absent**

```bash
uv run pytest tests/test_reporting_provisional_observations.py -k observation_history -q
```

Expected: FAIL with `AttributeError` for `list_provisional_observations`.

- [ ] **Step 3: Implement the protocol and in-memory suffix read**

```python
async def list_provisional_observations(
    self, *, account_id: str, reporting_obligation_id: str, limit: int
) -> tuple[ProvisionalObservation, ...]:
    if type(limit) is not int or limit <= 0:
        raise ValueError("observation history limit must be a positive integer")
    matches = sorted(
        (
            (ordinal, value)
            for (account, obligation, ordinal), value in self._provisional_observations.items()
            if account == account_id and obligation == reporting_obligation_id
        ),
        key=lambda item: item[0],
    )
    return tuple(value for _, value in matches[-limit:])
```

- [ ] **Step 4: Implement the PostgreSQL suffix read**

Query the immutable table with exact owner predicates and `ORDER BY ordinal DESC LIMIT %s`, decode rows, then reverse the tuple so both stores expose ascending order. Bind `limit` as a SQL parameter.

- [ ] **Step 5: Run memory and PostgreSQL observation tests**

```bash
uv run pytest tests/test_reporting_provisional_observations.py -k 'observation_history or unchanged_scheduled_read' -q
uv run ruff check src/adcp/reporting/ledger/provisional.py src/adcp/reporting/ledger/store.py packages/adcp-reporting-storage/src/adcp_reporting_storage/ledger/pg.py tests/test_reporting_provisional_observations.py
```

Expected: memory passes; PostgreSQL passes when `ADCP_PG_TEST_URL` is configured and otherwise follows the existing skip behavior.

- [ ] **Step 6: Commit observation history support**

```bash
git add src/adcp/reporting/ledger/provisional.py src/adcp/reporting/ledger/store.py packages/adcp-reporting-storage/src/adcp_reporting_storage/ledger/pg.py tests/test_reporting_provisional_observations.py
git commit -m "feat(reporting): expose retained observation history"
```

---

### Task 4: Source-final and contractual-cutoff proof flows

**Files:**
- Modify: `src/adcp/reporting/ledger/finality.py`
- Modify: `src/adcp/reporting/ledger/producer.py:700-920,1095-1325,1720-1885,2050-2145`
- Test: `tests/test_reporting_official_finality.py`
- Test: `tests/test_reporting_source_availability.py`

**Interfaces:**
- Produces: `FinalityProof(finality_policy_id: str, basis: ..., finalized_at: datetime)`.
- Produces: `contractual_cutoff_at(obligation, policy) -> datetime`.
- Produces: `source_finality_proof(obligation, manifest, policy) -> FinalityProof`.
- Changes: `commit_revision_from_manifest(..., finality_proof: FinalityProof | None = None)` requires proof for new official revisions.

- [ ] **Step 1: Write failing source-final tests**

```python
async def test_source_final_emits_exact_selected_policy() -> None:
    policy = ReportingSourceFinalPolicy("delivery-final-v1", "delivery.is_final")
    producer, store, configuration, obligation, authoritative_manifest = await official_harness(policy)

    revision = await producer.acquire_obligation(configuration, obligation)

    assert revision is not None
    assert revision.finality_basis == "source_final"
    assert revision.finality_policy_id == "delivery-final-v1"
    assert revision.finalized_at == authoritative_manifest.finality_evidence.observed_at


async def test_source_final_rejects_a_different_source_signal() -> None:
    with pytest.raises(LedgerConflictError, match="source signal"):
        await producer.acquire_obligation(configuration, obligation)
```

- [ ] **Step 2: Write failing contractual-cutoff tests**

```python
async def test_contractual_cutoff_promotes_a_post_cutoff_snapshot() -> None:
    policy = ReportingContractualCutoffPolicy("two-day-cutoff", "P2D")
    producer, store, configuration, obligation, calls, clock = await official_harness(policy)

    clock[0] = obligation.period.end + timedelta(days=2) - timedelta(microseconds=1)
    assert (await producer.run_configuration(configuration, now=clock[0])).revisions_committed == []
    assert calls == []

    clock[0] += timedelta(microseconds=1)
    turn = await producer.run_configuration(configuration, now=clock[0])
    revision = await store.get_revision(
        account_id=configuration.account_id,
        reporting_revision_id=turn.revisions_committed[0],
    )
    assert revision is not None
    assert calls[0].publication_class == "PROVISIONAL_SNAPSHOT"
    assert revision.finality_basis == "contractual_cutoff"
    assert revision.finality_policy_id == "two-day-cutoff"
    assert revision.finalized_at == revision.created_at
```

- [ ] **Step 3: Run focused tests and verify current synthetic behavior fails**

```bash
uv run pytest tests/test_reporting_official_finality.py -k 'source_final or contractual' -q
```

Expected: exact IDs/signals and snapshot promotion fail under current producer behavior.

- [ ] **Step 4: Implement policy proofs and source/output finality separation**

```python
@dataclass(frozen=True)
class FinalityProof:
    finality_policy_id: str
    basis: Literal["source_final", "contractual_cutoff", "stabilized"]
    finalized_at: datetime


def contractual_cutoff_at(
    obligation: ReportingObligationRecord,
    policy: ReportingContractualCutoffPolicy,
) -> datetime:
    return _utc(obligation.period.end) + timedelta(
        milliseconds=iso_duration_milliseconds_v1(policy.duration_after_period_end)
    )
```

Split source request class from output finality. Source-final requests `AUTHORITATIVE`; contractual cutoff requests `PROVISIONAL_SNAPSHOT` but passes `finality="official"` and a proof to revision construction. `_automatic_read_ready()` must include the cutoff boundary.

- [ ] **Step 5: Make official revision construction proof-only**

```python
if finality == "official" and finality_proof is None:
    raise LedgerConflictError("FINALITY_POLICY_UNPROVEN", "official finality requires policy proof")

finality_basis = finality_proof.basis if finality_proof else None
finality_policy_id = finality_proof.finality_policy_id if finality_proof else None
finalized_at = finality_proof.finalized_at if finality_proof else None
```

Validate `period.end <= finalized_at <= created_at`. Keep snapshot fields absent. Preserve replay of already-retained official revisions before applying the new-write requirement.

- [ ] **Step 6: Run source-final, cutoff, and availability regressions**

```bash
uv run pytest tests/test_reporting_official_finality.py -k 'source_final or contractual' -q
uv run pytest tests/test_reporting_source_availability.py -q
uv run pytest tests/test_reporting_producer_retry.py -q
uv run ruff check src/adcp/reporting/ledger/finality.py src/adcp/reporting/ledger/producer.py tests/test_reporting_official_finality.py tests/test_reporting_source_availability.py
```

Expected: all commands pass.

- [ ] **Step 7: Commit the two proof flows**

```bash
git add src/adcp/reporting/ledger/finality.py src/adcp/reporting/ledger/producer.py tests/test_reporting_official_finality.py tests/test_reporting_source_availability.py
git commit -m "feat(reporting): prove official source and cutoff finality"
```

---

### Task 5: Stabilized finality from retained observation history

**Files:**
- Modify: `src/adcp/reporting/ledger/finality.py`
- Modify: `src/adcp/reporting/ledger/producer.py:920-1095,1190-1280,1810-1965`
- Modify: `src/adcp/reporting/ledger/store.py:700-750`
- Test: `tests/test_reporting_official_finality.py`
- Test: `tests/test_reporting_provisional_observations.py`

**Interfaces:**
- Produces: `prove_stabilized_finality(...) -> FinalityProof | None` over a bounded ordered observation suffix.
- Consumes: `list_provisional_observations(..., limit=ceil(unchanged_for/cadence)+2)` from Task 3.
- Produces: a policy-controlled official stabilization loop that commits the qualifying observation before the official revision.

- [ ] **Step 1: Write failing stable-history tests**

```python
async def test_stabilized_policy_promotes_unchanged_content_without_duplicate_snapshots() -> None:
    policy = ReportingStabilizedPolicy("stable-12h", "P1D", "PT12H")
    producer, store, configuration, obligation, fetch, clock = await official_harness(policy)

    await producer.run_configuration(configuration, now=clock[0])
    clock[0] += timedelta(hours=6)
    await producer.run_configuration(configuration, now=clock[0])
    clock[0] += timedelta(hours=6)
    turn = await restarted_producer(producer, store, clock).run_configuration(
        configuration, now=clock[0]
    )

    revisions = await store.list_revisions(
        account_id=configuration.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )
    assert [item.finality for item in revisions].count("snapshot") == 1
    assert [item.finality for item in revisions].count("official") == 1
    official = next(item for item in revisions if item.finality == "official")
    assert official.reporting_revision_id in turn.revisions_committed
    assert official.finality_policy_id == "stable-12h"
```

- [ ] **Step 2: Write failing reset and fail-closed tests**

```python
async def test_stability_run_restarts_after_content_change() -> None:
    await observe_at(hours=0, impressions=10)
    await observe_at(hours=6, impressions=11)
    await observe_at(hours=12, impressions=11)
    assert not await official_revision()
    await observe_at(hours=18, impressions=11)
    assert await official_revision()


async def test_stability_run_restarts_after_missed_deadline() -> None:
    await observe_at(hours=0, impressions=10)
    await observe_at(hours=13, impressions=10)  # cadence is six hours
    assert not await official_revision()
```

Add an unreadable referenced-revision case:

```python
async def test_stability_proof_refuses_unreadable_snapshot_history() -> None:
    await observe_at(hours=0, impressions=10)
    snapshot = await current_snapshot_revision()
    await mark_revision_unreadable(snapshot.reporting_revision_id)
    await observe_at(hours=6, impressions=10)

    with pytest.raises(LedgerConflictError, match="history.*readable"):
        await evaluate_stabilization()
    assert not await official_revision()
```

- [ ] **Step 3: Run stabilization tests and confirm no proof engine exists**

```bash
uv run pytest tests/test_reporting_official_finality.py -k stabil -q
```

Expected: FAIL because official-required configurations currently perform one authoritative read instead of a retained snapshot loop.

- [ ] **Step 4: Implement bounded stabilization proof evaluation**

Walk history backward from the latest observation. For each adjacent pair:

```python
same_content = current_manifest.content_fingerprint == prior_manifest.content_fingerprint
dispatch_at = current.acquisition.request().period.source_read_cutoff_at
on_time = prior.next_due_at is not None and dispatch_at <= prior.next_due_at
same_revision = current.revision_id == prior.revision_id
```

Stop at the first false condition. Validate the latest referenced revision and rows with `validate_retained_provisional_observation`. Return `None` unless the latest checked time reaches `period.end + minimum_age` and `run_start + unchanged_for`. When proven, return `FinalityProof(policy_id, "stabilized", latest.checked_at)`.

- [ ] **Step 5: Add the policy-controlled stabilization scheduler**

For a stabilized policy, freeze a provisional acquisition policy whose cadence comes from the snapshot offering and whose window remains open until proof succeeds. Every successful source read commits its observation through the existing observation store. After commit, load bounded history and evaluate it. If unproven, leave durable work pending.

When proven, reuse the qualifying manifest and rows to commit official. If the manifest is already represented by snapshot, derive a distinct official ID:

```python
def _derived_official_revision_id(manifest, proof: FinalityProof) -> str:
    digest = hashlib.sha256(
        canonical_json_utf8_v1(
            [manifest.publication_id, "official", proof.finality_policy_id]
        )
    ).hexdigest()
    return f"rpr_{digest[:40]}"
```

Do not supersede the snapshot. Commit the qualifying observation first, then official. Replay must reconstruct the same ID and proof instant.

- [ ] **Step 6: Run stabilization, observation, and settling suites**

```bash
uv run pytest tests/test_reporting_official_finality.py -k stabil -q
uv run pytest tests/test_reporting_provisional_observations.py -q
uv run pytest tests/test_reporting_settling.py -q
uv run pytest tests/test_reporting_scheduling_jitter.py -q
uv run ruff check src/adcp/reporting/ledger/finality.py src/adcp/reporting/ledger/producer.py src/adcp/reporting/ledger/store.py tests/test_reporting_official_finality.py tests/test_reporting_provisional_observations.py
```

Expected: all commands pass; PostgreSQL cases follow their configured run/skip behavior.

- [ ] **Step 7: Commit stabilized finality**

```bash
git add src/adcp/reporting/ledger/finality.py src/adcp/reporting/ledger/producer.py src/adcp/reporting/ledger/store.py tests/test_reporting_official_finality.py tests/test_reporting_provisional_observations.py
git commit -m "feat(reporting): prove stabilized official finality"
```

---

### Task 6: Legacy handling, explicit close migration, and reporting regressions

**Files:**
- Modify: `tests/test_reporting_settling.py`
- Modify: `tests/test_reporting_source_availability.py`
- Modify: `tests/test_reporting_producer_retry.py`
- Modify: `tests/test_reporting_scheduling_jitter.py`
- Modify: `tests/conformance/reporting/test_reporting_production_settling.py`
- Modify: `tests/conformance/reporting/_production_support.py`
- Modify: `docs/reporting-source-adapters.md`
- Modify: `docs/reporting-provisional-observations.md`

**Interfaces:**
- Consumes: all policy, admission, proof, and observation APIs from Tasks 1-5.
- Produces: documented migration for new official work and read-only compatibility for retained legacy evidence.

- [ ] **Step 1: Add explicit legacy and snapshot-only tests**

```python
async def test_retained_legacy_official_revision_remains_readable() -> None:
    await store.commit_revision(legacy_official_revision(), rows)
    result = await ReportingStatusHandler(store).handle({"view": "revision"}, caller)
    assert result["revisions"][0]["finality"] == "official"


async def test_snapshot_only_settling_never_emits_official_without_selected_policy() -> None:
    producer, store, _, clock = await snapshot_package_harness()
    await producer.run_worker()
    clock[0] += timedelta(days=120)
    await producer.run_worker()
    assert {item.finality for item in await _revisions(store)} == {"snapshot"}
```

- [ ] **Step 2: Run affected suites and inventory fixture failures**

```bash
uv run pytest tests/test_reporting_settling.py tests/test_reporting_source_availability.py tests/conformance/reporting/test_reporting_production_settling.py -q
rg -n 'required_finality="official"|required_finality=.official.' tests -g '*.py'
```

Expected: fixtures that create new official work without a policy fail with the closed error.

- [ ] **Step 3: Update official fixtures with the policy they actually assert**

For each new-work fixture, add one explicit policy matching its tested behavior. Do not add a global default helper. Source-final fixtures name the exact manifest `evidence_ref`; cutoff and stabilization fixtures use explicit protocol durations. Leave pure read/projection fixtures unchanged when they intentionally model retained legacy bytes.

- [ ] **Step 4: Document adopter migration and policy ownership**

Add this guidance to both reporting guides:

```text
required_finality: official requires one selected policy copied from the pinned report definition.
The SDK does not choose that policy. source_final uses the authoritative source offering;
contractual_cutoff and stabilized use the snapshot offering. stable_since and
official_close_lag are source evidence/scheduling metadata, not finality policies.
```

Document that explicit snapshot-to-official setups pin a policy, while snapshot-only offerings require no policy and never close merely because time elapsed.

- [ ] **Step 5: Run all reporting-focused suites**

```bash
uv run pytest tests/test_reporting_*.py tests/conformance/reporting -q
uv run mypy src/adcp/reporting packages/adcp-reporting-storage/src tests/type_checks
uv run pyright tests/type_checks/reporting_finality.py
uv run ruff check src/adcp/reporting packages/adcp-reporting-storage/src tests/test_reporting_*.py tests/conformance/reporting tests/type_checks/reporting_finality.py
```

Expected: all commands pass, with only existing environment-dependent skips.

- [ ] **Step 6: Commit regressions and documentation**

Stage only task files shown by `git diff --name-only`, adding every path explicitly:

```bash
git add docs/reporting-source-adapters.md docs/reporting-provisional-observations.md tests/test_reporting_settling.py tests/test_reporting_source_availability.py tests/test_reporting_producer_retry.py tests/test_reporting_scheduling_jitter.py tests/conformance/reporting/test_reporting_production_settling.py tests/conformance/reporting/_production_support.py
git commit -m "docs(reporting): explain official finality policy ownership"
```

---

### Task 7: Full verification and pull request

**Files:**
- Verify: all files changed since `origin/main`
- Modify only if verification exposes a defect in implementation or tests

**Interfaces:**
- Consumes: completed implementation from Tasks 1-6.
- Produces: a conventional-commit PR linked to AI-22659 with verification evidence.

- [ ] **Step 1: Inspect the complete diff**

```bash
git status --short
git diff --stat origin/main...
git diff --check origin/main...
git diff origin/main... -- src/adcp/reporting packages/adcp-reporting-storage tests docs/reporting-source-adapters.md docs/reporting-provisional-observations.md
```

Expected: only finality-policy, producer, observation-store, tests, and reporting documentation changes; `git diff --check` is silent.

- [ ] **Step 2: Run the repository verification matrix**

Confirm CI commands from `pyproject.toml` and workflow files, then run:

```bash
uv run pytest -q
uv run mypy src packages/adcp-reporting-storage/src tests/type_checks
uv run pyright
uv run ruff check .
uv run ruff format --check .
```

Expected: every command exits zero. Record environment-dependent skips; do not report skipped PostgreSQL tests as executed.

- [ ] **Step 3: Re-run PostgreSQL conformance when configured**

```bash
uv run pytest tests/test_reporting_provisional_observations.py tests/conformance/reporting/test_pg_reporting_ledger.py -q
```

Expected: memory and PostgreSQL pass when `ADCP_PG_TEST_URL` is configured; otherwise record the exact skip reason.

- [ ] **Step 4: Resolve verification failures through their owning task**

If verification fails, return to Task 1 for policy/storage defects, Task 2 for admission defects, Task 3 for observation-store defects, Task 4 for source-final/cutoff defects, Task 5 for stabilization defects, or Task 6 for fixture/documentation defects. Add a failing focused test, make the minimal correction, rerun that task's commands, and use that task's explicit commit command before repeating Step 2.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin rmatani/ai-22659-support-definition-driven-official
gh pr create --base main --title "feat(reporting): support definition-driven official finality" --body-file .context/ai-22659-pr.md
```

The PR body summarizes all three flows, states that snapshot-only Reliable Reporting 1.2 still does not require official close, links AI-22659, lists verification commands/results, and identifies skipped PostgreSQL cases.

- [ ] **Step 6: Start the PR feedback loop**

Invoke `scope3-developer:pr-feedback-loop`. Monitor checks and inline feedback, fix failures with test-first reproductions, push follow-up commits, and stop only when CI is green and review feedback is addressed. Never merge the PR.
