"""Backward-compat tests for the deprecated ``GeoPostalArea`` alias.

AdCP 3.1.0-rc.10 restructured postal-area targeting: the inline
``{system, values}`` ``GeoPostalArea`` shape was replaced by the
``PostalArea`` union of a native per-country arm (``{country, system,
values}``) and a legacy arm (``{system, values}`` with country-fused tokens
like ``us_zip``). ``GeoPostalArea`` is retained as a deprecated alias to the
legacy arm so old import and construction code keeps working.

These tests exercise the public surface (``from adcp.types import ...``) and
the public ``PostalArea`` / ``TargetingOverlay`` types — the wire contract,
not the generated class layout.
"""

from __future__ import annotations

import warnings

import pytest
from pydantic import TypeAdapter

from adcp.types import PostalArea, TargetingOverlay

_POSTAL_AREA_ADAPTER = TypeAdapter(PostalArea)

# The 11 legacy country-fused tokens that the removed GeoPostalArea accepted.
LEGACY_TOKENS = [
    "us_zip",
    "us_zip_plus_four",
    "gb_outward",
    "gb_full",
    "ca_fsa",
    "ca_full",
    "de_plz",
    "fr_code_postal",
    "au_postcode",
    "ch_plz",
    "at_plz",
]


@pytest.fixture(autouse=True)
def _reset_geopostalarea_cache():
    """Drop the cached ``GeoPostalArea`` so each test sees a first access.

    The lazy ``adcp.types.__getattr__`` caches ``GeoPostalArea`` after the first
    access so the DeprecationWarning fires only once per process. Tests that
    assert on the warning must therefore start from an uncached state.
    """
    import adcp.types

    adcp.types.__dict__.pop("GeoPostalArea", None)
    yield
    adcp.types.__dict__.pop("GeoPostalArea", None)


def _geo_postal_area_cls() -> type:
    """Access the deprecated alias without raising the DeprecationWarning."""
    import adcp.types

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return adcp.types.GeoPostalArea


def test_geo_postal_area_import_still_works():
    """``from adcp.types import GeoPostalArea`` does not raise ImportError."""
    import adcp.types

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        # Attribute access exercises the same PEP 562 ``__getattr__`` path as
        # a ``from adcp.types import GeoPostalArea`` statement.
        assert adcp.types.GeoPostalArea is not None


def test_geo_postal_area_access_emits_deprecation_warning():
    """Accessing the alias emits a DeprecationWarning naming the migration."""
    import adcp.types

    with pytest.warns(DeprecationWarning, match="GeoPostalArea is deprecated"):
        alias = adcp.types.GeoPostalArea

    assert alias is not None


def test_geo_postal_area_warning_names_postalarea_migration():
    """The warning points migrators at the PostalArea native arm."""
    import adcp.types

    with pytest.warns(DeprecationWarning) as record:
        _ = adcp.types.GeoPostalArea

    message = str(record[0].message)
    assert "PostalArea" in message
    assert "backward compatibility" in message
    assert "future major" in message


def test_old_construction_shape_still_works():
    """Constructing the old ``{system, values}`` shape succeeds."""
    geo_postal_area = _geo_postal_area_cls()

    area = geo_postal_area(system="us_zip", values=["10001", "10002"])

    dumped = area.model_dump(mode="json")
    assert dumped == {"system": "us_zip", "values": ["10001", "10002"]}


@pytest.mark.parametrize("token", LEGACY_TOKENS)
def test_every_legacy_token_constructs(token: str):
    """All 11 removed-type fused tokens still construct via the alias."""
    geo_postal_area = _geo_postal_area_cls()

    area = geo_postal_area(system=token, values=["X1"])
    assert area.model_dump(mode="json") == {"system": token, "values": ["X1"]}


def test_constructed_value_validates_against_postalarea_union_legacy_arm():
    """The constructed legacy value validates through the PostalArea union.

    Passing the constructed instance preserves the legacy arm — the union
    resolves to the legacy ``{system, values}`` shape rather than injecting a
    spurious ``country``.
    """
    geo_postal_area = _geo_postal_area_cls()

    area = geo_postal_area(system="gb_outward", values=["SW1A"])

    validated = _POSTAL_AREA_ADAPTER.validate_python(area)
    # Legacy arm round-trips faithfully with no injected country field.
    assert validated.model_dump(mode="json") == {
        "system": "gb_outward",
        "values": ["SW1A"],
    }


def test_raw_legacy_value_round_trips_through_postalarea_adapter():
    """Raw mappings must select the legacy arm before native defaults apply."""
    validated = _POSTAL_AREA_ADAPTER.validate_python({"system": "us_zip", "values": ["10001"]})

    wire = _POSTAL_AREA_ADAPTER.dump_python(validated, mode="json")
    assert wire == {"system": "us_zip", "values": ["10001"]}
    reparsed = _POSTAL_AREA_ADAPTER.validate_python(wire)
    assert _POSTAL_AREA_ADAPTER.dump_python(reparsed, mode="json") == wire


def test_legacy_value_accepted_where_geo_postal_areas_used():
    """Legacy postal areas are accepted in ``TargetingOverlay.geo_postal_areas``."""
    geo_postal_area = _geo_postal_area_cls()

    overlay = TargetingOverlay(
        geo_postal_areas=[
            geo_postal_area(system="us_zip", values=["10001"]),
            geo_postal_area(system="gb_outward", values=["SW1A"]),
        ]
    )

    dumped = overlay.model_dump(mode="json", exclude_none=True)["geo_postal_areas"]
    assert dumped == [
        {"system": "us_zip", "values": ["10001"]},
        {"system": "gb_outward", "values": ["SW1A"]},
    ]


def test_raw_legacy_value_round_trips_in_targeting_overlay():
    overlay = TargetingOverlay(
        geo_postal_areas=[{"system": "us_zip", "values": ["10001"]}],
        geo_postal_areas_exclude=[{"system": "gb_outward", "values": ["SW1A"]}],
    )

    wire = overlay.model_dump(mode="json", exclude_none=True)
    assert wire["geo_postal_areas"] == [{"system": "us_zip", "values": ["10001"]}]
    assert wire["geo_postal_areas_exclude"] == [{"system": "gb_outward", "values": ["SW1A"]}]
    assert TargetingOverlay.model_validate(wire).model_dump(mode="json", exclude_none=True) == wire


@pytest.mark.parametrize(
    ("country", "system"),
    [
        ("US", "zip"),
        ("US", "zip_plus_four"),
        ("GB", "outward"),
        ("DE", "plz"),
        ("NL", "postal_code"),
        ("NL", "custom"),
    ],
)
def test_native_postal_country_system_pairs_round_trip(country: str, system: str):
    area = _POSTAL_AREA_ADAPTER.validate_python(
        {"country": country, "system": system, "values": ["example"]}
    )

    assert area.model_dump(mode="json") == {
        "country": country,
        "system": system,
        "values": ["example"],
    }


@pytest.mark.parametrize(
    ("country", "system"),
    [("US", "plz"), ("DE", "zip"), ("NL", "plz")],
)
def test_native_postal_country_system_mismatches_fail_closed(country: str, system: str):
    with pytest.raises(ValueError, match="postal system .* is not valid for country"):
        _POSTAL_AREA_ADAPTER.validate_python(
            {"country": country, "system": system, "values": ["example"]}
        )


def test_targeting_overlay_rejects_mismatched_postal_pair():
    with pytest.raises(ValueError, match="postal system 'plz' is not valid for country 'US'"):
        TargetingOverlay(geo_postal_areas=[{"country": "US", "system": "plz", "values": ["10001"]}])


def test_schema_validation_rejects_mismatched_postal_pair():
    from adcp.validation import validate_request

    outcome = validate_request(
        "get_products",
        {
            "buying_mode": "brief",
            "brief": "Find inventory",
            "targeting_overlay": {
                "geo_postal_areas": [{"country": "US", "system": "plz", "values": ["10001"]}]
            },
        },
        version="3.2.0-beta.5",
    )

    assert outcome.valid is False
    assert any(issue.pointer == "/targeting_overlay/geo_postal_areas/0" for issue in outcome.issues)


def test_unknown_attribute_still_raises_attribute_error():
    """The shim does not swallow genuinely missing attributes."""
    import adcp.types

    with pytest.raises(AttributeError):
        _ = adcp.types.DefinitelyNotAPublicType
