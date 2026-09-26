from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "scripts" / "ci" / "reporting_interop"


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def load_python_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_disposition(applicability: dict, scenario: dict, cell: dict) -> str:
    default = applicability["defaults"][cell["family"]]
    override = next(
        (row for row in applicability["overrides"] if row["scenario"] == scenario["id"]),
        None,
    )
    if override is None:
        return default
    if "all" in override:
        return override["all"]
    if "by_family" in override:
        return override["by_family"][cell["family"]]
    if "by_client_language" in override:
        value = override["by_client_language"][cell["client"]["language"]]
        return default if value == "default" else value
    if "by_server_scope" in override:
        scope = "python_reporting_foundation" if cell["server"]["language"] == "python" else "other"
        value = override["by_server_scope"][scope]
        return default if value == "default" else value
    raise AssertionError(f"unrecognized applicability override: {override}")


def test_corpus_declares_exact_candidate_language_quadrants() -> None:
    applicability = load("applicability.json")
    quadrants = [cell for cell in applicability["cells"] if cell["family"] == "candidate_quadrant"]

    assert {(cell["client"]["language"], cell["server"]["language"]) for cell in quadrants} == {
        ("python", "python"),
        ("typescript", "python"),
        ("python", "typescript"),
        ("typescript", "typescript"),
    }
    assert len(quadrants) == 4
    assert all(cell["blocking"] is True for cell in quadrants)
    assert {cell["contract_id"] for cell in quadrants} == {"Q1", "Q2", "Q3", "Q4"}
    assert all(
        cell["protocol_contract"]
        == {"required": "3.2.0-rc.6", "alignment": "exact", "selected_artifacts": False}
        for cell in quadrants
    )


def test_skew_and_latest_canaries_are_separate_and_fail_closed() -> None:
    applicability = load("applicability.json")
    skew = [cell for cell in applicability["cells"] if cell["family"] == "supported_skew"]
    canaries = [cell for cell in applicability["cells"] if cell["family"] == "latest_canary"]

    assert {cell["id"] for cell in skew} == {
        "previous_py_client__candidate_ts_server",
        "candidate_py_client__previous_ts_server",
    }
    assert all(cell["blocking"] is True for cell in skew)
    assert {cell["contract_id"] for cell in skew} == {"S1", "S2"}
    assert all(
        cell["positive_requirement"]
        == {"status": "unmet_no_published_compatible_pair", "blocking": True}
        for cell in skew
    )
    assert all(
        cell["negative_skew_control"]
        == {"required": True, "credit": "refusal_only_never_positive_acceptance"}
        for cell in skew
    )
    assert {cell["client"]["language"] for cell in canaries} == {
        "python",
        "typescript",
    }
    assert all(cell["blocking"] is False for cell in canaries)

    corpus = load("corpus.json")
    scenario = next(
        item
        for item in corpus["scenarios"]
        if item["id"] == "foundation.skew.actionable_unsupported"
    )
    assert all(
        resolve_disposition(applicability, scenario, cell) == "actionable_unsupported"
        for cell in skew
    )


def test_every_scenario_has_a_valid_disposition_for_every_cell() -> None:
    corpus = load("corpus.json")
    applicability = load("applicability.json")
    scenario_ids = [scenario["id"] for scenario in corpus["scenarios"]]
    override_ids = [row["scenario"] for row in applicability["overrides"]]

    assert len(scenario_ids) == len(set(scenario_ids))
    assert len(override_ids) == len(set(override_ids))
    assert set(override_ids) <= set(scenario_ids)
    allowed = set(applicability["dispositions"])
    for scenario in corpus["scenarios"]:
        for cell in applicability["cells"]:
            assert resolve_disposition(applicability, scenario, cell) in allowed


def test_staged_foundation_areas_cannot_be_dropped() -> None:
    corpus = load("corpus.json")
    areas = {scenario["area"] for scenario in corpus["scenarios"]}

    assert {
        "capabilities",
        "cli_storyboards",
        "status_history",
        "configuration",
        "receipts",
        "security",
        "semantic_reconciliation",
        "auth",
        "isolation",
        "value_integrity",
        "pagination",
        "packaging",
        "revision_content",
        "server_foundation_notifications",
        "server_foundation_runtime",
        "official_precedence",
        "version_skew",
    } <= areas
    scenario_ids = {scenario["id"] for scenario in corpus["scenarios"]}
    assert {
        "foundation.pagination.zero_rows",
        "foundation.pagination.more_than_500_rows",
        "foundation.status.exact_revision_read",
        "foundation.delivery.typed_exact_revision_pagination",
        "foundation.security.ctx_metadata_credentials",
        "foundation.security.resource_location_credential_screen",
        "foundation.values.transport_large_integer_binding",
        "foundation.cli.reporting_storyboards",
        "foundation.packaging.reject_version_only_candidate",
        "foundation.webhook.signed_retry_dedup",
        "foundation.webhook.protocol_rc4_vectors",
        "foundation.webhook.cross_language_signer_verifier",
        "foundation.activity.durable_account_feed",
        "foundation.runtime.late_account_materialization_fairness",
        "foundation.runtime.producer_finality_temporal_order",
    } <= scenario_ids


def test_security_hardening_rows_remain_visible_and_nonblocking() -> None:
    corpus = load("corpus.json")
    applicability = load("applicability.json")
    scenarios = {
        row["id"]: row
        for row in corpus["scenarios"]
        if row["id"]
        in {
            "foundation.security.ctx_metadata_credentials",
            "foundation.security.resource_location_credential_screen",
        }
    }

    assert len(scenarios) == 2
    assert all(row["stage"] == "nonblocking_hardening" for row in scenarios.values())
    location = scenarios["foundation.security.resource_location_credential_screen"]
    assert location["advisory_only"] is True
    assert location["does_not_override_secret_leak_failure"] is True
    assert location["evidence_scope"] == "in_process_installed_seller_persistence_guards_only"
    assert all(
        resolve_disposition(applicability, scenario, cell) == "visible_nonblocking"
        for scenario in scenarios.values()
        for cell in applicability["cells"]
    )


def test_resource_location_advisory_preserves_exact_safe_inputs() -> None:
    security = DATA / "security"
    expected_hashes = {
        "resource_location_sentinels.json": (
            "082db6b6f64f2998920f5946382c0228178a80869f0ce65cc8cb044d19d8f065"
        ),
        "resource_location_python.original.py.txt": (
            "cf53cbfd9c5e5ffde723706254509ab4dc5c9d680462bad522bdfd16cdcdf94c"
        ),
        "resource_location_typescript.cjs": (
            "36e6894e98886de6848bf92ea6c9fa4299f540cafa73a81d5c27811f068ef1ca"
        ),
    }

    for name, expected in expected_hashes.items():
        assert hashlib.sha256((security / name).read_bytes()).hexdigest() == expected
    sentinels = json.loads((security / "resource_location_sentinels.json").read_text())
    assert len(sentinels) == 17
    assert all("RLXSENTINEL" not in row["id"] for row in sentinels)
    assert {row["expect"] for row in sentinels} == {"accept", "reject", "either"}


def test_official_precedence_vectors_require_fixed_behavior_without_exemption() -> None:
    corpus = load("corpus.json")
    vectors = {
        scenario["id"]: scenario
        for scenario in corpus["scenarios"]
        if scenario["area"] == "official_precedence"
    }

    assert set(vectors) == {
        "foundation.official_precedence.unlinked_both_artifacts",
        "foundation.official_precedence.no_official_materialization_unlinked",
        "foundation.official_precedence.no_official_materialization_linked",
    }
    assert len({row["historical_vector"] for row in vectors.values()}) == 3
    assert all(row["required_outcome"] == "corrected_behavior" for row in vectors.values())
    assert all(row["known_defect_exemption"] is False for row in vectors.values())
    assert all(row["normative_rc4_fixture_coverage"] is False for row in vectors.values())
    assert load("corpus.json")["rules"]["expected_defect_exemptions"] is False


def test_language_specific_reconciliation_does_not_invent_python_facade() -> None:
    corpus = load("corpus.json")
    applicability = load("applicability.json")
    python_scenario = next(
        row
        for row in corpus["scenarios"]
        if row["id"] == "foundation.reconciliation.python_standalone"
    )

    assert all("client.reporting" not in entry for entry in python_scenario["public_entrypoints"])
    for cell in applicability["cells"]:
        disposition = resolve_disposition(applicability, python_scenario, cell)
        if cell["client"]["language"] == "python":
            assert disposition in {"blocking_required", "visible_nonblocking"}
        else:
            assert disposition == "not_applicable"


@pytest.mark.parametrize(
    "scenario_id",
    [
        "foundation.webhook.signed_retry_dedup",
        "foundation.activity.durable_account_feed",
        "foundation.runtime.late_account_materialization_fairness",
        "foundation.runtime.producer_finality_temporal_order",
    ],
)
def test_server_foundation_scenarios_are_python_server_specific(
    scenario_id: str,
) -> None:
    corpus = load("corpus.json")
    applicability = load("applicability.json")
    scenario = next(row for row in corpus["scenarios"] if row["id"] == scenario_id)

    assert scenario["server_scope"] == "python_reporting_foundation"
    for cell in applicability["cells"]:
        disposition = resolve_disposition(applicability, scenario, cell)
        if cell["server"]["language"] == "python":
            expected = (
                "visible_nonblocking" if cell["family"] == "latest_canary" else "blocking_required"
            )
            assert disposition == expected
        else:
            assert disposition == "not_applicable"


def test_protocol_webhook_vectors_are_not_replaced_by_narrow_signing_controls() -> None:
    corpus = load("corpus.json")
    applicability = load("applicability.json")
    scenario = next(
        row for row in corpus["scenarios"] if row["id"] == "foundation.webhook.protocol_rc4_vectors"
    )

    assert scenario["protocol_owned_inputs"] is True
    assert "29" in scenario["expectation"]
    assert "13-vector" in scenario["expectation"]
    for cell in applicability["cells"]:
        expected = (
            "visible_nonblocking" if cell["family"] == "latest_canary" else "blocking_required"
        )
        assert resolve_disposition(applicability, scenario, cell) == expected


def test_cross_language_webhook_signing_is_a_distinct_blocking_row() -> None:
    corpus = load("corpus.json")
    applicability = load("applicability.json")
    scenario = next(
        row
        for row in corpus["scenarios"]
        if row["id"] == "foundation.webhook.cross_language_signer_verifier"
    )

    assert "Python-to-TypeScript" in scenario["expectation"]
    assert "Content-Digest" in scenario["expectation"]
    assert scenario["signature_profile"] == "webhook_signature_unpadded_base64url"
    assert scenario["protocol_source_commit"] == "94976657c8456e5ad6de55d9793a883542a4fc5f"
    for cell in applicability["cells"]:
        expected = (
            "visible_nonblocking" if cell["family"] == "latest_canary" else "blocking_required"
        )
        assert resolve_disposition(applicability, scenario, cell) == expected


def test_protocol_webhook_vector_runners_preserve_corrected_originals() -> None:
    signing = DATA / "signing"
    expected = {
        "webhook_vectors_python.original.py.txt": (
            "86d9467ee2ecba518ebc5f89b751281c57865711c8fada107f98c61d94e26f68"
        ),
        "webhook_vectors_typescript.cjs": (
            "ad0c3d495c6a7f1fff066693251130b4c3fa2adb8286bc54d74adb06a19832dc"
        ),
        "signer_roundtrip_python.original.py.txt": (
            "c053d2b2951a5508fd332b64d5f834007ea4b355872625063048df8d8e1732b2"
        ),
        "crossverify_typescript.cjs": (
            "fb1b8e09d4459e9b705b3784b7a0c8018b0b870fb47f437118eb61e6cc5c84c9"
        ),
        "sign_webhook_typescript.cjs": (
            "07700c1fef060afe44ba705fad8d6532e5f2ef5fc519e38edfd0c9506224baa9"
        ),
    }

    for name, digest in expected.items():
        assert hashlib.sha256((signing / name).read_bytes()).hexdigest() == digest


def test_1172_and_1182_rows_remain_explicitly_deferred_in_every_cell() -> None:
    corpus = load("corpus.json")
    applicability = load("applicability.json")
    deferred = [scenario for scenario in corpus["scenarios"] if scenario["area"] == "deferred"]

    assert {scenario["id"] for scenario in deferred} == {
        "deferred.1172.service_composition",
        "deferred.1172.client_reporting_facade",
        "deferred.1182.provisional_reread",
    }
    assert all(
        resolve_disposition(applicability, scenario, cell) == "deferred_open"
        for scenario in deferred
        for cell in applicability["cells"]
    )


def test_execution_contract_requires_real_isolated_process_boundary() -> None:
    contract = load("applicability.json")["execution_contract"]

    assert contract == {
        "fresh_processes_per_cell": True,
        "isolated_database_per_cell": True,
        "real_mcp_http": True,
        "postgresql": "required_for_durable_seller_scenarios",
        "installed_packages_only": True,
        "latest_canaries_nonblocking": True,
    }


def test_exact_candidate_pins_are_immutable_and_keep_source_separate() -> None:
    pins = load("pins.json")
    python = pins["python"]["candidate"]
    typescript = pins["typescript"]["historical_candidate_rc45"]

    assert python["source_commit"] == "25e0c7278a19493345975881d1578c76fc64dc1b"
    assert python["source_tree"] == "bcc4ad30ea4ee43d7b82f39e12f60b5a3ac2aead"
    assert python["source_parents"] == [
        "7af569985174235739be4007e693d10ccb604817",
        "fe1a1cbd070bc94ec32685026ad39ec55058dc73",
    ]
    assert python["adoption_base"] == "1f953c40d761be71d11fde84c78359ff2074fe7c"
    assert python["build_kind"] == "prepublication_exact_pr1208_head"
    assert python["version_string_occupied"] is True
    assert "local_path_plus_sha256" in python["artifact_policy"]
    assert len(python["known_independent_builds"]) == 3
    harness_build = next(
        row
        for row in python["known_independent_builds"]
        if row["builder"] == "reporting_interop_harness"
    )
    assert harness_build["all_member_manifest_sha256"] == (
        "397b9b58ad4e72d68e1380a5ad86ff9a76852ca873c3466b78d7c9890035cfa5"
    )
    assert harness_build["sdk_member_manifest_sha256"] == (
        "0ed3b8de391a04be9aee477c6db11aa82e0edca12b1651ab9935dc83ba73b333"
    )
    assert "registry" not in python
    controls = python["development_dependency_controls"]
    assert {(row["pydantic"], row["mcp"]) for row in controls} == {
        ("2.13.0", "2.0.0"),
        ("2.13.5", "2.2.0"),
    }
    for row in controls:
        lock = ROOT / row["lock"]
        assert hashlib.sha256(lock.read_bytes()).hexdigest() == row["lock_sha256"]

    assert typescript["version"] == "14.0.0-rc.45"
    assert typescript["registry_git_head"] == "94171fe20d632f027eb362604c715ed361fadb51"
    assert typescript["source_tree"] == "8d3c29d9a1779f04520c33cdc9ed3e15e5296cdd"
    assert typescript["tarball_sha256"] == (
        "a0952ed8edaaad958bdb8f474c4f5cb68333ee272e7a8f3419d12930e9c57e6b"
    )
    assert typescript["tarball_bytes"] == 19_598_459
    assert typescript["tarball_members"] == 6_246
    assert typescript["integrity"].startswith("sha512-")
    assert typescript["version"] not in {"latest", "rc", "14.0.0-rc.41"}
    lock = ROOT / typescript["package_lock"]
    assert hashlib.sha256(lock.read_bytes()).hexdigest() == typescript["package_lock_sha256"]
    locked_sdk = json.loads(lock.read_text())["packages"]["node_modules/@adcp/sdk"]
    assert locked_sdk["version"] == typescript["version"]
    assert locked_sdk["integrity"] == typescript["integrity"]

    development = pins["typescript"]["development_candidate_rc47"]
    assert development["version"] == "14.0.0-rc.47"
    assert development["protocol"] == "3.2.0-rc.6"
    assert development["package_engines_node"] == "^20.19.0 || >=22.12.0"
    assert development["development_runtime_pin"] == "22.12.0"
    assert development["development_runtime_status"] == "selected_not_executed_by_this_harness"
    assert pins["pending_source_leads"]["typescript_calendar"]["source_lineage"] == {
        "pull_request": 3018,
        "reviewed_pr_head": None,
        "reviewed_pr_head_status": "not_recorded_in_current_harness_evidence",
        "squash_merge_commit_prefix": "f3c7accb",
        "published_registry_git_head": "b0d2886f0f5b8668fc134568c4cd22a4ef89e2fa",
        "identities_are_not_interchangeable": True,
    }
    assert development["executable_harness_input"] is False
    assert development["package_lock"] is None
    assert development["transitive_lock"] is None
    assert development["installed_member_binding"] is None
    member_evidence = development["retained_member_evidence"]
    assert member_evidence == {
        "universal_compliance_subset_members": 55,
        "scope": "universal_compliance_subset_only_not_complete_package_binding",
        "complete_installed_members_expected": 6261,
        "complete_installed_member_binding_available": False,
        "can_substitute_for_transitive_lock": False,
    }
    assert pins["protocol"]["required_contract"] == {
        "version": "3.2.0-rc.6",
        "bundle_selected": False,
        "status": "required_alignment_not_a_selected_protocol_bundle",
    }
    assert pins["protocol"]["historical_fixture_rc4"]["version"] == "3.2.0-rc.4"
    latest = pins["typescript"]["latest_canary"]
    assert latest["observed_version"] == "13.1.1"
    assert latest["observed_at"] == "2026-09-25T00:00:00Z"
    assert latest["execution_policy"] == "resolve_and_record_exact_current_identity_at_execution"

    historical = pins["typescript"]["historical_candidate_rc42"]
    assert historical["version"] == "14.0.0-rc.42"
    assert historical["qualification"] == "historical_matrix12_red_not_current_candidate"


def test_previous_typescript_skew_pin_is_exact_and_locked() -> None:
    previous = load("pins.json")["typescript"]["previous_compatible"]

    assert previous["version"] == "14.0.0-rc.41"
    assert previous["version"] not in {"latest", "rc", "adcp-3.1"}
    assert previous["protocol"] == "3.2.0-rc.4"
    assert previous["tarball_sha256"] == (
        "c9bb1e22b63dfedf6bc1ae9c07d1a1312465489671e9841dd5f1425b817b0a90"
    )
    lock = ROOT / previous["package_lock"]
    assert hashlib.sha256(lock.read_bytes()).hexdigest() == previous["package_lock_sha256"]
    locked_sdk = json.loads(lock.read_text())["packages"]["node_modules/@adcp/sdk"]
    assert locked_sdk["version"] == previous["version"]
    assert locked_sdk["integrity"] == previous["integrity"]


def test_runner_executes_skew_storyboards_and_receiver_contracts_fail_closed() -> None:
    runner = (DATA / "run_foundation_matrix.py").read_text(encoding="utf-8")
    orchestration = (DATA / "storyboard_orchestration.py").read_text(encoding="utf-8")
    python_client = (DATA / "python_core_client.py").read_text(encoding="utf-8")
    receiver_server = (DATA / "signing" / "webhook_receiver_server.py").read_text(encoding="utf-8")
    storyboard_inventory = (DATA / "ts_storyboard_inventory.cjs").read_text(encoding="utf-8")
    assert "server.CONTROLLER_SCENARIOS || {}" not in storyboard_inventory
    assert "public CONTROLLER_SCENARIOS export is missing or malformed" in storyboard_inventory
    assert (
        "public CONTROLLER_SCENARIOS export contains no declared scenarios" in storyboard_inventory
    )
    assert "source_member_identity" in storyboard_inventory
    operation_map = load("controller-operation-map.json")

    assert "--previous-python-runtime" in runner
    assert "--previous-python-artifact" in runner
    assert 'role not in {"candidate", "historical_candidate", "previous", "lead"}' in runner
    assert "candidate_py_client__previous_ts_server" in runner
    assert "previous_py_client__candidate_ts_server" in runner
    assert "expect_unsupported=True" in runner
    assert '"mutation_attempted": False' in python_client
    assert "--allow-historical-red" not in runner
    assert 'result.get("blocking_acceptance") is not blocking_acceptance' in runner
    assert "_execution_accounting" in runner
    assert "observed_protocol_does_not_match_required" in runner
    assert "observed_artifact_identity_does_not_match_required" in runner
    assert "negative_or_partial_skew_cannot_satisfy_positive_requirement" in runner
    assert "executed_visible_canaries" in runner
    assert "missing_visible_canaries" in runner

    assert '"storyboard",' in orchestration
    assert "storyboard selection drifted" in orchestration
    assert "storyboard inventory must contain the exact ordered denominator" in orchestration
    assert '"storyboard_executions": storyboard_executions' in runner
    assert "resolved_steps" in orchestration
    assert "executed_step_ids" in orchestration
    assert "unexecuted_step_ids" in orchestration
    assert "requires_all_capabilities" in storyboard_inventory
    assert "controller_scenarios" in storyboard_inventory
    assert "storyboardSteps" in storyboard_inventory
    assert "storyboard step identities drifted" in storyboard_inventory
    assert "controller_public_surface" in storyboard_inventory
    assert "_validate_python_artifact_inputs" in runner
    assert "sdk_member_manifest_sha256" in runner
    assert "STORYBOARD_ORCHESTRATION" in runner
    assert '"--external-pg-admin-dsn"' in runner
    assert 'result.get("blocking_acceptance") is not False' in runner
    assert {item["scenario"] for item in operation_map["scenarios"]} == {
        "reporting_core_lifecycle_probe",
        "reliable_reporting_core_integrity_probe",
        "reliable_reporting_managed_delivery_probe",
        "reliable_reporting_reconciled_billing_probe",
    }
    assert any(
        operation["operation"] == "advance_time"
        and operation["implementation"]
        == "controlled_clock_public_store_and_real_mcp_preflight_passed"
        for scenario in operation_map["scenarios"]
        for operation in scenario["operations"]
    )
    assert any(
        operation["operation"] == "probe_scheduler_dst"
        and operation["implementation"] == "rc47_development_selected_execution_pending"
        and any(
            boundary.endswith("producer.js:940-979") for boundary in operation["source_boundary"]
        )
        for scenario in operation_map["scenarios"]
        for operation in scenario["operations"]
    )
    managed_operations = {
        operation["operation"]: operation
        for scenario in operation_map["scenarios"]
        if scenario["scenario"] == "reliable_reporting_managed_delivery_probe"
        for operation in scenario["operations"]
    }
    assert set(managed_operations) == {
        "prepare",
        "suppress_readiness",
        "advance_within_retention",
        "revoke_access",
    }
    assert all("preflight_passed" in row["implementation"] for row in managed_operations.values())
    billing_operations = {
        operation["operation"]: operation
        for scenario in operation_map["scenarios"]
        if scenario["scenario"] == "reliable_reporting_reconciled_billing_probe"
        for operation in scenario["operations"]
    }
    assert set(billing_operations) == {"prepare", "publish_adjustment"}
    assert all("preflight_passed" in row["implementation"] for row in billing_operations.values())

    controller_probe = (DATA / "ts_reporting_controller_probe.cjs").read_text()
    assert "handleTestControllerRequest" in controller_probe
    assert "createReportingProducer" in controller_probe
    assert "PostgresReportingLedgerStore" in controller_probe
    assert "blocking_acceptance: false" in controller_probe
    assert "literal_storyboard_identity_match" in controller_probe

    core_server = (DATA / "ts_core_server.cjs").read_text()
    assert "createSyncReportingStatusHandler" in core_server
    assert "consumer_status_task: 'sync_reporting_status'" in core_server
    assert "mcp.registerTool('sync_reporting_status'" in core_server
    assert "TOKEN_BINDINGS" not in core_server
    assert "auth-token-a" not in core_server and "auth-token-b" not in core_server
    assert "corePrivateInputs" in core_server
    assert "process_start_token" in core_server
    assert "startup_proof_sha256" in core_server
    assert "seller_package" in core_server
    assert "wire_adcp_version" in core_server
    assert "flag: 'wx', mode: 0o600" in core_server

    controlled_store = (DATA / "ts_controlled_reporting_fixture.cjs").read_text()
    controlled_server = (DATA / "ts_controlled_reporting_server.cjs").read_text()
    controlled_probe = (DATA / "ts_controlled_reporting_probe.cjs").read_text()
    controlled_http_probe = (DATA / "ts_controlled_reporting_http_probe.cjs").read_text()
    assert "class ControlledReportingStore" in controlled_store
    assert "syncConsumerStatusBatch" in controlled_store
    assert "literal reporting vector digest changed" in controlled_store
    assert "TOOL_INPUT_SHAPE" in controlled_server
    assert "mcp.registerTool('comply_test_controller'" in controlled_server
    assert "createReportingStatusHandler" in controlled_server
    assert "createSyncReportingStatusHandler" in controlled_server
    assert "controlled_clock_fixture_not_postgresql" in controlled_server
    assert "probe_scheduler_dst_unavailable" in controlled_server
    assert "reporting-revision.ecc62efa00946aa1e2788ad9" in controlled_probe
    assert "idempotent replay must return the retained ordered result" in controlled_probe
    assert "blocking_acceptance: false" in controlled_probe
    assert "ProtocolClient.callTool" in controlled_http_probe
    assert "reporting-revision.ecc62efa00946aa1e2788ad9" in controlled_http_probe
    assert "sync_reporting_status" in controlled_http_probe
    assert "focused_real_mcp_preflight_not_cli_storyboard_execution" in controlled_http_probe

    managed_fixture = (DATA / "ts_managed_reporting_fixture.cjs").read_text()
    managed_server = (DATA / "ts_managed_reporting_server.cjs").read_text()
    managed_probe = (DATA / "ts_managed_reporting_probe.cjs").read_text()
    managed_http_probe = (DATA / "ts_managed_reporting_http_probe.cjs").read_text()
    assert "PostgresReportingLedgerStore" in managed_fixture
    assert "PostgresReportingManagedDeliveryStore" in managed_fixture
    assert "createReportingManagedDeliveryRuntime" in managed_fixture
    assert "authorizeDestination" in managed_fixture
    assert managed_fixture.index("installBinding") < managed_fixture.index("putObligation")
    assert "runtime.readResource" in managed_fixture
    assert "revokeDestination" in managed_fixture
    assert "runtime.runWorker" in managed_fixture
    assert "reportingCanonicalAdjustmentSha256V1" in managed_fixture
    assert "core.commitAdjustment" in managed_fixture
    assert "syncReceiptBatch" not in managed_fixture
    assert "mcp.registerTool('comply_test_controller'" in managed_server
    assert "mcp.registerTool('sync_reporting_receipts'" in managed_server
    assert "runtime.syncReportingReceipts" in managed_server
    assert "focused_public_controller_preflight_not_storyboard_execution" in managed_probe
    assert "stale_after_terminal" in managed_probe
    assert "ProtocolClient.callTool" in managed_http_probe
    assert "focused_real_mcp_preflight_not_cli_storyboard_execution" in managed_http_probe
    assert "const account = { account_id: accountId, sandbox: true }" in managed_http_probe
    assert "expectSelectorRejected({ account_id: accountId })" in managed_http_probe
    assert "expectSelectorRejected({ account_id: accountId, sandbox: false })" in managed_http_probe
    assert "selector_negatives: selectorNegatives" in managed_http_probe
    assert "probe_scheduler_dst" not in managed_fixture
    assert "blocking_acceptance: false" in managed_probe
    assert "blocking_acceptance: false" in managed_http_probe

    abort_helper = (DATA / "ts_probe_abort.cjs").read_text()
    assert "controller.abort(error)" in abort_helper
    assert "process.once('SIGINT'" in abort_helper
    assert "process.once('SIGTERM'" in abort_helper
    for probe in [
        controlled_http_probe,
        managed_http_probe,
        (DATA / "ts_core_buyer.cjs").read_text(),
        (DATA / "ts_mcp_buyer.cjs").read_text(),
    ]:
        assert "callWithProbeAbort" in probe
        assert "signal," in probe or "signal, transport" in probe

    facade_probe = (DATA / "ts_primary_facade_gap.cjs").read_text()
    assert "semantic_lane_complete: false" in facade_probe
    assert "surface_available: !gapReproduced" in facade_probe
    assert "surface_available_nonsemantic" in facade_probe

    assert "WebhookReceiver" in receiver_server
    assert "CachingRevocationChecker" in receiver_server
    assert "RevocationListFetcher" in receiver_server


def test_ts_version_and_abort_helpers_fail_closed_without_sdk_execution() -> None:
    receiver_server = (DATA / "signing" / "webhook_receiver_server.py").read_text()
    receiver_client = (DATA / "signing" / "webhook_receiver_client.py").read_text()
    script = f"""
      const assert = require('node:assert/strict');
      const nodeCrypto = require('node:crypto');
      const version = require({json.dumps(str(DATA / 'ts_adcp_version.cjs'))});
      const abort = require({json.dumps(str(DATA / 'ts_probe_abort.cjs'))});
      const controlled = require({json.dumps(str(DATA / 'ts_controlled_reporting_server.cjs'))});
      const managed = require({json.dumps(str(DATA / 'ts_managed_reporting_server.cjs'))});
      const core = require({json.dumps(str(DATA / 'ts_core_server.cjs'))});
      const privateInputsModule = require({json.dumps(str(DATA / 'ts_private_inputs.cjs'))});
      assert.equal(version.wireAdcpVersion('3.2.0-rc.6'), '3.2-rc.6');
      assert.equal(version.wireAdcpVersion('3.1.20'), '3.1');
      assert.equal(version.wireAdcpVersion('3.2.1'), '3.2');
      assert.equal(version.wireAdcpVersion('3.2.1-rc.6'), '3.2-rc.6');
      assert.throws(() => version.wireAdcpVersion('3.2'));
      assert.throws(() => version.wireAdcpVersion('03.2.1'));
      assert.throws(() => version.wireAdcpVersion('3.2.1-rc..6'));
      assert.throws(() => version.wireAdcpVersion('3.2.1-rc.06'));
      assert.equal(version.assertCapabilityVersion, undefined);
      version.assertFixtureCapabilityVersion({{
        adcp_version: '3.2-rc.6', adcp: {{ supported_versions: ['3.2-rc.6'] }},
      }}, '3.2.0-rc.6');
      assert.throws(() => version.assertFixtureCapabilityVersion({{
        adcp_version: '3.2-rc.4', adcp: {{ supported_versions: ['3.2-rc.4'] }},
      }}, '3.2.0-rc.6'));
      assert.throws(() => version.assertFixtureCapabilityVersion({{
        adcp_version: '3.2-rc.6', adcp: {{ supported_versions: ['3.2-rc.6', '3.1'] }},
      }}, '3.2.0-rc.6'));
      let canonicalPayload;
      const validator = value => {{ canonicalPayload = value; return true; }};
      validator.errors = [];
      const api = {{ schemas: {{
        getCanonicalToolValidator(tool, variant, options) {{
          assert.equal(tool, 'get_adcp_capabilities');
          assert.equal(variant, 'sync');
          assert.deepEqual(options, {{ adcpVersion: '3.2.0-rc.6' }});
          return validator;
        }},
      }} }};
      const advertised = controlled.validateAdvertisedCapabilities(api, '3.2.0-rc.6');
      assert.equal(advertised.adcp_version, '3.2-rc.6');
      assert.deepEqual(canonicalPayload, {{ ...advertised, status: 'completed' }});
      assert.throws(() => controlled.validateAdvertisedCapabilities(
        api,
        '3.2.0-rc.6',
        controlled.capabilities('3.2.0-rc.4'),
      ), /wire version does not match/);
      (async () => {{
        const outputs = [];
        const fakeApi = {{
          expectedIntegrity: 'sha512-fixture',
          installed: {{ name: '@adcp/sdk', version: '14.0.0-test' }},
          schemas: api.schemas,
        }};
        await controlled.main(['--probe', '--adcp-version', '3.2.0-rc.6'], {{
          loadPinned: () => fakeApi,
          createControlledController: () => ({{ factory: {{ scenarios: ['core'] }} }}),
          writeOutput: value => outputs.push(JSON.parse(value)),
        }});
        class FakePool {{
          async query() {{ return {{}}; }}
          async end() {{}}
        }}
        const managedApi = {{
          ...fakeApi,
          pg: {{ Pool: FakePool }},
        }};
        await managed.main([
          '--probe', '--mode', 'managed', '--pg-url', 'postgresql://fixture/db',
          '--adcp-version', '3.2.0-rc.6',
        ], {{
          loadPinned: () => managedApi,
          createManagedReportingFixture: async () => ({{
            mode: 'managed', runtime: {{ reportingDeliveryCapabilities: {{ supported: true }} }},
          }}),
          writeOutput: value => outputs.push(JSON.parse(value)),
        }});
        await core.run(['--probe', '--adcp-version', '3.2.0-rc.6'], {{
          packageContext: () => ({{
            expectedIntegrity: 'sha512-fixture', installed: {{ version: '14.0.0-test' }},
            packageName: '@adcp/sdk',
          }}),
          loadPublicSurface: () => fakeApi,
          emit: (_stream, value) => outputs.push(value),
          stdout: {{}},
        }});
        assert.deepEqual(outputs.map(value => value.kind), [
          'controlled_reporting_storyboard_server_probe',
          'managed_reporting_storyboard_server_probe',
          'reporting_interop_ts_core_server_probe',
        ]);
        const rejectingApi = {{ schemas: {{
          getCanonicalToolValidator() {{
            const reject = () => false; reject.errors = [{{ message: 'wrong version' }}];
            return reject;
          }},
        }} }};
        await assert.rejects(controlled.main(
          ['--probe', '--adcp-version', '3.2.0-rc.6'],
          {{
            loadPinned: () => rejectingApi,
            createControlledController: () => ({{ factory: {{ scenarios: [] }} }}),
            writeOutput: () => {{}},
          }},
        ), /wrong version/);
        await assert.rejects(managed.main([
          '--probe', '--mode', 'managed', '--pg-url', 'postgresql://fixture/db',
          '--adcp-version', '3.2.0-rc.6',
        ], {{
          loadPinned: () => ({{ ...managedApi, schemas: rejectingApi.schemas }}),
          createManagedReportingFixture: async () => ({{
            mode: 'managed', runtime: {{ reportingDeliveryCapabilities: {{ supported: true }} }},
          }}),
          writeOutput: () => {{}},
        }}), /wrong version/);
        await assert.rejects(core.run(['--probe', '--adcp-version', '3.2.0-rc.6'], {{
          packageContext: () => ({{
            expectedIntegrity: 'sha512-fixture', installed: {{ version: '14.0.0-test' }},
            packageName: '@adcp/sdk',
          }}),
          loadPublicSurface: () => rejectingApi,
          emit: () => {{}},
          stdout: {{}},
        }}), /wrong version/);
        let controlledFixtureCalls = 0;
        await assert.rejects(controlled.main([
          '--adcp-version', '3.2.0-rc.6', '--port', '55555',
          '--ready-file', '/owned/controlled.ready',
        ], {{
          loadPinned: () => fakeApi,
          environment: {{
            ADCP_INTEROP_TS_STORYBOARD_AUTH_TOKEN: 'short',
            ADCP_INTEROP_TS_STORYBOARD_STARTUP_PROOF: 'b'.repeat(64),
          }},
          createControlledController() {{ controlledFixtureCalls += 1; return {{}}; }},
        }}), /strong per-run private input/);
        assert.equal(controlledFixtureCalls, 0);
        await assert.rejects(core.run([
          '--adcp-version', '3.2.0-rc.6', '--port', '55555',
        ], {{
          packageContext: () => ({{
            expectedIntegrity: 'sha512-fixture', installed: {{ version: '14.0.0-test' }},
            packageName: '@adcp/sdk',
          }}),
          loadPublicSurface: () => fakeApi,
          environment: {{
            ADCP_INTEROP_TS_CORE_AUTH_TOKEN_A: 'short',
            ADCP_INTEROP_TS_CORE_AUTH_TOKEN_B: 'b'.repeat(43),
            ADCP_INTEROP_TS_CORE_STARTUP_PROOF: 'c'.repeat(64),
          }},
        }}), /strong per-run private input/);
        const strongA = nodeCrypto.randomBytes(32).toString('base64url');
        const strongB = nodeCrypto.randomBytes(32).toString('base64url');
        const privateEnvironment = {{
          ADCP_INTEROP_TS_CORE_AUTH_TOKEN_A: strongA,
          ADCP_INTEROP_TS_CORE_AUTH_TOKEN_B: strongB,
          ADCP_INTEROP_TS_CORE_STARTUP_PROOF: nodeCrypto.randomBytes(32).toString('hex'),
        }};
        assert.throws(() => core.corePrivateInputs({{
          ...privateEnvironment, ADCP_INTEROP_TS_CORE_AUTH_TOKEN_A: 'fixed-a',
        }}), /strong per-run private input/);
        assert.throws(() => core.corePrivateInputs({{
          ...privateEnvironment, ADCP_INTEROP_TS_CORE_AUTH_TOKEN_B: strongA,
        }}), /distinct per-run tokens/);
        const privateInputs = core.corePrivateInputs(privateEnvironment);
        const bindings = core.authenticationBindings(privateInputs);
        assert.equal(bindings.get(strongA).account_id, 'interop-account-a');
        const storyboardEnvironment = {{
          ADCP_INTEROP_TS_STORYBOARD_AUTH_TOKEN: strongA,
          ADCP_INTEROP_TS_STORYBOARD_STARTUP_PROOF: privateInputs.startupProof,
        }};
        assert.deepEqual(
          privateInputsModule.storyboardPrivateInputs(storyboardEnvironment),
          {{ authToken: strongA, startupProof: privateInputs.startupProof }},
        );
        assert.throws(() => privateInputsModule.storyboardPrivateInputs({{
          ...storyboardEnvironment, ADCP_INTEROP_TS_STORYBOARD_STARTUP_PROOF: 'short',
        }}), /strong per-run private input/);
        const ready = core.buildReadyRecord({{
          expectedIntegrity: 'sha512-fixture', installed: {{ version: '14.0.0-test' }},
          packageName: '@adcp/sdk',
        }}, {{
          adcpVersion: '3.2.0-rc.6', authBindings: bindings, port: 55555,
          sellerRole: 'candidate', startupProof: privateInputs.startupProof,
          url: 'http://127.0.0.1:55555/mcp',
        }});
        const encodedReady = JSON.stringify(ready);
        assert.equal(encodedReady.includes(strongA), false);
        assert.equal(encodedReady.includes(strongB), false);
        assert.equal(encodedReady.includes(privateInputs.startupProof), false);
        assert.equal(ready.seller_package.package_role, 'candidate');
        assert.equal(ready.seller_package.protocol, '3.2.0-rc.6');
        for (const fixtureReady of [
          controlled.buildReadyRecord(fakeApi, {{
            adcpVersion: '3.2.0-rc.6', authToken: strongA, port: 55555,
            startupProof: privateInputs.startupProof,
            url: 'http://127.0.0.1:55555/mcp',
          }}),
          managed.buildReadyRecord(fakeApi, {{
            adcpVersion: '3.2.0-rc.6', authToken: strongA, mode: 'managed', port: 55555,
            startupProof: privateInputs.startupProof,
            url: 'http://127.0.0.1:55555/mcp',
          }}),
        ]) {{
          const encoded = JSON.stringify(fixtureReady);
          assert.equal(encoded.includes(strongA), false);
          assert.equal(encoded.includes(privateInputs.startupProof), false);
          assert.equal(fixtureReady.startup_proof_sha256.length, 64);
        }}
        const boundary = abort.createProbeAbort('unit');
        await assert.rejects(abort.callWithProbeAbort(boundary, async () => {{
          throw new Error('first failure');
        }}), /first failure/);
        assert.equal(boundary.signal.aborted, true);
        boundary.dispose();
      }})().catch(error => {{ console.error(error); process.exitCode = 1; }});
    """
    subprocess.run(["node", "-e", script], check=True, cwd=ROOT)
    assert "urllib.request.urlopen" in receiver_server
    assert '"normative_019_match"' in receiver_client
    assert '"blocking_acceptance"' in receiver_client
    assert '"historical_reproducer_mode"' in receiver_client
    assert '"replay_persistence_exercised": False' in receiver_client
    assert '"stale_failure_classification"' in receiver_client
    assert "RevocationListFreshnessError" in receiver_client
    assert "stale_historical_red" in receiver_client
    assert '"emitted_headers"' not in receiver_client
    assert '"private_key"' not in receiver_client


def test_managed_server_owns_every_resource_phase_with_recording_fakes() -> None:
    script = r"""
      const assert = require('node:assert/strict');
      const managed = require(__MANAGED__);

      const strongEnvironment = {
        ADCP_INTEROP_TS_STORYBOARD_AUTH_TOKEN: 'a'.repeat(43),
        ADCP_INTEROP_TS_STORYBOARD_STARTUP_PROOF: 'b'.repeat(64),
      };
      const normalArgs = [
        '--mode', 'managed', '--pg-url', 'postgresql://fixture/db',
        '--adcp-version', '3.2.0-rc.6', '--port', '55555',
        '--ready-file', '/owned/ready.json',
      ];

      function fixture(kind) {
        const events = [];
        let poolCount = 0;
        class FakePool {
          constructor() {
            this.id = ++poolCount;
            events.push(`new:${this.id}`);
            if (kind === 'fixture-pool-constructor' && this.id === 2) {
              throw new Error('fixture pool constructor failed');
            }
          }
          async query(statement) {
            const verb = String(statement).split(' ')[0];
            events.push(`query:${this.id}:${verb}`);
            if (kind === 'schema-create' && verb === 'CREATE') {
              throw new Error('schema create failed');
            }
          }
          async end() {
            events.push(`end:${this.id}`);
            if (kind === 'cleanup-and-primary' && this.id === 2) {
              throw new Error('fixture pool cleanup failed');
            }
          }
        }
        const validator = value => {
          events.push(`validate:${value.adcp_version}`);
          return kind !== 'canonical';
        };
        validator.errors = [{ message: 'canonical rejection' }];
        const api = {
          expectedIntegrity: 'sha512-fixture',
          installed: { name: '@adcp/sdk', version: '14.0.0-test' },
          pg: { Pool: FakePool },
          schemas: { getCanonicalToolValidator() { return validator; } },
          server: {
            verifyApiKey() {
              events.push('verify-api-key');
              if (kind === 'authentication') throw new Error('authentication setup failed');
              return {};
            },
            serve(_factory, options) {
              events.push('serve');
              if (kind === 'serve') throw new Error('serve construction failed');
              const listeners = new Map();
              const server = {
                closed: false,
                on(event, listener) {
                  events.push(`server:on:${event}`);
                  listeners.set(event, listener);
                  return this;
                },
                removeListener(event, listener) {
                  events.push(`server:remove:${event}`);
                  if (listeners.get(event) === listener) listeners.delete(event);
                  return this;
                },
                emit(event, value) {
                  const listener = listeners.get(event);
                  if (listener) {
                    listener(value);
                    return true;
                  }
                  if (event === 'error') throw value;
                  return false;
                },
                close(callback) {
                  events.push('server:close');
                  this.closed = true;
                  callback(
                    ['close-failure', 'bind-error-close-failure'].includes(kind)
                      ? new Error('server close failed')
                      : undefined,
                  );
                },
              };
              queueMicrotask(() => {
                if (kind === 'bind-error-close-failure') {
                  server.emit('error', new Error('bind failed before listening'));
                } else {
                  options.onListening('http://127.0.0.1:55555/mcp');
                }
              });
              return server;
            },
          },
        };
        const dependencies = {
          loadPinned: () => api,
          environment: strongEnvironment,
          async createManagedReportingFixture() {
            events.push('fixture');
            if (['fixture', 'cleanup-and-primary'].includes(kind)) {
              throw new Error('fixture construction failed');
            }
            return {
              mode: 'managed',
              runtime: { reportingDeliveryCapabilities: { supported: true } },
            };
          },
          registerShutdownHandlers() {
            events.push('handlers:install');
            return () => events.push('handlers:remove');
          },
          writeReadyFile(_path, _value, options) {
            events.push(`ready:${options.flag}:${options.mode.toString(8)}`);
            if (kind === 'ready') throw new Error('ready file already exists');
          },
          writeOutput() { events.push('output'); },
        };
        return { api, dependencies, events, get poolCount() { return poolCount; } };
      }

      (async () => {
        {
          const state = fixture('missing-version');
          await assert.rejects(managed.main([
            '--mode', 'managed', '--pg-url', 'postgresql://fixture/db', '--probe',
          ], state.dependencies), /missing --adcp-version/);
          assert.equal(state.poolCount, 0);
        }
        {
          const state = fixture('bad-private-input');
          await assert.rejects(managed.main(normalArgs, {
            ...state.dependencies,
            environment: {
              ...strongEnvironment,
              ADCP_INTEROP_TS_STORYBOARD_AUTH_TOKEN: 'short',
            },
          }), /strong per-run private input/);
          assert.equal(state.poolCount, 0);
        }
        for (const [kind, message] of [
          ['schema-create', 'schema create failed'],
          ['fixture-pool-constructor', 'fixture pool constructor failed'],
          ['fixture', 'fixture construction failed'],
          ['canonical', 'canonical rejection'],
          ['authentication', 'authentication setup failed'],
          ['serve', 'serve construction failed'],
          ['ready', 'ready file already exists'],
        ]) {
          const state = fixture(kind);
          await assert.rejects(managed.main(normalArgs, state.dependencies), new RegExp(message));
          if (kind === 'schema-create') {
            assert.deepEqual(state.events, ['new:1', 'query:1:CREATE', 'end:1']);
            continue;
          }
          if (kind === 'fixture-pool-constructor') {
            assert.equal(state.events.includes('end:2'), false);
          } else {
            assert.equal(state.events.includes('end:2'), true, `${kind}: fixture pool not ended`);
          }
          assert.equal(state.events.includes('query:1:DROP'), true, `${kind}: schema not dropped`);
          assert.equal(state.events.includes('end:1'), true, `${kind}: bootstrap not ended`);
          if (kind === 'ready') {
            assert.ok(state.events.indexOf('server:close') < state.events.indexOf('end:2'));
          }
        }
        {
          const state = fixture('probe-success');
          await managed.main([
            '--probe', '--mode', 'managed', '--pg-url', 'postgresql://fixture/db',
            '--adcp-version', '3.2.0-rc.6',
          ], state.dependencies);
          assert.equal(state.events.includes('serve'), false);
          assert.equal(state.events.filter(value => value === 'end:2').length, 1);
          assert.equal(state.events.filter(value => value === 'query:1:DROP').length, 1);
          assert.equal(state.events.filter(value => value === 'end:1').length, 1);
        }
        {
          const state = fixture('success');
          const running = await managed.main(normalArgs, state.dependencies);
          assert.equal(state.events.includes('server:close'), false);
          await running.close();
          assert.equal(state.events.filter(value => value === 'server:close').length, 1);
          assert.equal(state.events.filter(value => value === 'end:2').length, 1);
          assert.equal(state.events.filter(value => value === 'query:1:DROP').length, 1);
          assert.equal(state.events.filter(value => value === 'end:1').length, 1);
          await running.close();
          assert.equal(state.events.filter(value => value === 'server:close').length, 1);
        }
        {
          const state = fixture('close-failure');
          const running = await managed.main(normalArgs, state.dependencies);
          await assert.rejects(running.close(), /resource cleanup failed/);
          await assert.rejects(running.close(), /resource cleanup failed/);
          assert.equal(state.events.filter(value => value === 'server:close').length, 1);
          assert.equal(state.events.filter(value => value === 'end:2').length, 1);
          assert.equal(state.events.filter(value => value === 'query:1:DROP').length, 1);
          assert.equal(state.events.filter(value => value === 'end:1').length, 1);
        }
        {
          const state = fixture('bind-error-close-failure');
          await assert.rejects(managed.main(normalArgs, state.dependencies), error => {
            assert.equal(error instanceof AggregateError, true);
            assert.match(error.errors[0].message, /bind failed before listening/);
            assert.match(error.errors[1].message, /resource cleanup failed/);
            return true;
          });
          assert.equal(state.events.includes('ready:wx:600'), false);
          assert.equal(state.events.includes('output'), false);
          assert.equal(state.events.filter(value => value === 'server:on:error').length, 1);
          assert.equal(state.events.filter(value => value === 'server:close').length, 1);
          assert.equal(state.events.filter(value => value === 'server:remove:error').length, 1);
          assert.equal(state.events.filter(value => value === 'end:2').length, 1);
          assert.equal(state.events.filter(value => value === 'query:1:DROP').length, 1);
          assert.equal(state.events.filter(value => value === 'end:1').length, 1);
          assert.ok(state.events.indexOf('server:close') < state.events.indexOf('end:2'));
          assert.ok(state.events.indexOf('server:remove:error') < state.events.indexOf('end:2'));
        }
        {
          const state = fixture('cleanup-and-primary');
          await assert.rejects(managed.main(normalArgs, state.dependencies), error => {
            assert.equal(error instanceof AggregateError, true);
            assert.match(error.errors[0].message, /fixture construction failed/);
            assert.match(error.errors[1].message, /resource cleanup failed/);
            return true;
          });
          assert.equal(state.events.includes('query:1:DROP'), true);
          assert.equal(state.events.includes('end:1'), true);
        }
        process.stdout.write('managed-resource-phase-fakes-complete\n');
      })().catch(error => { console.error(error); process.exitCode = 1; });
    """.replace(
        "__MANAGED__", json.dumps(str(DATA / "ts_managed_reporting_server.cjs"))
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.stdout.splitlines() == ["managed-resource-phase-fakes-complete"]


def test_webhook_receiver_result_disposition_cannot_promote_a_historical_red() -> None:
    client = load_python_module(
        "reporting_interop_webhook_receiver_client",
        DATA / "signing" / "webhook_receiver_client.py",
    )

    assert client._disposition(
        normative_match=False,
        receiver_http_mapping_match=False,
        historical_red_reproduced=True,
        allow_historical_red=False,
    ) == ("blocking_red", False, 1)
    assert client._disposition(
        normative_match=False,
        receiver_http_mapping_match=False,
        historical_red_reproduced=True,
        allow_historical_red=True,
    ) == ("historical_red_reproduced", False, 0)
    assert client._disposition(
        normative_match=True,
        receiver_http_mapping_match=False,
        historical_red_reproduced=False,
        allow_historical_red=False,
    ) == ("blocking_red", False, 1)
    assert client._disposition(
        normative_match=True,
        receiver_http_mapping_match=True,
        historical_red_reproduced=False,
        allow_historical_red=False,
    ) == ("passed", True, 0)
    assert client._disposition(
        normative_match=True,
        receiver_http_mapping_match=True,
        historical_red_reproduced=False,
        allow_historical_red=True,
    ) == ("historical_red_not_reproduced", False, 1)
    assert client._disposition(
        normative_match=False,
        receiver_http_mapping_match=False,
        historical_red_reproduced=False,
        allow_historical_red=True,
    ) == ("historical_red_not_reproduced", False, 1)


def test_typescript_semantic_reconciliation_requires_managed_evidence() -> None:
    corpus = load("corpus.json")
    applicability = load("applicability.json")
    scenario = next(
        row
        for row in corpus["scenarios"]
        if row["id"] == "foundation.reconciliation.typescript_public"
    )
    override = next(row for row in applicability["overrides"] if row["scenario"] == scenario["id"])

    assert scenario["requires_server_tier"] == "managed_delivery"
    assert override["requires_server_tier"] == "managed_delivery"


def test_official_precedence_probe_inputs_preserve_supplied_bytes() -> None:
    expected = {
        "fixture.json": "dc16c44297a315122c4450bdb4c7278b5635894f7a60fb0266456b8d083da013",
        "pins.json": "a56a77f6db242704d134af7f1c7a514b3f904f006180744ad3123716ce7a9ad1",
        "repro-rc42.cjs": "56b5540918fcedf23e7ff2035708a3b0ccef909f4afd72bf8a4ea2d1630bfdef",
        "cases.json": "fd7c0167a45da95b6c756f85cc39b7845210d1a929ecc0d6ef0a10a5b658c100",
    }
    root = DATA / "official_precedence"

    assert (root / "fixture.json").stat().st_size == 9908
    assert (root / "fixture.json").read_bytes().endswith(b"\n")
    assert not (root / "fixture.json").read_bytes().endswith(b"\n\n")
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_official_precedence_candidate_adapter_is_separate_and_exact() -> None:
    root = DATA / "official_precedence"
    candidate = json.loads((root / "pins-rc45.json").read_text())
    adapter = (root / "repro-candidate.cjs").read_text()

    assert candidate["version"] == "14.0.0-rc.45"
    assert candidate["tarballSha256"] == (
        "a0952ed8edaaad958bdb8f474c4f5cb68333ee272e7a8f3419d12930e9c57e6b"
    )
    assert candidate["fixtureSha256"] == (
        "dc16c44297a315122c4450bdb4c7278b5635894f7a60fb0266456b8d083da013"
    )
    assert "require('./pins.json')" in adapter
    assert "require('./pins-rc45.json')" in adapter
    assert "require('./repro-rc42.cjs')" in adapter


def test_transport_header_defect_stays_visible_nonblocking() -> None:
    corpus = load("corpus.json")
    applicability = load("applicability.json")
    scenario_id = "foundation.transport.response_headers_metadata"

    assert any(row["id"] == scenario_id for row in corpus["scenarios"])
    override = next(row for row in applicability["overrides"] if row["scenario"] == scenario_id)
    assert override["all"] == "visible_nonblocking"


def test_official_precedence_manifest_binds_every_case_identity_and_expectation() -> None:
    manifest = json.loads((DATA / "official_precedence" / "cases.json").read_text())
    cases = manifest["cases"]

    assert manifest["protocol_owned"] is False
    assert manifest["scope"] == "supplemental_shared_neutral_regression"
    assert len(cases) == 17
    assert len({row["id"] for row in cases}) == 17
    assert cases[-1]["id"] == "official_precedence/single_official"
    matrix = cases[:-1]
    assert {tuple(row["id"].removeprefix("official_precedence/").split("/")) for row in matrix} == {
        (finality, artifact, linked, order)
        for finality in ("snapshot", "official")
        for artifact in ("both_artifacts", "official_without_materialization")
        for linked in ("unlinked", "linked")
        for order in ("natural", "reversed")
    }
    assert all("reportingRevisionId=revision-august-official" in row["required"] for row in cases)
