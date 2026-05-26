"""Public surface checks for AdCP 3.1 beta 4 schema additions."""

from __future__ import annotations

_BETA4_EXPORTS = (
    "FormatOptionReference",
    "PackageSignalTargeting",
    "PackageSignalTargetingGroup",
    "PackageSignalTargetingGroups",
    "Placement",
    "PlacementReference",
    "ProductSignalTargetingOption",
    "SignalListing",
    "SignalRef",
    "SignalTargeting",
    "SignalTargetingExpression",
    "SignalTargetingRules",
    "WebhookChallenge",
    "WebhookChallengeResponse",
)


def test_beta4_symbols_are_publicly_exported() -> None:
    import adcp
    import adcp.types

    for name in _BETA4_EXPORTS:
        assert hasattr(adcp, name), f"{name} not exported from adcp"
        assert name in adcp.__all__, f"{name} not declared in adcp.__all__"
        assert hasattr(adcp.types, name), f"{name} not exported from adcp.types"
        assert name in adcp.types.__all__, f"{name} not declared in adcp.types.__all__"


def test_format_option_references_accept_product_and_publisher_scopes() -> None:
    from adcp import FormatOptionReference, PackageRequest

    product_ref = FormatOptionReference.model_validate(
        {"scope": "product", "format_option_id": "display_image"}
    )
    publisher_ref = FormatOptionReference.model_validate(
        {
            "scope": "publisher",
            "publisher_domain": "example.com",
            "format_option_id": "homepage_takeover",
        }
    )

    assert product_ref.scope == "product"
    assert publisher_ref.publisher_domain == "example.com"

    package = PackageRequest.model_validate(
        {
            "product_id": "prod_1",
            "pricing_option_id": "cpm",
            "budget": 1000,
            "format_option_refs": [{"scope": "product", "format_option_id": "display_image"}],
        }
    )

    assert package.format_option_refs is not None
    assert package.format_option_refs[0].format_option_id == "display_image"


def test_structured_placement_references_round_trip_on_assignments() -> None:
    from adcp import Placement, PlacementReference
    from adcp.types import CreativeAssignment

    placement = Placement.model_validate(
        {
            "kind": "publisher_ref",
            "placement_id": "home_top",
            "publisher_domain": "example.com",
            "mode": "targetable",
        }
    )
    ref = PlacementReference.model_validate(
        {"publisher_domain": "example.com", "placement_id": "home_top"}
    )
    assignment = CreativeAssignment.model_validate(
        {
            "creative_id": "cr_1",
            "placement_refs": [{"publisher_domain": "example.com", "placement_id": "home_top"}],
            "placement_ids": ["legacy_home_top"],
        }
    )

    assert placement.placement_id == ref.placement_id
    assert assignment.placement_refs is not None
    assert assignment.placement_refs[0].publisher_domain == "example.com"
    assert assignment.placement_ids == ["legacy_home_top"]


def test_signal_refs_and_targeting_validate_new_grouped_shapes() -> None:
    from adcp import (
        PackageRequest,
        PackageSignalTargetingGroups,
        ProductSignalTargetingOption,
        SignalRef,
        SignalTargeting,
    )

    signal_ref = {"scope": "product", "signal_id": "luxury_auto"}
    data_provider_ref = {
        "scope": "data_provider",
        "data_provider_domain": "signals.example.com",
        "signal_id": "auto_intenders",
    }

    assert SignalRef.model_validate(signal_ref).scope == "product"
    assert SignalRef.model_validate(data_provider_ref).data_provider_domain == (
        "signals.example.com"
    )

    binary = SignalTargeting.model_validate(
        {"signal_ref": signal_ref, "value_type": "binary", "value": True}
    )
    categorical = SignalTargeting.model_validate(
        {
            "signal_ref": signal_ref,
            "value_type": "categorical",
            "values": ["suv", "ev"],
        }
    )
    numeric = SignalTargeting.model_validate(
        {
            "signal_ref": signal_ref,
            "value_type": "numeric",
            "min_value": 1,
            "max_value": 10,
        }
    )

    assert binary.value is True
    assert categorical.values == ["suv", "ev"]
    assert numeric.min_value == 1

    option = ProductSignalTargetingOption.model_validate(
        {
            "signal_ref": signal_ref,
            "name": "Luxury Auto",
            "description": "Users likely to buy luxury autos.",
            "value_type": "binary",
            "activation_status": "ready",
            "allowed_targeting_modes": ["include", "exclude"],
        }
    )
    assert option.allowed_targeting_modes is not None
    assert [mode.value for mode in option.allowed_targeting_modes] == [
        "include",
        "exclude",
    ]

    groups_payload = {
        "operator": "all",
        "groups": [
            {
                "operator": "any",
                "signals": [
                    {
                        "signal_ref": signal_ref,
                        "value_type": "binary",
                        "value": True,
                        "pricing_option_id": "signal_cpm",
                    }
                ],
            },
            {
                "operator": "none",
                "signals": [
                    {
                        "signal_ref": data_provider_ref,
                        "value_type": "categorical",
                        "values": ["blocked"],
                    }
                ],
            },
        ],
    }
    groups = PackageSignalTargetingGroups.model_validate(groups_payload)
    assert groups.operator == "all"
    assert groups.groups[0].operator.value == "any"
    assert groups.groups[0].signals[0].pricing_option_id == "signal_cpm"
    assert groups.groups[1].signals[0].signal_ref.scope == "data_provider"

    package = PackageRequest.model_validate(
        {
            "product_id": "prod_1",
            "pricing_option_id": "cpm",
            "budget": 1000,
            "targeting_overlay": {"signal_targeting_groups": groups_payload},
        }
    )
    assert package.targeting_overlay is not None
    assert package.targeting_overlay.signal_targeting_groups is not None
    assert package.targeting_overlay.signal_targeting_groups.groups[0].signals[0].value
