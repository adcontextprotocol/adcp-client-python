"""Named-base codegen keeps wire literals distinct from private type names."""

import ast
import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "literal_schema, expected_literals",
    [
        ({"enum": ["_foo", "_bar"]}, ["_foo", "_bar"]),
        ({"const": "_foo"}, ["_foo"]),
    ],
    ids=["enum", "const"],
)
def test_generated_annotation_preserves_literals_and_qualifies_nested_type(
    monkeypatch: pytest.MonkeyPatch, literal_schema, expected_literals
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    generator = importlib.import_module("generate_versioned_bases")
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "oneOf": [literal_schema, {"$ref": "#/$defs/Nested"}],
            }
        },
        "required": ["value"],
        "$defs": {
            "Nested": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }
        },
    }
    monkeypatch.setattr(generator, "list_validator_keys", lambda **kwargs: ["probe::request"])
    monkeypatch.setattr(generator, "get_portable_schema", lambda *args, **kwargs: schema)

    generated = generator.generate("v31", "3.1")
    tree = ast.parse(generated["v31.pyi"])
    model = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    field = next(node for node in model.body if isinstance(node, ast.AnnAssign))
    annotation = field.annotation
    literals = [node.value for node in ast.walk(annotation) if isinstance(node, ast.Constant)]
    assert literals == expected_literals
    references = [node for node in ast.walk(annotation) if isinstance(node, ast.Attribute)]
    assert len(references) == 1
    assert isinstance(references[0].value, ast.Name)
    assert references[0].value.id == "_definitions"
    assert references[0].attr == "_Nested"
    runtime_definitions = ast.parse(generated["_v31.py"])
    assert any(
        isinstance(node, ast.ClassDef) and node.name == references[0].attr
        for node in runtime_definitions.body
    )
