"""``fix_signal_listing_range_subclasses`` reuses ``SignalListing.Range`` by shape.

A signal that extends ``core/signal-listing.json`` gets its own generated
``Range`` class, identical to ``SignalListing.Range`` but a different type. The
post-generation pass used to find that class by matching its generated text,
``extra='forbid'`` and all. The generator drifted (``StrictFloat``, new
descriptions), the text stopped matching, the duplicate classes stayed, and the
pass still printed success. adcp#8866 opens the schema (``extra='allow'``),
which would have kept the text match missing.

The pass now locates each copy by name, compares it with ``SignalListing.Range``
by what it validates (bases, ``model_config``, field types, constraints,
defaults; not descriptions), and raises when a copy diverges or survives.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts import post_generate_fixes as fixes

FORBID = "    model_config = ConfigDict(\n        extra='forbid',\n    )\n"
ALLOW = "    model_config = ConfigDict(\n        extra='allow',\n    )\n"

HEADER = "from __future__ import annotations\n\n"

TARGETS = {
    "signals/get_signals_response.py": (
        "from ..core.signal_listing import Range as _SignalListingRange\n"
    ),
    "core/wholesale_feed_event.py": "from .signal_listing import Range as _SignalListingRange\n",
}


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
    text = (
        HEADER
        + "from typing import Annotated\n\n"
        + "from adcp.types.base import AdCPBaseModel, NoExplicitNull\n"
        + "from pydantic import ConfigDict, Field, StrictFloat\n\n\n"
    )
    if range_class:
        text += range_class + "\n\n"
    text += (
        "class SignalListing(AdCPBaseModel):\n"
        "    model_config = ConfigDict(\n        extra='allow',\n    )\n"
        "    range: Annotated[Range | None, NoExplicitNull] = None\n"
    )
    return text


def _wholesale(range_class: str | None, import_line: str | None = None) -> str:
    text = (
        HEADER
        + "from typing import Annotated\n\n"
        + "from adcp.types.base import AdCPBaseModel, NoExplicitNull\n"
        + "from pydantic import ConfigDict, Field, StrictFloat\n\n"
        + (import_line or "from .signal_listing import SignalListing\n")
        + "\n\n"
    )
    if range_class:
        text += range_class + "\n\n"
    text += "class Signal(SignalListing):\n"
    text += "    range: Annotated[Range | None, NoExplicitNull] = None\n"
    return text


def _signals_response(range_class: str | None) -> str:
    text = (
        HEADER
        + "from typing import Annotated\n\n"
        + "from adcp.types.base import AdCPBaseModel, NoExplicitNull\n"
        + "from pydantic import ConfigDict, Field, StrictFloat\n\n"
        + "from ..core.signal_listing import SignalListing\n"
        + "from ..enums import signal_value_type\n\n\n"
    )
    if range_class:
        text += range_class + "\n\n"
    text += "class Signal(AdCPBaseModel):\n"
    text += "    range: Annotated[Range | None, NoExplicitNull] = None\n"
    return text


@pytest.fixture
def generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "domains"
    for sub in ("core", "signals"):
        (directory / sub).mkdir(parents=True)
    monkeypatch.setattr(fixes, "OUTPUT_DIR", directory)
    return directory


def _write(
    generated: Path,
    listing: str,
    signals: str | None,
    wholesale: str | None,
) -> dict[str, Path]:
    (generated / "core" / "signal_listing.py").write_text(listing)
    paths = {name: generated / name for name in TARGETS}
    if signals is not None:
        paths["signals/get_signals_response.py"].write_text(signals)
    if wholesale is not None:
        paths["core/wholesale_feed_event.py"].write_text(wholesale)
    return paths


def _assert_reused(paths: dict[str, Path]) -> None:
    for name, alias_import in TARGETS.items():
        source = paths[name].read_text()
        tree = ast.parse(source)
        compile(source, name, "exec")
        assert not any(isinstance(n, ast.ClassDef) and n.name == "Range" for n in tree.body), name
        assert "Range = _SignalListingRange\n" in source, name
        assert alias_import in source, name


def _scenario(generated: Path, config: str, *, listing_described: bool = True) -> dict[str, Path]:
    return _write(
        generated,
        _listing(_range(config, described=listing_described)),
        _signals_response(_range(config, described=True)),
        _wholesale(_range(config)),
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
    paths = _write(
        generated,
        _listing(listing),
        _signals_response(_range(FORBID, described=True)),
        _wholesale(_range(FORBID)),
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
@pytest.mark.parametrize("target", sorted(TARGETS))
def test_a_divergent_copy_raises_and_leaves_the_file_alone(
    generated: Path, listing: str, copy: str, target: str
) -> None:
    signals = _signals_response(copy)
    wholesale = _wholesale(copy)
    paths = _write(generated, _listing(listing), signals, wholesale)
    before = paths[target].read_text()
    with pytest.raises(RuntimeError, match="no longer matches SignalListing.Range"):
        fixes.fix_signal_listing_range_subclasses()
    assert paths[target].read_text() == before


def test_running_twice_is_idempotent(generated: Path) -> None:
    paths = _scenario(generated, FORBID)
    fixes.fix_signal_listing_range_subclasses()
    first = {name: path.read_text() for name, path in paths.items()}
    fixes.fix_signal_listing_range_subclasses()
    assert {name: path.read_text() for name, path in paths.items()} == first


def test_a_target_without_range_is_unchanged(generated: Path) -> None:
    wholesale = _wholesale(None)
    paths = _write(generated, _listing(_range(FORBID)), None, wholesale)
    fixes.fix_signal_listing_range_subclasses()
    assert paths["core/wholesale_feed_event.py"].read_text() == wholesale
    assert not paths["signals/get_signals_response.py"].exists()


def test_a_target_range_without_a_listing_range_raises(generated: Path) -> None:
    paths = _write(generated, _listing(None), None, _wholesale(_range(FORBID)))
    before = paths["core/wholesale_feed_event.py"].read_text()
    with pytest.raises(RuntimeError, match="core/signal_listing.py no longer does"):
        fixes.fix_signal_listing_range_subclasses()
    assert paths["core/wholesale_feed_event.py"].read_text() == before


def test_a_parenthesized_import_block_still_gets_the_alias_after_it(generated: Path) -> None:
    wholesale = _wholesale(
        _range(FORBID),
        import_line="from .signal_listing import (\n    SignalListing,\n)\n",
    )
    paths = _write(generated, _listing(_range(FORBID)), None, wholesale)
    fixes.fix_signal_listing_range_subclasses()
    source = paths["core/wholesale_feed_event.py"].read_text()
    compile(source, "wholesale_feed_event.py", "exec")
    assert (
        "from .signal_listing import (\n    SignalListing,\n)\n"
        "from .signal_listing import Range as _SignalListingRange\n"
    ) in source
    assert "class Range" not in source


def test_the_generated_tree_shares_one_range_class() -> None:
    from adcp.types.domains import signals
    from adcp.types.domains.core import signal_listing, wholesale_feed_event
    from adcp.types.domains.signals import get_signals_response

    assert get_signals_response.Range is signal_listing.Range
    assert wholesale_feed_event.Range is signal_listing.Range
    assert signals.Range is signal_listing.Range
    assert (
        wholesale_feed_event.Signal.model_fields["range"].annotation
        == signal_listing.SignalListing.model_fields["range"].annotation
    )
    for module in (get_signals_response, wholesale_feed_event):
        tree = ast.parse(Path(module.__file__).read_text())
        assert not any(isinstance(n, ast.ClassDef) and n.name == "Range" for n in tree.body)
