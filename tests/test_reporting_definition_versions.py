"""AdCP 3.3 report definitions at contract_version 1.2 serve 3.3 callers only (#1906)."""

from __future__ import annotations

import hashlib
import json
import logging
import warnings
from dataclasses import replace
from typing import Any

import pytest

from adcp.reporting.definition_versions import (
    ReportDefinitionVersions,
    caller_accepts_1_2,
    definition_contract_version,
    served_reporting_delivery,
)
from adcp.reporting.fixtures import redacted_capabilities
from adcp.reporting.ledger import ReportingConfiguration, ReportingDefinitionBinding
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.service import (
    ReliableReportingConfigurationError,
    ReliableReportingService,
    ReportingAccountContext,
)
from adcp.reporting.source import reporting_source_capabilities_sha256_v1
from adcp.reporting.testing import ScriptedReportingAdapter
from adcp.server import ADCPHandler
from tests.test_reliable_reporting_service import (
    DEFINITION,
    _account_context,
    _capability_offering,
    _configuration,
    _rows,
)

SHA_11 = DEFINITION.report_definition_sha256  # "a" * 64
SHA_12 = "d" * 64
URI_12 = "https://contracts.example.test/reporting/paid-media-daily-v1-2"
CALLER_33 = "3.3-rc.3"
CALLER_32 = "3.2"


# --------------------------------------------------------------------- units


@pytest.mark.parametrize("version", [None, "3.3-rc.3", "3.3.0-rc.3", "3.3", "3.4", "4.0", " 3.3 "])
def test_caller_accepts_1_2_for_3_3_and_later(version: str | None) -> None:
    assert caller_accepts_1_2(version) is True


@pytest.mark.parametrize("version", ["3.2", "3.2-rc.7", "3.1", "3.0", "bogus", ""])
def test_caller_below_3_3_or_unparseable_does_not_accept_1_2(version: str) -> None:
    assert caller_accepts_1_2(version) is False


def test_definition_contract_version_reads_bytes_str_and_mapping() -> None:
    document = {"contract_version": "1.2", "other": 1}
    assert definition_contract_version(json.dumps(document).encode()) == "1.2"
    assert definition_contract_version(json.dumps(document)) == "1.2"
    assert definition_contract_version(document) == "1.2"
    assert definition_contract_version({"contract_version": "1.0"}) == "1.0"


@pytest.mark.parametrize(
    "value",
    [
        b"not json",
        "{",
        "[]",
        b'"1.2"',
        {},
        {"contract_version": "2.0"},
        {"contract_version": 1.2},
        {"contract_version": None},
    ],
)
def test_definition_contract_version_is_none_when_absent_or_invalid(value: Any) -> None:
    assert definition_contract_version(value) is None


@pytest.mark.parametrize("key", ["", "abc", "g" * 64, "a" * 63, None, 7])
def test_declared_keys_must_be_sha256_digests(key: Any) -> None:
    with pytest.raises(ReliableReportingConfigurationError, match="hex digests"):
        ReportDefinitionVersions({key: "1.1"})


@pytest.mark.parametrize("version", ["2.0", "1.3", "", None])
def test_declared_values_must_be_a_known_contract_version(version: Any) -> None:
    with pytest.raises(ReliableReportingConfigurationError, match="must be one of"):
        ReportDefinitionVersions({SHA_12: version})


def test_unknown_undeclared_policy_is_refused() -> None:
    with pytest.raises(ReliableReportingConfigurationError, match="undeclared_report_definitions"):
        ReportDefinitionVersions(undeclared="fail_closed")  # type: ignore[arg-type]


def test_declared_keys_are_case_and_whitespace_insensitive() -> None:
    versions = ReportDefinitionVersions({f" {SHA_12.upper()} ": "1.2"})
    assert versions.version(SHA_12) == "1.2"
    assert versions.version(f"  {SHA_12.upper()}") == "1.2"
    assert versions.known() == {SHA_12: "1.2"}
    assert versions.version(None) is None
    assert versions.version("e" * 64) is None


def test_record_adds_versions_and_ignores_none() -> None:
    versions = ReportDefinitionVersions()
    versions.record(SHA_12, None, source="bytes")
    assert versions.known() == {}
    versions.record(SHA_12, "1.2", source="bytes")
    versions.record(SHA_12, "1.2", source="bytes")  # agreeing is idempotent
    assert versions.known() == {SHA_12: "1.2"}


def test_record_refuses_a_version_contradicting_the_declaration() -> None:
    versions = ReportDefinitionVersions({SHA_12: "1.1"})
    with pytest.raises(ReliableReportingConfigurationError, match="declares 1.1") as caught:
        versions.record(SHA_12, "1.2", source="its verifier's definition bytes")
    assert "contract_version 1.2" in str(caught.value)
    assert "its verifier's definition bytes" in str(caught.value)
    assert versions.known() == {SHA_12: "1.1"}


def test_record_refuses_a_contradiction_with_an_earlier_recording() -> None:
    versions = ReportDefinitionVersions()
    versions.record(SHA_12, "1.2", source="bytes")
    with pytest.raises(ReliableReportingConfigurationError, match="declares 1.2"):
        versions.record(SHA_12, "1.1", source="other bytes")


def test_record_refuses_a_malformed_digest() -> None:
    with pytest.raises(ReliableReportingConfigurationError, match="hex digests"):
        ReportDefinitionVersions().record("nope", "1.2", source="bytes")


class _Key:
    def __init__(self, sha256: str) -> None:
        self.definition = ReportingDefinitionBinding(
            report_definition_uri=URI_12,
            report_definition_sha256=sha256,
            schema_version="1.0.0",
            schema_uri="https://contracts.example.test/schema",
            schema_sha256="b" * 64,
        )


class _Registry:
    def __init__(self, verifiers: dict[str, Any]) -> None:
        self._verifiers = verifiers

    def require(self, key: _Key) -> Any:
        return self._verifiers[key.definition.report_definition_sha256]


def _verifier_for(document: dict[str, Any] | bytes) -> tuple[str, Any]:
    raw = document if isinstance(document, bytes) else json.dumps(document).encode()
    digest = hashlib.sha256(raw).hexdigest()
    return digest, type("Verifier", (), {"definition_bytes": raw})()


def test_record_verified_reads_the_proven_definition_bytes() -> None:
    sha_12, verifier_12 = _verifier_for({"contract_version": "1.2"})
    sha_11, verifier_11 = _verifier_for({"contract_version": "1.1"})
    sha_x, verifier_x = _verifier_for(b"not json")
    registry = _Registry({sha_12: verifier_12, sha_11: verifier_11, sha_x: verifier_x})
    versions = ReportDefinitionVersions()
    versions.record_verified(registry, [_Key(sha_12), _Key(sha_11), _Key(sha_x), _Key("f" * 64)])
    # An unreadable document and a key with no verifier are skipped, not fatal.
    assert versions.known() == {sha_12: "1.2", sha_11: "1.1"}


def test_record_verified_refuses_a_declaration_the_bytes_contradict() -> None:
    sha_12, verifier_12 = _verifier_for({"contract_version": "1.2"})
    versions = ReportDefinitionVersions({sha_12: "1.0"})
    with pytest.raises(ReliableReportingConfigurationError, match="declares 1.0"):
        versions.record_verified(_Registry({sha_12: verifier_12}), [_Key(sha_12)])


def _offering(offering_id: str, sha256: Any, uri: str = URI_12) -> dict[str, Any]:
    return {
        "offering_id": offering_id,
        "report_definition_sha256": sha256,
        "report_definition_uri": uri,
    }


@pytest.mark.parametrize("caller", [None, CALLER_33, "3.4"])
def test_serves_everything_to_a_3_3_caller(caller: str | None) -> None:
    versions = ReportDefinitionVersions({SHA_12: "1.2"}, undeclared="withhold_from_pre_3_3")
    assert versions.serves(_offering("one-two", SHA_12), caller)
    assert versions.serves(_offering("undeclared", "e" * 64), caller)


@pytest.mark.parametrize("caller", [CALLER_32, "3.2-rc.7", "bogus"])
def test_withholds_a_1_2_offering_from_a_caller_below_3_3(caller: str) -> None:
    versions = ReportDefinitionVersions({SHA_12: "1.2", SHA_11: "1.1", "c" * 64: "1.0"})
    assert not versions.serves(_offering("one-two", SHA_12), caller)
    assert versions.serves(_offering("one-one", SHA_11), caller)
    assert versions.serves(_offering("one-zero", "c" * 64), caller)


def test_undeclared_offerings_follow_the_policy_below_3_3() -> None:
    undeclared = _offering("undeclared", "e" * 64)
    assert ReportDefinitionVersions().serves(undeclared, CALLER_32)
    assert ReportDefinitionVersions(undeclared="assume_pre_1_2").serves(undeclared, CALLER_32)
    strict = ReportDefinitionVersions(undeclared="withhold_from_pre_3_3")
    assert not strict.serves(undeclared, CALLER_32)
    assert strict.serves(undeclared, CALLER_33)
    assert not strict.serves({"offering_id": "no-digest"}, CALLER_32)


def test_refusal_message_for_a_1_2_offering_is_the_shared_wording() -> None:
    versions = ReportDefinitionVersions({SHA_12: "1.2"})
    assert versions.refusal_message(_offering("GAM_V12", SHA_12)) == (
        'Reporting offering "GAM_V12" serves AdCP 3.3 callers only: its report definition '
        "is at contract_version 1.2. Choose an offering from get_adcp_capabilities."
    )


def test_refusal_message_for_an_undeclared_offering_is_the_shared_wording() -> None:
    versions = ReportDefinitionVersions(undeclared="withhold_from_pre_3_3")
    assert versions.refusal_message(_offering("GAM_UNKNOWN", "e" * 64)) == (
        'Reporting offering "GAM_UNKNOWN" serves AdCP 3.3 callers only: its report '
        "definition's contract_version is undeclared. Choose an offering from "
        "get_adcp_capabilities."
    )


def test_warn_undeclared_names_the_offering_digest_uri_and_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    versions = ReportDefinitionVersions({SHA_11: "1.1"})
    offerings = [
        _offering("KNOWN", SHA_11),
        _offering("MYSTERY", "E" * 64, "https://contracts.example.test/mystery"),
        _offering("MYSTERY_TWIN", "e" * 64, "https://contracts.example.test/twin"),
        {"offering_id": "no-digest"},
    ]
    with caplog.at_level(logging.WARNING, logger="adcp.reporting.definition_versions"):
        with pytest.warns(UserWarning, match="cannot tell") as recorded:
            undeclared = versions.warn_undeclared(offerings)
    assert undeclared == ["e" * 64]
    expected = (
        "report_definition_versions[" + repr("e" * 64) + "] = '<1.0, 1.1 or 1.2>'  "
        "# https://contracts.example.test/mystery"
    )
    (warning,) = recorded
    message = str(warning.message)
    assert "1 advertised offering(s)" in message
    assert "offering 'MYSTERY'" in message
    assert expected in message
    assert "KNOWN" not in message and "MYSTERY_TWIN" not in message
    assert "served to every caller" in message
    (record,) = [r for r in caplog.records if r.name == "adcp.reporting.definition_versions"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == message


def test_warn_undeclared_states_the_strict_consequence() -> None:
    versions = ReportDefinitionVersions(undeclared="withhold_from_pre_3_3")
    with pytest.warns(UserWarning, match="withheld from buyers pinned below AdCP 3.3"):
        versions.warn_undeclared([_offering("MYSTERY", "e" * 64)])


def test_warn_undeclared_is_silent_when_every_version_is_known() -> None:
    versions = ReportDefinitionVersions({SHA_11: "1.1"})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert versions.warn_undeclared([_offering("KNOWN", SHA_11)]) == []
        assert versions.warn_undeclared([]) == []


def _block() -> dict[str, Any]:
    return {
        "supported": True,
        "offerings": [
            _offering("ONE_ONE", SHA_11),
            _offering("ONE_TWO", SHA_12),
            _offering("ONE_TWO_B", SHA_12),
        ],
        "reliability_statistics": [
            {"offering_id": "ONE_ONE", "n": 1},
            {"offering_id": "ONE_TWO", "n": 2},
        ],
    }


def test_served_reporting_delivery_drops_unserved_offerings_and_their_statistics() -> None:
    versions = ReportDefinitionVersions({SHA_11: "1.1", SHA_12: "1.2"})
    block = _block()
    served = served_reporting_delivery(block, versions, CALLER_32)
    assert [o["offering_id"] for o in served["offerings"]] == ["ONE_ONE"]
    assert served["reliability_statistics"] == [{"offering_id": "ONE_ONE", "n": 1}]
    assert served["supported"] is True
    assert len(block["offerings"]) == 3  # the input is not mutated


def test_served_reporting_delivery_prunes_empty_reliability_statistics() -> None:
    versions = ReportDefinitionVersions({SHA_11: "1.2", SHA_12: "1.2"})
    block = _block()
    block["offerings"] = [_offering("ONE_ONE", SHA_11), _offering("ONE_TWO", SHA_12)]
    block["offerings"][0] = _offering("KEEP", "c" * 64)
    block["reliability_statistics"] = [{"offering_id": "ONE_TWO", "n": 2}]
    served = served_reporting_delivery(block, versions, CALLER_32)
    assert [o["offering_id"] for o in served["offerings"]] == ["KEEP"]
    assert "reliability_statistics" not in served


def test_served_reporting_delivery_is_empty_when_nothing_remains() -> None:
    versions = ReportDefinitionVersions({SHA_11: "1.2", SHA_12: "1.2"})
    block = _block()
    assert served_reporting_delivery(block, versions, CALLER_32) == {}


def test_served_reporting_delivery_returns_the_block_when_all_are_served() -> None:
    versions = ReportDefinitionVersions({SHA_11: "1.1", SHA_12: "1.2"})
    block = _block()
    assert served_reporting_delivery(block, versions, CALLER_33) == block
    assert served_reporting_delivery(block, versions, None) == block
    only_old = {"offerings": [_offering("ONE_ONE", SHA_11)]}
    assert served_reporting_delivery(only_old, versions, CALLER_32) == only_old
    assert served_reporting_delivery({}, versions, CALLER_32) == {}


# ------------------------------------------------------------ core service


def _capabilities_with_definition(sha256: str) -> Any:
    capabilities = redacted_capabilities()
    payload = capabilities.model_dump(mode="json", exclude_none=True)
    for offering in payload["offerings"]:
        offering["contract"]["report_definition_sha256"] = sha256
        offering["contract"]["report_definition_uri"] = URI_12
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
    return type(capabilities).model_validate(payload)


def _offering_wire(route: str, *, one_two: bool) -> dict[str, Any]:
    offering = _capability_offering(route)
    if one_two:
        offering["report_definition_uri"] = URI_12
        offering["report_definition_sha256"] = SHA_12
    return offering


def _config(route: str, *, one_two: bool) -> ReportingConfiguration:
    configuration = _configuration(route)
    if not one_two:
        return configuration
    return replace(
        configuration,
        definition=replace(
            configuration.definition,
            report_definition_uri=URI_12,
            report_definition_sha256=SHA_12,
        ),
    )


def _service(
    routes: dict[str, bool], **options: Any
) -> tuple[ReliableReportingService, dict[str, bool]]:
    """A service with one route per entry; ``True`` marks the 1.2 definition."""

    def resolve(configuration: ReportingConfiguration) -> ReportingAccountContext:
        route = configuration.delivery_config_id.removesuffix("-delivery")
        return replace(
            _account_context(configuration),
            capability_offering=_offering_wire(route, one_two=routes[route]),
        )

    service = ReliableReportingService.memory(account_context=resolve, **options)
    for route, one_two in routes.items():
        capabilities = _capabilities_with_definition(SHA_12) if one_two else redacted_capabilities()
        service.sources.register(
            route,
            ScriptedReportingAdapter(capabilities, [_rows(1)]),
            capability_offerings=[_offering_wire(route, one_two=one_two)],
        )
    return service, routes


def _ids(block: dict[str, Any]) -> list[str]:
    return [offering["offering_id"] for offering in block.get("offerings", [])]


async def test_a_1_2_offering_is_advertised_to_3_3_callers_only() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    try:
        await service.start()
        assert _ids(service.capability_block(adcp_version=CALLER_33)) == ["GAM_SNAPSHOT_V1"]
        assert _ids(service.capability_block()) == ["GAM_SNAPSHOT_V1"]
        assert service.capability_block(adcp_version=CALLER_32) == {}
        assert service.capability_block(adcp_version="3.2-rc.7") == {}
    finally:
        await service.close()


async def test_only_the_1_2_offering_leaves_a_mixed_block_for_3_2_callers() -> None:
    service, _ = _service(
        {"gam": True, "freewheel": False},
        report_definition_versions={SHA_12: "1.2", SHA_11: "1.1"},
    )
    try:
        await service.start()
        assert sorted(_ids(service.capability_block(adcp_version=CALLER_33))) == [
            "FREEWHEEL_SNAPSHOT_V1",
            "GAM_SNAPSHOT_V1",
        ]
        assert _ids(service.capability_block(adcp_version=CALLER_32)) == ["FREEWHEEL_SNAPSHOT_V1"]
    finally:
        await service.close()


async def test_inject_capabilities_adds_no_reporting_when_nothing_is_served() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    base = {"supported_protocols": ["media_buy"]}
    try:
        await service.start()
        old = service.inject_capabilities(base, adcp_version=CALLER_32)
        assert "reporting_delivery" not in (old.get("media_buy") or {})
        assert "media_buy.reporting_delivery" not in old.get("experimental_features", [])
        new = service.inject_capabilities(base, adcp_version=CALLER_33)
        assert new["media_buy"]["reporting_delivery"]["offerings"]
        assert "media_buy.reporting_delivery" in new["experimental_features"]
    finally:
        await service.close()


async def test_configure_refuses_a_1_2_offering_below_3_3_and_binds_nothing() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    configuration = _config("gam", one_two=True)
    with pytest.raises(LedgerConflictError) as caught:
        await service.configure(configuration, adcp_version=CALLER_32)
    error = caught.value
    assert error.code == "UNSUPPORTED_FEATURE"
    assert error.field == "reporting_delivery_configs"
    assert error.details == {
        "offering_id": "GAM_SNAPSHOT_V1",
    }
    assert str(error) == (
        'Reporting offering "GAM_SNAPSHOT_V1" serves AdCP 3.3 callers only: its report '
        "definition is at contract_version 1.2. Choose an offering from get_adcp_capabilities."
    )
    assert service._bindings == {}
    assert await service.store.list_all_configurations() == ()

    await service.configure(configuration, adcp_version=CALLER_33)
    assert set(service._bindings) == {configuration.generation_key}


async def test_a_bound_1_2_configuration_resent_by_a_3_2_caller_is_refused() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    configuration = _config("gam", one_two=True)
    await service.configure(configuration, adcp_version=CALLER_33)
    with pytest.raises(LedgerConflictError) as caught:
        await service.configure(configuration, adcp_version=CALLER_32)
    assert caught.value.code == "UNSUPPORTED_FEATURE"
    assert set(service._bindings) == {configuration.generation_key}


async def test_configure_without_a_version_is_taken_as_the_sdk_release() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    await service.configure(_config("gam", one_two=True))
    assert len(service._bindings) == 1


@pytest.mark.filterwarnings("ignore:Reliable Reporting cannot tell")
async def test_strict_policy_refuses_an_undeclared_offering_at_3_2() -> None:
    service, _ = _service({"gam": False}, undeclared_report_definitions="withhold_from_pre_3_3")
    configuration = _config("gam", one_two=False)
    with pytest.raises(LedgerConflictError) as caught:
        await service.configure(configuration, adcp_version=CALLER_32)
    assert caught.value.code == "UNSUPPORTED_FEATURE"
    assert "undeclared" in str(caught.value)
    assert caught.value.details == {
        "offering_id": "GAM_SNAPSHOT_V1",
    }
    assert service._bindings == {}
    await service.configure(configuration, adcp_version=CALLER_33)
    assert len(service._bindings) == 1


async def test_strict_policy_withholds_undeclared_offerings_from_3_2_capabilities() -> None:
    service, _ = _service({"gam": False}, undeclared_report_definitions="withhold_from_pre_3_3")
    try:
        with pytest.warns(UserWarning, match="withheld from buyers"):
            await service.start()
        assert service.capability_block(adcp_version=CALLER_32) == {}
        assert _ids(service.capability_block(adcp_version=CALLER_33)) == ["GAM_SNAPSHOT_V1"]
    finally:
        await service.close()


@pytest.mark.parametrize("decisioning", [False, True], ids=["handler", "decisioning"])
async def test_installed_reporting_call_carries_field_and_details_to_the_wire(
    decisioning: bool,
) -> None:
    from adcp.reporting._errors import installed_reporting_call

    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    configuration = _config("gam", one_two=True)

    async def sync_accounts() -> None:
        await service.configure(configuration, adcp_version=CALLER_32)

    with pytest.raises(Exception) as caught:
        await installed_reporting_call("sync_accounts", sync_accounts, decisioning=decisioning)
    expected_details = {
        "offering_id": "GAM_SNAPSHOT_V1",
    }
    if decisioning:
        from adcp.decisioning import AdcpError

        error = caught.value
        assert isinstance(error, AdcpError)
        assert error.code == "UNSUPPORTED_FEATURE"
        assert error.field == "reporting_delivery_configs"
        assert error.details == expected_details
        assert "serves AdCP 3.3 callers only" in str(error)
        return
    errors = getattr(caught.value, "errors", None)
    assert errors, caught.value
    (wire,) = errors
    assert wire.code == "UNSUPPORTED_FEATURE"
    assert wire.field == "reporting_delivery_configs"
    assert wire.details == expected_details
    assert "serves AdCP 3.3 callers only" in wire.message


async def test_validate_warns_about_undeclared_definitions_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _ = _service({"gam": False})
    with caplog.at_level(logging.WARNING, logger="adcp.reporting.definition_versions"):
        with pytest.warns(UserWarning, match="GAM_SNAPSHOT_V1") as recorded:
            service.validate()
            service.validate()
    assert len([w for w in recorded if "cannot tell" in str(w.message)]) == 1
    assert len([r for r in caplog.records if "cannot tell" in r.getMessage()]) == 1
    message = str(recorded[0].message)
    assert "a" * 64 in message and DEFINITION.report_definition_uri in message


async def test_validate_is_silent_when_every_definition_is_declared() -> None:
    service, _ = _service({"gam": False}, report_definition_versions={SHA_11: "1.1"})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        service.validate()


class _Seller(ADCPHandler):
    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
        return {"supported_protocols": ["media_buy"]}


class _Seller32(_Seller):
    def get_adcp_version(self) -> str:
        return CALLER_32


async def test_install_refuses_a_server_pinned_below_3_3_that_advertises_a_1_2_offering() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    with pytest.raises(ReliableReportingConfigurationError, match="pinned to AdCP 3.2") as caught:
        service.install(_Seller32())
    assert "GAM_SNAPSHOT_V1" in str(caught.value)


async def test_install_accepts_a_pinned_3_2_server_serving_only_older_definitions() -> None:
    service, _ = _service({"gam": False}, report_definition_versions={SHA_11: "1.1"})
    assert service.install(_Seller32()) is not None


async def test_install_accepts_a_1_2_offering_on_an_unpinned_or_3_3_server() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    assert service.install(_Seller()) is not None

    class Seller33(_Seller):
        def get_adcp_version(self) -> str:
            return CALLER_33

    other, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    assert other.install(Seller33()) is not None


# -------------------------------------------------------------- regression


async def test_without_declarations_a_3_2_caller_sees_and_configures_as_before() -> None:
    service, _ = _service({"gam": False, "freewheel": False})
    try:
        with pytest.warns(UserWarning, match="cannot tell"):
            await service.start()
        for version in (CALLER_32, CALLER_33, None):
            assert sorted(_ids(service.capability_block(adcp_version=version))) == [
                "FREEWHEEL_SNAPSHOT_V1",
                "GAM_SNAPSHOT_V1",
            ]
        await service.configure(_config("gam", one_two=False), adcp_version=CALLER_32)
        await service.configure(_config("freewheel", one_two=False), adcp_version="3.1")
        assert len(service._bindings) == 2
        injected = service.inject_capabilities(
            {"supported_protocols": ["media_buy"]}, adcp_version=CALLER_32
        )
        assert injected["media_buy"]["reporting_delivery"]["offerings"]
        assert "media_buy.reporting_delivery" in injected["experimental_features"]
    finally:
        await service.close()


# -------------------------------------------------------------- production


def _reference_verifier_1_2(capability: Any = None) -> Any:
    """The reference verifier over its definition re-stated at contract_version 1.2."""
    from adcp.reporting.materializer import ReportingRevisionVerifier
    from adcp.reporting.materializer.reference import reference_verifier

    base = reference_verifier(capability)
    document = json.loads(base.definition_bytes)
    assert document["contract_version"] == "1.1"
    document["contract_version"] = "1.2"
    raw = json.dumps(document, indent=2).encode()
    key = replace(
        base.key,
        definition=replace(
            base.key.definition, report_definition_sha256=hashlib.sha256(raw).hexdigest()
        ),
    )
    return ReportingRevisionVerifier(key, raw, base.schema_bytes, base.canonicalization_bytes)


@pytest.fixture
def one_two_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conformance.reporting import _production_support

    monkeypatch.setattr(_production_support, "reference_verifier", _reference_verifier_1_2)


def _wire_configuration(h: Any) -> dict[str, Any]:
    config, binding = h.item.config, h.item.binding
    offering = h.production.offerings[0]
    return {
        "delivery_config_id": config.delivery_config_id,
        "delivery_config_version": config.delivery_config_version,
        "offering_id": offering.offering_id,
        "active": True,
        "feed_purpose": config.feed_purpose,
        "report_definition_id": config.report_definition_id,
        "reporting_profile": config.reporting_profile,
        "scope": {"media_buy_ids": list(config.media_buy_ids)},
        "coverage_requirement": "full",
        "required_finality": config.required_finality,
        "reconciliation_mode": binding.reconciliation_mode,
        "schedule": offering.configuration_schedule(config),
        "method": h.item.writer.configuration_binding(binding).wire(),
    }


@pytest.fixture
def declared_memory_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the memory production store declare capabilities, as a durable store does.

    A non-durable graph advertises no reporting, which would hide the filter.
    """
    from adcp.reporting.production.memory import InMemoryReportingProductionStore

    monkeypatch.setattr(InMemoryReportingProductionStore, "durable", property(lambda _: True))


async def _sync_accounts(h: Any, version: str) -> Any:
    from adcp.exceptions import ADCPTaskError
    from adcp.server.base import ToolContext

    request = {
        "idempotency_key": "definition-gate-0001",
        "accounts": [
            {
                "account": {"account_id": h.item.config.account_id},
                "reporting_delivery_configs": [_wire_configuration(h)],
            }
        ],
    }
    with pytest.raises(ADCPTaskError) as caught:
        await h.production.handler.sync_accounts(
            request, ToolContext(caller_identity="buyer", resolved_adcp_version=version)
        )
    return caught.value


async def test_production_reads_a_1_2_definition_from_its_verifier_bytes(
    one_two_reference: None, tmp_path: Any
) -> None:
    from tests.conformance.reporting._production_support import production_harness

    async with production_harness("memory", tmp_path / "v12.sqlite") as h:
        offering = h.production.offerings[0]
        sha256 = offering.wire()["report_definition_sha256"]
        assert h.production.definition_versions.version(sha256) == "1.2"
        assert h.production.definition_versions.known() == {sha256: "1.2"}


async def test_production_capabilities_filter_per_caller_version(
    one_two_reference: None, declared_memory_store: None, tmp_path: Any
) -> None:
    from adcp.server.base import ToolContext
    from tests.conformance.reporting._production_support import production_harness

    async with production_harness("memory", tmp_path / "caps.sqlite", adcp_version=CALLER_33) as h:
        handler = h.production.handler
        new = await handler.get_adcp_capabilities(
            {}, ToolContext(caller_identity="buyer", resolved_adcp_version=CALLER_33)
        )
        assert [o["offering_id"] for o in new["media_buy"]["reporting_delivery"]["offerings"]] == [
            "managed-fixture"
        ]
        old = await handler.get_adcp_capabilities(
            {}, ToolContext(caller_identity="buyer", resolved_adcp_version=CALLER_32)
        )
        assert "reporting_delivery" not in (old.get("media_buy") or {})


async def test_production_sync_accounts_refuses_a_1_2_offering_below_3_3(
    one_two_reference: None, tmp_path: Any
) -> None:
    from tests.conformance.reporting._production_support import production_harness

    async with production_harness("memory", tmp_path / "sync.sqlite", adcp_version=CALLER_33) as h:
        error = await _sync_accounts(h, CALLER_32)
        assert error.error_codes == ["UNSUPPORTED_FEATURE"]
        (wire,) = error.errors
        assert wire.field == "reporting_delivery_configs"
        assert wire.details == {
            "offering_id": "managed-fixture",
        }
        assert "serves AdCP 3.3 callers only" in wire.message


async def test_production_pinned_below_3_3_refuses_a_1_2_offering_at_construction(
    one_two_reference: None, tmp_path: Any
) -> None:
    from tests.conformance.reporting._production_support import production_harness

    with pytest.raises(ValueError, match="pinned to AdCP 3.2.*managed-fixture"):
        async with production_harness("memory", tmp_path / "pin.sqlite", adcp_version=CALLER_32):
            pass  # pragma: no cover


async def test_production_with_a_1_1_definition_serves_3_2_callers_unchanged(
    declared_memory_store: None,
    tmp_path: Any,
) -> None:
    from adcp.server.base import ToolContext
    from tests.conformance.reporting._production_support import production_harness

    async with production_harness("memory", tmp_path / "v11.sqlite", adcp_version=CALLER_33) as h:
        sha256 = h.production.offerings[0].wire()["report_definition_sha256"]
        assert h.production.definition_versions.known() == {sha256: "1.1"}
        old = await h.production.handler.get_adcp_capabilities(
            {}, ToolContext(caller_identity="buyer", resolved_adcp_version=CALLER_32)
        )
        assert old["media_buy"]["reporting_delivery"]["offerings"]


# ------------------------------------------- application-declared base blocks


def _base_block() -> dict[str, Any]:
    """A reporting block the application declared on its own, with 1.1 and 1.2 offerings."""
    return {
        "supported": True,
        "offerings": [
            _offering("BASE_ONE_ONE", SHA_11),
            _offering("BASE_ONE_TWO", SHA_12),
        ],
    }


def _seller_declaring(block: dict[str, Any]) -> ADCPHandler:
    class Seller(ADCPHandler):
        async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
            return {
                "supported_protocols": ["media_buy"],
                "media_buy": {"reporting_delivery": block},
                "experimental_features": ["media_buy.reporting_delivery"],
            }

    return Seller()


def _only_ones(block: dict[str, Any], *keep: str) -> dict[str, Any]:
    return {**block, "offerings": [o for o in block["offerings"] if o["offering_id"] in keep]}


async def _installed_capabilities(service: Any, application: Any, version: str) -> Any:
    from adcp.server.base import ToolContext

    installed = service.install(application)
    return await installed.get_adcp_capabilities(
        {}, ToolContext(caller_identity="buyer", resolved_adcp_version=version)
    )


async def test_installed_handler_drops_an_application_declared_1_2_block_below_3_3() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2", SHA_11: "1.1"})
    block = _only_ones(_base_block(), "BASE_ONE_TWO")
    old = await _installed_capabilities(service, _seller_declaring(block), CALLER_32)
    assert "reporting_delivery" not in (old.get("media_buy") or {})
    assert "media_buy.reporting_delivery" not in old.get("experimental_features", [])


async def test_installed_handler_keeps_an_application_declared_block_at_3_3() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2", SHA_11: "1.1"})
    new = await _installed_capabilities(
        service, _seller_declaring(_only_ones(_base_block(), "BASE_ONE_TWO")), CALLER_33
    )
    assert new["media_buy"]["reporting_delivery"]["offerings"]
    assert "media_buy.reporting_delivery" in new["experimental_features"]


async def test_installed_handler_keeps_only_the_1_1_offering_of_a_mixed_base_block() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2", SHA_11: "1.1"})
    old = await _installed_capabilities(service, _seller_declaring(_base_block()), CALLER_32)
    assert _ids(old["media_buy"]["reporting_delivery"]) == ["BASE_ONE_ONE"]
    assert "media_buy.reporting_delivery" in old["experimental_features"]


async def test_inject_capabilities_leaves_a_block_without_1_2_offerings_untouched() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2", SHA_11: "1.1"})
    block = _only_ones(_base_block(), "BASE_ONE_ONE")
    payload = {"media_buy": {"reporting_delivery": block}}
    assert service.inject_capabilities(payload, adcp_version=CALLER_32) == {
        "media_buy": {"reporting_delivery": block}
    }


def _decisioning_platform(block: dict[str, Any]) -> Any:
    from adcp.decisioning.capabilities import MediaBuy
    from adcp.types import ReportingDeliveryCapabilities
    from tests.test_lazy_platform_router import _SyncSalesPlatform

    class Platform(_SyncSalesPlatform):
        capabilities = replace(
            _SyncSalesPlatform.capabilities,
            media_buy=MediaBuy(
                reporting_delivery=ReportingDeliveryCapabilities.model_validate(block)
            ),
        )

    return Platform("reporting")


async def _decisioning_capabilities(
    service: ReliableReportingService, block: dict[str, Any], version: str
) -> Any:
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.server.base import ToolContext

    handler, executor, _ = create_adcp_server_from_platform(
        service.install(_decisioning_platform(block)), validate_at_init=False
    )
    try:
        return await handler.get_adcp_capabilities(
            {}, ToolContext(caller_identity="buyer", resolved_adcp_version=version)
        )
    finally:
        executor.shutdown(wait=True)


def _decisioning_block(service: ReliableReportingService, *offering_ids: str) -> dict[str, Any]:
    """A complete valid block (the service's own) re-labelled with BASE_* offerings."""
    import copy

    block = copy.deepcopy(service.capability_block())
    template = block["offerings"][0]
    shas = {"ONE_ONE": SHA_11, "ONE_TWO": SHA_12}
    block["offerings"] = [
        {**template, "offering_id": f"BASE_{item}", "report_definition_sha256": shas[item]}
        for item in offering_ids
    ]
    block.pop("reliability_statistics", None)
    return block


async def test_decisioning_handler_drops_a_declared_1_2_block_below_3_3() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    old = await _decisioning_capabilities(
        service, _decisioning_block(service, "ONE_TWO"), CALLER_32
    )
    assert "reporting_delivery" not in (old.get("media_buy") or {})
    assert "media_buy.reporting_delivery" not in old.get("experimental_features", [])


async def test_decisioning_handler_keeps_a_declared_block_at_3_3() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2"})
    new = await _decisioning_capabilities(
        service, _decisioning_block(service, "ONE_TWO"), CALLER_33
    )
    assert new["media_buy"]["reporting_delivery"]["offerings"]


async def test_decisioning_handler_keeps_only_the_1_1_offering_of_a_mixed_block() -> None:
    service, _ = _service({"gam": True}, report_definition_versions={SHA_12: "1.2", SHA_11: "1.1"})
    old = await _decisioning_capabilities(
        service, _decisioning_block(service, "ONE_ONE", "ONE_TWO"), CALLER_32
    )
    assert _ids(old["media_buy"]["reporting_delivery"]) == ["BASE_ONE_ONE"]


# ----------------------------------------- the warning after recovery and binding


def _undeclared_service() -> ReliableReportingService:
    """A source with no offerings of its own; account contexts advertise the 1.2 one."""

    def resolve(configuration: ReportingConfiguration) -> ReportingAccountContext:
        route = configuration.delivery_config_id.removesuffix("-delivery")
        return replace(
            _account_context(configuration),
            capability_offering=_offering_wire(route, one_two=True),
        )

    service = ReliableReportingService.memory(account_context=resolve)
    service.sources.register(
        "gam", ScriptedReportingAdapter(_capabilities_with_definition(SHA_12), [_rows(1)])
    )
    return service


def _definition_warnings(recorded: Any) -> list[str]:
    return [str(w.message) for w in recorded if "cannot tell" in str(w.message)]


async def test_recovered_context_offering_is_warned_about_once() -> None:
    service = _undeclared_service()
    await service.store.put_configuration(_config("gam", one_two=True))
    try:
        with pytest.warns(UserWarning, match="cannot tell") as recorded:
            await service.start()
        (message,) = _definition_warnings(recorded)
        assert "GAM_SNAPSHOT_V1" in message and SHA_12 in message
        with warnings.catch_warnings(record=True) as later:
            warnings.simplefilter("always")
            service.validate()
            await service.configure(_config("gam", one_two=True))
        assert _definition_warnings(later) == []
    finally:
        await service.close()


async def test_a_configuration_added_after_startup_warns_about_its_offering_once() -> None:
    service = _undeclared_service()
    try:
        with warnings.catch_warnings(record=True) as startup:
            warnings.simplefilter("always")
            await service.start()
        assert _definition_warnings(startup) == []
        with pytest.warns(UserWarning, match="cannot tell") as recorded:
            await service.configure(_config("gam", one_two=True))
        (message,) = _definition_warnings(recorded)
        assert "GAM_SNAPSHOT_V1" in message and SHA_12 in message
        with warnings.catch_warnings(record=True) as later:
            warnings.simplefilter("always")
            service.validate()
            await service.configure(
                replace(_config("gam", one_two=True), delivery_config_version="2")
            )
        assert _definition_warnings(later) == []
    finally:
        await service.close()


async def test_a_declared_digest_is_never_warned_about_after_recovery() -> None:
    service = ReliableReportingService.memory(
        account_context=lambda c: replace(
            _account_context(c), capability_offering=_offering_wire("gam", one_two=True)
        ),
        report_definition_versions={SHA_12: "1.2"},
    )
    service.sources.register(
        "gam", ScriptedReportingAdapter(_capabilities_with_definition(SHA_12), [_rows(1)])
    )
    await service.store.put_configuration(_config("gam", one_two=True))
    try:
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            await service.start()
            await service.configure(_config("gam", one_two=True), adcp_version=CALLER_33)
        assert _definition_warnings(recorded) == []
    finally:
        await service.close()


# ------------------------------------------ the service's map reaches production


def _reference_verifier_edited(edit: Any) -> Any:
    """The reference verifier over its definition document after ``edit`` changed it."""
    from adcp.reporting.materializer import ReportingRevisionVerifier
    from adcp.reporting.materializer.reference import reference_verifier

    def build(capability: Any = None) -> Any:
        base = reference_verifier(capability)
        document = json.loads(base.definition_bytes)
        edit(document)
        raw = json.dumps(document, indent=2).encode()
        key = replace(
            base.key,
            definition=replace(
                base.key.definition, report_definition_sha256=hashlib.sha256(raw).hexdigest()
            ),
        )
        return ReportingRevisionVerifier(key, raw, base.schema_bytes, base.canonicalization_bytes)

    return build


def _with_service_options(monkeypatch: pytest.MonkeyPatch, **options: Any) -> None:
    """Make the factory harness build its service with the given declarations."""
    original = ReliableReportingService.memory.__func__  # type: ignore[attr-defined]

    def memory(cls: Any, **kwargs: Any) -> Any:
        return original(cls, **kwargs, **options)

    monkeypatch.setattr(ReliableReportingService, "memory", classmethod(memory))


def _use_verifier(monkeypatch: pytest.MonkeyPatch, build: Any) -> None:
    from tests.conformance.reporting import test_reporting_production_service_factory as factory

    monkeypatch.setattr(factory, "reference_verifier", build)


async def test_production_graph_refuses_a_declaration_its_verifier_bytes_contradict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from adcp.reporting.materializer.reference import reference_verifier
    from tests.conformance.reporting.test_reporting_production_service_factory import (
        factory_harness,
    )

    verifier_11 = reference_verifier()
    sha_11 = verifier_11.key.definition.report_definition_sha256
    assert json.loads(verifier_11.definition_bytes)["contract_version"] == "1.1"
    _with_service_options(monkeypatch, report_definition_versions={sha_11: "1.2"})

    with pytest.raises(ReliableReportingConfigurationError, match="declares 1.2") as caught:
        async with factory_harness("memory", tmp_path / "contradiction.sqlite"):
            pytest.fail("a contradicted declaration must not reach READY")  # pragma: no cover
    assert "contract_version 1.1" in str(caught.value)


async def test_production_graph_agreeing_declaration_reaches_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from adcp.reporting.materializer.reference import reference_verifier
    from tests.conformance.reporting.test_reporting_production_service_factory import (
        factory_harness,
    )

    sha_11 = reference_verifier().key.definition.report_definition_sha256
    _with_service_options(monkeypatch, report_definition_versions={sha_11: "1.1"})
    async with factory_harness("memory", tmp_path / "agreeing.sqlite") as h:
        assert h.production.definition_versions is h.service._definition_versions
        assert h.service._definition_versions.known() == {sha_11: "1.1"}


@pytest.mark.filterwarnings("ignore:Reliable Reporting cannot tell")
async def test_production_verifier_without_contract_version_follows_the_service_policy(
    monkeypatch: pytest.MonkeyPatch, declared_memory_store: None, tmp_path: Any
) -> None:
    from adcp.server.base import ToolContext
    from tests.conformance.reporting.test_reporting_production_service_factory import (
        factory_harness,
    )

    _use_verifier(monkeypatch, _reference_verifier_edited(lambda d: d.pop("contract_version")))
    _with_service_options(monkeypatch, undeclared_report_definitions="withhold_from_pre_3_3")
    async with factory_harness("memory", tmp_path / "undeclared.sqlite") as h:
        sha256 = h.production.offerings[0].wire()["report_definition_sha256"]
        assert h.service._definition_versions.version(sha256) is None
        handler = h.production.handler

        def capabilities(version: str) -> Any:
            return handler.get_adcp_capabilities(
                {}, ToolContext(caller_identity="buyer", resolved_adcp_version=version)
            )

        old = await capabilities(CALLER_32)
        assert "reporting_delivery" not in (old.get("media_buy") or {})
        assert "media_buy.reporting_delivery" not in old.get("experimental_features", [])
        new = await capabilities(CALLER_33)
        assert [o["offering_id"] for o in new["media_buy"]["reporting_delivery"]["offerings"]] == [
            "managed-adapter"
        ]


async def test_production_verifier_without_contract_version_is_served_by_default(
    monkeypatch: pytest.MonkeyPatch, declared_memory_store: None, tmp_path: Any
) -> None:
    from adcp.server.base import ToolContext
    from tests.conformance.reporting.test_reporting_production_service_factory import (
        factory_harness,
    )

    _use_verifier(monkeypatch, _reference_verifier_edited(lambda d: d.pop("contract_version")))
    with pytest.warns(UserWarning, match="cannot tell"):
        async with factory_harness("memory", tmp_path / "default.sqlite") as h:
            old = await h.production.handler.get_adcp_capabilities(
                {}, ToolContext(caller_identity="buyer", resolved_adcp_version=CALLER_32)
            )
    assert old["media_buy"]["reporting_delivery"]["offerings"]
