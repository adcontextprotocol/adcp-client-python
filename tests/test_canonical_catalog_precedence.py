"""Catalog resolution fails closed outside the bundled AAO fallback."""

from __future__ import annotations

import pytest

from adcp.canonical_formats import build_catalog_index, project_legacy_format_id


def _entry(owner: str, identifier: str, kind: str = "image") -> dict[str, object]:
    return {
        "format_id": {"agent_url": owner, "id": identifier},
        "canonical": {"kind": kind},
    }


def test_duplicate_exact_owner_and_id_is_rejected() -> None:
    catalog = build_catalog_index(
        [
            _entry("https://catalog.example/formats", "duplicate", "image"),
            _entry("https://catalog.example/formats", "duplicate", "display_tag"),
        ]
    )

    result = project_legacy_format_id(
        {"agent_url": "https://catalog.example/formats", "id": "duplicate"},
        product_id="p-1",
        field="format_ids[0]",
        catalog=catalog,
    )

    assert result.declaration is None
    assert result.diagnostic is not None
    assert result.diagnostic.resolution_failure == "catalog_collision"


def test_custom_catalog_does_not_bare_id_match_an_unrelated_owner() -> None:
    catalog = build_catalog_index([_entry("https://catalog.example/formats", "unique-custom")])

    result = project_legacy_format_id(
        {"agent_url": "https://unrelated.example/formats", "id": "unique-custom"},
        product_id="p-1",
        field="format_ids[0]",
        catalog=catalog,
    )

    assert result.declaration is None
    assert result.diagnostic is not None
    assert result.diagnostic.resolution_failure == "no_match"


@pytest.mark.parametrize(
    "owner",
    [
        "http://catalog.example/formats",
        "https://127.0.0.1/formats",
        "https://metadata.internal/formats",
    ],
)
def test_custom_catalog_never_resolves_an_unsafe_owner(owner: str) -> None:
    catalog = build_catalog_index([_entry(owner, "unsafe")])

    result = project_legacy_format_id(
        {"agent_url": owner, "id": "unsafe"},
        product_id="p-1",
        field="format_ids[0]",
        catalog=catalog,
    )

    assert result.declaration is None
    assert result.diagnostic is not None
    assert result.diagnostic.resolution_failure == "no_match"


def test_bundled_aao_catalog_keeps_its_unique_bare_id_fallback() -> None:
    result = project_legacy_format_id(
        {
            "agent_url": "https://publisher.example/formats",
            "id": "display_300x250_image",
        },
        product_id="p-1",
        field="format_ids[0]",
    )

    assert result.declaration is not None
    assert result.declaration.format_kind.value == "image"
