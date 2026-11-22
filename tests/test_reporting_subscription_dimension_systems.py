"""A selector's ``dimension_systems`` narrows offering resolution (adcp#9049).

``core/reporting-subscription.json`` ``selector.dimension_systems``: an offering
matches only when its ``dimension_systems.metro`` equals the selector's and its
``dimension_systems.postal_area`` pairs include every selector pair. An omitted
key accepts any system.
"""

from __future__ import annotations

from copy import deepcopy

from adcp.reporting.subscriptions import (
    SubscriptionOffering,
    SubscriptionSelector,
    resolve_subscription_offerings,
)
from adcp.reporting.subscriptions.facade import _mismatches
from adcp.reporting.subscriptions.registry import _semantic
from adcp.reporting.subscriptions.resolution import STANDARD_REPORTING_DIMENSIONS
from adcp.types import ReportingDeliveryOffering
from adcp.types.domains.core.reporting_subscription import ReportingSubscription
from adcp.types.domains.enums.reporting_dimension import ReportingDimension

US_ZIP = ("US", "zip")
GB_POSTAL = ("GB", "postal_code")


def offering(
    offering_id: str,
    *,
    metro: str | None = None,
    postal: tuple[tuple[str, str], ...] = (),
    dimensions: tuple[str, ...] = ("metro", "postal_area"),
) -> SubscriptionOffering:
    return SubscriptionOffering(
        offering_id=offering_id,
        report_definition_id=f"def-{offering_id}",
        reporting_profile="profile-1",
        feed_purpose="analytics",
        period_duration="P1D",
        alignment="utc",
        delivery_sla="PT6H",
        supported_finality=frozenset({"official"}),
        reconciliation_mode="delivery_only",
        dimensions=frozenset(dimensions),
        metro_system=metro,
        postal_systems=frozenset(postal),
    )


def config(systems: dict | None = None, **selector) -> dict:
    chosen: dict = {
        "feed_purpose": "analytics",
        "period_duration": "P1D",
        "required_finality": "official",
        "dimensions": ["metro", "postal_area"],
    }
    chosen.update(selector)
    if systems is not None:
        chosen["dimension_systems"] = systems
    return {
        "subscription_id": "geo",
        "subscription_version": 1,
        "active": True,
        "scope": {"all_authorized_accounts": True},
        "selector": chosen,
        "reconciliation_mode": "delivery_only",
    }


def postal(*pairs: tuple[str, str]) -> list[dict]:
    return [{"country": country, "system": system} for country, system in pairs]


def resolved(subscription: dict, offerings: list[SubscriptionOffering]) -> tuple[str, ...]:
    return resolve_subscription_offerings(
        subscription,
        offerings,
        product_offering_ids=[item.offering_id for item in offerings],
    ).offering_ids


LOW_DMA = offering("a-dma", metro="nielsen_dma")
HIGH_ITL = offering("b-itl", metro="uk_itl1")


class TestMetro:
    def test_a_named_metro_system_picks_the_matching_offering_not_the_lower_id(self) -> None:
        wanted = config({"metro": "uk_itl1"})
        assert resolved(wanted, [LOW_DMA, HIGH_ITL]) == ("b-itl",)

    def test_an_omitted_metro_accepts_any_system_and_takes_the_lowest_id(self) -> None:
        assert resolved(config(), [HIGH_ITL, LOW_DMA]) == ("a-dma",)
        assert resolved(config({"postal_area": postal(US_ZIP)}), [HIGH_ITL, LOW_DMA]) == ()

    def test_an_offering_without_dimension_systems_never_matches_a_named_metro(self) -> None:
        bare = offering("bare")
        assert resolved(config({"metro": "nielsen_dma"}), [bare]) == ()
        assert resolved(config(), [bare]) == ("bare",)


class TestPostalArea:
    def test_a_subset_of_the_offerings_pairs_matches(self) -> None:
        both = offering("both", postal=(US_ZIP, GB_POSTAL))
        assert resolved(config({"postal_area": postal(GB_POSTAL)}), [both]) == ("both",)
        assert resolved(config({"postal_area": postal(US_ZIP, GB_POSTAL)}), [both]) == ("both",)

    def test_two_selector_pairs_do_not_match_an_offering_with_one(self) -> None:
        only_us = offering("only-us", postal=(US_ZIP,))
        assert resolved(config({"postal_area": postal(US_ZIP, GB_POSTAL)}), [only_us]) == ()

    def test_metro_and_postal_both_have_to_hold(self) -> None:
        wanted = config({"metro": "nielsen_dma", "postal_area": postal(US_ZIP)})
        wrong_metro = offering("a-wrong", metro="uk_itl1", postal=(US_ZIP,))
        right = offering("b-right", metro="nielsen_dma", postal=(US_ZIP,))
        assert resolved(wanted, [wrong_metro, right]) == ("b-right",)


WIRE_OFFERING = {
    "offering_id": "geo-wire",
    "feed_purpose": "analytics",
    "report_definition_id": "def-geo",
    "reporting_profile": {"id": "delivery"},
    "schedule": {"period_duration": "P1D", "alignment": "utc", "delivery_sla": "PT6H"},
    "supported_finality": ["official"],
    "reconciliation_mode": "delivery_only",
    "standard_dimensions": ["metro", "postal_area"],
    "dimension_systems": {
        "metro": "nielsen_dma",
        "postal_area": [
            {"country": "US", "system": "zip"},
            {"country": "GB", "system": "postal_code"},
        ],
    },
}


class TestParsing:
    def test_from_offering_reads_a_wire_mapping(self) -> None:
        parsed = SubscriptionOffering.from_offering(WIRE_OFFERING)
        assert parsed.metro_system == "nielsen_dma"
        assert parsed.postal_systems == frozenset({US_ZIP, GB_POSTAL})

    def test_from_offering_reads_a_generated_model(self) -> None:
        published = {
            **WIRE_OFFERING,
            "report_definition_uri": "https://seller.example/definitions/geo.json",
            "report_definition_sha256": "a" * 64,
            "reporting_profile": {
                "id": "delivery",
                "version": "1",
                "schema_uri": "https://seller.example/profiles/delivery.json",
                "schema_sha256": "b" * 64,
                "grain": "row",
                "primary_keys": ["row_id"],
            },
        }
        model = ReportingDeliveryOffering.model_validate(published)
        parsed = SubscriptionOffering.from_offering(model)
        assert parsed.metro_system == "nielsen_dma"
        assert parsed.postal_systems == frozenset({US_ZIP, GB_POSTAL})

    def test_an_offering_without_dimension_systems_parses_empty(self) -> None:
        wire = {k: v for k, v in WIRE_OFFERING.items() if k != "dimension_systems"}
        parsed = SubscriptionOffering.from_offering(wire)
        assert parsed.metro_system is None
        assert parsed.postal_systems == frozenset()

    def test_a_selector_parses_dimension_systems_from_a_generated_subscription(self) -> None:
        model = ReportingSubscription.model_validate(
            config({"metro": "uk_itl1", "postal_area": postal(US_ZIP, GB_POSTAL)})
        )
        selector = SubscriptionSelector.from_subscription(model)
        assert selector.metro_system == "uk_itl1"
        assert selector.postal_systems == frozenset({US_ZIP, GB_POSTAL})

    def test_a_selector_without_dimension_systems_names_nothing(self) -> None:
        selector = SubscriptionSelector.from_subscription(config())
        assert selector.metro_system is None
        assert selector.postal_systems == frozenset()


class TestSelectorUnmatchedAndVersioning:
    def test_mismatches_lists_dimension_systems(self) -> None:
        wanted = config({"metro": "uk_itl1"})
        failed = _mismatches(LOW_DMA, wanted["selector"], wanted, None)
        assert failed == ("dimension_systems",)
        assert _mismatches(HIGH_ITL, wanted["selector"], wanted, None) == ()

    def test_a_reordered_postal_area_is_the_same_version(self) -> None:
        forward = config({"postal_area": postal(US_ZIP, GB_POSTAL)})
        reversed_ = deepcopy(forward)
        reversed_["selector"]["dimension_systems"]["postal_area"].reverse()
        assert _semantic(forward) == _semantic(reversed_)

    def test_a_different_postal_area_is_a_new_version(self) -> None:
        assert _semantic(config({"postal_area": postal(US_ZIP)})) != _semantic(
            config({"postal_area": postal(GB_POSTAL)})
        )


def test_the_standard_vocabulary_is_the_reporting_dimension_enum() -> None:
    assert STANDARD_REPORTING_DIMENSIONS == {member.value for member in ReportingDimension}
