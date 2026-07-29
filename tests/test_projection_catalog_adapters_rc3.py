"""Exact RC3 safety rules for durable projection catalog adapters."""

from __future__ import annotations

from copy import deepcopy

import pytest

from adcp import Format
from adcp.canonical_formats import (
    CanonicalFormatLegacyResolutionError,
    canonical_format_legacy_resolver_from_catalog_snapshots,
    legacy_format_converter_from_catalog_snapshots,
    project_canonical_response_to_legacy,
    project_legacy_format_id,
    projection_adapters_from_catalog_snapshots,
    resolve_legacy_format_refs,
)
from adcp.canonical_formats.projection import (
    LegacyCreativeProjectionError,
    LegacyFormatConversionContext,
)
from adcp.types.legacy import LegacyFormatId

LEGACY_REF = {
    "agent_url": "https://formats.vox.example/mcp",
    "id": "vox_mrec_html",
    "width": 300,
    "height": 250,
}


def _snapshot(**overrides: object) -> dict[str, object]:
    declaration = {
        "format_kind": "display_tag",
        "format_option_id": "vox_mrec_html",
        "params": {"width": 300, "height": 250},
        "v1_format_ref": [deepcopy(LEGACY_REF)],
    }
    declaration.update(overrides)
    return {
        "source": "configured",
        "publisher_domain": "vox.example",
        "formats": [declaration],
    }


def test_publisher_route_lookup_normalizes_case_and_terminal_dot() -> None:
    adapters = projection_adapters_from_catalog_snapshots([_snapshot()])

    projected = project_canonical_response_to_legacy(
        {
            "creative_id": "creative-1",
            "format_kind": "display_tag",
            "params": {"width": 300, "height": 250},
            "format_option_ref": {
                "scope": "publisher",
                "publisher_domain": "VOX.EXAMPLE.",
                "format_option_id": "vox_mrec_html",
            },
        },
        resolver=adapters.canonical_format_legacy_resolver,
    )

    assert projected["format_id"] == LEGACY_REF


def test_bidirectional_adapter_rejects_product_local_route() -> None:
    snapshot = _snapshot()
    snapshot.pop("publisher_domain")
    with pytest.raises(ValueError, match="publisher-scoped format options"):
        projection_adapters_from_catalog_snapshots([snapshot])


def test_bidirectional_adapter_rejects_many_to_one_route() -> None:
    with pytest.raises(ValueError, match="exactly one legacy route"):
        projection_adapters_from_catalog_snapshots(
            [
                _snapshot(
                    v1_format_ref=[
                        LEGACY_REF,
                        {
                            **LEGACY_REF,
                            "id": "vox_leaderboard_html",
                            "width": 728,
                            "height": 90,
                        },
                    ]
                )
            ]
        )


def test_bidirectional_adapter_rejects_canonical_only_kind() -> None:
    with pytest.raises(ValueError, match="canonical-only format kind"):
        projection_adapters_from_catalog_snapshots(
            [
                _snapshot(
                    format_kind="image_carousel",
                    format_option_id="vox_carousel",
                    params={"min_items": 2, "max_items": 4},
                )
            ]
        )


def test_bidirectional_adapter_rejects_conflicting_reverse_routes() -> None:
    mirror = _snapshot(
        v1_format_ref=[{**LEGACY_REF, "agent_url": "https://mirror.vox.example/mcp"}]
    )
    mirror["source"] = "approved_community_mirror"
    with pytest.raises(ValueError, match="conflicting reverse routes"):
        projection_adapters_from_catalog_snapshots([_snapshot(), mirror])


def test_bidirectional_adapter_rejects_one_route_for_two_options() -> None:
    snapshot = _snapshot()
    snapshot["formats"].append(  # type: ignore[union-attr]
        {
            **deepcopy(snapshot["formats"][0]),  # type: ignore[index]
            "format_kind": "image",
            "format_option_id": "vox_mrec_image",
        }
    )
    with pytest.raises(ValueError, match="conflicting forward routes"):
        projection_adapters_from_catalog_snapshots([snapshot])


def test_bidirectional_adapter_rejects_duplicate_declarations() -> None:
    snapshot = _snapshot()
    snapshot["formats"].append(deepcopy(snapshot["formats"][0]))  # type: ignore[union-attr,index]
    with pytest.raises(ValueError, match="duplicate declarations"):
        projection_adapters_from_catalog_snapshots([snapshot])


def test_standalone_reverse_resolver_skips_canonical_only_kind() -> None:
    snapshot = _snapshot(
        format_kind="image_carousel",
        format_option_id="vox_carousel",
        params={"min_items": 2, "max_items": 4},
    )
    resolver = canonical_format_legacy_resolver_from_catalog_snapshots([snapshot])
    declaration = Format(
        publisher_domain="vox.example",
        format_option_id="vox_carousel",
        format_kind="image_carousel",
        params={"min_items": 2, "max_items": 4},
    )

    with pytest.raises(CanonicalFormatLegacyResolutionError, match="no durable legacy route"):
        resolve_legacy_format_refs(declaration, resolver=resolver)


def test_standalone_reverse_resolver_raises_on_ambiguous_aliases() -> None:
    snapshot = _snapshot()
    snapshot["formats"].append(  # type: ignore[union-attr]
        {
            **deepcopy(snapshot["formats"][0]),  # type: ignore[index]
            "v1_format_ref": [{**LEGACY_REF, "id": "other_vox_mrec"}],
        }
    )
    resolver = canonical_format_legacy_resolver_from_catalog_snapshots([snapshot])
    declaration = Format(
        publisher_domain="vox.example",
        format_option_id="vox_mrec_html",
        format_kind="display_tag",
        params={"width": 300, "height": 250},
    )

    with pytest.raises(CanonicalFormatLegacyResolutionError, match="ambiguous canonical"):
        resolve_legacy_format_refs(declaration, resolver=resolver)


def test_standalone_forward_converter_raises_on_ambiguous_aliases() -> None:
    snapshot = _snapshot()
    snapshot["formats"].append(  # type: ignore[union-attr]
        {
            **deepcopy(snapshot["formats"][0]),  # type: ignore[index]
            "format_kind": "image",
            "format_option_id": "vox_mrec_image",
        }
    )
    converter = legacy_format_converter_from_catalog_snapshots([snapshot])

    with pytest.raises(LegacyCreativeProjectionError, match="ambiguous legacy"):
        converter(
            LegacyFormatConversionContext(
                format_id=LegacyFormatId.model_validate(LEGACY_REF),
                product_id="vox-homepage",
                field="format_ids[0]",
            )
        )


def test_publisher_snapshot_precedes_exact_bundled_aao_route() -> None:
    aao_ref = {
        "agent_url": "https://creative.adcontextprotocol.org/",
        "id": "display_300x250_image",
    }
    snapshot = {
        "source": "publisher",
        "publisher_domain": "publisher.example",
        "formats": [
            {
                "format_kind": "display_tag",
                "format_option_id": "publisher_mrec_override",
                "params": {"width": 300, "height": 250},
                "v1_format_ref": [aao_ref],
            }
        ],
    }
    converter = legacy_format_converter_from_catalog_snapshots([snapshot])

    projected = project_legacy_format_id(
        aao_ref,
        product_id="publisher-homepage",
        field="format_ids[0]",
        legacy_format_converter=converter,
    )

    assert projected.declaration is not None
    assert projected.declaration.format_kind.value == "display_tag"
    assert projected.declaration.format_option_id == "publisher_mrec_override"
    assert projected.declaration.publisher_domain == "publisher.example"


def test_ordinary_converter_does_not_override_exact_bundled_aao_route() -> None:
    aao_ref = {
        "agent_url": "https://creative.adcontextprotocol.org/",
        "id": "display_300x250_image",
    }

    def ordinary_converter(context):
        return {
            "format_kind": "display_tag",
            "format_option_id": "ordinary_override",
            "params": {"width": 300, "height": 250},
        }

    projected = project_legacy_format_id(
        aao_ref,
        product_id="publisher-homepage",
        field="format_ids[0]",
        legacy_format_converter=ordinary_converter,
    )

    assert projected.declaration is not None
    assert projected.declaration.format_kind.value == "image"
    assert projected.declaration.format_option_id != "ordinary_override"


@pytest.mark.parametrize(
    ("sources", "expected_option_id"),
    [
        (["configured", "approved_community_mirror"], "mirror_option"),
        (
            ["configured", "approved_community_mirror", "publisher"],
            "publisher_option",
        ),
    ],
)
def test_snapshot_source_precedence_is_protocol_order(
    sources: list[str], expected_option_id: str
) -> None:
    legacy_ref = {
        "agent_url": "https://formats.publisher.example/mcp",
        "id": "shared_format",
    }
    option_by_source = {
        "configured": "configured_option",
        "approved_community_mirror": "mirror_option",
        "publisher": "publisher_option",
    }
    snapshots = [
        {
            "source": source,
            "publisher_domain": "publisher.example",
            "formats": [
                {
                    "format_kind": "display_tag",
                    "format_option_id": option_by_source[source],
                    "params": {"width": 300, "height": 250},
                    "v1_format_ref": [legacy_ref],
                }
            ],
        }
        for source in sources
    ]
    converter = legacy_format_converter_from_catalog_snapshots(snapshots)

    projected = project_legacy_format_id(
        legacy_ref,
        product_id="publisher-homepage",
        field="format_ids[0]",
        legacy_format_converter=converter,
    )

    assert projected.declaration is not None
    assert projected.declaration.format_option_id == expected_option_id
