"""AdCP 3.3 reporting rules the SDK owns (#1669).

* Delivery rows may omit ``impressions``: an absent count is "not measured",
  never zero, and no impression total is derived from rows that lack it.
* Receivers ignore ``aggregated_totals`` in delivery webhooks and never reject
  a payload for carrying it.
* Reliable Reporting report definitions may carry ``source.custody``; the
  definition digest covers it and the installed verifier exposes it.
* Property-grain rollups are built only from rows injected through
  ``simulate_delivery``.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from adcp.client import ADCPClient
from adcp.reporting.conformance import validate_reporting_source_execution
from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import InlineReportingSource
from adcp.reporting.ledger.models import ReportingDefinitionBinding
from adcp.reporting.materializer.contracts import (
    ReportingCanonicalization,
    ReportingVerificationKey,
    ReportingWriterCapability,
    ReportingWriterError,
)
from adcp.reporting.materializer.reference import reference_verifier
from adcp.reporting.materializer.verification import ReportingRevisionVerifier
from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.server.test_controller import TestControllerStore, _handle_test_controller
from adcp.signing import StaticJwksResolver, private_key_from_jwk
from adcp.types import (
    AgentConfig,
    GetMediaBuyDeliveryResponse,
    MediaBuyDeliveryWebhookResult,
    Protocol,
    ReportingReportDefinition,
)
from adcp.types.core import TaskStatus
from adcp.types.domains.enums.reporting_custody import ReportingCustody
from adcp.webhooks import (
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookVerifyOptions,
    create_mcp_webhook_payload,
    sign_webhook,
    to_wire_dict,
)

# ---------------------------------------------------------------------------
# Delivery rows without impressions
# ---------------------------------------------------------------------------

OBSERVED_AT = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _delivery_response(request: Any, packages: list[dict[str, Any]]) -> Any:
    return GetMediaBuyDeliveryResponse.model_validate(
        {
            "reporting_period": {
                "start": request.period.start.isoformat(),
                "end": request.period.source_read_cutoff_at.isoformat(),
            },
            "media_buy_deliveries": [
                {
                    "media_buy_id": "media-buy-redacted",
                    "status": "active",
                    "currency": "USD",
                    "totals": {"spend": 1.25},
                    "by_package": packages,
                }
            ],
        }
    )


def _package(package_id: str, spend: float, **metrics: Any) -> dict[str, Any]:
    return {
        "package_id": package_id,
        "pricing_model": "cpm",
        "rate": 1.0,
        "currency": "USD",
        "spend": spend,
        **metrics,
    }


async def _staged_rows(packages: list[dict[str, Any]]) -> tuple[Any, list[dict[str, Any]]]:
    request = redacted_snapshot_request()
    source = InlineReportingSource(
        fetch=lambda _request: _delivery_response(request, packages),
        capabilities=redacted_capabilities(),
        clock=lambda: OBSERVED_AT,
    )
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await source.execute(request, cancel=asyncio.Event()),
        object_reader=source.staging,
    )
    staged = manifest.objects[0]
    payload = await source.staging.read(
        object_ref=staged.object_ref,
        object_generation=staged.object_generation,
        account_id=request.identity.account_id,
        source_scope=request.identity.source_scope,
        cancel=asyncio.Event(),
    )
    return manifest, [json.loads(line) for line in payload.splitlines()]


def test_generated_delivery_rows_make_impressions_optional() -> None:
    response = _delivery_response(redacted_snapshot_request(), [_package("pkg-1", 0.75)])
    row = response.media_buy_deliveries[0].by_package[0]
    assert row.impressions is None
    assert "impressions" not in row.model_dump(mode="json", exclude_none=True)


async def test_a_row_without_impressions_is_staged_without_a_zero() -> None:
    manifest, rows = await _staged_rows(
        [_package("pkg-1", 0.75, impressions=6), _package("pkg-2", 0.5)]
    )
    by_package = {row["package_id"]: row for row in rows}
    assert str(by_package["pkg-1"]["impressions"]) == "6"
    assert "impressions" not in by_package["pkg-2"]
    totals = {total.name: total.value for total in manifest.control_totals}
    # A total over rows that do not all carry the metric would count the
    # unmeasured row as zero, so none is published for it.
    assert "impressions" not in totals
    assert totals["spend"] == "1.25"


async def test_rows_that_all_carry_impressions_still_total_them() -> None:
    manifest, _ = await _staged_rows(
        [_package("pkg-1", 0.75, impressions=6), _package("pkg-2", 0.5, impressions=0)]
    )
    totals = {total.name: total.value for total in manifest.control_totals}
    assert totals["impressions"] == "6"


# ---------------------------------------------------------------------------
# Delivery webhooks: aggregated_totals is ignored, never a rejection
# ---------------------------------------------------------------------------

VECTORS_DIR = Path(__file__).parent / "conformance" / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
WEBHOOK_KEY = {
    **copy.deepcopy(next(k for k in KEYS if k["kid"] == "test-ed25519-2026")),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}
URL = "https://buyer.example.com/webhooks/adcp"
JSON_HEADERS = {"Content-Type": "application/json"}

DELIVERY_RESULT: dict[str, Any] = {
    "notification_type": "scheduled",
    "sequence_number": 3,
    "reporting_period": {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z"},
    # Senders must not include it; a 3.3 receiver ignores it if one does.
    "aggregated_totals": {"impressions": 999, "spend": 999.0, "media_buy_count": 1},
    "media_buy_deliveries": [
        {
            "media_buy_id": "mb-1",
            "status": "active",
            "currency": "USD",
            "totals": {"spend": 12.5},
            "by_package": [{"package_id": "pkg-1", "currency": "USD", "spend": 12.5}],
        }
    ],
}


def _delivery_webhook(key: str) -> dict[str, Any]:
    payload = create_mcp_webhook_payload(
        task_id="delivery_report_mb-1_000003",
        task_type="media_buy_delivery",
        operation_id="op-reporting",
        status="completed",  # type: ignore[arg-type]
        result=copy.deepcopy(DELIVERY_RESULT),
        idempotency_key=key,
    )
    return to_wire_dict(payload)


async def test_webhook_receiver_accepts_aggregated_totals_and_rows_without_impressions() -> None:
    receiver = WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_KEY]}),
            ),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="test-receiver",
            publisher_scope_for=lambda _signer: "test-publisher",
        ),
    )
    body = json.dumps(
        _delivery_webhook("whk_reporting_33_rules_0001"), separators=(",", ":")
    ).encode()
    signed = sign_webhook(
        method="POST",
        url=URL,
        headers=JSON_HEADERS,
        body=body,
        private_key=private_key_from_jwk(WEBHOOK_KEY, d_field="_private_d_for_test_only"),
        key_id=WEBHOOK_KEY["kid"],
        alg="ed25519",
    )
    outcome = await receiver.receive(
        method="POST", url=URL, headers={**JSON_HEADERS, **signed.as_dict()}, body=body
    )
    assert outcome.rejected is False, outcome.rejection_reason
    result = outcome.payload.result
    result = getattr(result, "root", result)
    assert isinstance(result, MediaBuyDeliveryWebhookResult)
    assert result.media_buy_deliveries[0].by_package[0].impressions is None
    # Nothing in the SDK reads it; it stays an unmodelled extra.
    assert "aggregated_totals" not in type(result).model_fields


async def test_client_handle_webhook_accepts_a_delivery_report_with_aggregated_totals() -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example.com", protocol=Protocol.MCP),
        allow_unauthenticated_webhooks=True,
    )
    result = await client.handle_webhook(
        _delivery_webhook("whk_reporting_33_rules_0002"),
        task_type="media_buy_delivery",
        operation_id="op-reporting",
    )
    assert result.success is True
    assert result.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# Reliable Reporting source.custody
# ---------------------------------------------------------------------------

_ASSETS = files("adcp.reporting.materializer").joinpath("assets")


def _verifier(definition_bytes: bytes) -> ReportingRevisionVerifier:
    schema = _ASSETS.joinpath("reference-row-schema.json").read_bytes()
    contract = _ASSETS.joinpath("reference-canonicalization.json").read_bytes()
    return ReportingRevisionVerifier(
        ReportingVerificationKey(
            "reference-report-v1",
            "paid_media_delivery",
            ReportingDefinitionBinding(
                "https://contracts.example.test/reference-definition-custody.json",
                hashlib.sha256(definition_bytes).hexdigest(),
                "1.0.0",
                "https://contracts.example.test/reference-row-schema.json",
                hashlib.sha256(schema).hexdigest(),
                monetary_metric_units=(("spend", "USD"),),
                monetary_control_total_units=(("spend", "USD"),),
            ),
            ReportingCanonicalization(
                "reference-jcs-rows-v1",
                "https://contracts.example.test/reference-canonicalization.json",
                hashlib.sha256(contract).hexdigest(),
            ),
            ReportingWriterCapability(
                "file_transfer",
                "reference-memory",
                "jsonl",
                "canonical_digest",
                "producer",
                "immutable_location",
                "sha256",
                "conditional_create",
            ),
        ),
        definition_bytes,
        schema,
        contract,
    )


def _definition(**source: Any) -> dict[str, Any]:
    definition = json.loads(_ASSETS.joinpath("reference-definition.json").read_bytes())
    definition["source"].update(source)
    return definition


def _bytes(definition: dict[str, Any]) -> bytes:
    return json.dumps(definition, separators=(",", ":")).encode()


def test_a_definition_without_custody_makes_no_claim() -> None:
    assert reference_verifier().custody is None


@pytest.mark.parametrize("custody", list(ReportingCustody))
def test_the_verifier_accepts_and_surfaces_custody(custody: ReportingCustody) -> None:
    verifier = _verifier(_bytes(_definition(custody=custody.value)))
    assert verifier.custody is custody
    assert json.loads(verifier.definition_bytes)["source"]["custody"] == custody.value


def test_custody_is_covered_by_the_definition_digest() -> None:
    claimed = _bytes(_definition(custody="system_of_record"))
    unclaimed = _bytes(_definition())
    assert hashlib.sha256(claimed).hexdigest() != hashlib.sha256(unclaimed).hexdigest()
    pinned = _verifier(unclaimed).key
    # Adding the claim to an existing definition needs a new digest: the old
    # pin does not verify the edited bytes.
    with pytest.raises(ReportingWriterError) as raised:
        ReportingRevisionVerifier(
            pinned,
            claimed,
            _ASSETS.joinpath("reference-row-schema.json").read_bytes(),
            _ASSETS.joinpath("reference-canonicalization.json").read_bytes(),
        )
    assert raised.value.failure.code == "UNSUPPORTED_VERIFICATION"


@pytest.mark.parametrize("custody", ["mixed", "", 1, None, ["system_of_record"]])
def test_a_malformed_custody_claim_fails_closed(custody: Any) -> None:
    with pytest.raises(ReportingWriterError) as raised:
        _verifier(_bytes(_definition(custody=custody)))
    assert raised.value.failure.code == "UNSUPPORTED_VERIFICATION"


def test_the_generated_definition_model_round_trips_custody() -> None:
    definition = ReportingReportDefinition.model_validate(_definition(custody="upstream_derived"))
    assert definition.source.custody == ReportingCustody.upstream_derived
    assert definition.model_dump(mode="json", exclude_none=True)["source"]["custody"] == (
        "upstream_derived"
    )
    assert ReportingReportDefinition.model_validate(_definition()).source.custody is None


# ---------------------------------------------------------------------------
# simulate_delivery property-grain rows
# ---------------------------------------------------------------------------

PROPERTY_ROWS = [
    {
        "publisher_domain": "nova-sports.example",
        "identifier": {"type": "domain", "value": "nova-sports.example"},
        "impressions": 2000,
        "spend": 10,
    }
]
INSTALLMENT_PROPERTY_ROWS = [
    {
        "installment_ref": {
            "collection_ref": {
                "publisher_domain": "studio-network.example",
                "collection_id": "nightly_news",
            },
            "installment_id": "ep_102",
        },
        "publisher_domain": "streamhaus.example",
        "identifier": {"type": "domain", "value": "streamhaus.example"},
        "impressions": 1000,
        "spend": 5,
    }
]


@pytest.fixture
def _sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADCP_SANDBOX", "1")


class _PropertyStore(TestControllerStore):
    def __init__(self) -> None:
        self.received: dict[str, Any] = {}

    async def simulate_delivery(
        self,
        media_buy_id: str,
        impressions: int | None = None,
        property_delivery: list[dict[str, Any]] | None = None,
        installment_property_delivery: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.received = {
            "media_buy_id": media_buy_id,
            "impressions": impressions,
            "property_delivery": property_delivery,
            "installment_property_delivery": installment_property_delivery,
        }
        return {"simulated": {"media_buy_id": media_buy_id}}


class _LegacyStore(TestControllerStore):
    async def simulate_delivery(
        self, media_buy_id: str, impressions: int | None = None
    ) -> dict[str, Any]:
        raise AssertionError("rows the store cannot accept must not be dropped silently")


def _simulate(**params: Any) -> dict[str, Any]:
    return {
        "scenario": "simulate_delivery",
        "account": {"sandbox": True},
        "params": {"media_buy_id": "mb-1", **params},
    }


@pytest.mark.usefixtures("_sandbox")
async def test_injected_property_rows_reach_the_store_verbatim() -> None:
    store = _PropertyStore()
    result = await _handle_test_controller(
        store,
        _simulate(
            impressions=10000,
            property_delivery=PROPERTY_ROWS,
            installment_property_delivery=INSTALLMENT_PROPERTY_ROWS,
        ),
        validate_schema=True,
    )
    assert result["success"] is True, result
    assert store.received["property_delivery"] == PROPERTY_ROWS
    assert store.received["installment_property_delivery"] == INSTALLMENT_PROPERTY_ROWS


@pytest.mark.usefixtures("_sandbox")
async def test_impressions_are_optional_and_never_defaulted_to_zero() -> None:
    store = _PropertyStore()
    result = await _handle_test_controller(store, _simulate(), validate_schema=True)
    assert result["success"] is True, result
    assert store.received["impressions"] is None


@pytest.mark.usefixtures("_sandbox")
@pytest.mark.parametrize(
    ("name", "rows"),
    [
        ("property_delivery", PROPERTY_ROWS),
        ("installment_property_delivery", INSTALLMENT_PROPERTY_ROWS),
    ],
)
async def test_injected_rows_are_undated(name: str, rows: list[dict[str, Any]]) -> None:
    store = _PropertyStore()
    result = await _handle_test_controller(
        store, _simulate(delivery_date="2026-09-01", **{name: rows}), validate_schema=True
    )
    assert result["success"] is False
    assert result["error"] == "INVALID_PARAMS"
    assert name in result["error_detail"]
    assert store.received == {}


@pytest.mark.usefixtures("_sandbox")
@pytest.mark.parametrize(
    ("name", "rows"),
    [
        ("property_delivery", PROPERTY_ROWS),
        ("installment_property_delivery", INSTALLMENT_PROPERTY_ROWS),
    ],
)
async def test_a_store_that_cannot_take_injected_rows_refuses_them(
    name: str, rows: list[dict[str, Any]]
) -> None:
    result = await _handle_test_controller(
        _LegacyStore(), _simulate(**{name: rows}), validate_schema=True
    )
    assert result["success"] is False
    assert result["error"] == "INVALID_PARAMS"
    assert name in result["error_detail"]


@pytest.mark.usefixtures("_sandbox")
async def test_a_kwargs_store_receives_every_injected_array() -> None:
    received: dict[str, Any] = {}

    class _KwargsStore(TestControllerStore):
        async def simulate_delivery(self, media_buy_id: str, **params: Any) -> dict[str, Any]:
            received.update(params)
            return {"simulated": {"media_buy_id": media_buy_id}}

    result = await _handle_test_controller(
        _KwargsStore(), _simulate(property_delivery=PROPERTY_ROWS), validate_schema=True
    )
    assert result["success"] is True, result
    assert received["property_delivery"] == PROPERTY_ROWS
