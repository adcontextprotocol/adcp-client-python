"""``fix_signal_listing_range_subclasses`` reuses ``SignalListing.Range`` by shape.

A class that extends ``core/signal-listing.json`` gets its own generated
``Range`` class, identical to ``SignalListing.Range`` but a different type. The
post-generation pass used to find that class by matching the generated text of
two named files, ``extra='forbid'`` and all. The generator drifted
(``StrictFloat``, new descriptions), the text stopped matching, the duplicate
classes stayed, and the pass still printed success. adcp#8866 opens the schema
(``extra='allow'``), which would have kept the text match missing.

The pass now scans every generated module for a ``SignalListing`` subclass that
declares ``Range``, compares the copy with ``SignalListing.Range`` by what it
validates (bases, ``model_config``, field types, constraints, defaults; not
descriptions), deletes it, and adds ``Range`` to the module's
``signal_listing`` import. It raises when a copy diverges, when the import
cannot be extended, or when a ``Range`` survives. A module that declares
``Range`` without subclassing ``SignalListing`` (``get_signals_response``
inlines its signal) keeps its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts import post_generate_fixes as fixes

FORBID = "    model_config = ConfigDict(\n        extra='forbid',\n    )\n"
ALLOW = "    model_config = ConfigDict(\n        extra='allow',\n    )\n"

HEADER = "from __future__ import annotations\n\n"

# Subclass modules in two domains, to show the scan is not tied to file names.
SUBCLASSES = {
    "core/wholesale_feed_event.py": "from .signal_listing import SignalListing\n",
    "media_buy/some_feed.py": "from ..core.signal_listing import SignalListing\n",
}
INLINE = "signals/get_signals_response.py"
IMPORTS = (
    "from typing import Annotated\n\n"
    "from adcp.types.base import AdCPBaseModel, NoExplicitNull\n"
    "from pydantic import ConfigDict, Field, StrictFloat\n\n"
)


def _range(config: str = FORBID, *, described: bool = False, body: str | None = None) -> str:
    if body is None:
        if described:
            body = (
                "    min: Annotated[StrictFloat, Field(description='Minimum value, inclusive.')]\n"
                "    max: Annotated[StrictFloat, Field(description='Maximum value, inclusive.')]\n"
            )
        else:
            body = "    min: StrictFloat\n    max: StrictFloat\n"
    return f"class Range(AdCPBaseModel):\n{config}{body}"


def _listing(range_class: str | None) -> str:
    text = HEADER + IMPORTS + "\n"
    if range_class:
        text += range_class + "\n\n"
    text += (
        "class SignalListing(AdCPBaseModel):\n"
        "    model_config = ConfigDict(\n        extra='allow',\n    )\n"
        "    range: Annotated[Range | None, NoExplicitNull] = None\n"
    )
    return text


def _subclass(
    range_class: str | None,
    import_line: str = "from .signal_listing import SignalListing\n",
    base: str = "SignalListing",
) -> str:
    text = HEADER + IMPORTS + import_line + "\n\n"
    if range_class:
        text += range_class + "\n\n"
    text += f"class Signal({base}):\n    range: Annotated[Range | None, NoExplicitNull] = None\n"
    return text


def _inline(range_class: str) -> str:
    return (
        HEADER
        + IMPORTS
        + "from ..core.signal_listing import SignalListing\n\n\n"
        + range_class
        + "\n\n"
        + "class Signal(AdCPBaseModel):\n"
        + "    range: Annotated[Range | None, NoExplicitNull] = None\n"
    )


@pytest.fixture
def generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "domains"
    for sub in ("core", "signals", "media_buy"):
        (directory / sub).mkdir(parents=True)
    monkeypatch.setattr(fixes, "OUTPUT_DIR", directory)
    return directory


def _put(generated: Path, listing: str, modules: dict[str, str]) -> dict[str, Path]:
    (generated / "core" / "signal_listing.py").write_text(listing)
    paths = {}
    for name, text in modules.items():
        paths[name] = generated / name
        paths[name].write_text(text)
    return paths


def _assert_reused(paths: dict[str, Path]) -> None:
    for name, path in paths.items():
        source = path.read_text()
        tree = ast.parse(source)
        compile(source, name, "exec")
        assert not any(isinstance(n, ast.ClassDef) and n.name == "Range" for n in tree.body), name
        assert "import Range, SignalListing\n" in source, name
        assert "_SignalListingRange" not in source, name


def _scenario(generated: Path, config: str) -> dict[str, Path]:
    return _put(
        generated,
        _listing(_range(config, described=True)),
        {
            "core/wholesale_feed_event.py": _subclass(_range(config)),
            "media_buy/some_feed.py": _subclass(
                _range(config, described=True),
                "from ..core.signal_listing import SignalListing\n",
            ),
        },
    )


def test_closed_copies_of_the_beta5_shape_are_reused(generated: Path) -> None:
    paths = _scenario(generated, FORBID)
    fixes.fix_signal_listing_range_subclasses()
    _assert_reused(paths)


def test_open_copies_of_the_8866_shape_are_reused(generated: Path) -> None:
    paths = _scenario(generated, ALLOW)
    fixes.fix_signal_listing_range_subclasses()
    _assert_reused(paths)


def test_copies_with_no_model_config_are_reused(generated: Path) -> None:
    paths = _scenario(generated, "")
    fixes.fix_signal_listing_range_subclasses()
    _assert_reused(paths)


def test_description_only_differences_do_not_count(generated: Path) -> None:
    listing = _range(
        FORBID,
        body=(
            "    min: Annotated[StrictFloat, Field(description='Lower bound.')]\n"
            "    max: Annotated[StrictFloat, Field(description='Upper bound.')]\n"
        ),
    )
    paths = _put(
        generated,
        _listing(listing),
        {"core/wholesale_feed_event.py": _subclass(_range(FORBID))},
    )
    fixes.fix_signal_listing_range_subclasses()
    _assert_reused(paths)


DIVERGENT = {
    "listing open, copy closed": (
        _range(ALLOW, described=True),
        _range(FORBID, described=True),
    ),
    "field type": (
        _range(FORBID, described=True),
        _range(FORBID, body="    min: int\n    max: StrictFloat\n"),
    ),
    "extra field": (
        _range(FORBID, described=True),
        _range(FORBID, body="    min: StrictFloat\n    max: StrictFloat\n    step: StrictFloat\n"),
    ),
    "field constraint": (
        _range(FORBID, described=True),
        _range(
            FORBID,
            body=(
                "    min: Annotated[StrictFloat, Field(ge=0)]\n"
                "    max: Annotated[StrictFloat, Field(description='Maximum value, inclusive.')]\n"
            ),
        ),
    ),
}


@pytest.mark.parametrize("listing,copy", DIVERGENT.values(), ids=DIVERGENT.keys())
@pytest.mark.parametrize("target", sorted(SUBCLASSES))
def test_a_divergent_copy_raises_and_leaves_the_file_alone(
    generated: Path, listing: str, copy: str, target: str
) -> None:
    paths = _put(
        generated,
        _listing(listing),
        {target: _subclass(copy, SUBCLASSES[target])},
    )
    before = paths[target].read_text()
    with pytest.raises(RuntimeError, match="no longer matches SignalListing.Range"):
        fixes.fix_signal_listing_range_subclasses()
    assert paths[target].read_text() == before


def test_a_module_that_does_not_subclass_keeps_its_own_range(generated: Path) -> None:
    # get_signals_response inlines its signal: no parent type, so no reuse and no raise,
    # even when its Range differs from SignalListing.Range.
    for copy in (_range(FORBID, described=True), _range(ALLOW, body="    min: int\n")):
        inline = _inline(copy)
        paths = _put(generated, _listing(_range(FORBID, described=True)), {INLINE: inline})
        fixes.fix_signal_listing_range_subclasses()
        assert paths[INLINE].read_text() == inline


def test_running_twice_is_idempotent(generated: Path) -> None:
    paths = _scenario(generated, FORBID)
    fixes.fix_signal_listing_range_subclasses()
    first = {name: path.read_text() for name, path in paths.items()}
    fixes.fix_signal_listing_range_subclasses()
    assert {name: path.read_text() for name, path in paths.items()} == first


def test_a_subclass_without_range_is_unchanged(generated: Path) -> None:
    text = _subclass(None)
    paths = _put(generated, _listing(_range(FORBID)), {"core/wholesale_feed_event.py": text})
    fixes.fix_signal_listing_range_subclasses()
    assert paths["core/wholesale_feed_event.py"].read_text() == text


def test_a_subclass_range_without_a_listing_range_raises(generated: Path) -> None:
    paths = _put(
        generated,
        _listing(None),
        {"core/wholesale_feed_event.py": _subclass(_range(FORBID))},
    )
    before = paths["core/wholesale_feed_event.py"].read_text()
    with pytest.raises(RuntimeError, match="core/signal_listing.py no longer declares Range"):
        fixes.fix_signal_listing_range_subclasses()
    assert paths["core/wholesale_feed_event.py"].read_text() == before


def test_a_parenthesized_import_gets_range_inside_it(generated: Path) -> None:
    paths = _put(
        generated,
        _listing(_range(FORBID)),
        {
            "core/wholesale_feed_event.py": _subclass(
                _range(FORBID),
                import_line="from .signal_listing import (\n    SignalListing,\n)\n",
            )
        },
    )
    fixes.fix_signal_listing_range_subclasses()
    source = paths["core/wholesale_feed_event.py"].read_text()
    compile(source, "wholesale_feed_event.py", "exec")
    assert "from .signal_listing import (\n    Range,\n    SignalListing,\n)\n" in source
    assert "class Range" not in source


def test_an_attribute_base_without_a_direct_import_raises(generated: Path) -> None:
    text = _subclass(
        _range(FORBID),
        import_line="from . import signal_listing as signal_listing_1\n",
        base="signal_listing_1.SignalListing",
    )
    paths = _put(generated, _listing(_range(FORBID)), {"core/wholesale_feed_event.py": text})
    with pytest.raises(
        RuntimeError,
        match="subclasses SignalListing without a 'from ...signal_listing import SignalListing'",
    ):
        fixes.fix_signal_listing_range_subclasses()
    assert paths["core/wholesale_feed_event.py"].read_text() == text


def test_the_generated_tree_shares_range_only_where_it_subclasses() -> None:
    from adcp.types.domains import signals
    from adcp.types.domains.core import signal_listing, wholesale_feed_event
    from adcp.types.domains.signals import get_signals_response

    assert wholesale_feed_event.Range is signal_listing.Range
    assert (
        wholesale_feed_event.Signal.model_fields["range"].annotation
        == signal_listing.SignalListing.model_fields["range"].annotation
    )
    assert get_signals_response.Range is not signal_listing.Range
    assert signals.Range is get_signals_response.Range

    def declares_range(module: object) -> bool:
        tree = ast.parse(Path(module.__file__).read_text())  # type: ignore[attr-defined]
        return any(isinstance(n, ast.ClassDef) and n.name == "Range" for n in tree.body)

    assert not declares_range(wholesale_feed_event)
    assert declares_range(get_signals_response)
