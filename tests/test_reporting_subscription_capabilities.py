"""Capability fields for AdCP 3.3 reporting subscriptions.

Each projected block is validated against the pinned 3.3.0-beta.1
``core/reporting-delivery-capabilities.json``, whose ``allOf`` couples
``configuration_task``, ``configuration_tasks`` and ``subscriptions``.
"""

from __future__ import annotations

import pytest

from adcp.reporting.subscriptions import (
    REPORTING_SUBSCRIPTIONS_FEATURE,
    REPORTING_SUBSCRIPTIONS_SECTION,
    ReportingSubscriptionSupport,
    advertise_reporting_subscriptions,
    reporting_configuration_tasks,
    subscription_capabilities,
)
from adcp.validation import schema_loader

VERSION = "3.3.0-beta.1"

BASE = {
    "supported": True,
    "reliable_reporting_version": "1.0",
    "configuration_task": "sync_accounts",
    "status_task": "get_reporting_status",
    "revision_content_task": "get_media_buy_delivery",
    "offerings": [
        {
            "offering_id": "daily-official",
            "feed_purpose": "analytics",
            "report_definition_id": "delivery-v1",
            "report_definition_uri": "https://seller.example/defs/delivery-v1.json",
            "report_definition_sha256": "0" * 64,
            "reporting_profile": {
                "id": "delivery",
                "version": "1",
                "schema_uri": "https://seller.example/defs/delivery-v1.schema.json",
                "schema_sha256": "0" * 64,
                "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
                "schema_ref_policy": "local_fragment_only",
                "grain": "package_day",
                "primary_keys": ["account_id", "media_buy_id", "package_id", "period_start"],
            },
            "schedule": {
                "period_duration": "P1D",
                "alignment": "account_timezone",
                "delivery_sla": "PT6H",
            },
            "supported_finality": ["official"],
            "reconciliation_mode": "delivery_only",
        }
    ],
    "automated_recovery_window_seconds": 21600,
    "status_retention_days": 400,
}


def _errors(block: dict) -> list[str]:
    validator = schema_loader.get_named_validator(
        "core/reporting-delivery-capabilities.json", version=VERSION
    )
    return [error.message for error in validator.iter_errors(block)]


def test_base_block_is_valid_and_unchanged_without_support() -> None:
    assert _errors(BASE) == []
    assert subscription_capabilities(BASE, None) == BASE
    assert advertise_reporting_subscriptions({"experimental_features": []}, None) == {
        "experimental_features": []
    }


def test_subscriptions_alongside_account_configuration() -> None:
    block = subscription_capabilities(
        BASE,
        ReportingSubscriptionSupport(
            max_accounts=500, backfill_on_regrant=True, dimensions=("package", "creative")
        ),
    )
    assert _errors(block) == []
    assert block["configuration_tasks"] == ["sync_principal", "sync_accounts"]
    assert block["configuration_task"] == "sync_accounts"
    assert block["subscriptions"] == {
        "supported": True,
        "max_accounts": 500,
        "backfill_on_regrant": True,
        "dimensions": ["package", "creative"],
    }


def test_subscriptions_only_drops_the_deprecated_field() -> None:
    block = subscription_capabilities(
        BASE, ReportingSubscriptionSupport(account_configuration=False)
    )
    assert _errors(block) == []
    assert block["configuration_tasks"] == ["sync_principal"]
    assert "configuration_task" not in block
    assert block["subscriptions"] == {"supported": True}


def test_schema_rejects_the_shapes_the_helper_never_emits() -> None:
    # configuration_task without sync_accounts listed, and subscriptions
    # without sync_principal listed, are both invalid.
    assert _errors(
        {**BASE, "configuration_tasks": ["sync_principal"], "subscriptions": {"supported": True}}
    )
    assert _errors({**BASE, "subscriptions": {"supported": True}})


def test_capabilities_response_gets_feature_and_principal_section() -> None:
    response = {
        "experimental_features": ["media_buy.reporting_delivery", "protocol.principal"],
        "adcp": {
            "major_versions": [3],
            "principal": {
                "supported": True,
                "sync_task": "sync_principal",
                "read_task": "get_principal",
                "supported_sections": ["reporting_destinations"],
            },
        },
    }
    advertised = advertise_reporting_subscriptions(response, ReportingSubscriptionSupport())
    assert advertised["experimental_features"] == [
        "media_buy.reporting_delivery",
        "protocol.principal",
        REPORTING_SUBSCRIPTIONS_FEATURE,
    ]
    assert advertised["adcp"]["principal"]["supported_sections"] == [
        "reporting_destinations",
        REPORTING_SUBSCRIPTIONS_SECTION,
    ]
    assert advertise_reporting_subscriptions(advertised, ReportingSubscriptionSupport()) == (
        advertised
    )
    assert response["adcp"]["principal"]["supported_sections"] == ["reporting_destinations"]


def test_configuration_tasks_and_support_validation() -> None:
    assert reporting_configuration_tasks(subscriptions=False) == ["sync_accounts"]
    with pytest.raises(ValueError):
        reporting_configuration_tasks(subscriptions=False, account_configuration=False)
    with pytest.raises(ValueError):
        ReportingSubscriptionSupport(max_accounts=0)
    with pytest.raises(ValueError):
        ReportingSubscriptionSupport(dimensions=("Package",))
    support = ReportingSubscriptionSupport(dimensions=("package", "dma_zone", "package"))
    assert support.dimensions == ("package", "dma_zone")
    assert support.standard_dimensions == ("package",)
