"""Code generation must preserve both reporting selector contracts."""

import ast
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from scripts import post_generate_fixes

ROOT = Path(__file__).parents[1] / "src/adcp/types/generated_poc"
SOURCES = (
    ("core/reporting_delivery_config.py", "Scope", "adcp.types.generated_poc.core"),
    (
        "media_buy/get_media_buy_delivery_request.py",
        "GetMediaBuyDeliveryRequest",
        "adcp.types.generated_poc.media_buy",
    ),
)
METHODS = {
    "_select_reporting_scope",
    "_unique_reporting_scope",
    "_serialize_reporting_scope",
    "_validate_delivery_selector_mode",
    "_serialize_delivery_selector_mode",
}


def unrepaired(source):
    """Restore the two original codegen defaults without changing field schemas."""
    lines = source.splitlines(keepends=True)
    cuts = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name in METHODS:
            cuts.append((min(d.lineno for d in node.decorator_list) - 1, node.end_lineno))
    for start, end in sorted(cuts, reverse=True):
        del lines[start:end]
    return "".join(lines).replace(
        "all_media_buys: Literal[True] | None = None", "all_media_buys: Literal[True] = True"
    )


def test_regeneration_repairs_canonical_and_self_contained_clones(tmp_path, monkeypatch):
    targets = []
    for relative, class_name, package in SOURCES:
        source = unrepaired((ROOT / relative).read_text())
        for clone in (False, True):
            name = class_name + ("2" if clone else "")
            target = tmp_path / (("bundled/" if clone else "") + relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(re.sub(r"\b" + class_name + r"\b", name, source))
            targets.append((target, name, package))
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", tmp_path)
    post_generate_fixes.fix_reporting_request_selectors()
    first = [p.read_bytes() for p, _, _ in targets]
    post_generate_fixes.fix_reporting_request_selectors()
    assert first == [p.read_bytes() for p, _, _ in targets]
    for index, (target, name, package) in enumerate(targets):
        module = ModuleType(package + ".selector_regeneration_" + str(index))
        module.__package__ = package
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(compile(target.read_bytes(), str(target), "exec"), vars(module))
        model = getattr(module, name)
        if name.startswith("Scope"):
            explicit = model.model_validate({"media_buy_ids": ["shared-media-buy"]})
            assert json.loads(explicit.model_dump_json()) == {"media_buy_ids": ["shared-media-buy"]}
            assert model().model_dump() == {"all_media_buys": True}
            with pytest.raises(ValidationError):
                model.model_validate(
                    {"all_media_buys": True, "media_buy_ids": ["shared-media-buy"]}
                )
        else:
            exact = {"account": {"account_id": "acct_a"}, "reporting_revision_id": "rpr_exact"}
            value = model.model_validate(exact)
            assert "include_package_daily_breakdown" not in value.model_dump()
            assert "include_window_breakdown" not in json.loads(value.model_dump_json())
            with pytest.raises(ValidationError):
                model.model_validate({**exact, "include_window_breakdown": False})
            assert model().model_dump()["include_window_breakdown"] is False


def test_changed_scope_schema_requires_an_explicit_generator_decision(tmp_path, monkeypatch):
    schema = tmp_path / "core/reporting-delivery-config.json"
    schema.parent.mkdir()
    original = post_generate_fixes.SCHEMA_DIR / "core/reporting-delivery-config.json"
    value = json.loads(original.read_bytes())
    value["properties"]["scope"]["maxProperties"] = 2
    schema.write_text(json.dumps(value))
    monkeypatch.setattr(post_generate_fixes, "SCHEMA_DIR", tmp_path)
    with pytest.raises(ValueError, match="reporting scope schema changed"):
        post_generate_fixes.fix_reporting_request_selectors()
