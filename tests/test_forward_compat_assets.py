"""Regression tests for issue #742: open-union forward compatibility.

Verifies that Format.assets and RepeatableAssetGroup.assets tolerate an
asset_type this pin has no class for, without raising ValidationError, and that
every asset_type the pin DOES have a class for still parses as that class.

The novel value is DERIVED, not written down. Four tests here used
``pixel_tracker`` as their stand-in novel type and asserted it parsed as
``UnknownFormatAsset``. ``pixel_tracker`` is declared by the pinned schema and
has had a generated class all along, so those assertions were encoding the
defect: the vocabulary the union routed by was a hand-written frozenset in
aliases.py that had fallen 7 behind the generated individual classes and 9
behind the group ones, and the tests agreed with the frozenset instead of with
the schema. A hardcoded "novel" value can become declared by a schema refresh
without the test noticing, which is how these four came to be wrong.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adcp.types import (
    FormatAssetUnion,
    GroupFormatAssetUnion,
    ImageFormatAsset,
    RepeatableAssetGroup,
    UnknownFormatAsset,
    UnknownGroupAsset,
)
from adcp.types.domains.core import format as format_module
from adcp.types.domains.core.format import Assets94, Format


def _make_format_id(name: str) -> dict:
    return {"agent_url": "https://test.example.com", "id": name}


def _declared_asset_types(base: type) -> dict[str, type]:
    """``asset_type`` -> the class this pin generated for it, for one asset base.

    Read off the live classes rather than listed, which is the whole point: the
    union's own vocabulary is derived the same way, so a schema refresh moves
    both together. Asserted non-empty because a derivation that matches nothing
    passes everything.
    """
    found = {
        value.model_fields["asset_type"].default: value
        for value in vars(format_module).values()
        if isinstance(value, type)
        and issubclass(value, base)
        and value is not base
        and "asset_type" in getattr(value, "model_fields", {})
        and isinstance(value.model_fields["asset_type"].default, str)
    }
    assert found, f"no generated subclass of {base.__name__} declares an asset_type"
    return found


def _an_undeclared_asset_type(base: type) -> str:
    """An ``asset_type`` this pin has no class for, checked against the ones it has."""
    candidate = "x_future_asset_type"
    assert candidate not in _declared_asset_types(base), (
        f"{candidate!r} became a declared asset_type; pick another stand-in, and "
        "check whether the tests below were grading routing or only acceptance"
    )
    return candidate


_UNDECLARED_INDIVIDUAL = _an_undeclared_asset_type(format_module.BaseIndividualAsset)
_UNDECLARED_GROUP = _an_undeclared_asset_type(format_module.BaseGroupAsset)


class TestUnknownFormatAssetParsing:
    """Format.assets accepts novel asset_type values after _forward_compat patch."""

    def test_novel_asset_type_parses_as_unknown(self):
        """A novel asset_type that is not in the SDK parses as UnknownFormatAsset."""
        fmt = Format.model_validate(
            {
                "format_id": _make_format_id("test"),
                "name": "Test",
                "assets": [
                    {
                        "asset_id": "impression_tracker",
                        "asset_type": _UNDECLARED_INDIVIDUAL,
                        "item_type": "individual",
                        "required": False,
                    },
                ],
            }
        )
        assert fmt.assets is not None
        assert len(fmt.assets) == 1
        asset = fmt.assets[0]
        assert isinstance(asset, UnknownFormatAsset)
        assert asset.asset_type == _UNDECLARED_INDIVIDUAL
        assert asset.asset_id == "impression_tracker"

    def test_known_asset_type_still_parses_correctly(self):
        """Known asset types continue to parse as their specific typed class."""
        fmt = Format.model_validate(
            {
                "format_id": _make_format_id("test"),
                "name": "Test",
                "assets": [
                    {
                        "asset_id": "hero",
                        "asset_type": "image",
                        "item_type": "individual",
                        "required": True,
                    },
                ],
            }
        )
        assert fmt.assets is not None
        asset = fmt.assets[0]
        assert isinstance(asset, ImageFormatAsset)
        assert asset.asset_type == "image"

    def test_mixed_known_and_unknown_assets_parse(self):
        """A format with both known and novel asset types parses fully."""
        fmt = Format.model_validate(
            {
                "format_id": _make_format_id("mixed"),
                "name": "Mixed",
                "assets": [
                    {
                        "asset_id": "img",
                        "asset_type": "image",
                        "item_type": "individual",
                        "required": True,
                    },
                    {
                        "asset_id": "tracker",
                        "asset_type": _UNDECLARED_INDIVIDUAL,
                        "item_type": "individual",
                        "required": False,
                    },
                    {
                        "asset_id": "headline",
                        "asset_type": "text",
                        "item_type": "individual",
                        "required": True,
                    },
                ],
            }
        )
        assert fmt.assets is not None
        assert len(fmt.assets) == 3
        assert isinstance(fmt.assets[0], ImageFormatAsset)
        assert isinstance(fmt.assets[1], UnknownFormatAsset)
        assert fmt.assets[1].asset_type == _UNDECLARED_INDIVIDUAL

    def test_unknown_asset_preserves_extra_fields(self):
        """Extra fields on unknown assets are preserved via extra='allow'."""
        fmt = Format.model_validate(
            {
                "format_id": _make_format_id("test"),
                "name": "Test",
                "assets": [
                    {
                        "asset_id": "track",
                        "asset_type": _UNDECLARED_INDIVIDUAL,
                        "item_type": "individual",
                        "required": False,
                        "tracking_url": "https://example.com/track",
                        "fire_on": "impression",
                    },
                ],
            }
        )
        asset = fmt.assets[0]  # type: ignore[index]
        assert isinstance(asset, UnknownFormatAsset)
        extra = asset.__pydantic_extra__ or {}
        assert extra.get("tracking_url") == "https://example.com/track"
        assert extra.get("fire_on") == "impression"

    def test_repeatable_group_still_routes_correctly(self):
        """RepeatableAssetGroup (item_type='repeatable_group') still parses."""
        fmt = Format.model_validate(
            {
                "format_id": _make_format_id("carousel"),
                "name": "Carousel",
                "assets": [
                    {
                        "asset_group_id": "slide",
                        "item_type": "repeatable_group",
                        "required": True,
                        "min_count": 2,
                        "max_count": 10,
                        "assets": [],
                    },
                ],
            }
        )
        assert fmt.assets is not None
        group = fmt.assets[0]
        assert isinstance(group, RepeatableAssetGroup)

    def test_multiple_novel_asset_types_no_validation_error(self):
        """Multiple assets with novel asset_types all parse without error."""
        assets_data = [
            {
                "asset_id": f"slot_{i}",
                "asset_type": f"novel_type_{i}",
                "item_type": "individual",
                "required": False,
            }
            for i in range(10)
        ]
        fmt = Format.model_validate(
            {
                "format_id": _make_format_id("test"),
                "name": "Test",
                "assets": assets_data,
            }
        )
        assert fmt.assets is not None
        assert len(fmt.assets) == 10
        for asset in fmt.assets:
            assert isinstance(asset, UnknownFormatAsset)


class TestUnknownGroupAssetParsing:
    """Assets94.assets (RepeatableAssetGroup) also tolerates novel asset_type."""

    def test_novel_group_asset_parses_as_unknown(self):
        """A novel asset_type inside a repeatable group parses as UnknownGroupAsset."""
        group = Assets94.model_validate(
            {
                "asset_group_id": "slide",
                "item_type": "repeatable_group",
                "required": True,
                "min_count": 1,
                "max_count": 5,
                "assets": [
                    {
                        "asset_id": "track",
                        "asset_type": _UNDECLARED_GROUP,
                        "required": False,
                    },
                ],
            }
        )
        assert len(group.assets) == 1
        assert isinstance(group.assets[0], UnknownGroupAsset)
        assert group.assets[0].asset_type == _UNDECLARED_GROUP

    def test_known_group_asset_still_parses(self):
        """Known asset_type inside a group still routes to the typed class."""
        from adcp.types import ImageFormatGroupAsset

        group = Assets94.model_validate(
            {
                "asset_group_id": "product",
                "item_type": "repeatable_group",
                "required": True,
                "min_count": 1,
                "max_count": 10,
                "assets": [
                    {
                        "asset_id": "img",
                        "asset_type": "image",
                        "required": True,
                    },
                ],
            }
        )
        assert isinstance(group.assets[0], ImageFormatGroupAsset)


class TestUnknownFormatAssetContract:
    """Structural invariants on the fallback type."""

    def test_requires_asset_id(self):
        """asset_id is required (inherited from BaseIndividualAsset)."""
        with pytest.raises(ValidationError):
            UnknownFormatAsset.model_validate(
                {
                    "asset_type": _UNDECLARED_INDIVIDUAL,
                    "item_type": "individual",
                    "required": False,
                    # asset_id missing
                }
            )

    def test_requires_required_field(self):
        """required is required (inherited from BaseIndividualAsset)."""
        with pytest.raises(ValidationError):
            UnknownFormatAsset.model_validate(
                {
                    "asset_id": "track",
                    "asset_type": _UNDECLARED_INDIVIDUAL,
                    "item_type": "individual",
                    # required missing
                }
            )

    def test_asset_type_is_required_not_empty_default(self):
        """asset_type has no default — a missing discriminator is a real error."""
        with pytest.raises(ValidationError):
            UnknownFormatAsset.model_validate(
                {
                    "asset_id": "track",
                    "item_type": "individual",
                    "required": False,
                    # asset_type missing
                }
            )

    def test_unknown_format_asset_is_unknown_form(self):
        """UnknownFormatAsset carries an asset_type this pin has no class for.

        The vocabulary is derived, not restated. This assertion used to list 14
        asset types by hand — the same stale frozenset the union routed by, in a
        second copy, which is why ``pixel_tracker`` satisfied it while being a
        type the schema declares and the generator had a class for.
        """
        asset = UnknownFormatAsset.model_validate(
            {
                "asset_id": "x",
                "asset_type": _UNDECLARED_INDIVIDUAL,
                "item_type": "individual",
                "required": False,
            }
        )
        assert asset.asset_type not in _declared_asset_types(format_module.BaseIndividualAsset)


class TestPublicAPIExports:
    """New types are accessible from the public surface."""

    def test_imports_from_adcp_types(self):
        from adcp.types import (
            UnknownFormatAsset,
            UnknownGroupAsset,
        )

        assert UnknownFormatAsset is not None
        assert UnknownGroupAsset is not None
        assert FormatAssetUnion is not None
        assert GroupFormatAssetUnion is not None


class TestDeclaredAssetTypesAreNotDegraded:
    """Every asset_type this pin generated a class for must reach that class.

    The open union exists so an asset_type defined AFTER this pin survives. It
    must not swallow the ones defined at it. The vocabulary it routed by was a
    hand-written frozenset 7 behind the generated individual classes and 9
    behind the group ones, so seven individual and nine group types arrived as
    UnknownFormatAsset / UnknownGroupAsset with their typed fields in
    __pydantic_extra__, while the generated classes for all of them sat unused.
    The vocabulary is now emitted from the arms themselves, and this grades that
    end to end — at the PARSE level, because the callable discriminator reads
    the vocabulary before pydantic reaches the arms, so counting arms says
    nothing about where a document lands.

    Parametrized over the generated classes rather than a list, so an asset type
    a schema refresh adds is covered the moment its class is generated.
    """

    @pytest.mark.parametrize(
        "asset_type,expected",
        sorted(_declared_asset_types(format_module.BaseIndividualAsset).items()),
    )
    def test_each_declared_individual_type_reaches_its_class(
        self, asset_type: str, expected: type
    ) -> None:
        fmt = Format.model_validate(
            {
                "format_id": _make_format_id("declared"),
                "name": "Declared",
                "assets": [
                    {
                        "asset_id": "slot",
                        "asset_type": asset_type,
                        "item_type": "individual",
                        "required": True,
                    }
                ],
            }
        )
        assert fmt.assets is not None
        asset = fmt.assets[0]
        assert type(asset) is expected
        assert not isinstance(asset, UnknownFormatAsset)
        # A degraded asset keeps its type-specific fields as untyped extras, so
        # an empty extra dict is part of what "reached its class" means.
        assert not (asset.__pydantic_extra__ or {})

    @pytest.mark.parametrize(
        "asset_type,expected",
        sorted(_declared_asset_types(format_module.BaseGroupAsset).items()),
    )
    def test_each_declared_group_type_reaches_its_class(
        self, asset_type: str, expected: type
    ) -> None:
        group = Assets94.model_validate(
            {
                "asset_group_id": "slide",
                "item_type": "repeatable_group",
                "required": True,
                "min_count": 1,
                "max_count": 5,
                "assets": [{"asset_id": "slot", "asset_type": asset_type, "required": True}],
            }
        )
        slot = group.assets[0]
        assert type(slot) is expected
        assert not isinstance(slot, UnknownGroupAsset)
        assert not (slot.__pydantic_extra__ or {})

    def test_the_derivation_covers_what_the_pinned_schema_declares(self) -> None:
        """The parametrized population is the schema's, not a subset of it.

        Without this, a generator that stopped emitting a class for one asset
        type would shrink the population above and stay green — the two tests
        would grade fewer rows and say nothing about the one that vanished.
        """
        import json
        from pathlib import Path

        from adcp.validation.version import resolve_bundle_key

        root = Path(__file__).parent.parent
        pinned = (root / "src/adcp/ADCP_VERSION").read_text().strip()
        schema_path = root / "schemas/cache" / resolve_bundle_key(pinned) / "core/format.json"
        assert schema_path.is_file(), f"no pinned format schema at {schema_path}"
        arms = json.loads(schema_path.read_text())["properties"]["assets"]["items"]["oneOf"]

        def slot(item_type: str) -> dict:
            matching = [
                arm
                for arm in arms
                if arm.get("properties", {}).get("item_type", {}).get("const") == item_type
            ]
            assert len(matching) == 1, f"expected one {item_type!r} arm, got {len(matching)}"
            return matching[0]

        # The two arms nest to different depths and are selected by their
        # item_type const, not by position.
        individual = {a["properties"]["asset_type"]["const"] for a in slot("individual")["oneOf"]}
        group = {
            a["properties"]["asset_type"]["const"]
            for a in slot("repeatable_group")["properties"]["assets"]["items"]["oneOf"]
        }
        assert individual, "the individual arm declares no asset_type consts"
        assert group, "the repeatable-group arm declares no asset_type consts"
        assert set(_declared_asset_types(format_module.BaseIndividualAsset)) == individual
        assert set(_declared_asset_types(format_module.BaseGroupAsset)) == group
