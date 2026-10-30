"""AdCP 3.3 seller-side rules owned by the decisioning framework (#1668).

Covers version literals that must include the 3.3 line, the
``sales-fixed-rate`` specialism, advertised measurement terms, property domain
identifiers, the counterparty-asserted domain-list bounds and the
``CREATIVE_MERGE_REJECTED`` / ``RIGHTS_CHANNEL_VIOLATION`` helpers. Wire shapes
are checked against the bundled 3.3 JSON schemas, not mocks.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from adcp.decisioning import (
    CreativeMergeRejectedError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    RightsChannelViolationError,
    SingletonAccounts,
    TermsRejectedError,
    bound_counterparty_domains,
    creative_merge_rejected_details,
    is_advertised_terms_replay,
    rights_channel_violation_details,
)
from adcp.decisioning.capabilities import (
    Account,
    MediaBuy,
    Portfolio,
    Signals,
    SupportedProtocol,
)
from adcp.decisioning.dispatch import SPEC_SPECIALISM_ENUM
from adcp.decisioning.domain_lists import (
    COUNTERPARTY_DOMAIN_LIST_BYTE_BUDGET,
    bound_capability_domain_lists,
)
from adcp.decisioning.handler import (
    SPECIALISM_TO_PROTOCOLS,
    PlatformHandler,
    _is_adcp_32_or_newer,
)
from adcp.decisioning.measurement_terms import undeclared_measurement_window
from adcp.decisioning.types import AdcpError
from adcp.property_lists import (
    PropertyListIdentifierError,
    is_valid_domain_identifier,
    normalize_property_list_identifiers,
    validate_property_list_identifiers,
)
from adcp.server import PrincipalIdentity, PrincipalService
from adcp.server.base import ToolContext
from adcp.types import (
    CreatePropertyListRequest,
    GetProductsRequest,
    GetProductsResponse,
    Product,
    UpdatePropertyListRequest,
)
from adcp.validation.schema_validator import validate_request, validate_response

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas" / "cache" / "3.3.0-beta.2"


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-33-rules-")
    yield pool
    pool.shutdown(wait=True)


def _handler(platform: DecisioningPlatform, executor: ThreadPoolExecutor) -> PlatformHandler:
    return PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())


# ---------------------------------------------------------------------------
# 1. Version literals include the 3.3 line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [
        (None, True),
        ("3.2", True),
        ("3.3-beta.2", True),
        # Bundle precision used to fail to parse and read as pre-3.2.
        ("3.3.0-beta.2", True),
        ("3.2.4", True),
        ("3.1", False),
        ("3.0.7", False),
        ("not-a-version", False),
    ],
)
def test_signing_capability_gate_counts_3_3_as_3_2_or_newer(version, expected) -> None:
    assert _is_adcp_32_or_newer(version) is expected


@pytest.mark.asyncio
async def test_principal_service_accepts_3_3_async_payloads_by_default() -> None:
    service = PrincipalService()
    response = await service.sync_principal(
        PrincipalIdentity("https://buyer.example/agent", "buyer_agent"),
        {
            "idempotency_key": "principal-3-3-async-0001",
            "configuration": {"declarations": {"async_adcp_versions": ["3.3", "3.2"]}},
        },
    )

    declarations = response.result.configuration.declarations
    assert list(declarations.accepted.async_adcp_versions) == ["3.3", "3.2"]
    assert declarations.selected_async_adcp_version == "3.3"
    assert declarations.exclusions is None


# ---------------------------------------------------------------------------
# 2. sales-fixed-rate
# ---------------------------------------------------------------------------


def test_every_spec_sales_specialism_rolls_up_to_media_buy() -> None:
    enum = json.loads((SCHEMAS / "enums" / "specialism.json").read_text())["enum"]
    sales = {slug for slug in enum if slug.startswith("sales-")}

    assert "sales-fixed-rate" in sales
    assert sales <= SPEC_SPECIALISM_ENUM
    for slug in sales:
        assert SPECIALISM_TO_PROTOCOLS.get(slug) == frozenset({"media_buy"}), slug


@pytest.mark.filterwarnings("ignore:DecisioningCapabilities.supported_protocols")
def test_fixed_rate_platform_derives_media_buy_and_advertises_the_specialism(executor) -> None:
    class _FixedRateSeller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-fixed-rate"],
            account=Account(supported_billing=["operator"]),
            media_buy=MediaBuy(supported_pricing_models=["cpp", "cpm"]),
        )
        accounts = SingletonAccounts(account_id="fixed-rate")

    response = asyncio.run(_handler(_FixedRateSeller(), executor).get_adcp_capabilities())

    # The sales_fixed_rate storyboard's capability_discovery step.
    assert response["supported_protocols"] == ["media_buy"]
    assert response["specialisms"] == ["sales-fixed-rate"]
    assert validate_response("get_adcp_capabilities", response).valid


# ---------------------------------------------------------------------------
# 3. Advertised measurement terms
# ---------------------------------------------------------------------------

_PRODUCT: dict[str, Any] = {
    "product_id": "spot_q4",
    "name": "Q4 spots",
    "description": "Fixed-rate spot package",
    "publisher_properties": [{"selection_type": "all", "publisher_domain": "pub.example.com"}],
    "format_options": [
        {
            "format_option_id": "video_30s",
            "format_kind": "video_hosted",
            "params": {"duration_ms_exact": 30000},
        }
    ],
    "delivery_type": "non_guaranteed",
    "pricing_options": [
        {
            "pricing_model": "cpm",
            "pricing_option_id": "cpm_fixed",
            "currency": "USD",
            "fixed_price": 12.0,
        }
    ],
    "reporting_capabilities": {
        "available_reporting_frequencies": ["daily"],
        "expected_delay_minutes": 60,
        "timezone": "UTC",
        "supports_webhooks": False,
        "available_metrics": ["impressions"],
        "date_range_support": "date_range",
        "measurement_windows": [
            {"window_id": "live", "duration_days": 0},
            {"window_id": "c7", "duration_days": 7},
        ],
    },
    "measurement_terms": {
        "billing_measurement": {
            "vendor": {"domain": "measurement.example"},
            "measurement_window": "c7",
        },
        "makegood_policy": {"available_remedies": ["additional_delivery"]},
    },
}


def _product(**overrides: Any) -> Product:
    return Product.model_validate({**_PRODUCT, **overrides})


def _with_window(window: str) -> Product:
    terms = json.loads(json.dumps(_PRODUCT["measurement_terms"]))
    terms["billing_measurement"]["measurement_window"] = window
    return _product(measurement_terms=terms)


def test_declared_window_passes_and_undeclared_window_is_reported() -> None:
    assert undeclared_measurement_window(_product()) is None
    assert undeclared_measurement_window(_with_window("c3")) == "c3"
    no_windows = json.loads(json.dumps(_PRODUCT["reporting_capabilities"]))
    del no_windows["measurement_windows"]
    assert undeclared_measurement_window(_product(reporting_capabilities=no_windows)) == "c7"
    assert undeclared_measurement_window({"product_id": "no_terms"}) is None


class _CatalogSeller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-fixed-rate"],
        supported_protocols=[SupportedProtocol.media_buy],
    )
    accounts = SingletonAccounts(account_id="catalog")

    def __init__(self, products: list[Product]) -> None:
        self.products = products

    def get_products(self, req, ctx):
        return GetProductsResponse(products=self.products)


@pytest.mark.asyncio
async def test_get_products_refuses_a_product_advertising_an_undeclared_window(executor) -> None:
    handler = _handler(_CatalogSeller([_product(), _with_window("c3")]), executor)

    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="spots"), ToolContext()
        )

    assert exc.value.code == "INTERNAL_ERROR"
    assert exc.value.field == "products[1].measurement_terms.billing_measurement.measurement_window"


@pytest.mark.asyncio
async def test_get_products_serves_products_whose_windows_are_declared(executor) -> None:
    handler = _handler(_CatalogSeller([_product()]), executor)

    response = await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="spots"), ToolContext()
    )

    assert [p.product_id for p in response.products] == ["spot_q4"]
    assert validate_response("get_products", response).valid


def test_replay_of_advertised_terms_is_detected() -> None:
    advertised = _product().measurement_terms

    assert is_advertised_terms_replay(advertised, advertised)
    # A term-by-term replay (one advertised term, unchanged) is still a replay.
    assert is_advertised_terms_replay(
        {"billing_measurement": _PRODUCT["measurement_terms"]["billing_measurement"]}, advertised
    )
    changed = json.loads(json.dumps(_PRODUCT["measurement_terms"]))
    changed["billing_measurement"]["measurement_window"] = "live"
    assert not is_advertised_terms_replay(changed, advertised)
    dropped_window = {"billing_measurement": {"vendor": {"domain": "measurement.example"}}}
    assert not is_advertised_terms_replay(dropped_window, advertised)
    assert not is_advertised_terms_replay({}, advertised)


def test_terms_rejected_refuses_to_reject_a_replay() -> None:
    advertised = _PRODUCT["measurement_terms"]
    with pytest.raises(ValueError, match="replay"):
        TermsRejectedError(proposed_terms=advertised, advertised_terms=advertised)

    proposal = {"billing_measurement": {"vendor": {"domain": "other-vendor.example"}}}
    error = TermsRejectedError(
        proposed_terms=proposal,
        advertised_terms=advertised,
        term="billing_measurement",
        field="packages[0].measurement_terms.billing_measurement.vendor",
    )
    assert error.to_wire() == {
        "code": "TERMS_REJECTED",
        "message": "Proposed measurement terms were rejected.",
        "recovery": "correctable",
        "field": "packages[0].measurement_terms.billing_measurement.vendor",
        "details": {"term": "billing_measurement"},
    }


# ---------------------------------------------------------------------------
# 4. Property domain identifiers
# ---------------------------------------------------------------------------

_IDENTIFIER_VALUES = [
    "example.com",
    "Example.com",
    "example.com.",
    " example.com",
    "example.com\n",
    "https://www.example.com/path",
    "example.com:443",
    "*.example.com",
    "*.Example.com",
    "a.*.example.com",
    "xn--strae-oqa.de",
    "straße.de",
    "localhost",
    "-bad.example.com",
    "a" * 64 + ".example.com",
]


@pytest.mark.parametrize("identifier_type", ["domain", "subdomain"])
@pytest.mark.parametrize("value", _IDENTIFIER_VALUES)
def test_identifier_check_matches_the_3_3_identifier_schema(identifier_type, value) -> None:
    payload = {
        "name": "Blocklist",
        "idempotency_key": "property-list-key-0001",
        "filters": {"exclude_identifiers": [{"type": identifier_type, "value": value}]},
    }
    schema_valid = validate_request("create_property_list", payload).valid

    assert is_valid_domain_identifier(identifier_type, value) is schema_valid


def test_non_domain_identifier_types_are_not_constrained() -> None:
    assert is_valid_domain_identifier("bundle_id", "Com.Example.App")


def _create_request(*values: str, publisher_domain: str | None = None) -> dict[str, Any]:
    base: list[dict[str, Any]] = [
        {
            "selection_type": "identifiers",
            "identifiers": [{"type": "domain", "value": value} for value in values],
        }
    ]
    if publisher_domain is not None:
        base.append(
            {
                "selection_type": "publisher_tags",
                "publisher_domain": publisher_domain,
                "tags": ["premium"],
            }
        )
    return {
        "name": "Allowlist",
        "idempotency_key": "property-list-key-0002",
        "base_properties": base,
        "filters": {
            "exclude_identifiers": [{"type": "subdomain", "value": "Edition.Example.com."}]
        },
    }


def test_sender_normalizer_converts_explicitly_and_the_result_validates() -> None:
    request = _create_request("Example.COM.", "*.News.Example.com", "straße.de")

    converted = normalize_property_list_identifiers(request)

    assert [i["value"] for i in converted["base_properties"][0]["identifiers"]] == [
        "example.com",
        "*.news.example.com",
        "xn--strae-oqa.de",
    ]
    assert converted["filters"]["exclude_identifiers"][0]["value"] == "edition.example.com"
    assert request["base_properties"][0]["identifiers"][0]["value"] == "Example.COM."
    assert validate_request("create_property_list", converted).valid
    assert not validate_request("create_property_list", request).valid


def test_sender_normalizer_keeps_the_model_type_and_normalizes_publisher_domain() -> None:
    model = UpdatePropertyListRequest.model_validate(
        {**_create_request("Example.com"), "list_id": "list_1"}
    )

    converted = normalize_property_list_identifiers(model)

    assert isinstance(converted, UpdatePropertyListRequest)
    wire = converted.model_dump(mode="json", exclude_none=True)
    assert wire["base_properties"][0]["identifiers"][0]["value"] == "example.com"


@pytest.mark.parametrize(
    "value,reason",
    [
        ("https://www.example.com/path", "expected_hostname"),
        ("example.com:443", "expected_hostname"),
        ("a.*.example.com", "expected_hostname"),
        ("192.0.2.1", "ip_address_not_allowed"),
    ],
)
def test_sender_normalizer_refuses_what_it_cannot_convert(value, reason) -> None:
    with pytest.raises(PropertyListIdentifierError) as exc:
        normalize_property_list_identifiers(_create_request(value))

    assert exc.value.field == "base_properties[0].identifiers[0].value"
    assert exc.value.reason == reason
    assert value not in str(exc.value)


def test_sender_normalizer_refuses_a_url_publisher_domain() -> None:
    with pytest.raises(PropertyListIdentifierError) as exc:
        normalize_property_list_identifiers(
            _create_request("example.com", publisher_domain="https://pub.example.com")
        )
    assert exc.value.field == "base_properties[1].publisher_domain"


def test_receiver_rejects_the_whole_request_without_rewriting() -> None:
    request = _create_request("example.com", "Example.org")
    request["filters"]["exclude_identifiers"][0]["value"] = "edition.example.com"

    with pytest.raises(AdcpError) as exc:
        validate_property_list_identifiers(request)

    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.recovery == "correctable"
    assert exc.value.field == "base_properties[0].identifiers[1].value"
    validate_property_list_identifiers(normalize_property_list_identifiers(request))


class _ListsPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["property-lists"],
        supported_protocols=[SupportedProtocol.governance],
    )
    accounts = SingletonAccounts(account_id="lists")

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def create_property_list(self, req, ctx):
        self.calls.append(req)
        return {"list": {"list_id": "list_1", "name": req.name}, "auth_token": "token"}


@pytest.mark.asyncio
async def test_create_property_list_rejects_mixed_case_domains_before_the_platform(
    executor,
) -> None:
    platform = _ListsPlatform()
    handler = _handler(platform, executor)

    with pytest.raises(AdcpError) as exc:
        await handler.create_property_list(
            CreatePropertyListRequest.model_validate(_create_request("Example.com")),
            ToolContext(),
        )

    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "base_properties[0].identifiers[0].value"
    assert platform.calls == []

    accepted = normalize_property_list_identifiers(
        CreatePropertyListRequest.model_validate(_create_request("Example.com"))
    )
    await handler.create_property_list(accepted, ToolContext())
    assert len(platform.calls) == 1


# ---------------------------------------------------------------------------
# 5. Counterparty-asserted domain lists
# ---------------------------------------------------------------------------


def _domains(count: int, *, width: int = 0) -> list[str]:
    return [f"pub{index}{'x' * width}.example" for index in range(count)]


def _member_bytes(field: str, domains: list[str]) -> int:
    return len(json.dumps({field: domains}, separators=(",", ":")).encode()) - 2


def test_short_list_is_kept_whole_and_complete() -> None:
    domains = _domains(3)
    bounded = bound_counterparty_domains(domains)

    assert bounded.domains == tuple(domains)
    assert bounded.complete is True
    assert bounded.serialized_bytes == _member_bytes("publisher_domains", domains)


def test_entry_bound_truncates_to_100_in_declared_order() -> None:
    bounded = bound_counterparty_domains(_domains(150))

    assert bounded.domains == tuple(_domains(100))
    assert bounded.complete is False


def test_byte_budget_counts_member_name_and_punctuation() -> None:
    domains = _domains(60, width=180)
    bounded = bound_counterparty_domains(domains, field="data_provider_domains")

    size = _member_bytes("data_provider_domains", list(bounded.domains))
    assert bounded.serialized_bytes == size <= COUNTERPARTY_DOMAIN_LIST_BYTE_BUDGET
    next_entry = len(json.dumps(domains[len(bounded.domains)])) + 1
    assert size + next_entry > COUNTERPARTY_DOMAIN_LIST_BYTE_BUDGET
    assert bounded.complete is False


def test_duplicates_are_dropped_without_marking_the_list_incomplete() -> None:
    bounded = bound_counterparty_domains(["a.example", "b.example", "a.example"])

    assert bounded.domains == ("a.example", "b.example")
    assert bounded.complete is True


def test_over_length_entries_are_dropped_and_mark_the_list_incomplete() -> None:
    long_domain = ("a" * 62 + ".") * 4 + "example"
    assert len(long_domain) > 253
    bounded = bound_counterparty_domains(["a.example", long_domain])

    assert bounded.domains == ("a.example",)
    assert bounded.complete is False
    with pytest.raises(ValueError):
        bound_counterparty_domains([long_domain])


def test_combined_budget_binds_the_second_list() -> None:
    response = {
        "media_buy": {"portfolio": {"publisher_domains": _domains(5)}},
        "signals": {"data_provider_domains": _domains(5)},
    }
    first = _member_bytes("publisher_domains", _domains(5))

    assert bound_capability_domain_lists(response, combined_byte_budget=first + 60) is True
    assert response["media_buy"]["portfolio"] == {"publisher_domains": _domains(5)}
    assert response["signals"]["data_provider_domains_complete"] is False
    kept = response["signals"]["data_provider_domains"]
    assert 0 < len(kept) < 5
    assert _member_bytes("data_provider_domains", kept) <= 60


@pytest.mark.asyncio
async def test_capabilities_projection_truncates_and_flags_oversized_lists(executor) -> None:
    class _NetworkSeller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed", "signal-marketplace"],
            supported_protocols=[SupportedProtocol.media_buy, SupportedProtocol.signals],
            account=Account(supported_billing=["operator"]),
            media_buy=MediaBuy(
                supported_pricing_models=["cpm"],
                portfolio=Portfolio(
                    publisher_domains=_domains(400, width=40), primary_channels=["display"]
                ),
            ),
            signals=Signals(data_provider_domains=_domains(3)),
        )
        accounts = SingletonAccounts(account_id="network")

    response = await _handler(_NetworkSeller(), executor).get_adcp_capabilities()

    portfolio = response["media_buy"]["portfolio"]
    assert portfolio["publisher_domains_complete"] is False
    assert (
        portfolio["publisher_domains"]
        == _domains(400, width=40)[: len(portfolio["publisher_domains"])]
    )
    assert _member_bytes("publisher_domains", portfolio["publisher_domains"]) <= 8192
    assert response["signals"]["data_provider_domains"] == _domains(3)
    assert "data_provider_domains_complete" not in response["signals"]
    assert validate_response("get_adcp_capabilities", response).valid


# ---------------------------------------------------------------------------
# 7. CREATIVE_MERGE_REJECTED / RIGHTS_CHANNEL_VIOLATION
# ---------------------------------------------------------------------------


def _validate_details(schema_file: str, details: dict[str, Any]) -> None:
    import jsonschema

    schema = json.loads((SCHEMAS / "error-details" / schema_file).read_text())
    enum = json.loads((SCHEMAS / "enums" / "creative-merge-rejection-reason.json").read_text())
    schema["properties"]["rejections"]["items"]["properties"]["reason"] = enum
    jsonschema.Draft7Validator(schema).validate(details)


def test_creative_merge_rejected_mints_schema_valid_details() -> None:
    error = CreativeMergeRejectedError(
        rejections=[
            {
                "operation_index": 3,
                "creative_id": "cr_buyer_1",
                "package_id": "pkg_1",
                "merged_creative_id": "cr_seller_9",
                "reason": "content_mismatch",
            },
            {
                "operation_index": 1,
                "creative_id": "cr_buyer_2",
                "package_id": "pkg_1",
                "merged_creative_id": "cr_seller_8",
                "reason": "not_on_package",
            },
        ]
    )
    wire = error.to_wire()

    assert wire["code"] == "CREATIVE_MERGE_REJECTED"
    assert wire["recovery"] == "correctable"
    assert wire["field"] == "assignment_operations[1]"
    _validate_details("creative-merge-rejected.json", wire["details"])

    narrowed = creative_merge_rejected_details(wire)
    assert narrowed is not None
    assert [r.merged_creative_id for r in narrowed.rejections] == ["cr_seller_9", "cr_seller_8"]
    assert creative_merge_rejected_details(error) == narrowed


def test_creative_merge_rejected_refuses_a_reason_outside_the_enum() -> None:
    with pytest.raises(PydanticValidationError):
        CreativeMergeRejectedError(
            rejections=[
                {
                    "operation_index": 0,
                    "creative_id": "a",
                    "package_id": "p",
                    "merged_creative_id": "b",
                    "reason": "looks_different",
                }
            ]
        )


def test_merge_narrowing_ignores_other_codes_and_nonconforming_details() -> None:
    assert creative_merge_rejected_details({"code": "VALIDATION_ERROR", "details": {}}) is None
    assert creative_merge_rejected_details({"code": "CREATIVE_MERGE_REJECTED"}) is None
    assert (
        creative_merge_rejected_details(
            {"code": "CREATIVE_MERGE_REJECTED", "details": {"rejections": []}}
        )
        is None
    )


def test_rights_channel_violation_mints_and_narrows() -> None:
    error = RightsChannelViolationError(
        rights_id="rights_42",
        channel="ctv",
        capability_id="cap_ctv_30s",
        field="creative_manifest.rights[0].excluded_channels",
    )
    wire = error.to_wire()

    assert wire["code"] == "RIGHTS_CHANNEL_VIOLATION"
    assert wire["recovery"] == "correctable"
    assert wire["field"] == "creative_manifest.rights[0].excluded_channels"
    assert wire["details"] == {
        "rights_id": "rights_42",
        "channel": "ctv",
        "capability_id": "cap_ctv_30s",
    }
    # Advisory entry on an otherwise successful build: errors[] items are core/error.json.
    error_schema = json.loads((SCHEMAS / "core" / "error.json").read_text())
    assert set(wire) <= set(error_schema["properties"])

    narrowed = rights_channel_violation_details(wire)
    assert narrowed is not None
    assert (narrowed.rights_id, narrowed.channel, narrowed.capability_id) == (
        "rights_42",
        "ctv",
        "cap_ctv_30s",
    )
    partial = rights_channel_violation_details({"code": "RIGHTS_CHANNEL_VIOLATION"})
    assert partial is not None and partial.rights_id is None
    assert rights_channel_violation_details({"code": "CREATIVE_REJECTED"}) is None
